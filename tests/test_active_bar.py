"""Headless tests for the persistent active-config status bar (#9).

Verifies ActiveConfigBar reflects the selected profile, the connection state
(dot colour via dynamic property), and the live rate readout — and that the
MainWindow keeps it in sync across tabs. Skipped where Qt is absent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from ui.widgets import ActiveConfigBar
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.share_link import parse_link

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class ActiveConfigBarTest(unittest.TestCase):

    def test_empty_profile_shows_hint(self):
        bar = ActiveConfigBar()
        bar.set_profile(None)
        self.assertIn("SNI Only", bar._name.text())

    def test_shows_profile_name_and_endpoint(self):
        bar = ActiveConfigBar()
        p = parse_link("vless://u@h.example:8443?security=tls&sni=h.example#MyServer")
        bar.set_profile(p)
        self.assertIn("MyServer", bar._name.text())
        self.assertIn("vless", bar._name.text())

    def test_status_updates_dot_property(self):
        bar = ActiveConfigBar()
        for state in ("connecting", "active", "error", "idle"):
            bar.set_status(state)
            self.assertEqual(bar._dot.property("state"), state)
        # active state text is Farsi for "connected"
        bar.set_status("active")
        self.assertEqual(bar._state.text(), "متصل")

    def test_rate_shown_and_cleared(self):
        bar = ActiveConfigBar()
        bar.set_status("active")
        bar.set_rate(2048, 1024)
        self.assertIn("↓", bar._rate.text())
        self.assertIn("↑", bar._rate.text())
        # leaving the active state clears the live rate
        bar.set_status("idle")
        self.assertEqual(bar._rate.text(), "")


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class MainWindowActiveBarTest(unittest.TestCase):
    """The bar lives on the window (above the tab stack) and stays in sync."""

    def test_window_has_active_bar_above_stack(self):
        from ui.window import MainWindow
        w = MainWindow(theme="dark")
        try:
            self.assertTrue(hasattr(w, "active_bar"))
            self.assertIsInstance(w.active_bar, ActiveConfigBar)
            # selecting a profile through the page handler updates the bar
            p = parse_link("trojan://pw@srv.example:443#Picked")
            w._on_profile_selected(p)
            self.assertIn("Picked", w.active_bar._name.text())
        finally:
            w.deleteLater()


if __name__ == "__main__":
    unittest.main()
