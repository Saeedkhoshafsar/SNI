"""Unit tests for :class:`core.engine.EngineController`.

The real ``ProxyServer`` needs WinDivert (Windows + admin) and ``XrayManager``
needs the bundled ``xray.exe``; neither runs in CI/sandbox. We therefore stub
both with fakes and assert the *orchestration* logic: mode selection, the
auto-chained spoof port, callback fan-out and clean start/stop transitions.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.engine as engine_mod
from core.engine import (
    EngineController, STATUS_IDLE, STATUS_ACTIVE, STATUS_CONNECTING,
    STATUS_ERROR)
from core.profile import Profile


# --------------------------------------------------------------------------
#  Fakes
# --------------------------------------------------------------------------

class _HangingOpener:
    """An opener whose every request blocks for (almost) the full per-request
    timeout before raising — simulating a dead route that neither answers nor
    fails fast. Used to prove the hard wall-clock deadline aborts the whole
    measurement instead of letting each fallback attempt burn its own timeout.
    """

    def open(self, _req, timeout=None):
        import time
        # sleep close to the (already deadline-bounded) per-request timeout so
        # the wall-clock guard — not the socket timeout — is what stops us.
        time.sleep(min(float(timeout or 1.0), 1.0))
        raise OSError("simulated dead route (no response)")


class FakeProxy:
    last_instance = None

    def __init__(self, config):
        self.config = config
        self.bypass_method = "wrong_seq"
        self.resilience = None  # set by the engine when resilience is enabled
        self.on_log = None
        self.on_status_change = None
        self.on_connection_count_change = None
        self.on_traffic = None
        self.started = False
        self.stopped = False
        self._start_error = None
        FakeProxy.last_instance = self

    def start(self):
        # mirror the real ProxyServer contract: start() blocks until listening
        # and returns True on success / False on failure.
        self.started = True
        if self.on_log:
            self.on_log("fake proxy started")
        if self.on_status_change:
            self.on_status_change(True)
        return True

    def stop(self):
        self.stopped = True


class FakeXray:
    last_instance = None

    def __init__(self, profile, socks_port=10808, http_port=10809,
                 spoof_port=None, gaming_mode=False, listen="127.0.0.1",
                 api_port=None):
        self.profile = profile
        self.socks_port = socks_port
        self.http_port = http_port
        self.spoof_port = spoof_port
        self.gaming_mode = gaming_mode
        self.listen = listen
        self.api_port = api_port
        self.on_log = None
        self.started = False
        self.stopped = False
        FakeXray.last_instance = self

    @property
    def is_available(self):
        return True

    @property
    def is_running(self):
        return self.started and not self.stopped

    def query_stats(self):
        # the live-usage poller (#3) calls this; under fakes there is no real
        # xray, so report no stats (the poller then simply skips the tick).
        return None

    def start(self):
        # mirror the real XrayManager contract: return True when the process is
        # up and stayed up. The engine now honours this to avoid falsely
        # reporting "connected" when xray died on a port-bind conflict.
        self.started = True
        return True

    def stop(self):
        self.stopped = True


def _install_fakes():
    """Patch the lazily-imported core dependencies with fakes.

    Returns a callable that restores the originals so the patches never leak
    into other test modules (pytest runs everything in one process).
    """
    saved_main = sys.modules.get("main")
    fake_main = type(sys)("main")
    fake_main.ProxyServer = FakeProxy
    sys.modules["main"] = fake_main

    import core.xray_manager as xm
    saved_xray = xm.XrayManager
    saved_find = xm.find_free_port
    xm.XrayManager = FakeXray
    # deterministic port so we can assert the chain
    xm.find_free_port = lambda preferred=None: preferred or 40443

    # neutralise the post-start connectivity self-test so the test suite never
    # spins up a real network probe thread (no xray/spoofer exist under fakes).
    saved_selftest = EngineController._self_test_chain
    EngineController._self_test_chain = lambda self, *a, **k: None

    def restore():
        if saved_main is not None:
            sys.modules["main"] = saved_main
        else:
            sys.modules.pop("main", None)
        xm.XrayManager = saved_xray
        xm.find_free_port = saved_find
        EngineController._self_test_chain = saved_selftest

    return restore


def _wait_status(ctrl, status, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if ctrl.status == status:
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
#  Tests
# --------------------------------------------------------------------------

class EngineControllerTest(unittest.TestCase):
    def setUp(self):
        self._restore = _install_fakes()
        FakeProxy.last_instance = None
        FakeXray.last_instance = None

    def tearDown(self):
        self._restore()

    def _profile(self):
        return Profile(protocol="vless", address="real.example.com", port=8443,
                       uuid="11111111-1111-1111-1111-111111111111")

    def test_uses_core_logic(self):
        # No profile + SNI Only → raw forwarder, no core.
        ctrl = EngineController({"connection_mode": "SNI Only"})
        self.assertFalse(ctrl.uses_core)
        # A selected profile ALWAYS needs xray, even in SNI Only (a VLESS
        # profile can't run on a raw forwarder).
        ctrl.set_profile(self._profile())
        self.assertTrue(ctrl.uses_core)
        ctrl.update_config({"connection_mode": "SNI Only"})
        self.assertTrue(ctrl.uses_core)

    def test_sni_only_no_profile_starts_proxy_no_xray(self):
        # The standalone raw-forwarder case: SNI Only with NO profile selected.
        ctrl = EngineController({
            "connection_mode": "SNI Only",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.2.3.4", "CONNECT_PORT": 443,
        })
        ctrl.set_profile(None)
        logs = []
        ctrl.on_log = logs.append
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNotNone(FakeProxy.last_instance)
        self.assertTrue(FakeProxy.last_instance.started)
        self.assertIsNone(FakeXray.last_instance)  # no core without a profile
        self.assertEqual(FakeProxy.last_instance.config["CONNECT_IP"], "1.2.3.4")
        ctrl.stop()
        self.assertEqual(ctrl.status, STATUS_IDLE)

    def test_core_mode_chains_spoofer_under_xray(self):
        # #6: only a SPOOF config (loopback share link) chains the spoofer. The
        # spoofer dials the fixed CDN IP with the decoy SNI; xray's outbound is
        # pointed at the local spoofer port.
        prof = self._spoof_profile()
        ctrl = EngineController({"connection_mode": "Tunnel"})
        ctrl.set_profile(prof)
        self.assertTrue(ctrl.chains_spoofer)
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))

        proxy = FakeProxy.last_instance
        xray = FakeXray.last_instance
        self.assertIsNotNone(proxy)
        self.assertIsNotNone(xray)
        # spoofer forwards to the fixed CDN IP (decoy SNI rides on top)
        self.assertEqual(proxy.config["CONNECT_IP"], prof.spoof_connect_ip)
        self.assertEqual(proxy.config["CONNECT_PORT"], prof.spoof_connect_port)
        # xray's outbound is pointed at the local spoofer port
        self.assertEqual(xray.spoof_port, proxy.config["LISTEN_PORT"])
        ctrl.stop()
        self.assertTrue(proxy.stopped)
        self.assertTrue(xray.stopped)

    def test_ordinary_config_never_chains_spoofer(self):
        # #6: an ordinary (routable) config connects directly — no spoofer is
        # ever started, regardless of the connection mode.
        for mode in ("Tunnel", "SNI Only"):
            with self.subTest(mode=mode):
                FakeProxy.last_instance = None
                ctrl = EngineController({"connection_mode": mode})
                ctrl.set_profile(self._profile())
                self.assertFalse(ctrl.chains_spoofer)
                ctrl.start()
                self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
                # core-only path: xray runs, no spoofer ProxyServer
                self.assertIsNotNone(FakeXray.last_instance)
                self.assertIsNone(FakeProxy.last_instance)
                ctrl.stop()

    def test_spoofer_start_failure_aborts_and_no_xray(self):
        # If the spoofer can't come up (e.g. WinDivert missing / port busy),
        # start() returns False — the engine must NOT launch xray against a
        # dead port and must report STATUS_ERROR. This is the regression guard
        # for the "connects in V2RayTun but not standalone" class of bug.
        class FailingProxy(FakeProxy):
            def start(self):
                self.started = True
                self._start_error = "WinDivert نصب نیست"
                return False

        import main as fake_main
        fake_main.ProxyServer = FailingProxy
        try:
            # a spoof config is the case that actually starts the spoofer (#6)
            ctrl = EngineController({"connection_mode": "Tunnel"})
            ctrl.set_profile(self._spoof_profile())
            logs = []
            ctrl.on_log = logs.append
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ERROR))
            # xray must never have been chained behind a dead spoofer
            self.assertIsNone(FakeXray.last_instance)
            self.assertTrue(any("WinDivert" in m for m in logs))
        finally:
            fake_main.ProxyServer = FakeProxy

    def test_tunnel_reports_error_when_xray_dies_immediately(self):
        # Bug A (the user's "healthy configs don't connect"): if xray exits
        # right after launch (port-bind conflict on 10808/10809, bad config,
        # missing geoip), start() returns False. The engine must report
        # STATUS_ERROR — NOT a false "✓ اتصال برقرار شد" with the system proxy
        # left pointing at a dead port.
        class DyingXray(FakeXray):
            def start(self):           # mirrors a process that crashed on bind
                self.started = False
                return False

            @property
            def is_running(self):
                return False

        import core.xray_manager as xm
        saved = xm.XrayManager
        xm.XrayManager = DyingXray
        try:
            ctrl = EngineController({"connection_mode": "Tunnel",
                                     "system_proxy": True})
            ctrl.set_profile(self._profile())
            logs = []
            ctrl.on_log = logs.append
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ERROR))
            self.assertNotEqual(ctrl.status, STATUS_ACTIVE)
            # the honest failure message, and NO false success
            self.assertTrue(any("اتصال برقرار نشد" in m or "اجرا نشد" in m
                                for m in logs))
            self.assertFalse(any("✓ اتصال برقرار شد" in m for m in logs))
            ctrl.stop()
        finally:
            xm.XrayManager = saved

    def test_spoof_reports_error_when_xray_dies_immediately(self):
        # Same honesty guard for the spoof-chain path: even if the spoofer comes
        # up, a dead xray means the tunnel can't carry traffic → STATUS_ERROR,
        # not a fake success.
        class DyingXray(FakeXray):
            def start(self):
                self.started = False
                return False

            @property
            def is_running(self):
                return False

        import core.xray_manager as xm
        saved = xm.XrayManager
        xm.XrayManager = DyingXray
        try:
            ctrl = EngineController({"connection_mode": "Tunnel"})
            ctrl.set_profile(self._spoof_profile())
            logs = []
            ctrl.on_log = logs.append
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ERROR))
            self.assertFalse(any("✓ اتصال برقرار شد" in m for m in logs))
            ctrl.stop()
        finally:
            xm.XrayManager = saved

    def test_plain_tunnel_runs_xray_directly_no_spoofer(self):
        # plain "Tunnel" must behave like V2RayTun: xray connects straight to
        # the server (spoof_port=None) and NO spoofer ProxyServer is started, so
        # the tunnel handshake is never re-mangled (the slow/broken feedback).
        ctrl = EngineController({"connection_mode": "Tunnel"})
        ctrl.set_profile(self._profile())
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))

        xray = FakeXray.last_instance
        self.assertIsNotNone(xray)
        self.assertIsNone(xray.spoof_port)            # direct, no chaining
        self.assertTrue(xray.started)
        self.assertIsNone(FakeProxy.last_instance)    # no spoofer at all
        ctrl.stop()
        self.assertTrue(xray.stopped)
        self.assertEqual(ctrl.status, STATUS_IDLE)

    def _spoof_profile(self):
        # SNI-spoof config: the 127.0.0.1:40443 target IS our spoofer. xray
        # dials it; the spoofer forwards to a fixed Cloudflare IP and injects a
        # decoy ClientHello. The real sni/host/path ride inside xray's TLS.
        return Profile(
            protocol="vless", address="127.0.0.1", port=40443,
            uuid="84524180-c2d5-4bc1-83bb-c36f22d69a3b",
            transport="xhttp", security="tls",
            sni="lucky-union-b89c.hamijafayi.workers.dev",
            host="lucky-union-b89c.hamijafayi.workers.dev",
            path="/vless-xhttp", mode="auto", fingerprint="chrome")

    def test_spoof_config_chains_xray_through_spoofer_to_fixed_cdn_ip(self):
        # The defining bug fix: a 127.0.0.1:40443 config must run BOTH our xray
        # (dialing the local spoofer) AND the spoofer (dialing the fixed CF IP
        # with the decoy SNI) — self-contained, replacing V2RayTun. It connects
        # in V2RayTun precisely because V2RayTun dials our spoofer; we now do
        # the same internally instead of dialing workers.dev directly.
        ctrl = EngineController({"connection_mode": "Tunnel"})
        prof = self._spoof_profile()
        ctrl.set_profile(prof)
        self.assertTrue(prof.is_spoof_config)
        self.assertTrue(ctrl.chains_spoofer)    # spoofer IS chained
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))

        # xray dials the local spoofer on the config's own port (40443)
        xray = FakeXray.last_instance
        self.assertIsNotNone(xray)
        self.assertEqual(xray.spoof_port, 40443)
        self.assertTrue(xray.started)

        # the spoofer dials the FIXED Cloudflare IP and injects the decoy SNI
        proxy = FakeProxy.last_instance
        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.config["LISTEN_PORT"], 40443)
        self.assertEqual(proxy.config["CONNECT_IP"], "104.19.229.21")
        self.assertEqual(proxy.config["CONNECT_PORT"], 443)
        self.assertEqual(proxy.config["FAKE_SNI"], "www.hcaptcha.com")

        # xray's transport hop is the loopback spoofer, never workers.dev
        self.assertEqual(prof.dial_address, "127.0.0.1")
        self.assertEqual(prof.dial_port, 40443)
        ctrl.stop()
        self.assertTrue(xray.stopped)
        self.assertTrue(proxy.stopped)

    def test_spoof_config_honours_explicit_connect_ip_and_fake_sni(self):
        # An explicit CONNECT_IP / FAKE_SNI in the engine config overrides the
        # profile/spoof defaults (lets the user tune the two knobs).
        ctrl = EngineController({
            "connection_mode": "Tunnel",
            "CONNECT_IP": "104.16.0.1",
            "FAKE_SNI": "www.bing.com",
        })
        ctrl.set_profile(self._spoof_profile())
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        proxy = FakeProxy.last_instance
        self.assertEqual(proxy.config["CONNECT_IP"], "104.16.0.1")
        self.assertEqual(proxy.config["FAKE_SNI"], "www.bing.com")
        ctrl.stop()

    def test_count_callback_forwarded(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        counts = []
        ctrl.on_count = lambda a, t: counts.append((a, t))
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        # simulate the proxy reporting a new connection
        FakeProxy.last_instance.on_connection_count_change(3, 10)
        self.assertIn((3, 10), counts)
        ctrl.stop()

    def test_strategy_callback_fires_and_active_strategy_consistent(self):
        # SNI Only, no auto-prober → the configured method is what's in force,
        # and it must be reported to the UI exactly once on start.
        ctrl = EngineController(
            {"connection_mode": "SNI Only", "bypass_method": "fake_disorder"})
        seen = []
        ctrl.on_strategy = lambda m: seen.append(m)
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        # the dashboard (via on_strategy) and engine.active_strategy must agree
        self.assertEqual(seen, ["fake_disorder"])
        self.assertEqual(ctrl.active_strategy, "fake_disorder")
        ctrl.stop()
        # cleared on stop so a stale strategy never lingers in the UI
        self.assertIsNone(ctrl.active_strategy)

    def test_traffic_callback_forwarded_and_reset_on_stop(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        traffic = []
        ctrl.on_traffic = lambda u, d, ub, db: traffic.append((u, d, ub, db))
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        # simulate the proxy reporting live throughput
        FakeProxy.last_instance.on_traffic(1024, 4096, 512.0, 2048.0)
        self.assertIn((1024, 4096, 512.0, 2048.0), traffic)
        ctrl.stop()
        # stop emits a zeroing event so the graph returns to baseline
        self.assertEqual(traffic[-1], (0, 0, 0.0, 0.0))

    def test_double_start_is_noop(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        first = FakeProxy.last_instance
        ctrl.start()  # should not spin up a second proxy
        time.sleep(0.1)
        self.assertIs(FakeProxy.last_instance, first)
        ctrl.stop()

    def test_stop_when_idle_is_safe(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl.stop()  # must not raise
        self.assertEqual(ctrl.status, STATUS_IDLE)

    # -- start/stop epoch guard (rapid config-switch race) ---------------
    #
    # User report: "I switch the active config while connected (it auto-resets)
    # and sometimes it breaks / gets stuck / doesn't reconnect." Root cause: a
    # slow, in-flight start() from the OLD config could finish AFTER the user
    # switched/stopped and falsely commit ACTIVE over a half-torn-down session.
    # The epoch guard makes a superseded start refuse to commit.

    def test_commit_active_only_when_epoch_current(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl._start_epoch = 5
        # the current epoch commits ACTIVE
        self.assertTrue(ctrl._commit_active(5))
        self.assertEqual(ctrl.status, STATUS_ACTIVE)
        # a stale epoch (older start) must NOT commit
        ctrl._set_status(STATUS_IDLE)
        self.assertFalse(ctrl._commit_active(4))
        self.assertEqual(ctrl.status, STATUS_IDLE)

    def test_stop_bumps_epoch_so_inflight_start_is_superseded(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        # a start() captures epoch e1; the engine is told to start
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        e_before = ctrl._start_epoch
        # stop() must move the epoch on so any racing _do_start can't re-commit
        ctrl.stop()
        self.assertGreater(ctrl._start_epoch, e_before)
        # a stale start (the pre-stop epoch) can no longer commit ACTIVE
        self.assertFalse(ctrl._commit_active(e_before))
        self.assertEqual(ctrl.status, STATUS_IDLE)

    def test_superseded_start_does_not_resurrect_active(self):
        """A new start()/stop() between an old start's bind and its ACTIVE
        commit invalidates the old commit — no false green over a dead session.
        """
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        old_epoch = ctrl._start_epoch
        # simulate the user switching config: stop then a fresh start
        ctrl.stop()
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        new_epoch = ctrl._start_epoch
        self.assertNotEqual(old_epoch, new_epoch)
        # the OLD start trying to commit now is a no-op (epoch moved on)
        before = ctrl.status
        self.assertFalse(ctrl._commit_active(old_epoch))
        self.assertEqual(ctrl.status, before)
        ctrl.stop()

    # -- self-test enforcement (sabotaged config that fakes data flow) ----
    #
    # User report: a deliberately-broken spoof config still pinged "connected"
    # and the time/usage tab showed a few KB flowing (xray retry overhead to the
    # dead backend), yet NO site loaded. The self-test now DEMOTES such a session
    # to ERROR so a config that only reaches the CDN edge can't masquerade as
    # working while leaking bytes.

    def test_demote_failed_selftest_flips_active_to_error(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        epoch = ctrl._start_epoch
        ctrl._demote_failed_selftest(epoch, "فقط لبهٔ CDN پاسخ داد")
        self.assertEqual(ctrl.status, STATUS_ERROR)

    def test_demote_respects_enforce_flag_off(self):
        ctrl = EngineController({"connection_mode": "SNI Only",
                                 "self_test_enforce": False})
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        epoch = ctrl._start_epoch
        ctrl._demote_failed_selftest(epoch, "edge only")
        # enforcement disabled → stays ACTIVE (log-only legacy behaviour)
        self.assertEqual(ctrl.status, STATUS_ACTIVE)
        ctrl.stop()

    def test_demote_ignored_for_stale_epoch(self):
        ctrl = EngineController({"connection_mode": "SNI Only"})
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        stale = ctrl._start_epoch - 1   # a superseded (older) self-test
        ctrl._demote_failed_selftest(stale, "edge only")
        # epoch moved on → must not touch the current ACTIVE session
        self.assertEqual(ctrl.status, STATUS_ACTIVE)
        ctrl.stop()

    # -- auto-prober integration -----------------------------------------

    def test_auto_prober_picks_winner_and_sets_bypass_method(self):
        import core.prober as prober_mod
        from core.prober import ProbeResult, OK, RST

        # fake probe: only "fake_disorder" succeeds, everything else RSTs
        def fake_probe(candidate, host, port, timeout):
            if candidate.strategy == "fake_disorder":
                return ProbeResult(candidate, OK, latency_ms=5.0)
            return ProbeResult(candidate, RST)

        saved = prober_mod.tcp_probe
        prober_mod.tcp_probe = fake_probe
        try:
            ctrl = EngineController({
                "connection_mode": "SNI Only",
                "LISTEN_PORT": 40443, "CONNECT_IP": "9.9.9.9", "CONNECT_PORT": 443,
                "auto_prober": True, "bypass_method": "wrong_seq",
            })
            seen = []
            ctrl.on_strategy = lambda m: seen.append(m)
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
            # engine must have locked onto the only successful candidate
            self.assertEqual(FakeProxy.last_instance.bypass_method, "fake_disorder")
            # consistency: the strategy reported to the UI, engine.active_strategy,
            # and the diagnostics snapshot must all agree on the prober's winner
            # (this is the bug the user hit: dashboard said wrong_seq while
            #  diagnostics said the probed winner).
            self.assertEqual(seen, ["fake_disorder"])
            self.assertEqual(ctrl.active_strategy, "fake_disorder")
            self.assertEqual(ctrl.diagnostics().active_strategy, "fake_disorder")
            ctrl.stop()
        finally:
            prober_mod.tcp_probe = saved

    def test_auto_prober_falls_back_when_all_fail(self):
        import core.prober as prober_mod
        from core.prober import ProbeResult, RST

        def all_fail(candidate, host, port, timeout):
            return ProbeResult(candidate, RST)

        saved = prober_mod.tcp_probe
        prober_mod.tcp_probe = all_fail
        try:
            ctrl = EngineController({
                "connection_mode": "SNI Only",
                "LISTEN_PORT": 40443, "CONNECT_IP": "9.9.9.9", "CONNECT_PORT": 443,
                "auto_prober": True, "bypass_method": "multi_fake",
            })
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
            # no candidate succeeded → fall back to the configured method
            self.assertEqual(FakeProxy.last_instance.bypass_method, "multi_fake")
            ctrl.stop()
        finally:
            prober_mod.tcp_probe = saved


    # -- resilience integration ------------------------------------------

    def test_resilience_controller_built_and_handed_to_proxy(self):
        ctrl = EngineController({
            "connection_mode": "SNI Only",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.2.3.4", "CONNECT_PORT": 443,
            "bypass_method": "fake_disorder",
            "resilience": True, "rst_budget": 2,
        })
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        res = ctrl.resilience
        self.assertIsNotNone(res)
        # config knobs propagated
        self.assertEqual(res.rst_budget, 2)
        # the chosen method heads the strategy fallback chain
        self.assertEqual(res.current_strategy, "fake_disorder")
        # the upstream IP heads the IP chain
        self.assertEqual(res.current_ip, "1.2.3.4")
        # other implemented strategies follow as fallbacks
        self.assertGreater(len(res._strategy_chain), 1)
        # and the proxy received it
        self.assertIs(FakeProxy.last_instance.resilience, res)
        ctrl.stop()
        self.assertIsNone(ctrl.resilience)  # cleared on stop

    def test_resilience_can_be_disabled(self):
        ctrl = EngineController({
            "connection_mode": "SNI Only",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.2.3.4", "CONNECT_PORT": 443,
            "resilience": False,
        })
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNone(ctrl.resilience)
        self.assertIsNone(FakeProxy.last_instance.resilience)
        ctrl.stop()

    # -- remote signed strategies integration ----------------------------

    def test_remote_strategies_feed_the_prober(self):
        """A verified remote manifest supplies the prober's candidate set."""
        import core.strategies_remote as sr
        import core.prober as prober_mod
        from core.prober import ProbeResult, OK, RST
        from tests.test_strategies_remote import _sign, _signed_manifest

        seed = b"\x21" * 32
        url = "https://mirror/strategies.json"
        recipes = [
            {"strategy": "fake_disorder", "score": 0.6},
            {"strategy": "fake_disorder", "score": 0.9},
        ]
        raw, sig, pub = _signed_manifest(seed, 3, recipes)
        store = {url: raw, url + ".sig": sig}

        saved_pk = sr.TRUSTED_PUBLIC_KEY_HEX
        saved_fetch = sr.urllib_fetcher
        saved_probe = prober_mod.tcp_probe
        sr.TRUSTED_PUBLIC_KEY_HEX = pub.hex()
        sr.urllib_fetcher = lambda timeout=8.0: (lambda u: store[u])

        # only fake_disorder (the manifest's top recipe) succeeds
        def fake_probe(candidate, host, port, timeout):
            if candidate.strategy == "fake_disorder":
                return ProbeResult(candidate, OK, latency_ms=3.0)
            return ProbeResult(candidate, RST)
        prober_mod.tcp_probe = fake_probe
        try:
            ctrl = EngineController({
                "connection_mode": "SNI Only", "LISTEN_PORT": 40443,
                "CONNECT_IP": "9.9.9.9", "CONNECT_PORT": 443,
                "auto_prober": True, "bypass_method": "wrong_seq",
                "remote_strategies": True, "strategies_mirrors": [url],
            })
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
            self.assertEqual(FakeProxy.last_instance.bypass_method, "fake_disorder")
            ctrl.stop()
        finally:
            sr.TRUSTED_PUBLIC_KEY_HEX = saved_pk
            sr.urllib_fetcher = saved_fetch
            prober_mod.tcp_probe = saved_probe

    def test_remote_strategies_bad_signature_falls_back_to_local(self):
        import core.strategies_remote as sr
        import core.prober as prober_mod
        from core.prober import ProbeResult, OK
        from tests.test_strategies_remote import _signed_manifest

        seed = b"\x22" * 32
        url = "https://mirror/strategies.json"
        raw, _sig, pub = _signed_manifest(seed, 3)
        store = {url: raw, url + ".sig": bytes(64)}  # invalid signature

        saved_pk = sr.TRUSTED_PUBLIC_KEY_HEX
        saved_fetch = sr.urllib_fetcher
        saved_probe = prober_mod.tcp_probe
        sr.TRUSTED_PUBLIC_KEY_HEX = pub.hex()
        sr.urllib_fetcher = lambda timeout=8.0: (lambda u: store[u])
        # every local candidate succeeds; first by prior wins
        prober_mod.tcp_probe = lambda c, h, p, t: ProbeResult(c, OK, latency_ms=1.0)
        try:
            ctrl = EngineController({
                "connection_mode": "SNI Only", "LISTEN_PORT": 40443,
                "CONNECT_IP": "9.9.9.9", "CONNECT_PORT": 443,
                "auto_prober": True, "bypass_method": "wrong_seq",
                "remote_strategies": True, "strategies_mirrors": [url],
            })
            logs = []
            ctrl.on_log = logs.append
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
            # rejected manifest → local registry was used (a known strategy)
            from strategies import REGISTRY
            self.assertIn(FakeProxy.last_instance.bypass_method, REGISTRY)
            ctrl.stop()
        finally:
            sr.TRUSTED_PUBLIC_KEY_HEX = saved_pk
            sr.urllib_fetcher = saved_fetch
            prober_mod.tcp_probe = saved_probe

    def test_allow_lan_binds_xray_on_all_interfaces(self):
        ctrl = EngineController({
            "connection_mode": "Tunnel",  # use_core → xray is built
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.1.1.1", "CONNECT_PORT": 443,
            "bypass_method": "wrong_seq", "allow_lan": True,
        })
        ctrl.set_profile(Profile(protocol="vless", address="srv.example.com",
                                 port=8443, uuid="x"))
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNotNone(FakeXray.last_instance)
        self.assertEqual(FakeXray.last_instance.listen, "0.0.0.0")
        ctrl.stop()

    def test_local_only_binds_loopback(self):
        ctrl = EngineController({
            "connection_mode": "Tunnel",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.1.1.1", "CONNECT_PORT": 443,
            "bypass_method": "wrong_seq", "allow_lan": False,
        })
        ctrl.set_profile(Profile(protocol="vless", address="srv.example.com",
                                 port=8443, uuid="x"))
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertEqual(FakeXray.last_instance.listen, "127.0.0.1")
        ctrl.stop()

    def test_system_proxy_enabled_on_start_disabled_on_stop(self):
        """When system_proxy is on (core mode), the OS proxy is flipped on at
        start (pointed at the local HTTP port) and back off at stop."""
        from core.system_proxy import SystemProxy
        store = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        refreshes = {"n": 0}

        def _writer(values):
            store.update(values)

        def _refresher():
            refreshes["n"] += 1

        def _make_sp():
            return SystemProxy(writer=_writer, refresher=_refresher,
                               reader=lambda: dict(store))

        ctrl = EngineController({
            "connection_mode": "Tunnel",  # use_core → eligible for system proxy
            "LISTEN_PORT": 40443, "http_port": 10809,
            "system_proxy": True,
        })
        ctrl._system_proxy_factory = _make_sp
        ctrl.set_profile(self._profile())
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        # OS proxy turned ON pointing at the local HTTP port
        self.assertEqual(store["ProxyEnable"], 1)
        self.assertEqual(store["ProxyServer"], "127.0.0.1:10809")
        self.assertIsNotNone(ctrl._system_proxy)
        ctrl.stop()
        # …and turned back OFF on stop
        self.assertEqual(store["ProxyEnable"], 0)
        self.assertIsNone(ctrl._system_proxy)

    def test_system_proxy_skipped_in_sni_only_mode(self):
        """System proxy needs a real local proxy (xray); SNI Only has none, so
        the toggle is ignored and the OS proxy is never touched."""
        from core.system_proxy import SystemProxy
        store = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}

        def _make_sp():
            return SystemProxy(writer=lambda v: store.update(v),
                               refresher=lambda: None,
                               reader=lambda: dict(store))

        ctrl = EngineController({
            "connection_mode": "SNI Only", "LISTEN_PORT": 40443,
            "CONNECT_IP": "1.2.3.4", "CONNECT_PORT": 443,
            "system_proxy": True,
        })
        ctrl._system_proxy_factory = _make_sp
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNone(ctrl._system_proxy)
        self.assertEqual(store["ProxyEnable"], 0)  # never touched
        ctrl.stop()

    def test_system_proxy_off_by_default(self):
        """With the toggle off, even in core mode the OS proxy stays untouched."""
        ctrl = EngineController({
            "connection_mode": "Tunnel", "LISTEN_PORT": 40443,
        })
        sentinel = {"called": False}
        ctrl._system_proxy_factory = lambda: sentinel.__setitem__("called", True)
        ctrl.set_profile(self._profile())
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNone(ctrl._system_proxy)
        self.assertFalse(sentinel["called"])
        ctrl.stop()

    def test_resilience_chain_includes_extra_ips(self):
        ctrl = EngineController({
            "connection_mode": "SNI Only",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.1.1.1", "CONNECT_PORT": 443,
            "bypass_method": "wrong_seq", "resilience": True,
            "CONNECT_IP_ALTS": ["8.8.8.8", "9.9.9.9"],
        })
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        res = ctrl.resilience
        self.assertEqual(res.current_ip, "1.1.1.1")
        self.assertEqual(res._ip_chain, ["1.1.1.1", "8.8.8.8", "9.9.9.9"])
        ctrl.stop()


