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
                 spoof_port=None, gaming_mode=False, listen="127.0.0.1"):
        self.profile = profile
        self.socks_port = socks_port
        self.http_port = http_port
        self.spoof_port = spoof_port
        self.gaming_mode = gaming_mode
        self.listen = listen
        self.on_log = None
        self.started = False
        self.stopped = False
        FakeXray.last_instance = self

    @property
    def is_available(self):
        return True

    def start(self):
        self.started = True

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
    EngineController._self_test_chain = lambda self: None

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


if __name__ == "__main__":
    unittest.main()
