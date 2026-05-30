"""Tests for the Windows system-proxy helper (step 22).

The real registry / WinINET calls are Windows-only, so :mod:`core.system_proxy`
factors the *decision + string* logic into pure functions and routes the actual
writes through injectable hooks. These tests exercise everything on any OS with
fakes — no real registry is ever touched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.system_proxy import (
    DEFAULT_BYPASS, SystemProxy, desired_state, format_proxy_server,
    normalise_bypass,
)


class PureLogicTest(unittest.TestCase):
    def test_format_proxy_server(self):
        self.assertEqual(format_proxy_server("127.0.0.1", 10809), "127.0.0.1:10809")
        # blank host falls back to loopback
        self.assertEqual(format_proxy_server("", 8080), "127.0.0.1:8080")

    def test_format_proxy_server_rejects_bad_port(self):
        for bad in (0, 70000, "abc", None):
            with self.assertRaises(ValueError):
                format_proxy_server("h", bad)

    def test_normalise_bypass_default_and_cleanup(self):
        self.assertEqual(normalise_bypass(""), DEFAULT_BYPASS)
        self.assertEqual(normalise_bypass(None), DEFAULT_BYPASS)
        self.assertEqual(
            normalise_bypass(" a ; b ,c;; "), "a;b;c")

    def test_desired_state_enable(self):
        st = desired_state(True, "127.0.0.1", 10809)
        self.assertEqual(st["ProxyEnable"], 1)
        self.assertEqual(st["ProxyServer"], "127.0.0.1:10809")
        self.assertEqual(st["ProxyOverride"], DEFAULT_BYPASS)

    def test_desired_state_disable_blanks_server(self):
        st = desired_state(False)
        self.assertEqual(st["ProxyEnable"], 0)
        self.assertEqual(st["ProxyServer"], "")

    def test_desired_state_custom_bypass(self):
        st = desired_state(True, "h", 1, bypass="x.com,y.com")
        self.assertEqual(st["ProxyOverride"], "x.com;y.com")


class _FakeBackend:
    def __init__(self, initial=None):
        self.store = dict(initial or {"ProxyEnable": 0, "ProxyServer": "",
                                      "ProxyOverride": ""})
        self.writes = []
        self.refreshes = 0

    def write(self, values):
        self.store.update(values)
        self.writes.append(dict(values))

    def refresh(self):
        self.refreshes += 1

    def read(self):
        return dict(self.store)


class SystemProxyTest(unittest.TestCase):
    def _proxy(self, backend, logs=None):
        return SystemProxy(writer=backend.write, refresher=backend.refresh,
                           reader=backend.read,
                           on_log=(logs.append if logs is not None else None))

    def test_enable_writes_and_refreshes(self):
        b = _FakeBackend()
        logs = []
        sp = self._proxy(b, logs)
        vals = sp.enable("127.0.0.1", 10809)
        self.assertEqual(b.store["ProxyEnable"], 1)
        self.assertEqual(b.store["ProxyServer"], "127.0.0.1:10809")
        self.assertEqual(b.refreshes, 1)
        self.assertEqual(vals["ProxyServer"], "127.0.0.1:10809")
        self.assertTrue(any("روشن" in m for m in logs))
        self.assertTrue(sp.is_enabled())

    def test_disable_turns_off(self):
        b = _FakeBackend({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:10809",
                          "ProxyOverride": DEFAULT_BYPASS})
        sp = self._proxy(b)
        sp.disable()
        self.assertEqual(b.store["ProxyEnable"], 0)
        self.assertFalse(sp.is_enabled())
        self.assertEqual(b.refreshes, 1)

    def test_enable_then_disable_round_trip(self):
        b = _FakeBackend()
        sp = self._proxy(b)
        sp.enable("127.0.0.1", 10809)
        self.assertTrue(sp.is_enabled())
        sp.disable()
        self.assertFalse(sp.is_enabled())
        # two writes (enable + disable), two refreshes
        self.assertEqual(len(b.writes), 2)
        self.assertEqual(b.refreshes, 2)

    def test_is_enabled_swallows_reader_errors(self):
        def boom():
            raise RuntimeError("registry locked")
        sp = SystemProxy(reader=boom)
        self.assertFalse(sp.is_enabled())


if __name__ == "__main__":
    unittest.main()
