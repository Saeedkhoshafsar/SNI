"""UI tests for the professional log console (step 23).

Headless Qt (offscreen). Verifies the LogPage wires the pure LogBuffer to the
widget: classification, filtering, search, counters, clear.
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
class LogPageTest(unittest.TestCase):
    def _page(self):
        from ui.window import LogPage
        return LogPage()

    def test_seeds_initial_lines(self):
        page = self._page()
        # two seed lines added in __init__
        self.assertGreaterEqual(len(page._buffer), 2)

    def test_append_classifies_and_counts(self):
        page = self._page()
        page.clear()
        page.append("✓ اتصال برقرار شد")     # ok
        page.append("خطا در توقف xray")       # err
        self.assertEqual(page._buffer.counts["ok"], 1)
        self.assertEqual(page._buffer.counts["err"], 1)
        # counter label reflects the err count
        self.assertIn("1", page.counters.text())

    def test_level_filter_rerenders(self):
        page = self._page()
        page.clear()
        page.append("plain info line")        # info
        page.append("xray failed badly")      # err
        # filter to err only
        idx = page.cmb_level.findData("err")
        page.cmb_level.setCurrentIndex(idx)
        html = page.log.toPlainText()
        self.assertIn("failed", html)
        self.assertNotIn("plain info line", html)

    def test_search_filters(self):
        page = self._page()
        page.clear()
        page.append("warp connected ok")      # ok
        page.append("psiphon starting")       # info
        page.search.setText("warp")
        html = page.log.toPlainText()
        self.assertIn("warp", html)
        self.assertNotIn("psiphon", html)

    def test_clear_empties_buffer_and_view(self):
        page = self._page()
        page.append("something")
        page.clear()
        self.assertEqual(len(page._buffer), 0)
        self.assertEqual(page.log.toPlainText().strip(), "")
        self.assertTrue(all(v == 0 for v in page._buffer.counts.values()))


if __name__ == "__main__":
    unittest.main()
