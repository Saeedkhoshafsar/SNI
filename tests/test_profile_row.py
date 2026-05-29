"""Headless tests for the rich profile-list row (step 16).

Verifies the row shows the protocol glyph, name, server detail line, the
right transport/security badges, an "active" pill only when selected, and that
the inline edit button emits the ``edit`` signal. Skipped where Qt is absent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel
    from ui.widgets import ProfileRow
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.share_link import parse_link

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


def _texts(row):
    return [c.text() for c in row.findChildren(QLabel)]


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class ProfileRowTest(unittest.TestCase):

    def test_shows_name_detail_and_badges(self):
        p = parse_link(
            "vless://u-1@h.example:8443?type=ws&security=tls&sni=h.example#Srv")
        row = ProfileRow(p, active=False)
        texts = _texts(row)
        self.assertIn("Srv", texts)                      # display name
        self.assertIn("vless · h.example:8443", texts)   # detail line
        self.assertIn("WS", texts)                       # transport badge
        self.assertIn("TLS", texts)                      # security badge
        # not active → no pill
        self.assertFalse(any("فعال" in t for t in texts))

    def test_active_pill_only_when_active(self):
        p = parse_link("trojan://pw@h.example:443#TJ")
        row = ProfileRow(p, active=True)
        texts = _texts(row)
        self.assertTrue(any("فعال" in t for t in texts))
        self.assertEqual(row.property("active"), "1")

    def test_tcp_none_has_no_badges(self):
        p = parse_link("ss://YWVzLTI1Ni1nY206cHc=@h.example:443#SS")
        row = ProfileRow(p, active=False)
        texts = _texts(row)
        # ss is tcp + none security → no transport/security badges
        self.assertNotIn("WS", texts)
        self.assertNotIn("TLS", texts)

    def test_edit_signal_fires(self):
        p = parse_link("vless://u-1@h.example:443#X")
        row = ProfileRow(p)
        fired = []
        row.edit.connect(lambda: fired.append(True))
        row.btn_edit.click()
        self.assertEqual(fired, [True])


if __name__ == "__main__":
    unittest.main()
