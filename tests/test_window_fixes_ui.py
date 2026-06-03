"""Headless tests for the round of window UI fixes.

Covers the user-reported issues:
  * #1 — a visible resize grip exists and is hidden while maximized.
  * #2 — maximize/restore tracking is explicit (``_is_maximized`` + remembered
          normal geometry) so a minimize→restore cycle can't leave the window
          stuck maximized.
  * #5 — the Diagnostics ("تشخیص") page is gone from the nav + stack.
  * #6 — the dashboard does NOT claim a bypass strategy is active for an
          ordinary (non-spoof) config.

Skipped where Qt is unavailable.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_app = None
try:
    from PySide6.QtWidgets import QApplication, QSizeGrip
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


def _window():
    from ui.window import MainWindow
    w = MainWindow(theme="dark")
    w.show()
    if _app is not None:
        _app.processEvents()
    return w


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class ResizeGripTest(unittest.TestCase):
    def test_size_grip_exists(self):
        w = _window()
        self.assertIsInstance(w.size_grip, QSizeGrip)
        # visible in the normal (non-maximized) state
        self.assertTrue(w.size_grip.isVisible())

    def test_resize_band_is_grabbable(self):
        w = _window()
        # a wide enough invisible edge band so the cursor reliably hits it
        self.assertGreaterEqual(w._RESIZE_MARGIN, 8)
        # a point in the bottom-right corner reports a resize edge
        from PySide6.QtCore import QPoint
        r = w.rect()
        edges = w._edge_at(QPoint(r.width() - 1, r.height() - 1))
        self.assertIsNotNone(edges)


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class MaximizeRestoreTest(unittest.TestCase):
    def test_toggle_tracks_state_and_geometry(self):
        w = _window()
        self.assertFalse(w._is_maximized)
        w.toggle_maximize()
        self.assertTrue(w._is_maximized)
        # remembered the pre-maximize geometry so restore is exact
        self.assertIsNotNone(w._normal_geometry)
        w.toggle_maximize()
        self.assertFalse(w._is_maximized)

    def test_grip_hidden_when_maximized(self):
        w = _window()
        w.toggle_maximize()
        if _app is not None:
            _app.processEvents()
        self.assertFalse(w.size_grip.isVisible())
        w.toggle_maximize()
        if _app is not None:
            _app.processEvents()
        self.assertTrue(w.size_grip.isVisible())


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class DiagnosticsRemovedTest(unittest.TestCase):
    def test_no_diagnostics_in_nav_or_stack(self):
        w = _window()
        # 7 tabs: Dashboard / Profiles / Settings / Strategy / Pool /
        # Clean-IP scanner / Log (the route-pool and clean-IP scanner pages
        # were added; Diagnostics remains removed).
        self.assertEqual(len(w.nav_group.buttons()), 7)
        self.assertEqual(w.stack.count(), 7)
        self.assertFalse(hasattr(w, "page_diagnostics"))

    def test_nav_labels_have_no_diagnostics(self):
        w = _window()
        labels = [b.text() for b in w.nav_group.buttons()]
        self.assertNotIn("تشخیص", "".join(labels))


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class DashboardStrategyGatingTest(unittest.TestCase):
    def test_normal_config_shows_strategy_inactive(self):
        w = _window()
        d = w.page_dashboard
        d.set_active_strategy("wrong_seq")
        d.set_spoof_active(False)
        # value no longer names a strategy for a normal config (#6)
        self.assertNotEqual(d.stat_strategy.value_label.text(), "wrong_seq")
        self.assertIn("غیرفعال", d.lbl_resilience.text())

    def test_spoof_config_shows_real_strategy(self):
        w = _window()
        d = w.page_dashboard
        d.set_spoof_active(True)
        d.set_active_strategy("multi_fake")
        self.assertEqual(d.stat_strategy.value_label.text(), "multi_fake")

    def test_strategy_restored_after_switch_back(self):
        w = _window()
        d = w.page_dashboard
        d.set_active_strategy("fake_disorder")
        d.set_spoof_active(False)             # normal config hides it
        d.set_spoof_active(True)              # back to a spoof config
        self.assertEqual(d.stat_strategy.value_label.text(), "fake_disorder")


if __name__ == "__main__":
    unittest.main()