class EnginePingTest(unittest.TestCase):
    """Engine-level ping / strategy-test (core.ping) with injected network."""

    def setUp(self):
        self._restore = _install_fakes()

    def tearDown(self):
        self._restore()

    def _profile(self, address="srv.example.com", port=443, remark=""):
        return Profile(protocol="vless", address=address, port=port,
                       remark=remark, uuid="x")

    def test_ping_profiles_ranked_via_engine(self):
        import core.ping as ping_mod
        saved = ping_mod.tcp_latency
        ping_mod.tcp_latency = lambda h, p, t: {"fast": 10.0, "slow": 200.0}.get(h)
        try:
            ctrl = EngineController({"ping_samples": 1,
                                     "ping_measure_download": False})
            results = ctrl.ping_profiles([
                self._profile("slow", remark="Slow"),
                self._profile("fast", remark="Fast"),
            ])
            self.assertEqual([r.host for r in results], ["fast", "slow"])
            self.assertEqual(results[0].label, "Fast")
        finally:
            ping_mod.tcp_latency = saved

    def test_ping_single_profile_failsoft(self):
        import core.ping as ping_mod
        saved = ping_mod.tcp_latency
        ping_mod.tcp_latency = lambda h, p, t: 42.0
        try:
            ctrl = EngineController({"ping_samples": 2,
                                     "ping_measure_download": False})
            res = ctrl.ping_profile(self._profile("h"))
            self.assertIsNotNone(res)
            self.assertTrue(res.reachable)
            self.assertAlmostEqual(res.best_ms, 42.0)
        finally:
            ping_mod.tcp_latency = saved

    def test_measure_profile_delay_failsoft_when_no_core(self):
        """measure_profile_delay must never raise into the UI. When xray.exe is
        unavailable (or any error), it returns (False, None, detail) so the
        inline ping just shows an honest red instead of crashing."""
        ctrl = EngineController({})
        ok, ms, detail = ctrl.measure_profile_delay(
            self._profile("h"), timeout=1.0)
        self.assertFalse(ok)
        self.assertIsNone(ms)
        self.assertIsInstance(detail, str)

    def test_measure_profile_download_failsoft_when_no_core(self):
        """The new download speed test must also never raise; with no xray it
        returns an honest (False, None, detail)."""
        ctrl = EngineController({})
        ok, mbps, detail = ctrl.measure_profile_download(
            self._profile("h"), duration=0.2)
        self.assertFalse(ok)
        self.assertIsNone(mbps)
        self.assertIsInstance(detail, str)

    def test_measure_profile_download_reports_throughput(self):
        """When a temporary core is up and bytes stream through it, the helper
        returns megabits/second computed from bytes/elapsed."""
        ctrl = EngineController({})

        # fake _spawn_measure_core to hand back a fake opener that streams a
        # fixed payload, so we exercise the throughput math deterministically.
        class _Resp:
            def __init__(self):
                self._chunks = [b"x" * 65536] * 8 + [b""]  # ~512 KiB then EOF
                self._i = 0
            def getcode(self):
                return 200
            def read(self, _n=0):
                c = self._chunks[self._i] if self._i < len(self._chunks) else b""
                self._i += 1
                return c
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, _req, timeout=None):
                return _Resp()

        saved = EngineController._spawn_measure_core
        EngineController._spawn_measure_core = (
            lambda self, profile, **k: (_Opener(), lambda: None, None))
        try:
            ok, mbps, detail = ctrl.measure_profile_download(
                self._profile("h"), duration=1.0)
            self.assertTrue(ok)
            self.assertIsNotNone(mbps)
            self.assertGreater(mbps, 0.0)
        finally:
            EngineController._spawn_measure_core = saved

    def test_measure_profile_download_red_when_no_bytes(self):
        """If the stream yields nothing through every URL, report honest red."""
        ctrl = EngineController({})

        class _Opener:
            def open(self, _req, timeout=None):
                raise OSError("connection refused")

        saved = EngineController._spawn_measure_core
        EngineController._spawn_measure_core = (
            lambda self, profile, **k: (_Opener(), lambda: None, None))
        try:
            ok, mbps, detail = ctrl.measure_profile_download(
                self._profile("h"), duration=0.5)
            self.assertFalse(ok)
            self.assertIsNone(mbps)
        finally:
            EngineController._spawn_measure_core = saved

    def test_live_proxy_download_red_when_tunnel_not_active(self):
        """A download test on a config that isn't connected must fail soft
        (the live tunnel only exists for the ACTIVE config)."""
        ctrl = EngineController({})  # status starts IDLE
        ok, mbps, detail = ctrl.live_proxy_download(duration=0.2)
        self.assertFalse(ok)
        self.assertIsNone(mbps)
        self.assertIsInstance(detail, str)

    def test_live_proxy_download_reports_throughput_through_live_tunnel(self):
        """When the tunnel is active and bytes stream through the live http
        inbound, live_proxy_download returns Mbps — NOT a latency ms. This is
        the fix for "download test on the active config gave ms" (issue #3).

        The fake opener also absorbs the throwaway ``_warm_tunnel`` GET, so we
        exercise the real warm-then-stream path without touching the network.
        """
        ctrl = EngineController({})
        ctrl._status = STATUS_ACTIVE
        # pretend the live http inbound is bound to a port
        ctrl._effective_ports = lambda: (10808, 10809)

        class _Resp:
            def __init__(self):
                self._chunks = [b"x" * 65536] * 8 + [b""]  # ~512 KiB then EOF
                self._i = 0
            def getcode(self):
                return 200
            def read(self, _n=0):
                c = self._chunks[self._i] if self._i < len(self._chunks) else b""
                self._i += 1
                return c
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, _req, timeout=None):
                return _Resp()

        import urllib.request
        saved_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: _Opener()
        try:
            ok, mbps, detail = ctrl.live_proxy_download(duration=1.0)
            self.assertTrue(ok)
            self.assertIsNotNone(mbps)
            self.assertGreater(mbps, 0.0)
        finally:
            urllib.request.build_opener = saved_build

    def test_live_proxy_download_red_when_no_bytes(self):
        """Active tunnel but every URL refuses → honest red, never a ms."""
        ctrl = EngineController({})
        ctrl._status = STATUS_ACTIVE
        ctrl._effective_ports = lambda: (10808, 10809)

        class _Opener:
            def open(self, _req, timeout=None):
                raise OSError("connection refused")

        import urllib.request
        saved_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: _Opener()
        try:
            ok, mbps, detail = ctrl.live_proxy_download(duration=0.5)
            self.assertFalse(ok)
            self.assertIsNone(mbps)
            self.assertIsInstance(detail, str)
        finally:
            urllib.request.build_opener = saved_build

    def test_wait_proxy_ready_times_out_on_dead_port(self):
        """The readiness poll (race fix) returns False quickly for a dead port
        instead of blocking, so a broken core can't hang the batch."""
        import time
        ctrl = EngineController({})
        t0 = time.time()
        # port 1 is privileged/unbound in the sandbox → never accepts
        ready = ctrl._wait_proxy_ready(1, timeout=0.6)
        self.assertFalse(ready)
        self.assertLess(time.time() - t0, 2.0)

    def test_measure_profile_delay_returns_real_delay_through_core(self):
        """When the temporary core comes up and the request succeeds,
        measure_profile_delay returns the real round-trip delay — mirroring how
        v2rayNG times a request through the config's own outbound. An ordinary
        (non-spoof) config goes through the stable v2rayNG-style path."""
        ctrl = EngineController({})

        # fake the (now shared) core-spawn helper to hand back a dummy opener;
        # ordinary config → stable v2rayNG-style delay path is used.
        saved_spawn = EngineController._spawn_measure_core
        EngineController._spawn_measure_core = (
            lambda self, profile, **k: (object(), lambda: None, None))
        saved_probe = EngineController._v2ray_style_delay
        EngineController._v2ray_style_delay = (
            lambda self, opener, per_timeout, attempts=2, **_k:
            (True, 64.0, "ok"))
        try:
            ok, ms, detail = ctrl.measure_profile_delay(
                self._profile("h"), timeout=2.0)
            self.assertTrue(ok)
            self.assertAlmostEqual(ms, 64.0)
        finally:
            EngineController._spawn_measure_core = saved_spawn
            EngineController._v2ray_style_delay = saved_probe

    def test_v2ray_style_delay_keeps_best_of_attempts(self):
        """The v2rayNG algorithm fires the fixed 204 URL twice and keeps the
        MINIMUM round-trip — so one slow warm-up packet never flaps a working
        config to red. Success requires only HTTP 200/204 (NO body marker),
        which is the fix for working-but-slow configs falsely going red."""
        ctrl = EngineController({})

        # fake opener that returns 204 with rising latency; best-of must win.
        timings = iter([0.300, 0.040])  # first slow, second fast (seconds)

        class _Resp:
            def __init__(self):
                self._code = 204
            def getcode(self):
                return self._code
            def read(self, *_a):
                return b""           # empty body — must still count as success
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, _req, timeout=None):
                # simulate the elapsed time by advancing a fake clock
                import core.engine as _m
                _m.time.sleep(0)  # no real sleep
                return _Resp()

        # monkeypatch time.time inside the helper to feed deterministic gaps
        import core.engine as eng_mod
        real_time = eng_mod.time.time
        clock = [0.0]
        seq = [0.0, 0.300, 0.300, 0.340]  # t0,t1 (attempt1), t0,t1 (attempt2)
        it = iter(seq)

        def fake_time():
            try:
                return next(it)
            except StopIteration:
                return 0.340
        eng_mod.time.time = fake_time
        try:
            ok, ms, detail = ctrl._v2ray_style_delay(
                _Opener(), per_timeout=6.0, attempts=2)
            self.assertTrue(ok)
            # best-of: min(300ms, 40ms) == 40ms
            self.assertAlmostEqual(ms, 40.0, delta=0.5)
        finally:
            eng_mod.time.time = real_time

    def test_v2ray_style_delay_red_when_all_attempts_fail(self):
        """If every attempt to both 204 URLs errors out, the helper reports an
        honest red (ok=False, ms=None) — broken configs stay red."""
        ctrl = EngineController({})

        class _Opener:
            def open(self, _req, timeout=None):
                raise OSError("connection refused")

        ok, ms, detail = ctrl._v2ray_style_delay(
            _Opener(), per_timeout=1.0, attempts=2)
        self.assertFalse(ok)
        self.assertIsNone(ms)
        self.assertIsInstance(detail, str)

    def test_measure_profile_delay_hard_deadline_stops_a_dead_config(self):
        """A broken config used to grind through every fallback endpoint ×
        attempt (each with a multi-second timeout) for up to a minute. With a
        hard wall-clock ``deadline`` the whole measurement must return in
        roughly ``deadline`` seconds — not 30-60 s. We simulate a route that
        hangs each request until its per-request timeout fires, and assert the
        TOTAL wall-clock is bounded by the deadline (with slack), proving we
        stop spending fallback attempts past the budget.
        """
        import time as _t
        ctrl = EngineController({})

        saved_spawn = EngineController._spawn_measure_core
        EngineController._spawn_measure_core = (
            lambda self, profile, **k: (_HangingOpener(), lambda: None, None))
        try:
            t0 = _t.time()
            ok, ms, detail = ctrl.measure_profile_delay(
                self._profile("dead"), timeout=15.0, deadline=2.0)
            elapsed = _t.time() - t0
        finally:
            EngineController._spawn_measure_core = saved_spawn

        self.assertFalse(ok)            # dead config stays red
        self.assertIsNone(ms)
        # the whole thing must finish near the 2 s budget, NOT minutes. Generous
        # slack (one in-flight request may run to its own timeout) but still far
        # below the old worst case.
        self.assertLess(elapsed, 6.0, f"deadline not honored: {elapsed:.1f}s")

    def test_measure_profile_download_hard_deadline_stops_a_dead_config(self):
        """Same guarantee for the download test: a route that never streams
        bytes must be abandoned at the wall-clock ``deadline`` instead of
        marching through 5 URLs × 2 attempts × a ~13 s timeout (~2 min)."""
        import time as _t
        ctrl = EngineController({})

        saved_spawn = EngineController._spawn_measure_core
        EngineController._spawn_measure_core = (
            lambda self, profile, **k: (_HangingOpener(), lambda: None, None))
        # restore the ORIGINAL staticmethod descriptor (not the bound function)
        # so a later test relying on the real _warm_tunnel isn't broken.
        saved_warm = EngineController.__dict__["_warm_tunnel"]
        EngineController._warm_tunnel = staticmethod(lambda *_a, **_k: None)
        try:
            t0 = _t.time()
            ok, mbps, detail = ctrl.measure_profile_download(
                self._profile("dead"), duration=6.0, deadline=2.0)
            elapsed = _t.time() - t0
        finally:
            EngineController._spawn_measure_core = saved_spawn
            EngineController._warm_tunnel = saved_warm

        self.assertFalse(ok)
        self.assertIsNone(mbps)
        self.assertLess(elapsed, 6.0, f"deadline not honored: {elapsed:.1f}s")

    def test_v2ray_style_delay_respects_end_by(self):
        """The stable delay helper must stop firing attempts once the wall-clock
        ``end_by`` has passed — a working config returns immediately, a dead one
        is not retried into oblivion."""
        import time as _t
        ctrl = EngineController({})

        calls = {"n": 0}

        class _Opener:
            def open(self, _req, timeout=None):
                calls["n"] += 1
                raise OSError("refused")

        # end_by already in the past → no attempt should even start.
        ok, ms, _d = ctrl._v2ray_style_delay(
            _Opener(), per_timeout=5.0, attempts=2, end_by=_t.time() - 1.0)
        self.assertFalse(ok)
        self.assertEqual(calls["n"], 0, "fired requests past the deadline")

    def _spoof_profile(self):
        """A spoof profile: its server address is a loopback port (our SNI
        spoofer), which is exactly what ``Profile.is_spoof_config`` keys on."""
        p = self._profile("127.0.0.1", port=40443)
        assert p.is_spoof_config, "fixture must be recognised as a spoof config"
        return p

    def test_spoof_config_gets_longer_budget_than_ordinary(self):
        """A spoof config must be given a noticeably LONGER wall-clock budget
        than an ordinary one — it's slow to ESTABLISH and a too-tight cap
        false-reds working servers. We assert the per-attempt 'wall' the engine
        chooses for a spoof config exceeds the ordinary one (v2rayN gives spoof/
        relay paths a 1 s warm-up + a 10 s ping budget; we mirror the spirit).
        """
        ctrl = EngineController({})
        seen = []

        def _capture(self, profile, *, timeout, wall, is_spoof):
            seen.append((is_spoof, wall))
            return (False, None, "captured")

        saved = EngineController._measure_profile_delay_once
        EngineController._measure_profile_delay_once = _capture
        try:
            ctrl.measure_profile_delay(self._profile("plain"))
            ctrl.measure_profile_delay(self._spoof_profile())
        finally:
            EngineController._measure_profile_delay_once = saved

        ordinary_wall = next(w for sp, w in seen if not sp)
        spoof_wall = next(w for sp, w in seen if sp)
        self.assertGreater(spoof_wall, ordinary_wall,
                           "spoof must get a longer budget to establish")
        self.assertGreaterEqual(spoof_wall, 10.0,
                                "spoof budget should be roomy (v2rayN ~10 s+)")

    def test_spoof_config_gets_second_chance_before_red(self):
        """v2rayN re-tests whatever fails the first pass (RunRealPingBatchAsync
        → lstFailed). We mirror that: a spoof config that FAILS its first
        attempt but SUCCEEDS on the second must end up green — a slow wake-up
        no longer false-reds a working spoof server."""
        ctrl = EngineController({})
        calls = {"n": 0}

        def _flaky_once(self, profile, *, timeout, wall, is_spoof):
            calls["n"] += 1
            if calls["n"] == 1:
                return (False, None, "cold start")     # first pass: not ready
            return (True, 120.0, "verified on retry")  # second pass: works

        saved = EngineController._measure_profile_delay_once
        EngineController._measure_profile_delay_once = _flaky_once
        try:
            ok, ms, detail = ctrl.measure_profile_delay(self._spoof_profile())
        finally:
            EngineController._measure_profile_delay_once = saved

        self.assertTrue(ok, "spoof must get a second chance, not instant red")
        self.assertEqual(calls["n"], 2, "exactly one automatic retry expected")
        self.assertAlmostEqual(ms, 120.0)

    def test_ordinary_config_is_judged_on_a_single_pass(self):
        """An ordinary (non-spoof) config is fast and deterministic — it gets
        exactly ONE pass, no extra retry (that's reserved for slow spoof
        configs), so a healthy config still resolves quickly."""
        ctrl = EngineController({})
        calls = {"n": 0}

        def _once(self, profile, *, timeout, wall, is_spoof):
            calls["n"] += 1
            return (False, None, "dead")

        saved = EngineController._measure_profile_delay_once
        EngineController._measure_profile_delay_once = _once
        try:
            ctrl.measure_profile_delay(self._profile("plain"))
        finally:
            EngineController._measure_profile_delay_once = saved
        self.assertEqual(calls["n"], 1, "ordinary config must not be retried")

    def test_spoof_download_gets_second_chance(self):
        """Same automatic-retry guarantee for the download test: a spoof config
        that fails its first download attempt but streams on the second ends up
        green (mirrors v2rayN's failed-part retest)."""
        ctrl = EngineController({})
        calls = {"n": 0}

        def _flaky(self, profile, *, duration, max_bytes, wall, is_spoof):
            calls["n"] += 1
            if calls["n"] == 1:
                return (False, None, "cold")
            return (True, 12.5, "ok on retry")

        saved = EngineController._measure_profile_download_once
        EngineController._measure_profile_download_once = _flaky
        try:
            ok, mbps, detail = ctrl.measure_profile_download(
                self._spoof_profile())
        finally:
            EngineController._measure_profile_download_once = saved
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 2)
        self.assertAlmostEqual(mbps, 12.5)

    def test_probe_strategies_via_engine_picks_winner(self):
        import core.prober as prober_mod
        from core.prober import ProbeResult, OK, RST
        saved = prober_mod.tcp_probe
        def fake(cand, host, port, timeout):
            if cand.strategy == "fake_disorder":
                return ProbeResult(cand, OK, latency_ms=15.0)
            if cand.strategy == "wrong_seq":
                return ProbeResult(cand, OK, latency_ms=90.0)
            return ProbeResult(cand, RST)
        prober_mod.tcp_probe = fake
        try:
            ctrl = EngineController({})
            report = ctrl.probe_strategies_for(self._profile("h"))
            self.assertTrue(report.any_connected)
            self.assertEqual(report.best.strategy, "fake_disorder")
        finally:
            prober_mod.tcp_probe = saved

    def test_probe_strategies_pinned_single(self):
        import core.prober as prober_mod
        from core.prober import ProbeResult, OK
        saved = prober_mod.tcp_probe
        prober_mod.tcp_probe = lambda c, h, p, t: ProbeResult(c, OK, latency_ms=5.0)
        try:
            ctrl = EngineController({"ping_strategy": "multi_fake"})
            report = ctrl.probe_strategies_for(self._profile("h"))
            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.best.strategy, "multi_fake")
        finally:
            prober_mod.tcp_probe = saved


