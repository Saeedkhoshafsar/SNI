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
    EngineController, STATUS_IDLE, STATUS_ACTIVE, STATUS_CONNECTING)
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
        self.started = False
        self.stopped = False
        FakeProxy.last_instance = self

    def start(self):
        self.started = True
        if self.on_log:
            self.on_log("fake proxy started")
        if self.on_status_change:
            self.on_status_change(True)

    def stop(self):
        self.stopped = True


class FakeXray:
    last_instance = None

    def __init__(self, profile, socks_port=10808, http_port=10809,
                 spoof_port=None, gaming_mode=False):
        self.profile = profile
        self.socks_port = socks_port
        self.http_port = http_port
        self.spoof_port = spoof_port
        self.gaming_mode = gaming_mode
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

    def restore():
        if saved_main is not None:
            sys.modules["main"] = saved_main
        else:
            sys.modules.pop("main", None)
        xm.XrayManager = saved_xray
        xm.find_free_port = saved_find

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
        ctrl = EngineController({"connection_mode": "SNI Only"})
        self.assertFalse(ctrl.uses_core)
        ctrl.set_profile(self._profile())
        self.assertFalse(ctrl.uses_core)  # mode still SNI Only
        ctrl.update_config({"connection_mode": "SNI + Warp"})
        self.assertTrue(ctrl.uses_core)

    def test_sni_only_starts_proxy_no_xray(self):
        ctrl = EngineController({
            "connection_mode": "SNI Only",
            "LISTEN_PORT": 40443, "CONNECT_IP": "1.2.3.4", "CONNECT_PORT": 443,
        })
        logs = []
        ctrl.on_log = logs.append
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        self.assertIsNotNone(FakeProxy.last_instance)
        self.assertTrue(FakeProxy.last_instance.started)
        self.assertIsNone(FakeXray.last_instance)  # no core in SNI Only
        self.assertEqual(FakeProxy.last_instance.config["CONNECT_IP"], "1.2.3.4")
        ctrl.stop()
        self.assertEqual(ctrl.status, STATUS_IDLE)

    def test_core_mode_chains_spoofer_under_xray(self):
        ctrl = EngineController({
            "connection_mode": "SNI + Warp", "LISTEN_PORT": 40443,
        })
        ctrl.set_profile(self._profile())
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))

        proxy = FakeProxy.last_instance
        xray = FakeXray.last_instance
        self.assertIsNotNone(proxy)
        self.assertIsNotNone(xray)
        # spoofer forwards to the REAL server...
        self.assertEqual(proxy.config["CONNECT_IP"], "real.example.com")
        self.assertEqual(proxy.config["CONNECT_PORT"], 8443)
        # ...and xray's outbound is pointed at the local spoofer port
        self.assertEqual(xray.spoof_port, 40443)
        self.assertEqual(proxy.config["LISTEN_PORT"], 40443)
        ctrl.stop()
        self.assertTrue(proxy.stopped)
        self.assertTrue(xray.stopped)

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

        # fake probe: only "fake_ttl" succeeds, everything else RSTs
        def fake_probe(candidate, host, port, timeout):
            if candidate.strategy == "fake_ttl":
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
            ctrl.start()
            self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
            # engine must have locked onto the only successful candidate
            self.assertEqual(FakeProxy.last_instance.bypass_method, "fake_ttl")
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
            "bypass_method": "fake_ttl",
            "resilience": True, "rst_budget": 2,
        })
        ctrl.start()
        self.assertTrue(_wait_status(ctrl, STATUS_ACTIVE))
        res = ctrl.resilience
        self.assertIsNotNone(res)
        # config knobs propagated
        self.assertEqual(res.rst_budget, 2)
        # the chosen method heads the strategy fallback chain
        self.assertEqual(res.current_strategy, "fake_ttl")
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
            {"strategy": "fake_ttl", "score": 0.9},
        ]
        raw, sig, pub = _signed_manifest(seed, 3, recipes)
        store = {url: raw, url + ".sig": sig}

        saved_pk = sr.TRUSTED_PUBLIC_KEY_HEX
        saved_fetch = sr.urllib_fetcher
        saved_probe = prober_mod.tcp_probe
        sr.TRUSTED_PUBLIC_KEY_HEX = pub.hex()
        sr.urllib_fetcher = lambda timeout=8.0: (lambda u: store[u])

        # only fake_ttl (the manifest's top recipe) succeeds
        def fake_probe(candidate, host, port, timeout):
            if candidate.strategy == "fake_ttl":
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
            self.assertEqual(FakeProxy.last_instance.bypass_method, "fake_ttl")
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


if __name__ == "__main__":
    unittest.main()
