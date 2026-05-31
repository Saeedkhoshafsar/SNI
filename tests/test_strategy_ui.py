"""UI tests for the clickable StrategyPage + TitleBar native drag (step 24).

Headless Qt (offscreen).
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


class _FakeStore:
    def __init__(self, initial=None):
        self._d = dict(initial or {})
        self.saved = False

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class StrategyPageTest(unittest.TestCase):
    def _page(self, store=None):
        from ui.window import StrategyPage
        return StrategyPage(store or _FakeStore({"bypass_method": "wrong_seq"}))

    def test_initial_selection_from_store(self):
        page = self._page(_FakeStore({"bypass_method": "fake_disorder"}))
        self.assertEqual(page._selected, "fake_disorder")
        self.assertTrue(page._cards["fake_disorder"].property("selected"))
        self.assertFalse(page._cards["wrong_seq"].property("selected"))

    def test_click_selects_and_persists(self):
        store = _FakeStore({"bypass_method": "wrong_seq"})
        page = self._page(store)
        emitted = []
        page.strategy_selected.connect(emitted.append)
        page._on_card_clicked("multi_fake")
        self.assertEqual(page._selected, "multi_fake")
        self.assertEqual(store.get("bypass_method"), "multi_fake")
        self.assertEqual(emitted, ["multi_fake"])
        self.assertTrue(page._cards["multi_fake"].property("selected"))

    def test_click_disables_auto_prober(self):
        store = _FakeStore({"bypass_method": "wrong_seq", "auto_prober": True})
        page = self._page(store)
        self.assertTrue(page.btn_autoprobe.isChecked())
        page._on_card_clicked("fake_disorder")
        self.assertFalse(page.btn_autoprobe.isChecked())
        self.assertFalse(store.get("auto_prober"))

    def test_auto_prober_hides_manual_selection(self):
        store = _FakeStore({"bypass_method": "fake_disorder"})
        page = self._page(store)
        # turning auto-prober on clears the visual selection
        page.btn_autoprobe.setChecked(True)
        self.assertFalse(page._cards["fake_disorder"].property("selected"))
        self.assertIn("پراب خودکار", page.pick_hint.text())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class TitleBarDragTest(unittest.TestCase):
    def test_native_move_helper_falls_back_gracefully(self):
        from PySide6.QtWidgets import QWidget
        from ui.widgets import TitleBar
        win = QWidget()
        tb = TitleBar(win)
        # offscreen has no real window handle/system move; helper must not raise
        # and must return a bool so mousePressEvent can decide the fallback path.
        result = tb._begin_native_move()
        self.assertIsInstance(result, bool)

    def test_has_maximize_button_and_signal(self):
        """#6: the title bar exposes a maximize button + maximize_clicked."""
        from PySide6.QtWidgets import QWidget
        from ui.widgets import TitleBar
        win = QWidget()
        tb = TitleBar(win)
        self.assertTrue(hasattr(tb, "btn_max"))
        self.assertTrue(hasattr(tb, "maximize_clicked"))

    def test_maximize_button_click_emits_signal(self):
        """Clicking the maximize button emits maximize_clicked."""
        from PySide6.QtWidgets import QWidget
        from ui.widgets import TitleBar
        win = QWidget()
        tb = TitleBar(win)
        fired = []
        tb.maximize_clicked.connect(lambda: fired.append(True))
        tb.btn_max.click()
        self.assertEqual(fired, [True])

    def test_update_max_label_swaps_glyph(self):
        """The glyph reflects the next action (maximize vs restore)."""
        from PySide6.QtWidgets import QWidget
        from ui.widgets import TitleBar
        win = QWidget()
        tb = TitleBar(win)
        tb.update_max_label(False)
        normal = tb.btn_max.text()
        tb.update_max_label(True)
        maximized = tb.btn_max.text()
        self.assertNotEqual(normal, maximized)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class CardShadowTest(unittest.TestCase):
    def test_set_shadow_adjusts_effect(self):
        from ui.widgets import Card
        c = Card()
        # should not raise; blur/offset update the underlying effect
        c.set_shadow(blur=46, y=16, color="rgba(0,0,0,0.6)")
        self.assertEqual(c._shadow.blurRadius(), 46)


if __name__ == "__main__":
    unittest.main()
