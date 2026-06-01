"""EngineController — the single orchestration point between UI and core.

v2rayN-style "one click = everything". Given a selected :class:`Profile` and
the user's connection settings, this controller:

  1. picks a free internal loopback port for the SNI-spoofer (default 40443),
  2. starts :class:`main.ProxyServer` listening there, forwarding — with the
     DPI-bypass injection — to the *real* ``profile.address:profile.port``,
  3. starts :class:`core.xray_manager.XrayManager` whose outbound is pointed at
     ``127.0.0.1:<spoof_port>`` so traffic is auto-chained through the spoofer,
  4. surfaces log / status / connection-count events through plain callbacks.

The controller is **UI-framework agnostic** — it knows nothing about Qt. The
UI assigns ``on_log`` / ``on_status`` / ``on_count`` callbacks (which a Qt layer
marshals onto the GUI thread via signals). Everything that can block runs off
the UI thread, and ``start`` / ``stop`` are safe to call repeatedly.

Connection modes
----------------
* ``"SNI Only"``        — spoofer only, no xray core (raw forwarder).
* anything else / a profile present — spoofer chained under xray core.

When no profile is selected we still support the legacy raw-forwarder path so
the tool remains useful without a share link.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from core.profile import Profile


# Status strings shared with the UI (kept aligned with DashboardPage.set_status)
STATUS_IDLE = "idle"
STATUS_CONNECTING = "connecting"
STATUS_ACTIVE = "active"
STATUS_ERROR = "error"

LogCb = Callable[[str], None]
StatusCb = Callable[[str], None]
CountCb = Callable[[int, int], None]
StrategyCb = Callable[[str], None]
TrafficCb = Callable[[int, int, float, float], None]  # up_bytes, down_bytes, up_bps, down_bps


class EngineController:
    """Owns the spoofer + xray lifecycle for one connection."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config: dict[str, Any] = dict(config or {})
        self.profile: Optional[Profile] = None

        # external callbacks (set by the UI layer)
        self.on_log: Optional[LogCb] = None
        self.on_status: Optional[StatusCb] = None
        self.on_count: Optional[CountCb] = None
        self.on_strategy: Optional[StrategyCb] = None   # fires when the live bypass method is (re)chosen
        self.on_traffic: Optional[TrafficCb] = None     # fires with cumulative bytes + live rate

        # the bypass method currently in force (kept in sync with the UI)
        self._active_strategy: Optional[str] = None

        # internals
        self._proxy = None            # main.ProxyServer
        self._xray = None             # core.xray_manager.XrayManager
        self._prober = None           # core.prober.AutoProber (when enabled)
        self._resilience = None       # core.resilience.ResilienceController
        self._system_proxy = None     # core.system_proxy.SystemProxy (when on)
        self._spoof_port: Optional[int] = None
        self._status = STATUS_IDLE
        self._lock = threading.RLock()
        # Start/stop epoch (re-entrancy guard for rapid config switches). Every
        # start() and stop() bumps it under the lock; a background _do_start()
        # captures the epoch it was launched for and only commits ACTIVE (or an
        # ERROR) if it is still the current epoch. This stops a slow, in-flight
        # start from an OLD config resurrecting itself as "active" after the user
        # already switched away / stopped — the "switch config and it breaks or
        # gets stuck" class of bug.
        self._start_epoch = 0
        # latest live throughput (bytes/sec) reported by the spoofer; surfaced
        # in diagnostics even when the resilience baseline isn't built yet (#4)
        self._live_up_bps = 0.0
        self._live_down_bps = 0.0
        # live-usage poller for plain (xray-direct) configs (#3)
        self._stats_thread: Optional[threading.Thread] = None
        self._stats_stop = threading.Event()

        # Injectable factory so the OS-proxy lifecycle is testable without
        # touching the real Windows registry. Tests swap this for a fake;
        # production leaves it None → the real SystemProxy is built lazily.
        self._system_proxy_factory: Optional[Callable[[], Any]] = None

    # ------------------------------------------------------------------ wiring

    def set_profile(self, profile: Optional[Profile]) -> None:
        self.profile = profile

    def update_config(self, config: dict[str, Any]) -> None:
        self.config.update(config)

    # -- callback fan-out (each guarded so one bad handler can't crash us) --

    # log source tags (issue #4) — each line is attributed to a subsystem so
    # the UI can separate spoofer/WinDivert/Administrator lines from ordinary
    # xray-core lines and never confuse the user.
    TAG_ENGINE = "موتور"
    TAG_SPOOF = "اسپوف SNI"
    TAG_CORE = "هسته xray"

    def _log(self, msg: str, tag: str | None = None) -> None:
        """Emit a log line, prefixing a ``[tag]`` source marker (issue #4).

        If *msg* already starts with a ``[`` it is assumed to carry its own
        tag and is emitted unchanged; otherwise *tag* (default: engine) is
        prepended so every line is attributable to a source.
        """
        if not self.on_log:
            return
        text = msg if msg is None else str(msg)
        if text and not text.lstrip().startswith("["):
            text = f"[{tag or self.TAG_ENGINE}] {text}"
        try:
            self.on_log(text)
        except Exception:
            pass

    def _spoof_log(self, msg: str) -> None:
        """Log a line attributed to the SNI spoofer / WinDivert (issue #4)."""
        self._log(msg, tag=self.TAG_SPOOF)

    def _core_log(self, msg: str) -> None:
        """Log a line attributed to the xray core (issue #4)."""
        self._log(msg, tag=self.TAG_CORE)

    def _set_status(self, status: str) -> None:
        self._status = status
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:
                pass

    def _emit_count(self, active: int, total: int) -> None:
        if self.on_count:
            try:
                self.on_count(active, total)
            except Exception:
                pass

    def _emit_strategy(self, method: str) -> None:
        """Tell the UI which bypass method is now in force.

        This is the single source of truth so the Dashboard and the
        Diagnostics page never disagree about the "active strategy".
        """
        self._active_strategy = method
        if self.on_strategy:
            try:
                self.on_strategy(method)
            except Exception:
                pass

    def _emit_traffic(self, up: int, down: int, up_bps: float, down_bps: float) -> None:
        # remember the latest live rate so diagnostics can show real-time
        # throughput even when the resilience layer (which owns the baseline)
        # is turned off (feedback #4 — the throughput card "did nothing").
        self._live_up_bps = float(up_bps)
        self._live_down_bps = float(down_bps)
        if self.on_traffic:
            try:
                self.on_traffic(up, down, up_bps, down_bps)
            except Exception:
                pass

    @property
    def active_strategy(self) -> Optional[str]:
        """The bypass method currently in force (post auto-probe / rotation)."""
        # prefer a live resilience rotation, then the prober's lock, then ours
        res = self._resilience
        if res is not None and getattr(res, "current_strategy", None):
            return res.current_strategy
        prober = self._prober
        if prober is not None and getattr(prober, "selected", None) is not None:
            return prober.selected.strategy
        return self._active_strategy

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status in (STATUS_CONNECTING, STATUS_ACTIVE)

    @property
    def spoof_port(self) -> Optional[int]:
        return self._spoof_port

    @property
    def uses_core(self) -> bool:
        """True when an xray core is chained under the spoofer.

        A selected profile **always** needs xray-core — it carries the
        VLESS/VMess/Trojan protocol that a raw SNI forwarder cannot speak. This
        is especially true for *spoof configs* (``127.0.0.1:40443`` links):
        their whole point is xray → our spoofer → Cloudflare, so the core must
        run even when the user left the mode on ``"SNI Only"`` (that label only
        means "no Warp/Psiphon outer layer", not "no xray").

        ``"SNI Only"`` runs *without* a core only when **no profile** is
        selected — the standalone raw-forwarder use case.
        """
        return self.profile is not None

    @property
    def wants_core_but_no_profile(self) -> bool:
        """True when the chosen mode needs xray but no profile is selected.

        Used by the UI to warn the user instead of silently falling back to a
        plain SNI forward that can never reach a VLESS server.
        """
        mode = str(self.config.get("connection_mode", "Tunnel"))
        return self.profile is None and mode != "SNI Only"

    @property
    def chains_spoofer(self) -> bool:
        """True when the SNI-spoofer should sit *under* the xray core.

        Two situations chain the spoofer beneath xray:

        1. **Spoof configs** — share links whose server address is a loopback
           stand-in (e.g. ``vless://...@127.0.0.1:40443?...sni=foo.workers.dev``).
           That ``127.0.0.1:40443`` *is* our SNI spoofer: xray dials it, and the
           spoofer forwards to a fixed Cloudflare IP while injecting a decoy
           ClientHello. This is exactly what V2RayTun does (it dials our
           spoofer); to be self-contained we run our own xray in its place.
           These chain regardless of mode (even plain "Tunnel").

        2. A spoof config in ``"SNI Only"`` mode (the spoofer runs as the
           standalone DPI-evasion forwarder under xray).

        **#6 — ordinary (routable) configs never chain the spoofer.** A config
        whose server address is a real, routable host is connected exactly like
        a normal client (V2RayTun / Hiddify): xray dials the server directly and
        the connection-mode selector is irrelevant for it. Only *spoof* configs
        (loopback-IP share links) need the spoofer, so the Tunnel / SNI-Only
        modes only have an effect for them — this stops the app from spinning up
        a spoofer ProxyServer (and burning resources) for servers that don't
        need one.
        """
        if not self.uses_core:
            return False
        # Spoof configs (loopback share links): the spoofer is always in the
        # path (Tunnel chains xray under it; SNI Only runs it as the bypass
        # forwarder under xray).
        if getattr(self.profile, "is_spoof_config", False):
            return True
        # Ordinary, routable configs are normally direct (#6). BUT the user can
        # explicitly opt in to routing an ordinary config *through* the SNI
        # spoofer (issue #1): some clean Cloudflare IPs connect fine in
        # V2RayTun but get their real ClientHello (SNI) dropped by DPI when we
        # dial them directly. Forcing the spoofer makes xray → spoofer → the
        # config's own IP while injecting a decoy ClientHello, so DPI sees the
        # fake SNI exactly like the working spoof-config path. Opt-in via the
        # ``force_spoof`` config flag (set per-run from the UI).
        if bool(self.config.get("force_spoof", False)):
            return True
        return False

    def diagnostics(self):
        """Return a :class:`core.diagnostics.DiagnosticsSnapshot` of live state.

        Safe to call any time (idle or running); the diagnostics layer tolerates
        a not-yet-built prober / resilience controller and returns defaults.
        """
        from core.diagnostics import snapshot
        return snapshot(self)

    # ------------------------------------------------------------------ ping

    def _ping_tester(self):
        """Build a :class:`core.ping.PingTester` from current config."""
        from core.ping import PingTester, tcp_latency, tcp_throughput
        measure_dl = bool(self.config.get("ping_measure_download", True))
        return PingTester(
            latency_fn=tcp_latency,
            throughput_fn=tcp_throughput if measure_dl else None,
            samples=int(self.config.get("ping_samples", 3)),
            timeout=float(self.config.get("ping_timeout", 3.0)),
            on_log=self.on_log,
        )

    def ping_profiles(self, profiles):
        """Ping every profile; return PingResults sorted lowest-latency first.

        Blocking (call on a worker thread). Fully fail-soft.
        """
        try:
            tester = self._ping_tester()
            measure_dl = bool(self.config.get("ping_measure_download", True))
            return tester.ping_profiles(profiles, measure_download=measure_dl)
        except Exception as exc:  # never raise into the UI
            self._log(f"خطا در پینگ: {exc}")
            return []

    def ping_profile(self, profile):
        """Ping a single profile. Blocking, fail-soft."""
        results = self.ping_profiles([profile])
        return results[0] if results else None

    def probe_strategies_for(self, profile, *, strategy: str | None = None):
        """Test bypass strategies against one profile (which connects / wins).

        ``strategy`` pins a single strategy to ping with; ``None`` (or the
        configured ``ping_strategy`` when set) selects the strategy set.
        Returns a :class:`core.ping.StrategyPingReport`. Blocking, fail-soft.
        """
        from core.ping import probe_strategies_for_profile
        pinned = strategy or (self.config.get("ping_strategy") or None)
        strategies = [pinned] if pinned else None
        try:
            return probe_strategies_for_profile(
                profile,
                strategies=strategies,
                timeout=float(self.config.get("probe_timeout", 5.0)),
                on_log=self.on_log,
            )
        except Exception as exc:
            self._log(f"خطا در تست استراتژی: {exc}")
            from core.ping import StrategyPingReport, target_from_profile
            t = target_from_profile(profile)
            return StrategyPingReport(label=t.label, host=t.host, port=t.port)

    def is_active_profile(self, profile) -> bool:
        """True when *profile* is the config the live tunnel is **actually
        carrying right now** — selected AND the engine is ACTIVE.

        Used so the inline ping can give a **definitive** live measurement for
        the active config (a real request through the running proxy) rather than
        an offline guess. Compares by identity first, then by the fields that
        define a config endpoint (address/port/uuid/sni) so a fresh Profile
        object parsed from the same link still matches.

        CRITICAL (user reports, نکته ۱ و ۳): "active" must mean the tunnel is
        genuinely UP. ``set_profile`` records ``self.profile`` the moment a row
        is *selected*, long before — or without ever — pressing شروع. The old
        check only compared the selected profile, so:

          * نکته ۳ — a config that is merely *selected* (no tunnel) was treated
            as "active", and the inline ping tried a live-tunnel request that
            of course failed → false red, and the spoof ping buttons looked
            usable when there was nothing to ping through.
          * نکته ۱ — conversely, if the comparison ever missed while the tunnel
            WAS up, AYYILDIZ fell through to the offline edge probe (which a
            relay config can fail intermittently → "بدون پاسخ" red) even though
            it pings ~110ms through the live tunnel.

        Gating on ``STATUS_ACTIVE`` makes "active config" mean exactly what the
        user means: connected and carrying traffic.
        """
        if profile is None or self.profile is None:
            return False
        # the tunnel must really be up — a merely-selected config is NOT active.
        if self._status != STATUS_ACTIVE:
            return False
        if profile is self.profile:
            return True
        a, b = profile, self.profile
        try:
            return (
                getattr(a, "address", None) == getattr(b, "address", None)
                and int(getattr(a, "port", 0) or 0) == int(getattr(b, "port", 0) or 0)
                and getattr(a, "uuid", "") == getattr(b, "uuid", "")
                and getattr(a, "password", "") == getattr(b, "password", "")
            )
        except Exception:
            return False

    def live_proxy_ping(self, *, samples: int = 1, timeout: float = 12.0):
        """Measure REAL latency through the running tunnel (most honest ping).

        Returns ``(ok: bool, latency_ms: float|None, detail: str)``.

        This is the only test that faithfully reflects what the user
        experiences, because the request travels the *actual* live chain
        (xray → spoofer → CDN) — including the spoofer's decoy-SNI injection that
        an offline probe can never replicate. That injection is exactly why an
        offline ping of a spoof config's *real* SNI gets DPI-blocked (no ping)
        even though the config works (bug #1: "spoof config had no ping but
        worked"). When the tunnel is up we sidestep that contradiction entirely
        by measuring through it.

        Only meaningful while the tunnel is up (status active/connecting);
        returns ``(False, None, ...)`` otherwise.

        Robustness (the repeated "تونل زنده پاسخ نداد although I'm connected"
        bug): a single HTTPS-over-CONNECT request to ONE endpoint is fragile —
        the captive 204 host may be slow/blocked, or the TLS-through-proxy
        handshake may hiccup, even while normal browsing through the tunnel
        works fine. So we now try several lightweight connectivity-check
        endpoints (the same ones OSes use for captive-portal detection),
        preferring **plain-HTTP 204** probes (which travel the proxy as an
        ordinary GET — no CONNECT/TLS layer to trip over) and falling back to
        HTTPS. Success on ANY endpoint ⇒ the tunnel works; we report the best
        latency seen. We only report failure if EVERY endpoint failed.
        """
        import time
        import urllib.request

        # accept connecting too: the user may ping right as it comes up; the
        # proxy port is already bound by then. (Idle/error → nothing to test.)
        if self._status not in (STATUS_ACTIVE, STATUS_CONNECTING):
            return (False, None, "tunnel not active")
        try:
            _socks, http_port = self._effective_ports()
        except Exception:
            return (False, None, "no bound port")
        if not http_port:
            return (False, None, "no http inbound")
        proxy = f"http://127.0.0.1:{http_port}"
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        # a per-request timeout that scales with samples but stays snappy; the
        # caller's overall budget is roughly samples * per_timeout.
        per_timeout = max(4.0, float(timeout) / max(1, int(samples) + 1))
        attempts = max(1, int(samples))
        return self._live_proxy_ping_verified(opener, per_timeout, attempts)

    # --- v2rayNG-style REAL delay for an *inactive* profile -----------------
    #
    # The honest truth (the user's repeated bug reports — AYYILDIZ7 pinged red
    # though it works, a deliberately-broken vls-cf-xhttp pinged GREEN, spoof
    # configs got no estimate): NO offline hand-rolled TLS/WS/trace probe can
    # faithfully decide whether an arbitrary config (relay path, xhttp, spoof,
    # plain CDN) actually carries traffic. A /cdn-cgi/trace to a live anycast IP
    # answers for ANY config (false-green), and a relay/xhttp route can't be
    # validated by a bare WS upgrade (false-red).
    #
    # v2rayNG solves this the only reliable way: it spins up the REAL core with
    # the config's own outbound on a throwaway local inbound, then fetches a
    # known URL THROUGH it and times the round-trip. If the config is broken the
    # fetch fails (red); if it works the fetch returns the real body (green) and
    # the elapsed time IS the real delay. We mirror that exactly here — a
    # temporary xray (chained behind the spoofer for spoof configs, just like a
    # real connect) + a body-verified GET. This is the same machinery
    # ``_start_core_only`` / the spoofer path already use, just torn down right
    # after the measurement.
    def measure_profile_delay(self, profile, *, timeout: float = 12.0):
        """Measure a profile's REAL delay the v2rayNG way (temporary core).

        Returns ``(ok: bool, latency_ms: float|None, detail: str)``. Works for
        ANY config — relay/xhttp/spoof/plain — because the request travels the
        config's genuine outbound, not a hand-rolled probe. Fully fail-soft and
        self-contained: it binds its own free ports and always tears the
        temporary core (and spoofer, if any) down before returning.
        """
        import time
        import urllib.request

        try:
            from core.xray_manager import XrayManager, find_free_port
        except Exception as exc:  # pragma: no cover - import guard
            return (False, None, f"xray unavailable: {exc}")

        # never disturb a live tunnel: if the engine is currently running, the
        # caller should use live_proxy_ping for the active config instead.
        socks_port = find_free_port()
        http_port = find_free_port(socks_port + 1)

        spoofer = None
        xray = None
        try:
            is_spoof = bool(getattr(profile, "is_spoof_config", False))
            spoof_port = None
            if is_spoof:
                # bring up a throwaway spoofer exactly like a real connect would,
                # so the decoy-SNI injection (the whole point of a spoof config)
                # is in the path — otherwise the real SNI would be DPI-blocked
                # and a working config would falsely measure red.
                spoof_port = find_free_port(
                    int(getattr(profile, "dial_port", 0) or 40443))
                connect_ip = (getattr(profile, "spoof_connect_ip", "")
                              or "").strip()
                connect_port = int(getattr(profile, "spoof_connect_port", 0)
                                   or (443 if getattr(profile, "is_tls", False)
                                       else 80))
                fake_sni = (getattr(profile, "spoof_fake_sni", "") or "").strip()
                try:
                    from main import ProxyServer
                except Exception as exc:
                    return (False, None, f"spoofer unavailable: {exc}")
                spoofer = ProxyServer({
                    "LISTEN_HOST": "127.0.0.1",
                    "LISTEN_PORT": spoof_port,
                    "CONNECT_IP": connect_ip,
                    "CONNECT_PORT": connect_port,
                    "FAKE_SNI": fake_sni,
                    "gaming_mode": False,
                })
                # pick a sane bypass method without disturbing engine state
                try:
                    spoofer.bypass_method = self._choose_bypass_method(
                        connect_ip, connect_port)
                except Exception:
                    pass
                if spoofer.start() is False:
                    return (False, None,
                            getattr(spoofer, "_start_error", None)
                            or "spoofer failed to start")

            xray = XrayManager(
                profile,
                socks_port=socks_port,
                http_port=http_port,
                spoof_port=spoof_port,
                gaming_mode=False,
                listen="127.0.0.1",
            )
            if not xray.is_available:
                return (False, None, "xray.exe not found")
            if xray.start() is False:
                # a bad/broken config makes xray exit immediately → honest red,
                # which is EXACTLY what we want for the sabotaged vls-cf-xhttp.
                return (False, None, "core failed to start (config broken)")

            # let the temporary core finish binding + the first handshake.
            time.sleep(1.2)
            real_http = int(getattr(xray, "http_port", http_port) or http_port)
            proxy = f"http://127.0.0.1:{real_http}"
            handler = urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
            per_timeout = max(4.0, float(timeout) / 2.0)
            # reuse the body-verified prober so a config that only reaches the
            # CDN edge (the broken xhttp) is reported red, not green.
            return self._live_proxy_ping_verified(opener, per_timeout, 2)
        except Exception as exc:  # never raise into the UI
            return (False, None, f"{type(exc).__name__}: {exc}")
        finally:
            for obj in (xray, spoofer):
                if obj is not None:
                    try:
                        obj.stop()
                    except Exception:
                        pass

    # --- body-verified live tunnel probes (bug: "fake" green ping) ----------
    #
    # A captive-portal ``generate_204`` endpoint is NOT enough to prove the
    # tunnel actually works (the user's deliberately-sabotaged spoof config:
    # ping went green and a few KB flowed, yet no site loaded). The reason:
    # spoof configs dial a fixed **Cloudflare anycast IP**, so even when the
    # inner Worker route to the real backend is dead, the spoofer still reaches
    # Cloudflare's edge — and Cloudflare's own ``cp.cloudflare.com/generate_204``
    # is answered *by that edge directly*, returning 204 without the traffic ever
    # leaving CF to the open internet. An empty 204 therefore can't distinguish a
    # working tunnel from one that only reaches the CDN edge.
    #
    # The honest test must fetch a resource whose **body content is verifiable**
    # and can only be produced by genuinely reaching the open internet THROUGH
    # the proxy backend. We require at least one such body-verified fetch to
    # succeed before reporting the tunnel as working; the empty-204 endpoints are
    # used only as a fast latency refinement once a real fetch has proven the
    # path end-to-end.

    # (url, substring-that-must-appear-in-body-lower). These return a real body
    # that a CDN-edge-only path cannot fabricate, so a broken tunnel fails them.
    _LIVE_VERIFY_ENDPOINTS = (
        ("http://www.gstatic.com/generate_204", ""),   # latency hint (no body)
        ("http://cp.cloudflare.com/generate_204", ""),  # latency hint (no body)
        ("http://detectportal.firefox.com/success.txt", "success"),
        ("http://www.msftconnecttest.com/connecttest.txt",
         "microsoft connect test"),
        ("https://www.gstatic.com/generate_204", ""),
        ("https://cp.cloudflare.com/cdn-cgi/trace", "fl="),
    )

    def _live_proxy_ping_verified(self, opener, per_timeout, attempts):
        """Probe the live tunnel, requiring a real body-verified fetch.

        Returns ``(ok, latency_ms|None, detail)``. ``ok`` is True only if at
        least one *content-verified* endpoint round-tripped real body bytes
        through the proxy. Empty-204 endpoints contribute a latency sample but
        never, on their own, count as success — so a tunnel that merely reaches
        the CDN edge (the sabotaged-spoof "fake ping") is correctly reported red.
        """
        import time
        import urllib.request

        endpoints = list(self._LIVE_VERIFY_ENDPOINTS)
        best = None
        verified = False
        last_detail = ""
        # take at least one full pass over the endpoints so a body-verified one
        # is always tried even when samples == 1.
        rounds = max(attempts, 1)
        idx = 0
        while idx < max(rounds, len(endpoints)) and not (verified and best is not None):
            url, marker = endpoints[idx % len(endpoints)]
            idx += 1
            req = urllib.request.Request(
                url, headers={"User-Agent": "SNISpoofer-ping"})
            t0 = time.time()
            try:
                with opener.open(req, timeout=per_timeout) as resp:
                    code = resp.getcode()
                    body = b""
                    if marker:
                        try:
                            body = resp.read(4096) or b""
                        except Exception:
                            body = b""
                dt = (time.time() - t0) * 1000.0
                if code in (200, 204):
                    if marker:
                        # content check: the marker MUST appear in the body, or
                        # this is a CDN-edge/captive impostor, not a real fetch.
                        if marker.lower() in body.decode("latin-1", "ignore").lower():
                            verified = True
                            best = dt if best is None else min(best, dt)
                            last_detail = f"verified {code} ({url})"
                        else:
                            last_detail = f"no-body-marker {code} ({url})"
                    else:
                        # empty-204: only a latency hint, never proof on its own
                        best = dt if best is None else min(best, dt)
                        if not last_detail:
                            last_detail = f"http {code} (204-only)"
                else:
                    last_detail = f"unexpected http {code}"
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
        # success ONLY when a real body was verified end-to-end.
        if verified and best is not None:
            return (True, float(best), last_detail or "ok")
        # a 204 answered but nothing was content-verified → the tunnel reaches
        # the CDN edge but cannot carry real traffic. Report honestly red.
        if best is not None and not verified:
            return (False, None, last_detail
                    or "فقط لبهٔ CDN پاسخ داد (ترافیک واقعی رد نشد)")
        return (False, None, last_detail or "no response")

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Start spoofer (+ optional xray) on a background thread."""
        with self._lock:
            if self.is_running:
                self._log("موتور از قبل در حال اجراست")
                return
            # claim a fresh epoch for this start so a late/abandoned previous
            # _do_start() can't commit ACTIVE after we've moved on.
            self._start_epoch += 1
            epoch = self._start_epoch
            self._set_status(STATUS_CONNECTING)
        threading.Thread(
            target=self._start_blocking, args=(epoch,), daemon=True).start()

    def _start_blocking(self, epoch: int) -> None:
        try:
            self._do_start(epoch)
        except Exception as exc:  # never let the worker thread die silently
            self._log(f"خطا در راه‌اندازی: {exc}")
            # only surface the failure if this start is still the current one;
            # a superseded start failing is irrelevant to the new session.
            if self._epoch_current(epoch):
                self._set_status(STATUS_ERROR)
                self.stop()

    def _epoch_current(self, epoch: int) -> bool:
        """True when *epoch* is still the engine's active start epoch.

        A background ``_do_start`` captures the epoch it was launched for; if a
        newer start()/stop() has bumped the epoch since, this start has been
        superseded and must NOT mutate status / leave a half-built session
        behind.
        """
        with self._lock:
            return epoch == self._start_epoch

    def _commit_active(self, epoch: int) -> bool:
        """Mark the engine ACTIVE iff *epoch* is still current.

        Returns True when committed. A superseded start (the user switched
        config / stopped while we were binding) returns False so the caller can
        tear its half-built session down instead of falsely going green.
        """
        with self._lock:
            if epoch != self._start_epoch:
                return False
            self._set_status(STATUS_ACTIVE)
            return True

    def _load_remote_strategies(self):
        """Fetch + verify a signed ``strategies.json`` if remote updates are on.

        Returns the :class:`StrategiesUpdater` whose manifest was adopted, or
        ``None`` (remote disabled, no mirrors, fetch/verify failed). Never
        raises — a bad/absent manifest just leaves us on the local registry.
        """
        if not self.config.get("remote_strategies", False):
            return None
        mirrors = list(self.config.get("strategies_mirrors", []) or [])
        if not mirrors:
            self._log("strategies از راه دور روشن است اما mirror تنظیم نشده — رد شد")
            return None
        try:
            from core.strategies_remote import (
                StrategiesUpdater, trusted_public_key, urllib_fetcher)

            updater = StrategiesUpdater(
                public_key=trusted_public_key(), mirrors=mirrors,
                fetcher=urllib_fetcher(), on_log=self._log)
            if updater.update():
                return updater
            self._log("strategies.json معتبری از mirrorها دریافت نشد — رجیستری محلی")
            return None
        except Exception as exc:
            self._log(f"بارگیری strategies از راه دور خطا داد ({exc}) — رجیستری محلی")
            return None

    def _choose_bypass_method(self, host: str, port: int) -> str:
        """Pick the bypass method: auto-probe when enabled, else the config one.

        When ``auto_prober`` is on, build candidates from the implemented
        strategies (ordered by their static prior), probe them against the real
        upstream and lock the best. A verified remote ``strategies.json`` (when
        enabled) supplies the candidate set + score priors instead of the local
        registry. Falls back to the configured method on any failure / no host,
        so Start never blocks on the prober.
        """
        configured = str(self.config.get("bypass_method", "wrong_seq"))
        if not self.config.get("auto_prober", False):
            return configured
        if not host:
            self._log("auto-prober: مقصدی برای probe نیست — از روش پیکربندی‌شده استفاده می‌شود")
            return configured
        try:
            from strategies import all_strategies
            from core.prober import AutoProber, build_candidates, tcp_probe

            # prefer a verified remote manifest; else the local registry
            updater = self._load_remote_strategies()
            if updater is not None:
                base = updater.to_candidates()
                scored = updater.score_priors()
            else:
                base = None
                scored = {}
            if not base:
                keys = [s.meta.key for s in all_strategies(implemented_only=True)]
                scored = {s.meta.key: s.score()
                          for s in all_strategies(implemented_only=True)}
                base = build_candidates(
                    keys,
                    fragment_tcp=bool(self.config.get("fragment_tcp", False)),
                    fragment_tls=bool(self.config.get("fragment_tls", False)),
                    tls_chunk=int(self.config.get("fragment_tls_chunk", 64)),
                )
            candidates = AutoProber.candidate_order(scored, base)
            self._prober = AutoProber(
                candidates, tcp_probe, on_log=self._log,
                timeout=float(self.config.get("probe_timeout", 5.0)))
            best = self._prober.run(host, port)
            if best is None:
                self._log("auto-prober: کاندیدای موفقی نبود — بازگشت به روش پیکربندی‌شده")
                return configured
            return best.strategy
        except Exception as exc:  # prober must never block Start
            self._log(f"auto-prober خطا داد ({exc}) — از روش پیکربندی‌شده استفاده می‌شود")
            return configured

    @property
    def resilience(self):
        """The active :class:`ResilienceController`, or ``None`` when disabled."""
        return self._resilience

    def _build_resilience(self, primary_method: str, connect_ip: str) -> None:
        """Construct the resilience controller for this session.

        Survives *active* censorship: it ignores forged (DPI-injected) RSTs up
        to ``rst_budget`` then rotates the bypass strategy; on throttling it
        rotates immediately; when strategies are exhausted it rotates the
        upstream IP. The strategy fallback chain starts with the method we are
        about to use, followed by the prober's ``fallback_order`` (when the
        prober ran) or the other implemented strategies.
        """
        self._resilience = None
        if not self.config.get("resilience", True):
            return
        try:
            from core.resilience import ResilienceController, ThroughputMonitor

            # strategy chain: primary first, then probed fallbacks / the rest
            strat_chain = [primary_method]
            if self._prober is not None:
                try:
                    for cand in self._prober.fallback_order():
                        if cand.strategy not in strat_chain:
                            strat_chain.append(cand.strategy)
                except Exception:
                    pass
            if len(strat_chain) == 1:
                from strategies import all_strategies
                for s in all_strategies(implemented_only=True):
                    if s.meta.key not in strat_chain:
                        strat_chain.append(s.meta.key)

            # IP chain: the upstream we're using, plus any configured alternates
            ip_chain = [connect_ip] if connect_ip else []
            for alt in self.config.get("CONNECT_IP_ALTS", []) or []:
                if alt and alt not in ip_chain:
                    ip_chain.append(str(alt))

            ctrl = ResilienceController(
                rst_budget=int(self.config.get("rst_budget", 3)),
                throughput=ThroughputMonitor(
                    throttle_ratio=float(self.config.get("throttle_ratio", 0.4))),
                on_log=self._log,
            )
            ctrl.set_chains(strat_chain, ip_chain)
            self._resilience = ctrl
            self._log(
                f"تاب‌آوری فعال شد (بودجه‌ی RST={ctrl.rst_budget}، "
                f"زنجیره‌ی استراتژی={'→'.join(strat_chain)})")
        except Exception as exc:  # resilience must never block Start
            self._log(f"تاب‌آوری راه‌اندازی نشد ({exc}) — بدون آن ادامه می‌دهیم")
            self._resilience = None

    def _start_core_only(self, epoch: int | None = None) -> None:
        """Plain Tunnel: run xray-core alone, connecting straight to the server.

        No spoofer ProxyServer is started, so xray's TLS/WS handshake reaches
        the real server untouched — identical behaviour to V2RayTun. This is the
        fast, reliable default for normal configs *and* for CDN-placeholder
        configs (xray dials the real CDN endpoint directly; the old local 40443
        portal is unnecessary now that we ship a full xray core).
        """
        assert self.profile is not None
        if epoch is None:
            epoch = self._start_epoch
        from core.xray_manager import XrayManager, find_free_port

        # the configured strategy is what the UI shows even though no spoofer
        # is mangling packets (direct tunnel = no DPI bypass on the outer conn)
        self._emit_strategy(str(self.config.get("bypass_method", "wrong_seq")))

        allow_lan = bool(self.config.get("allow_lan", False))
        # allocate a loopback API port so xray's StatsService can report live
        # uplink/downlink — this is what makes the dashboard's "live usage" work
        # for plain configs (issue #3); without a spoofer in the path there was
        # no traffic source before and the usage card stayed at zero.
        api_port = find_free_port()
        self._xray = XrayManager(
            self.profile,
            socks_port=int(self.config.get("socks_port", 10808)),
            http_port=int(self.config.get("http_port", 10809)),
            spoof_port=None,                 # direct — no spoofer chaining
            gaming_mode=bool(self.config.get("gaming_mode", False)),
            listen="0.0.0.0" if allow_lan else "127.0.0.1",
            api_port=api_port,
        )
        # xray-core lines are tagged as the core source (issue #4)
        self._xray.on_log = self._core_log
        self._core_log(
            f"حالت تونل (مستقیم مثل V2RayTun): xray → "
            f"{self.profile.dial_address}:{self.profile.dial_port}")
        # make it explicit that this path uses NO spoofer/WinDivert and needs
        # no Administrator rights — so the user never sees the admin/WinDivert
        # warning for an ordinary config (issue #4).
        self._core_log(
            "این حالت از اسپوفر/WinDivert استفاده نمی‌کند و نیاز به دسترسی "
            "Administrator ندارد.")
        if not self._xray.is_available:
            self._core_log("هشدار: xray.exe یافت نشد — تونل اجرا نشد")
            self._set_status(STATUS_ERROR)
            self._xray = None
            return
        # honour the real launch result — xray exits within ms on a bad config
        # or a port-bind conflict. The previous code ignored start()'s outcome
        # and ALWAYS logged "✓ اتصال برقرار شد" + turned on the system proxy,
        # so a dead tunnel looked connected while every request silently failed
        # (the user's "healthy configs don't connect" bug). Now: only report
        # success when xray is genuinely up.
        ok = bool(self._xray.start())
        if not ok or not getattr(self._xray, "is_running", False):
            self._core_log(
                "✗ اتصال برقرار نشد — هستهٔ xray اجرا نشد یا بلافاصله بسته شد. "
                "لاگ‌های [هسته xray] بالا را بررسی کنید (تداخل پورت "
                "10808/10809، کانفیگ نامعتبر یا فایل geoip/geosite غایب).")
            self.stop()                 # tear down + reset status to IDLE
            self._set_status(STATUS_ERROR)
            return

        # If the user switched config / stopped while xray was launching, this
        # start has been superseded — don't go green over a half-torn-down
        # session (the "switch config and it breaks/sticks" race). Tear our own
        # xray down and bail; the newer start owns the session now.
        if not self._commit_active(epoch):
            self._core_log("راه‌اندازی لغو شد (کانفیگ عوض شد) — نشست رها شد")
            try:
                if self._xray is not None:
                    self._xray.stop()
            except Exception:
                pass
            return
        self._maybe_enable_system_proxy(True)
        self._core_log("✓ اتصال برقرار شد")
        # spin up the live-usage poller (issue #3): reads xray's cumulative
        # byte counters and turns them into the dashboard's traffic graph.
        self._start_stats_poller()
        # actively verify the tunnel really carries traffic (not just that the
        # process is alive) when self-test is on — same honest probe the spoof
        # path uses, so a config that loads but can't reach its server is
        # reported red instead of a misleading green.
        if self.config.get("self_test", True):
            threading.Thread(target=self._self_test_chain, args=(epoch,),
                             daemon=True).start()

    def _start_stats_poller(self) -> None:
        """Poll xray's StatsService and emit live traffic for plain configs (#3).

        Runs on a daemon thread. Each tick reads cumulative uplink/downlink byte
        totals from xray, derives the instantaneous rate (Δbytes / Δt) and fires
        :meth:`_emit_traffic` — exactly the signal the dashboard's live-usage
        card and the status-bar rate already consume from the spoofer path. This
        closes the gap where direct (non-spoof) configs showed zero usage.
        """
        if self._xray is None or getattr(self._xray, "api_port", None) is None:
            return
        self._stats_stop.clear()

        def _run():
            prev_up = prev_down = 0
            prev_t = time.monotonic()
            first = True
            while not self._stats_stop.wait(1.0):
                xray = self._xray
                if xray is None or not getattr(xray, "is_running", False):
                    break
                stats = None
                try:
                    stats = xray.query_stats()
                except Exception:
                    stats = None
                if stats is None:
                    continue
                up, down = stats
                now = time.monotonic()
                dt = max(now - prev_t, 1e-3)
                if first:
                    # first reading establishes the baseline; no rate yet
                    prev_up, prev_down, prev_t = up, down, now
                    first = False
                    self._emit_traffic(up, down, 0.0, 0.0)
                    continue
                up_bps = max(0.0, (up - prev_up) / dt)
                down_bps = max(0.0, (down - prev_down) / dt)
                prev_up, prev_down, prev_t = up, down, now
                self._emit_traffic(up, down, up_bps, down_bps)

        self._stats_thread = threading.Thread(target=_run, daemon=True)
        self._stats_thread.start()

    def _do_start(self, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = self._start_epoch
        use_core = self.uses_core
        chain_spoofer = self.chains_spoofer

        # loud, actionable warning when the chosen mode needs a profile but
        # none is selected (otherwise we'd silently do a plain SNI forward that
        # can never reach a VLESS/VMess/Trojan server — the "still need V2RayTun"
        # bug). The user must either pick a profile or switch to "SNI Only".
        if self.wants_core_but_no_profile:
            self._log(
                "⚠ هیچ کانفیگی انتخاب نشده — این حالت به یک پروفایل "
                "VLESS/VMess/Trojan نیاز دارد. لطفاً یک کانفیگ انتخاب کنید "
                "یا حالت را روی «SNI Only» بگذارید.")

        # plain Tunnel: xray talks to the server directly (no spoofer in the
        # path) so we never run a spoofer ProxyServer at all — this is the fast,
        # reliable, V2RayTun-equivalent path.
        if use_core and not chain_spoofer:
            self._spoof_port = None
            self._start_core_only(epoch)
            return

        # --- 1. work out the spoofer's listen port + upstream target ---
        # The spoofer always connects to a *fixed Cloudflare IP* and injects a
        # decoy ClientHello (FAKE_SNI); the real SNI rides end-to-end inside
        # xray's TLS, untouched. We never resolve / dial the workers.dev host
        # ourselves — that's xray's job, through the spoofer.
        if use_core:
            assert self.profile is not None
            prof = self.profile
            from core.xray_manager import find_free_port
            if prof.is_spoof_config:
                # the share link's own loopback port (e.g. 40443) is where the
                # core expects the spoofer to listen — keep it so the config's
                # 127.0.0.1:40443 target lands exactly on our spoofer.
                self._spoof_port = find_free_port(prof.dial_port)
                # the spoofer dials the fixed CDN IP with the decoy SNI; let an
                # explicit config CONNECT_IP / FAKE_SNI override the profile.
                connect_ip = (str(self.config.get("CONNECT_IP", "")).strip()
                              or prof.spoof_connect_ip)
                connect_port = (int(self.config.get("CONNECT_PORT", 0))
                                or prof.spoof_connect_port)
                fake_sni = (str(self.config.get("FAKE_SNI", "")).strip()
                            or prof.spoof_fake_sni)
                self._spoof_log(
                    f"حالت SNI-spoof (خودکفا): xray → 127.0.0.1:"
                    f"{self._spoof_port} → spoofer → {connect_ip}:{connect_port}"
                    f" (SNI جعلی: {fake_sni}، SNI واقعی: "
                    f"{prof.sni or prof.host})")
                self._spoof_log(
                    "این حالت از اسپوفر و درایور WinDivert استفاده می‌کند و "
                    "به دسترسی Administrator نیاز دارد.")
            else:
                # ordinary, routable config routed through the spoofer because
                # the user enabled ``force_spoof`` (issue #1). The spoofer dials
                # the config's OWN real IP/port and injects a decoy ClientHello
                # so DPI sees the fake SNI instead of the real one — the same
                # bypass the working spoof-config path uses.
                self._spoof_port = find_free_port(
                    int(self.config.get("LISTEN_PORT", 40443)))
                connect_ip = (str(self.config.get("CONNECT_IP", "")).strip()
                              or prof.address)
                connect_port = (int(self.config.get("CONNECT_PORT", 0))
                                or prof.port)
                fake_sni = (str(self.config.get("FAKE_SNI", "")).strip()
                            or prof.spoof_fake_sni)
                self._spoof_log(
                    f"حالت SNI-spoof اجباری: xray → 127.0.0.1:{self._spoof_port}"
                    f" → spoofer → {connect_ip}:{connect_port} (SNI جعلی: "
                    f"{fake_sni}، SNI واقعی: {prof.sni or prof.host})")
                self._spoof_log(
                    "این حالت از اسپوفر و درایور WinDivert استفاده می‌کند و "
                    "به دسترسی Administrator نیاز دارد.")
        else:
            self._spoof_port = int(self.config.get("LISTEN_PORT", 40443))
            connect_ip = str(self.config.get("CONNECT_IP", ""))
            connect_port = int(self.config.get("CONNECT_PORT", 443))
            fake_sni = str(self.config.get("FAKE_SNI", "www.speedtest.net"))
            self._spoof_log("حالت SNI Only: فقط فورواردر spoofer")
            self._spoof_log(
                "این حالت از اسپوفر و درایور WinDivert استفاده می‌کند و "
                "به دسترسی Administrator نیاز دارد.")

        # --- 2. build + start the spoofer (main.ProxyServer) ---
        proxy_cfg = {
            "LISTEN_HOST": "127.0.0.1" if use_core
            else str(self.config.get("LISTEN_HOST", "127.0.0.1")),
            "LISTEN_PORT": self._spoof_port,
            "CONNECT_IP": connect_ip,
            "CONNECT_PORT": connect_port,
            "FAKE_SNI": fake_sni,
            "gaming_mode": bool(self.config.get("gaming_mode", False)),
        }
        # choose the bypass method: auto-probe if enabled, else the configured one
        bypass_method = self._choose_bypass_method(connect_ip, connect_port)
        # single source of truth — push the live method to the UI so the
        # Dashboard "active strategy" matches what Diagnostics shows
        self._emit_strategy(bypass_method)

        # build the resilience controller for this session (forged-RST / throttle
        # detection + strategy/IP rotation) so the runtime can consult it
        self._build_resilience(bypass_method, connect_ip)

        from main import ProxyServer
        self._proxy = ProxyServer(proxy_cfg)
        self._proxy.bypass_method = bypass_method
        # hand the spoofer the resilience controller if it knows how to use one
        if self._resilience is not None and hasattr(self._proxy, "resilience"):
            self._proxy.resilience = self._resilience
        self._proxy.on_log = self._spoof_log
        self._proxy.on_status_change = self._on_proxy_status
        self._proxy.on_connection_count_change = self._emit_count
        # live throughput (upload/download) — the ProxyServer reports cumulative
        # bytes + rate; the UI turns it into the dashboard's traffic graph
        if hasattr(self._proxy, "on_traffic"):
            self._proxy.on_traffic = self._emit_traffic
        # start() now blocks until the spoofer is actually listening (or fails);
        # if it couldn't come up we must NOT launch xray against a dead port —
        # that's the classic "connects in V2RayTun but not standalone" trap
        # (xray dials 127.0.0.1:40443 before the spoofer has bound it).
        started = self._proxy.start()
        if started is False:
            err = (getattr(self._proxy, "_start_error", None)
                   or "راه‌اندازی spoofer ناموفق بود")
            self._spoof_log(f"✗ {err}")
            # tear down any partially-started pieces first (stop() resets the
            # status to IDLE), *then* set ERROR so it isn't clobbered.
            self.stop()
            self._set_status(STATUS_ERROR)
            return

        # --- 3. chain xray core in front of the spoofer ---
        # Reached for spoof configs (127.0.0.1:40443 links) and the explicit
        # SNI+X modes. xray dials the local spoofer (127.0.0.1:<spoof_port>)
        # carrying the *real* SNI/host/path inside its TLS; the spoofer rewrites
        # the destination to the fixed CDN IP and injects the decoy ClientHello.
        # Plain "Tunnel" with an ordinary config returned early via
        # _start_core_only (xray straight to the real server, no spoofer).
        if chain_spoofer:
            assert self.profile is not None
            from core.xray_manager import XrayManager
            allow_lan = bool(self.config.get("allow_lan", False))
            self._xray = XrayManager(
                self.profile,
                socks_port=int(self.config.get("socks_port", 10808)),
                http_port=int(self.config.get("http_port", 10809)),
                spoof_port=self._spoof_port,
                gaming_mode=bool(self.config.get("gaming_mode", False)),
                listen="0.0.0.0" if allow_lan else "127.0.0.1",
            )
            self._xray.on_log = self._core_log
            if not self._xray.is_available:
                self._core_log("هشدار: xray.exe یافت نشد — فقط spoofer اجرا می‌شود")
            else:
                ok = bool(self._xray.start())
                if not ok or not getattr(self._xray, "is_running", False):
                    self._core_log(
                        "✗ هستهٔ xray اجرا نشد یا بلافاصله بسته شد — اتصال "
                        "برقرار نشد. لاگ‌های [هسته xray] بالا را بررسی کنید "
                        "(تداخل پورت 10808/10809 یا کانفیگ نامعتبر).")
                    self.stop()
                    self._set_status(STATUS_ERROR)
                    return

        # If the user switched config / stopped while the spoofer (+xray) was
        # coming up, this start has been superseded — don't go green over a
        # half-torn-down session. Tear our own handles down and bail so the
        # newer start owns the session cleanly (rapid config-switch race).
        if not self._commit_active(epoch):
            self._log("راه‌اندازی لغو شد (کانفیگ عوض شد) — نشست رها شد")
            try:
                if self._xray is not None:
                    self._xray.stop()
            except Exception:
                pass
            try:
                if self._proxy is not None:
                    self._proxy.stop()
            except Exception:
                pass
            return

        # --- 4. optionally point the OS system proxy at our local HTTP port ---
        self._maybe_enable_system_proxy(chain_spoofer)

        self._log("✓ اتصال برقرار شد")

        # Tell the user *how* to actually route traffic through the tunnel.
        # This is the single biggest source of "works with V2RayTun but not
        # standalone" confusion: our app exposes a local SOCKS/HTTP proxy but,
        # unlike V2RayTun, does not capture OS traffic unless system-proxy is on.
        if chain_spoofer:
            socks_port = int(self.config.get("socks_port", 10808))
            http_port = int(self.config.get("http_port", 10809))
            if not self.config.get("system_proxy", False):
                self._log(
                    f"ℹ برای استفاده، یا گزینهٔ «پروکسی سیستم» را روشن کنید، "
                    f"یا مرورگر/برنامه را روی پروکسی SOCKS5 127.0.0.1:"
                    f"{socks_port} (یا HTTP 127.0.0.1:{http_port}) تنظیم کنید. "
                    f"بدون این کار، ترافیک سیستم وارد تونل نمی‌شود.")

        # --- 5. self-test the internal chain (xray → spoofer → CDN) ---------
        # The #1 confusion is "works with V2RayTun but not standalone": that's
        # almost always because nothing is pointing the OS/browser at our local
        # SOCKS/HTTP port, so xray never dials the spoofer. This active probe
        # drives a real request *through our own HTTP proxy port* a few seconds
        # after start, so the log unambiguously shows whether the full internal
        # path works — independent of the user's browser/system-proxy settings.
        if chain_spoofer and self.config.get("self_test", True):
            threading.Thread(target=self._self_test_chain, args=(epoch,),
                             daemon=True).start()

    def _effective_ports(self) -> tuple[int, int]:
        """The (socks, http) ports that are ACTUALLY in use right now.

        ``XrayManager.start()`` may switch off the configured 10808/10809 to a
        free port when a stale listener still holds them. The system proxy and
        the self-test MUST point at those *real* bound ports, not the configured
        defaults — otherwise we enable the OS proxy on a dead port (10809) while
        xray actually listens on e.g. 61483, so every request fails with
        ``WinError 10053`` / ``SSL UNEXPECTED_EOF`` even though the tunnel is up.
        This was the cause of "ordinary configs don't connect" in the latest log.
        """
        socks = int(self.config.get("socks_port", 10808))
        http = int(self.config.get("http_port", 10809))
        xray = self._xray
        if xray is not None:
            socks = int(getattr(xray, "socks_port", socks) or socks)
            http = int(getattr(xray, "http_port", http) or http)
        return socks, http

    def _maybe_enable_system_proxy(self, use_core: bool) -> None:
        """Set the Windows system proxy → local HTTP port, if requested.

        System-proxy mode only makes sense when xray-core is in the chain (it
        exposes a real local HTTP/SOCKS proxy). In SNI-Only mode the spoofer is
        a transparent forwarder, so there is nothing to point the OS proxy at.
        """
        self._system_proxy = None
        if not self.config.get("system_proxy", False):
            return
        if not use_core:
            self._log("پروکسی سیستم فقط در حالت‌های دارای xray کاربرد دارد "
                      "(در SNI Only نادیده گرفته شد)")
            return
        try:
            from core.system_proxy import SystemProxy, is_windows
            host = "127.0.0.1"
            # use the REAL bound http port (see _effective_ports), not the
            # configured default which xray may have abandoned on a conflict.
            port = self._effective_ports()[1]
            if self._system_proxy_factory is not None:
                sp = self._system_proxy_factory()
            else:
                if not is_windows():
                    self._log("پروکسی سیستم فقط روی ویندوز اعمال می‌شود")
                    return
                sp = SystemProxy(on_log=self._log)
            sp.enable(host, port)
            self._system_proxy = sp
        except Exception as exc:  # never block Start on proxy failure
            self._log(f"تنظیم پروکسی سیستم ناموفق: {exc}")
            self._system_proxy = None

    def _self_test_chain(self, epoch: int | None = None) -> None:
        """Probe the full internal chain through our own local HTTP proxy.

        Runs off-thread shortly after Start. Makes a real HTTPS request to a
        lightweight 204 endpoint *via* ``127.0.0.1:<http_port>`` (xray's HTTP
        inbound), so it exercises xray → spoofer → CDN exactly like the browser
        would. The result is logged in plain language so the user can tell
        whether the tunnel itself works, separately from whether their browser
        is pointed at the proxy.

        Enforcement (the sabotaged-spoof "connected + چند کیلوبایت دیتا ولی هیچ
        سایتی باز نشد" bug): when the test CONCLUSIVELY shows the tunnel only
        reaches the CDN edge — a real status came back but the verifiable body
        never did — we now DEMOTE the engine to ERROR instead of merely logging.
        That stops a broken config from sitting "active" while xray retries leak
        a few KB to the dead backend, which the user mistook for a working
        tunnel. Controlled by ``self_test_enforce`` (default on); a transient
        network/exception failure is logged but does NOT demote (could be the
        captive host, not the tunnel).
        """
        import time
        import urllib.request

        if epoch is None:
            epoch = self._start_epoch
        # give xray + the spoofer a moment to finish binding/handshaking
        time.sleep(3.0)
        if self._status != STATUS_ACTIVE:
            return  # already stopped / errored — nothing to test
        if not self._epoch_current(epoch):
            return  # superseded by a newer start/stop — don't touch this session

        # probe the REAL bound http port (xray may have moved off 10809 on a
        # port conflict); testing the configured default would hit a dead port.
        socks_port, http_port = self._effective_ports()
        proxy = f"http://127.0.0.1:{http_port}"
        # CRITICAL — DON'T use an empty ``generate_204`` here. A sabotaged /
        # broken spoof config still reaches the fixed Cloudflare anycast IP, so
        # the CDN edge answers ``generate_204`` with a 204 *directly* — the
        # traffic never reaches the open internet through the Worker. That made
        # the self-test log "✓ تونل کار می‌کند" for a config that loaded no site
        # (the user's "green ping + چند کیلوبایت دیتا ولی هیچ سایتی باز نشد").
        # So we fetch a resource with a VERIFIABLE body that only a genuinely
        # working tunnel can return, and require the body marker to be present.
        test_url = "http://detectportal.firefox.com/success.txt"
        body_marker = "success"
        self._log(f"[self-test] آزمایش مسیر داخلی از طریق پروکسی "
                  f"{proxy} → {test_url} …")
        try:
            handler = urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
            req = urllib.request.Request(
                test_url, headers={"User-Agent": "SNISpoofer-selftest"})
            t0 = time.time()
            with opener.open(req, timeout=12) as resp:
                code = resp.getcode()
                try:
                    body = (resp.read(4096) or b"").decode("latin-1", "ignore")
                except Exception:
                    body = ""
            dt = int((time.time() - t0) * 1000)
            verified = code in (200, 204) and body_marker in body.lower()
            if verified:
                self._log(
                    f"[self-test] ✓ مسیر داخلی سالم است (HTTP {code}، {dt}ms، "
                    f"محتوای واقعی دریافت شد). تونل کار می‌کند — اگر مرورگرتان "
                    f"باز نمی‌کند یعنی پروکسی سیستم/مرورگر روی "
                    f"127.0.0.1:{http_port} (یا SOCKS {socks_port}) تنظیم نشده.")
            elif code in (200, 204):
                # got a status but NOT the expected body → only the CDN edge
                # answered; real traffic isn't passing. This is exactly the
                # "fake healthy" config — report it honestly as broken AND, when
                # enforcement is on, demote the session to ERROR so it doesn't
                # masquerade as connected while leaking a few retry-KB.
                self._log(
                    f"[self-test] ✗ تونل واقعی کار نمی‌کند: فقط لبهٔ CDN پاسخ "
                    f"داد (HTTP {code} ولی محتوای واقعی رد نشد). این کانفیگ "
                    f"وصل به‌نظر می‌رسد اما ترافیک واقعی عبور نمی‌کند — مسیر "
                    f"Worker/سرور پشت CDN خراب است. [conn #...] را بررسی کنید.")
                self._demote_failed_selftest(
                    epoch, "فقط لبهٔ CDN پاسخ داد — ترافیک واقعی عبور نمی‌کند")
            else:
                self._log(f"[self-test] ⚠ پاسخ غیرمنتظره HTTP {code} — مسیر "
                          f"کامل برقرار نشد.", )
                self._demote_failed_selftest(
                    epoch, f"پاسخ غیرمنتظره HTTP {code} — مسیر کامل برقرار نشد")
        except Exception as exc:
            # a transient network/proxy hiccup here is NOT proof the tunnel is
            # dead (the captive host could be slow/blocked). Log it, but do not
            # demote — the live-tunnel ping / browsing remains the source of
            # truth, and demoting on a flaky probe would falsely red a working
            # config.
            self._log(
                f"[self-test] ✗ مسیر داخلی شکست خورد: {type(exc).__name__}: "
                f"{exc} — یعنی xray به اسپوفر یا اسپوفر به CDN وصل نمی‌شود. "
                f"لاگ‌های [conn #...] و [xray] بالا را بررسی کنید.")

    def _demote_failed_selftest(self, epoch: int | None, reason: str) -> None:
        """Flip an apparently-active-but-non-working session to ERROR.

        Called by :meth:`_self_test_chain` when the internal probe proves the
        tunnel only reaches the CDN edge (the sabotaged-spoof "connected but
        loads nothing" case). Guards:

        * ``self_test_enforce`` config flag (default on) — turn off to keep the
          old log-only behaviour.
        * epoch check — never demote a session a newer start/stop already owns.
        * only demote from ACTIVE — if we already moved on, do nothing.

        On demotion we tear the live chain down (so the leaked retry-KB stop)
        and set ERROR, which the UI surfaces as «تلاش دوباره» / red.
        """
        if not self.config.get("self_test_enforce", True):
            return
        if epoch is not None and not self._epoch_current(epoch):
            return
        with self._lock:
            if self._status != STATUS_ACTIVE:
                return
            if epoch is not None and epoch != self._start_epoch:
                return
            # supersede any racing start and claim the demotion under the lock
            self._start_epoch += 1
        self._log(
            f"[self-test] ⛔ اتصال به‌عنوان ناموفق علامت‌گذاری شد: {reason}. "
            f"وضعیت به «خطا» تغییر کرد تا کانفیگ خراب به‌اشتباه «متصل» نشان "
            f"داده نشود.")
        # tear the dead chain down FIRST (xray/spoofer/system-proxy + counters)
        # so it stops retrying and leaking bytes; stop() ends on IDLE, so we set
        # ERROR *after* it to leave the UI on the red «تلاش دوباره» state.
        try:
            self.stop()
        except Exception:
            pass
        self._set_status(STATUS_ERROR)

    def _on_proxy_status(self, running: bool) -> None:
        # the proxy reports its own listen-loop coming up/down; only downgrade
        # to idle if we believe we're running (avoids racing the start path)
        if not running and self._status == STATUS_ACTIVE:
            self._set_status(STATUS_IDLE)
            self._emit_count(0, 0)

    # ------------------------------------------------------------------- stop

    def stop(self) -> None:
        """Stop xray then the spoofer; safe to call when already stopped."""
        # signal the live-usage poller to exit before we drop the xray handle
        self._stats_stop.set()
        with self._lock:
            # bump the epoch so any in-flight _do_start() from a previous /
            # racing start is superseded and cannot commit ACTIVE behind us.
            self._start_epoch += 1
            xray, proxy = self._xray, self._proxy
            sysproxy = self._system_proxy
            self._xray = self._proxy = self._system_proxy = None
        # restore the OS proxy first so the browser stops pointing at a dead port
        if sysproxy is not None:
            try:
                sysproxy.disable()
            except Exception as exc:
                self._log(f"خطا در خاموش‌کردن پروکسی سیستم: {exc}")
        if xray is not None:
            try:
                xray.stop()
            except Exception as exc:
                self._log(f"خطا در توقف xray: {exc}")
        if proxy is not None:
            try:
                proxy.stop()
            except Exception as exc:
                self._log(f"خطا در توقف spoofer: {exc}")
        self._spoof_port = None
        self._resilience = None
        self._active_strategy = None
        self._set_status(STATUS_IDLE)
        self._emit_count(0, 0)
        self._emit_traffic(0, 0, 0.0, 0.0)
