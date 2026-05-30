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

    def _log(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

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

        Any mode other than the raw ``"SNI Only"`` forwarder needs xray-core to
        carry the VLESS/VMess/Trojan profile. ``"SNI Only"`` is the only mode
        that runs without a core (and therefore without a profile).
        """
        mode = str(self.config.get("connection_mode", "Tunnel"))
        return self.profile is not None and mode != "SNI Only"

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

        Plain ``"Tunnel"`` mode connects xray straight to the server — exactly
        like V2RayTun — so the spoofer never re-mangles xray's own TLS/WS
        handshake (which made the tunnel slow or broke it entirely; the
        "config still doesn't connect / much slower" feedback). Only the
        explicit SNI-bypass combos ("SNI + Warp", "SNI + Psiphon", …) chain the
        spoofer in front, because those modes specifically want DPI evasion on
        the outer connection.
        """
        if not self.uses_core:
            return False
        mode = str(self.config.get("connection_mode", "Tunnel"))
        return mode not in ("Tunnel",)

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

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Start spoofer (+ optional xray) on a background thread."""
        with self._lock:
            if self.is_running:
                self._log("موتور از قبل در حال اجراست")
                return
            self._set_status(STATUS_CONNECTING)
        threading.Thread(target=self._start_blocking, daemon=True).start()

    def _start_blocking(self) -> None:
        try:
            self._do_start()
        except Exception as exc:  # never let the worker thread die silently
            self._log(f"خطا در راه‌اندازی: {exc}")
            self._set_status(STATUS_ERROR)
            self.stop()

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

    def _start_core_only(self) -> None:
        """Plain Tunnel: run xray-core alone, connecting straight to the server.

        No spoofer ProxyServer is started, so xray's TLS/WS handshake reaches
        the real server untouched — identical behaviour to V2RayTun. This is the
        fast, reliable default for normal configs.
        """
        assert self.profile is not None
        from core.xray_manager import XrayManager

        # the configured strategy is what the UI shows even though no spoofer
        # is mangling packets (direct tunnel = no DPI bypass on the outer conn)
        self._emit_strategy(str(self.config.get("bypass_method", "wrong_seq")))

        allow_lan = bool(self.config.get("allow_lan", False))
        self._xray = XrayManager(
            self.profile,
            socks_port=int(self.config.get("socks_port", 10808)),
            http_port=int(self.config.get("http_port", 10809)),
            spoof_port=None,                 # direct — no spoofer chaining
            gaming_mode=bool(self.config.get("gaming_mode", False)),
            listen="0.0.0.0" if allow_lan else "127.0.0.1",
        )
        self._xray.on_log = self._log
        self._log(
            f"حالت تونل (مستقیم مثل V2RayTun): xray → "
            f"{self.profile.address}:{self.profile.port}")
        if not self._xray.is_available:
            self._log("هشدار: xray.exe یافت نشد — تونل اجرا نشد")
            self._set_status(STATUS_ERROR)
            self._xray = None
            return
        self._xray.start()

        self._maybe_enable_system_proxy(True)
        self._set_status(STATUS_ACTIVE)
        self._log("✓ اتصال برقرار شد")

    def _do_start(self) -> None:
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
            self._start_core_only()
            return

        # --- 1. work out the spoofer's listen port + upstream target ---
        if use_core:
            assert self.profile is not None
            from core.xray_manager import find_free_port
            self._spoof_port = find_free_port(
                int(self.config.get("LISTEN_PORT", 40443)))
            connect_ip = self.profile.address
            connect_port = self.profile.port
            self._log(
                f"حالت v2rayN: spoofer روی 127.0.0.1:{self._spoof_port} "
                f"→ {connect_ip}:{connect_port}")
        else:
            self._spoof_port = int(self.config.get("LISTEN_PORT", 40443))
            connect_ip = str(self.config.get("CONNECT_IP", ""))
            connect_port = int(self.config.get("CONNECT_PORT", 443))
            self._log("حالت SNI Only: فقط فورواردر spoofer")

        # --- 2. build + start the spoofer (main.ProxyServer) ---
        proxy_cfg = {
            "LISTEN_HOST": "127.0.0.1" if use_core
            else str(self.config.get("LISTEN_HOST", "127.0.0.1")),
            "LISTEN_PORT": self._spoof_port,
            "CONNECT_IP": connect_ip,
            "CONNECT_PORT": connect_port,
            "FAKE_SNI": str(self.config.get("FAKE_SNI", "www.speedtest.net")),
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
        self._proxy.on_log = self._log
        self._proxy.on_status_change = self._on_proxy_status
        self._proxy.on_connection_count_change = self._emit_count
        # live throughput (upload/download) — the ProxyServer reports cumulative
        # bytes + rate; the UI turns it into the dashboard's traffic graph
        if hasattr(self._proxy, "on_traffic"):
            self._proxy.on_traffic = self._emit_traffic
        self._proxy.start()

        # --- 3. chain xray core in front of the spoofer (SNI-bypass combos) ---
        # NB: plain "Tunnel" returned early via _start_core_only; only the
        # explicit SNI+X modes reach here with chain_spoofer == True.
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
            self._xray.on_log = self._log
            if not self._xray.is_available:
                self._log("هشدار: xray.exe یافت نشد — فقط spoofer اجرا می‌شود")
            else:
                self._xray.start()

        # --- 4. optionally point the OS system proxy at our local HTTP port ---
        self._maybe_enable_system_proxy(chain_spoofer)

        self._set_status(STATUS_ACTIVE)
        self._log("✓ اتصال برقرار شد")

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
            port = int(self.config.get("http_port", 10809))
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

    def _on_proxy_status(self, running: bool) -> None:
        # the proxy reports its own listen-loop coming up/down; only downgrade
        # to idle if we believe we're running (avoids racing the start path)
        if not running and self._status == STATUS_ACTIVE:
            self._set_status(STATUS_IDLE)
            self._emit_count(0, 0)

    # ------------------------------------------------------------------- stop

    def stop(self) -> None:
        """Stop xray then the spoofer; safe to call when already stopped."""
        with self._lock:
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
