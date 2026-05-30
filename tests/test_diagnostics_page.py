"""Headless render tests for the Qt DiagnosticsPage (step 12).

Runs offscreen and is skipped gracefully where PySide6 / a Qt platform plugin
is unavailable, so the pure-logic suite still passes everywhere. Asserts that
the page turns a DiagnosticsSnapshot into the right on-screen text — without
touching any engine internals (the page only consumes the snapshot).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from ui.window import DiagnosticsPage
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.diagnostics import DiagnosticsSnapshot, CandidateStat

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class DiagnosticsPageTest(unittest.TestCase):
    def _page(self, snap):
        page = DiagnosticsPage()
        page.set_provider(lambda: snap)
        page.refresh()
        return page

    def test_idle_snapshot_shows_placeholders(self):
        page = self._page(DiagnosticsSnapshot())
        self.assertIn("—", page.lbl_active.text())
        self.assertIn("بی‌کار", page.lbl_status.text())
        self.assertEqual(page.bar_tp.value(), 0)
        self.assertIn("هنوز probe", page.tbl.toPlainText())

    def test_active_snapshot_renders_strategy_and_candidates(self):
        snap = DiagnosticsSnapshot(
            status="active", active_strategy="fake_ttl", spoof_port=40443,
            candidates=[
                CandidateStat("fake_ttl", "fake_ttl", samples=3,
                              success_rate=1.0, mean_score=0.9,
                              selected=True, last_outcome="ok"),
                CandidateStat("wrong_seq", "wrong_seq", samples=3,
                              success_rate=0.0, mean_score=0.0,
                              last_outcome="rst"),
            ],
        )
        page = self._page(snap)
        self.assertIn("fake_ttl", page.lbl_active.text())
        self.assertIn("فعال", page.lbl_status.text())
        self.assertIn("40443", page.lbl_status.text())
        table = page.tbl.toPlainText()
        self.assertIn("fake_ttl", table)
        self.assertIn("wrong_seq", table)
        self.assertIn("★", table)            # selected marker
        self.assertIn("rst", table)

    def test_throttle_snapshot_fills_bar_and_flags(self):
        snap = DiagnosticsSnapshot(
            status="active", active_strategy="fake_ttl",
            resilience_on=True, forged_rst_count=2, rst_budget=3,
            throttled=True, recent_bps=200_000.0, baseline_bps=1_000_000.0,
            strategy_chain=["fake_ttl", "wrong_seq"], ip_chain=["1.1.1.1"],
            current_ip="1.1.1.1",
        )
        page = self._page(snap)
        self.assertEqual(page.bar_tp.value(), 20)          # 200k/1M
        self.assertIn("throttle", page.lbl_tp.text())
        self.assertIn("2", page.lbl_rst.text())
        self.assertIn("بودجه 3", page.lbl_rst.text())
        self.assertIn("fake_ttl", page.lbl_chain.text())
        self.assertIn("1.1.1.1", page.lbl_chain.text())

    def test_resilience_off_shows_disabled(self):
        snap = DiagnosticsSnapshot(status="active", resilience_on=False)
        page = self._page(snap)
        self.assertIn("غیرفعال", page.lbl_rst.text())

    def test_live_throughput_shown_without_baseline(self):
        # Active connection, no baseline yet (resilience off) but live rate
        # is flowing → the card must surface "سرعت فعلی" with a formatted
        # speed instead of looking dead (the #4 "does nothing" complaint).
        snap = DiagnosticsSnapshot(
            status="active", resilience_on=False,
            recent_bps=2000.0, baseline_bps=0.0,
        )
        page = self._page(snap)
        self.assertTrue(hasattr(page, "lbl_tp_help"))
        self.assertTrue(page.lbl_tp_help.text())            # explanation present
        self.assertIn("سرعت فعلی", page.lbl_tp_live.text())
        # 2000 bps ⇒ a non-placeholder formatted figure
        self.assertNotIn("—", page.lbl_tp_live.text())
        self.assertNotIn("در انتظار", page.lbl_tp_live.text())
        # bar can't show a throttle ratio without a baseline yet
        self.assertEqual(page.bar_tp.value(), 0)
        self.assertIn("خط پایه", page.lbl_tp.text())

    def test_live_throughput_waiting_when_no_traffic(self):
        snap = DiagnosticsSnapshot(status="active", recent_bps=0.0)
        page = self._page(snap)
        self.assertIn("در انتظار", page.lbl_tp_live.text())

    def test_live_throughput_dash_when_disconnected(self):
        snap = DiagnosticsSnapshot(status="idle", recent_bps=0.0)
        page = self._page(snap)
        self.assertIn("متصل نیست", page.lbl_tp_live.text())

    def test_polling_toggles(self):
        page = self._page(DiagnosticsSnapshot())
        self.assertFalse(page._timer.isActive())
        page.start_polling()
        self.assertTrue(page._timer.isActive())
        page.stop_polling()
        self.assertFalse(page._timer.isActive())


if __name__ == "__main__":
    setUpModule()
    unittest.main()
