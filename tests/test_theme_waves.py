"""Tests for the theme/UI overhaul (#10): mathematical wave backdrop + themes.

Logic-level assertions (palette tokens, QSS contents) run everywhere. The
Qt-dependent WaveBackdrop / MainWindow render checks run offscreen and skip
gracefully where PySide6 / a Qt platform plugin is unavailable.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.theme import (
    ACCENT2_DARK, ACCENT2_LIGHT, DARK, LIGHT, build_qss, get_palette,
)

try:
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import QApplication
    from ui.animations import WaveBackdrop
    from ui.window import MainWindow
    _HAVE_QT = True
except Exception:                                       # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


class PaletteTest(unittest.TestCase):
    """Pure-logic palette / QSS checks (no Qt platform needed)."""

    def test_both_themes_resolve(self):
        self.assertIs(get_palette("dark"), DARK)
        self.assertIs(get_palette("light"), LIGHT)
        # unknown name falls back to dark
        self.assertIs(get_palette("nope"), DARK)

    def test_dark_is_dark_light_is_light(self):
        self.assertTrue(DARK.is_dark)
        self.assertFalse(LIGHT.is_dark)

    def test_secondary_gaming_accents_present(self):
        # both secondary accents are valid hex colors
        for c in (ACCENT2_DARK, ACCENT2_LIGHT):
            self.assertTrue(c.startswith("#"))
            self.assertIn(len(c), (4, 7))

    def test_qss_builds_for_both_themes(self):
        for p in (DARK, LIGHT):
            qss = build_qss(p)
            self.assertIn("RootBackdrop", qss)
            self.assertIn(p.accent, qss)
            # the active bar (#9) and profile-row styling survive
            self.assertIn("ActiveBar", qss)
            self.assertIn("ProfileRow", qss)


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class WaveBackdropTest(unittest.TestCase):
    def test_paints_without_error(self):
        w = WaveBackdrop()
        w.resize(320, 240)
        w.set_palette(DARK.accent, ACCENT2_DARK)
        pm = QPixmap(320, 240)
        w.render(pm)                       # must not raise
        self.assertEqual(pm.width(), 320)

    def test_tiny_size_is_safe(self):
        w = WaveBackdrop()
        w.resize(1, 1)                     # below the guard threshold
        pm = QPixmap(1, 1)
        w.render(pm)                       # must not raise / divide-by-zero

    def test_enabled_toggle_controls_timer(self):
        w = WaveBackdrop()
        self.assertTrue(w._timer.isActive())
        w.set_enabled(False)
        self.assertFalse(w._timer.isActive())
        w.set_enabled(True)
        self.assertTrue(w._timer.isActive())

    def test_mouse_transparent(self):
        from PySide6.QtCore import Qt
        w = WaveBackdrop()
        self.assertTrue(w.testAttribute(Qt.WA_TransparentForMouseEvents))


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class WindowWaveIntegrationTest(unittest.TestCase):
    def test_window_mounts_and_sizes_wave(self):
        mw = MainWindow(theme="dark")
        mw.resize(900, 600)
        mw.show()
        try:
            self.assertIsInstance(mw.wave_bg, WaveBackdrop)
            # backdrop fills the window
            self.assertEqual(mw.wave_bg.width(), mw.width())
            self.assertEqual(mw.wave_bg.height(), mw.height())
        finally:
            mw.close()

    def test_theme_toggle_recolours_wave(self):
        mw = MainWindow(theme="dark")
        try:
            self.assertEqual(mw._theme, "dark")
            mw.toggle_theme()
            self.assertEqual(mw._theme, "light")
            # after toggle the palette propagated to the dashboard + wave
            self.assertFalse(mw._palette.is_dark)
        finally:
            mw.close()

    def test_hide_parks_animation(self):
        mw = MainWindow(theme="dark")
        mw.show()
        try:
            mw.wave_bg.set_enabled(True)
            mw.hide()
            self.assertFalse(mw.wave_bg._timer.isActive())
        finally:
            mw.close()


if __name__ == "__main__":
    setUpModule()
    unittest.main()
