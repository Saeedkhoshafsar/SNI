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


class EngineController:
    """Owns the spoofer + xray lifecycle for one connection."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config: dict[str, Any] = dict(config or {})
        self.profile: Optional[Profile] = None

        # external callbacks (set by the UI layer)
        self.on_log: Optional[LogCb] = None
        self.on_status: Optional[StatusCb] = None
        self.on_count: Optional[CountCb] = None

        # internals
        self._proxy = None            # main.ProxyServer
        self._xray = None             # core.xray_manager.XrayManager
        self._prober = None           # core.prober.AutoProber (when enabled)
        self._resilience = None       # core.resilience.ResilienceController
        self._spoof_port: Optional[int] = None
        self._status = STATUS_IDLE
        self._lock = threading.RLock()

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
        """True when an xray core is chained under the spoofer."""
        mode = str(self.config.get("connection_mode", "SNI Only"))
        return self.profile is not None and mode != "SNI Only"

    def diagnostics(self):
        """Return a :class:`core.diagnostics.DiagnosticsSnapshot` of live state.

        Safe to call any time (idle or running); the diagnostics layer tolerates
        a not-yet-built prober / resilience controller and returns defaults.
        """
        from core.diagnostics import snapshot
        return snapshot(self)

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

    def _do_start(self) -> None:
        use_core = self.uses_core

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
        self._proxy.start()

        # --- 3. optionally chain xray core in front of the spoofer ---
        if use_core:
            assert self.profile is not None
            from core.xray_manager import XrayManager
            self._xray = XrayManager(
                self.profile,
                socks_port=int(self.config.get("socks_port", 10808)),
                http_port=int(self.config.get("http_port", 10809)),
                spoof_port=self._spoof_port,
                gaming_mode=bool(self.config.get("gaming_mode", False)),
            )
            self._xray.on_log = self._log
            if not self._xray.is_available:
                self._log("هشدار: xray.exe یافت نشد — فقط spoofer اجرا می‌شود")
            else:
                self._xray.start()

        self._set_status(STATUS_ACTIVE)
        self._log("✓ اتصال برقرار شد")

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
            self._xray = self._proxy = None
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
        self._set_status(STATUS_IDLE)
        self._emit_count(0, 0)
