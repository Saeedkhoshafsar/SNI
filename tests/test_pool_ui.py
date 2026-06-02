"""UI tests for the multi-IP / multi-SNI route-pool surface.

Covers:
  * SettingsPage — the CONNECT_IPS / FAKE_SNIS textareas (load_from / collect,
    parsing, dedupe, the live "how many routes" hint).
  * PoolPage — the live status renderer for both the disabled (single-route)
    and enabled (multi-route) snapshots.

Headless Qt (offscreen); skipped automatically when PySide6 is unavailable.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_app = None
try:
    from PySide6.QtWidgets import QApplication
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class SettingsPagePoolTest(unittest.TestCase):
    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_pool_lists_roundtrip(self):
        page = self._page()
        page.load_from({
            "CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com",
            "CONNECT_IPS": ["9.9.9.9", "8.8.8.8"],
            "FAKE_SNIS": ["x.com", "y.com"],
        })
        out = page.collect()
        self.assertEqual(out["CONNECT_IPS"], ["9.9.9.9", "8.8.8.8"])
        self.assertEqual(out["FAKE_SNIS"], ["x.com", "y.com"])

    def test_empty_pool_lists_collect_empty(self):
        page = self._page()
        page.load_from({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        out = page.collect()
        self.assertEqual(out["CONNECT_IPS"], [])
        self.assertEqual(out["FAKE_SNIS"], [])

    def test_parse_lines_strips_and_dedupes(self):
        page = self._page()
        page.pool_ips.setPlainText(" 1.1.1.1 \n1.1.1.1\n\n2.2.2.2\n")
        self.assertEqual(page._pool_ip_list(), ["1.1.1.1", "2.2.2.2"])

    def test_hint_disabled_for_single_pair(self):
        page = self._page()
        page.load_from({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        self.assertIn("غیرفعال", page.pool_hint.text())

    def test_hint_enabled_and_counts_pairs(self):
        page = self._page()
        page.pool_ips.setPlainText("1.1.1.1\n2.2.2.2")
        page.pool_snis.setPlainText("a.com\nb.com\nc.com")
        page._update_pool_hint()
        # 2 × 3 = 6 routes
        self.assertIn("6", page.pool_hint.text())
        self.assertIn("فعال", page.pool_hint.text())

    def test_hint_uses_singular_fallback_for_count(self):
        # one pool IP + empty SNI list ⇒ falls back to single SNI ⇒ 1 route ⇒ disabled
        page = self._page()
        page.pool_ips.setPlainText("1.1.1.1")
        page.pool_snis.setPlainText("")
        page._update_pool_hint()
        self.assertIn("غیرفعال", page.pool_hint.text())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class PoolPageTest(unittest.TestCase):
    def _page(self):
        from ui.window import PoolPage
        return PoolPage()

    def test_no_provider_renders_disabled(self):
        page = self._page()
        page.refresh()
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_disabled_snapshot(self):
        page = self._page()
        page.set_provider(lambda: {"enabled": False})
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_enabled_snapshot_counts_and_check_age(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 6, "known": 4, "stable": 3,
            "weak": 1, "dead": 0, "unexplored": 2, "active": 2,
            "seconds_since_check": 12.0, "rows": [],
        })
        self.assertIn("فعال", page.lbl_state.text())
        self.assertIn("6", page.lbl_counts.text())
        self.assertIn("12", page.lbl_check.text())

    def test_enabled_snapshot_renders_rows(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 2, "known": 2, "stable": 1,
            "weak": 0, "dead": 1, "unexplored": 0, "active": 1,
            "seconds_since_check": 5.0,
            "rows": [
                {"ip": "9.9.9.9", "sni": "x.com", "loss": 0.05,
                 "alive": True, "active": 1, "in_pool": True},
                {"ip": "8.8.8.8", "sni": "y.com", "loss": 1.0,
                 "alive": False, "active": 0, "in_pool": False},
            ],
        })
        text = page.tbl.toPlainText()
        self.assertIn("9.9.9.9", text)
        self.assertIn("8.8.8.8", text)
        self.assertIn("★", text)        # active pair marker
        self.assertIn("مرده", text)     # dead pair state

    def test_seconds_none_shows_bootstrapping(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 4, "rows": [],
            "seconds_since_check": None,
        })
        self.assertIn("راه‌اندازی", page.lbl_check.text())

    def test_provider_exception_is_swallowed(self):
        page = self._page()

        def boom():
            raise RuntimeError("nope")

        page.set_provider(boom)        # must not raise
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_polling_start_stop(self):
        page = self._page()
        page.set_provider(lambda: {"enabled": False})
        page.start_polling()
        self.assertTrue(page._timer.isActive())
        page.stop_polling()
        self.assertFalse(page._timer.isActive())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class EngineBridgePoolSummaryTest(unittest.TestCase):
    def _bridge(self):
        from ui.engine_bridge import EngineBridge
        from core.engine import EngineController
        return EngineBridge(EngineController({}))

    def test_single_target_config_disabled(self):
        b = self._bridge()
        snap = b.pool_summary({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        self.assertFalse(snap.get("enabled"))

    def test_multi_target_config_enabled_static(self):
        b = self._bridge()
        snap = b.pool_summary({
            "CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com", "b.com"],
        })
        self.assertTrue(snap.get("enabled"))
        self.assertEqual(snap.get("total"), 4)

    def test_live_manager_summary_preferred(self):
        from core.pool import build_connection_manager
        b = self._bridge()
        mgr = build_connection_manager(
            {"CONNECT_IPS": ["1.1.1.1", "2.2.2.2"], "FAKE_SNIS": ["a.com"]},
            probe_fn=lambda ip, port, t: True,
        )
        # mark one pair probed/in-pool so the live summary differs from static
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        b.controller.conn_manager = mgr
        snap = b.pool_summary({})
        self.assertTrue(snap.get("enabled"))
        self.assertEqual(snap.get("total"), 2)
        self.assertGreaterEqual(snap.get("known", 0), 1)
        self.assertGreaterEqual(snap.get("active", 0), 1)


if __name__ == "__main__":
    unittest.main()
