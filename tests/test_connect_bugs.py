"""Regression tests for the three "very dangerous" connect/ping bugs.

User report (verbatim):
  * "من کانفیگ های عادی رو تست کردم هم خرابشو هم سالمشو همشون پینگ مثبت دادن
     در حالی که خراب نباید پاسخ مثبت بده" — broken AND healthy configs all
     pinged positive; a broken one must not.
  * "و بعد حتی سالم هارو تست کردم وصل نشدن" — even the healthy ones didn't
     connect.
  * "تنها با sni spoof وصل شدم" — only SNI-spoof connected.

Root causes fixed here:

  A. Tunnel mode falsely reported "✓ اتصال برقرار شد" even when xray died on a
     port-bind conflict (exit code 4294967295), because the engine ignored
     ``XrayManager.start()``'s outcome. ``start()`` now returns a bool and the
     engine honours it → STATUS_ERROR instead of a fake success.

  B. A rapid stop→start (auto-restart on server/strategy change) re-used the
     fixed inbound ports 10808/10809 before the OS released them →
     ``bind: Only one usage of each socket address``. We now wait for the port
     to free on stop, and on start fall back to a free port if it's still held.

  C. The ping/probe of a spoof (CDN-fronted) config validated the **decoy** SNI
     against the connect IP — and any live Cloudflare anycast IP answers TLS +
     /cdn-cgi/trace for *any* SNI, so every config went green. The target now
     carries the config's **real** SNI / Host so a dead Worker honestly fails.
"""
from __future__ import annotations

import socket
import unittest

from core.xray_manager import (
    XrayManager, find_free_port, port_in_use, wait_port_free,
)
from core.ping import target_from_profile
from core.profile import Profile


# ---------------------------------------------------------------------------
#  Bug B — port-conflict helpers
# ---------------------------------------------------------------------------

class PortHelperTest(unittest.TestCase):
    def test_port_in_use_false_for_free_port(self):
        p = find_free_port()
        # nobody is listening on a freshly-picked free port
        self.assertFalse(port_in_use(p))

    def test_port_in_use_true_when_bound(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertTrue(port_in_use(port))
        finally:
            s.close()

    def test_wait_port_free_returns_true_quickly_when_free(self):
        p = find_free_port()
        self.assertTrue(wait_port_free(p, timeout=0.5))

    def test_wait_port_free_times_out_when_held(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            # held for the whole window → returns False (caller picks free port)
            self.assertFalse(wait_port_free(port, timeout=0.3))
        finally:
            s.close()

    def test_invalid_port_never_reports_in_use(self):
        self.assertFalse(port_in_use(0))
        self.assertFalse(port_in_use(99999))


# ---------------------------------------------------------------------------
#  Bug A — start() must return a falsy result when xray can't run
# ---------------------------------------------------------------------------

class XrayStartContractTest(unittest.TestCase):
    def _profile(self) -> Profile:
        return Profile(protocol="vless", address="example.com", port=443,
                       uuid="00000000-0000-0000-0000-000000000000",
                       security="tls", sni="example.com")

    def test_start_returns_false_when_binary_missing(self):
        # force "binary missing" without depending on whether a bundled
        # xray.exe happens to exist in the dev tree.
        class _MissingXray(XrayManager):
            is_available = property(lambda self: False)  # type: ignore

        mgr = _MissingXray(self._profile())
        logs: list[str] = []
        mgr.on_log = logs.append
        self.assertFalse(mgr.is_available)
        self.assertIs(mgr.start(), False)
        self.assertTrue(any("not found" in m or "یافت نشد" in m for m in logs))

    def test_start_returns_false_on_invalid_profile(self):
        bad = Profile(protocol="vless", address="", port=443)  # no addr/uuid

        # subclass so we can fake an available binary without mutating the real
        # XrayManager class (which other tests share in the same process).
        class _AvailableXray(XrayManager):
            is_available = property(lambda self: True)  # type: ignore

        mgr = _AvailableXray(bad)
        logs: list[str] = []
        mgr.on_log = logs.append
        self.assertIs(mgr.start(), False)
        self.assertTrue(any("نامعتبر" in m for m in logs))


# ---------------------------------------------------------------------------
#  Bug C — spoof config ping validates the REAL config, not the decoy edge
# ---------------------------------------------------------------------------

class SpoofPingHonestyTest(unittest.TestCase):
    def _spoof_profile(self, **kw) -> Profile:
        return Profile(
            protocol="vless", address="127.0.0.1", port=40443,
            uuid="84524180-c2d5-4bc1-83bb-c36f22d69a3b",
            security="tls", transport="xhttp",
            sni=kw.get("sni", "lucky-union-b89c.hamijafayi.workers.dev"),
            host=kw.get("host", "lucky-union-b89c.hamijafayi.workers.dev"),
            path=kw.get("path", "/vless-xhttp"))

    def test_spoof_target_uses_real_sni_not_decoy(self):
        prof = self._spoof_profile()
        self.assertTrue(prof.is_spoof_config)
        tgt = target_from_profile(prof)
        # connects to the CDN edge the spoofer dials …
        self.assertEqual(tgt.host, prof.spoof_connect_ip)
        self.assertEqual(tgt.port, prof.spoof_connect_port)
        # … but validates with the REAL SNI / Host (the actual Worker route),
        # NOT the DPI decoy — this is what makes a broken config fail honestly.
        self.assertEqual(tgt.server_name, prof.sni)
        self.assertEqual(tgt.host_header, prof.host)
        self.assertNotEqual(tgt.server_name, prof.spoof_fake_sni)
        self.assertTrue(tgt.tls)

    def test_real_path_is_preserved_for_spoof_config(self):
        prof = self._spoof_profile(path="/vless-xhttp")
        tgt = target_from_profile(prof)
        # the real transport path must reach the edge check so an unrouteable
        # path fails; we don't silently rewrite it to "/".
        self.assertEqual(tgt.path, "/vless-xhttp")

    def test_plain_config_pings_its_own_address(self):
        prof = Profile(protocol="vless", address="real.example.com", port=8443,
                       uuid="x", security="tls", sni="real.example.com")
        self.assertFalse(prof.is_spoof_config)
        tgt = target_from_profile(prof)
        self.assertEqual((tgt.host, tgt.port), ("real.example.com", 8443))
        self.assertEqual(tgt.server_name, "real.example.com")


# ---------------------------------------------------------------------------
#  Bug D — system proxy / self-test must follow the REAL bound port
# ---------------------------------------------------------------------------

class EffectivePortsTest(unittest.TestCase):
    """When xray switches off the configured port (conflict), the system proxy
    and self-test must point at the REAL bound port — not the dead default.

    This was the cause of "ordinary configs don't connect" in the latest log:
    xray moved to 61483 but the engine enabled the OS proxy on 10809 (dead) and
    self-tested 10809 → WinError 10053 / SSL UNEXPECTED_EOF, even though the
    tunnel was healthy.
    """

    def test_effective_ports_prefers_real_bound_ports(self):
        from core.engine import EngineController

        class _XrayStub:
            socks_port = 61477
            http_port = 61483

        ctrl = EngineController({"socks_port": 10808, "http_port": 10809})
        ctrl._xray = _XrayStub()
        self.assertEqual(ctrl._effective_ports(), (61477, 61483))

    def test_effective_ports_falls_back_to_config_without_xray(self):
        from core.engine import EngineController

        ctrl = EngineController({"socks_port": 10808, "http_port": 10809})
        ctrl._xray = None
        self.assertEqual(ctrl._effective_ports(), (10808, 10809))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