class EnginePoolIntegrationTest(unittest.TestCase):
    """The route-pool wiring on EngineController (7.7).

    Exercises ``_build_pool`` / ``_stop_pool`` directly so we never need the
    Windows-only ``main`` / ``pydivert`` import path. The health-loop thread is
    a daemon; we stop it immediately so no real probing runs.
    """

    def test_single_target_builds_no_pool(self):
        c = EngineController({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        c._build_pool("1.1.1.1", 443, "a.com")
        try:
            self.assertIsNone(c.conn_manager)
        finally:
            c._stop_pool()

    def test_multi_target_builds_pool_and_starts_loop(self):
        c = EngineController({
            "CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com", "b.com"],
        })
        c._build_pool("1.1.1.1", 443, "a.com")
        try:
            self.assertIsNotNone(c.conn_manager)
            # 2 IPs × 2 SNIs = 4 routes
            self.assertEqual(len(c.conn_manager.explorer.stats), 4)
            # the tracker is wired in for per-IP failover
            self.assertTrue(hasattr(c.conn_manager, "tracker"))
        finally:
            c._stop_pool()
        self.assertIsNone(c.conn_manager)

    def test_folds_single_target_into_lists(self):
        # only the legacy singular keys set, plus one extra SNI list entry →
        # the resolved single target must be folded in as the fallback.
        c = EngineController({"FAKE_SNIS": ["a.com", "b.com"]})
        c._build_pool("9.9.9.9", 8443, "a.com")
        try:
            self.assertIsNotNone(c.conn_manager)
            ips = {ps.ip for ps in c.conn_manager.explorer.all_stats()}
            self.assertIn("9.9.9.9", ips)
            self.assertEqual(c.conn_manager.explorer.port, 8443)
        finally:
            c._stop_pool()

    def test_stop_pool_is_idempotent(self):
        c = EngineController({})
        c._stop_pool()        # nothing built yet — must be safe
        c._stop_pool()
        self.assertIsNone(c.conn_manager)

    def test_build_pool_never_raises_on_bad_config(self):
        c = EngineController({"CONNECT_IPS": None, "FAKE_SNIS": None})
        # must degrade to single-target (None) without raising
        c._build_pool("", 443, "")
        self.assertIsNone(c.conn_manager)


if __name__ == "__main__":
    unittest.main()
