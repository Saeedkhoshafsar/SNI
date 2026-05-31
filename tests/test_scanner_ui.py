"""Headless tests for the issue #2 (share) + #3 (scanner) UI pieces.

* ProfileRow now exposes ``share`` and ``scan`` buttons/signals.
* ScannerDialog turns simulated clean-IP hits into substituted profiles.

Skipped where Qt is absent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from ui.widgets import ProfileRow
    from ui.scanner_dialog import ScannerDialog
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.share_link import parse_link

_app = None

_LINK = (
    "vless://e0f8189f-1ca1-429e-82d8-447d8b356846@104.18.151.71:8443"
    "?encryption=none&security=tls&sni=hammm2.pages.dev&fp=chrome"
    "&type=ws&host=hammm2.pages.dev"
    "&path=%2Fstars%2Fhttp%3A%2F%2FPQ3YjMsJql%3AfCfJXXbDcw%40vps.webtun.xyz%3A2087"
    "#AYYILDIZ")


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ProfileRowSignalsTest(unittest.TestCase):
    def test_share_and_scan_buttons_emit(self):
        row = ProfileRow(parse_link(_LINK), active=False)
        self.assertTrue(hasattr(row, "btn_share"))
        self.assertTrue(hasattr(row, "btn_scan"))
        shared, scanned = [], []
        row.share.connect(lambda: shared.append(1))
        row.scan.connect(lambda: scanned.append(1))
        row.btn_share.click()
        row.btn_scan.click()
        self.assertEqual(shared, [1])
        self.assertEqual(scanned, [1])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScannerDialogTest(unittest.TestCase):
    def test_hits_become_substituted_profiles(self):
        p = parse_link(_LINK)
        dlg = ScannerDialog(p)
        dlg._on_hit("188.114.96.10", 30.0)
        dlg._on_hit("104.16.0.5", 55.0)
        self.assertEqual(dlg.list.count(), 2)

        # "add all" builds one profile per hit, address swapped, rest intact
        dlg._add_all()
        self.assertEqual(len(dlg.result_profiles), 2)
        addrs = {pr.address for pr in dlg.result_profiles}
        self.assertEqual(addrs, {"188.114.96.10", "104.16.0.5"})
        for pr in dlg.result_profiles:
            self.assertEqual(pr.uuid, p.uuid)
            self.assertEqual(pr.path, p.path)
            self.assertEqual(pr.sni, p.sni)
            self.assertEqual(pr.port, p.port)

    def test_check_none_then_add_selected_is_empty(self):
        p = parse_link(_LINK)
        dlg = ScannerDialog(p)
        dlg._on_hit("188.114.96.10", 30.0)
        dlg._set_all_checked(False)
        dlg._add_selected()
        # nothing checked → no profiles, dialog stays open (not accepted)
        self.assertEqual(dlg.result_profiles, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
