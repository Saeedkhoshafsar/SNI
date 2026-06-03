"""Headless tests for the standalone «اسکن IP تمیز» page (ScannerPage).

These exercise the SenPaiScanner setup-row wiring (source / count / workers /
timeout / ports / Top-N), the manual+file IP input, the live results table,
and the copy / export / add-to-configs actions — all without any sockets or a
real display. Skipped where Qt is unavailable.

Run:  python -m pytest tests/test_scanner_page.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ui.scanner_page import ScannerPage, _COL_CHECK, _COL_ENDPOINT
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.cf_scanner import SOURCE_RANDOM, SOURCE_FILE
from core.profile import Profile

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        # Modal dialogs block forever under the offscreen platform (there is no
        # user to click "OK"). Stub them so the page logic stays testable —
        # we assert on behaviour (no profiles built, etc.), not on the popup.
        import ui.scanner_page as sp
        sp.QMessageBox.information = staticmethod(lambda *a, **k: None)
        sp.QMessageBox.warning = staticmethod(lambda *a, **k: None)
        sp.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
        sp.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))


def _profile(port=443, transport="ws", remark="r1"):
    return Profile.from_dict({
        "address": "example.com", "port": port, "remark": remark,
        "transport": transport, "sni": "a.com", "host": "a.com",
        "path": "/ws",
    })


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScannerPageSetupTest(unittest.TestCase):
    def setUp(self):
        self.page = ScannerPage()
        self.p = _profile()
        self.page.set_profiles([self.p], selected=self.p)

    def test_config_combo_has_no_config_option_plus_profiles(self):
        # index 0 = «بدون کانفیگ», index 1 = our profile
        self.assertIsNone(self.page.cmb_config.itemData(0))
        self.assertIs(self.page.cmb_config.currentData(), self.p)

    def test_default_presets_match_senpai(self):
        self.assertEqual(self.page._selected_count(), 5000)
        self.assertEqual(self.page._selected_workers(), 50)
        self.assertEqual(self.page._selected_timeout(), 5.0)
        self.assertEqual(self.page._selected_topn(), 25)
        # 443 pill on by default; config-port pill on by default
        self.assertIn(443, self.page._selected_extra_ports())
        self.assertTrue(self.page.chk_port_config.isChecked())

    def test_custom_count_workers_timeout_topn(self):
        # pick the "custom" entry (last item) for each and set the spin
        self.page.cmb_count.setCurrentIndex(self.page.cmb_count.count() - 1)
        self.page.spin_count.setValue(99999)
        self.assertEqual(self.page._selected_count(), 99999)

        self.page.cmb_workers.setCurrentIndex(
            self.page.cmb_workers.count() - 1)
        self.page.spin_workers.setValue(300)
        self.assertEqual(self.page._selected_workers(), 300)

        self.page.cmb_timeout.setCurrentIndex(
            self.page.cmb_timeout.count() - 1)
        self.page.spin_timeout.setValue(7)
        self.assertEqual(self.page._selected_timeout(), 7.0)

        self.page.cmb_topn.setCurrentIndex(self.page.cmb_topn.count() - 1)
        self.page.spin_topn.setValue(80)
        self.assertEqual(self.page._selected_topn(), 80)

    def test_topn_all_is_zero(self):
        # the "همه" entry carries data 0 (validate all)
        for i in range(self.page.cmb_topn.count()):
            if self.page.cmb_topn.itemData(i) == 0:
                self.page.cmb_topn.setCurrentIndex(i)
                break
        self.assertEqual(self.page._selected_topn(), 0)

    def test_source_file_reveals_manual_box_and_parses_ips(self):
        idx = self.page.cmb_source.findData(SOURCE_FILE)
        self.page.cmb_source.setCurrentIndex(idx)
        self.page.txt_manual.setPlainText(
            "104.16.1.1\n104.16.1.2:8443\n108.162.0.0/30")
        # manual parsing strips ports + expands CIDRs
        self.assertIn("104.16.1.1", self.page._uploaded_ips)
        self.assertIn("104.16.1.2", self.page._uploaded_ips)
        self.assertIn("108.162.0.1", self.page._uploaded_ips)

    def test_xray_disabled_without_reference_config(self):
        # select «بدون کانفیگ»
        self.page.cmb_config.setCurrentIndex(0)
        self.assertIsNone(self.page.current_profile())
        self.assertFalse(self.page.chk_xray.isChecked())
        self.assertFalse(self.page.chk_xray.isEnabled())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScannerPageResultsTest(unittest.TestCase):
    def setUp(self):
        self.page = ScannerPage()
        self.p = _profile()
        self.page.set_profiles([self.p], selected=self.p)

    def test_hits_create_rows_keyed_by_ip_port(self):
        self.page._on_hit("104.16.5.5", 443, 88.0, "http ok · AMS")
        self.page._on_hit("104.16.5.6", 8443, 240.0, "http ok")
        # same IP on a different port → a separate row
        self.page._on_hit("104.16.5.5", 8443, 90.0, "http ok")
        self.assertEqual(self.page.table.rowCount(), 3)
        # endpoints come back as (ip, port) tuples
        all_eps = set(self.page._all_endpoints())
        self.assertEqual(all_eps, {
            ("104.16.5.5", 443), ("104.16.5.6", 8443), ("104.16.5.5", 8443)})

    def test_verified_marks_row_and_rejected_unchecks(self):
        self.page._on_hit("104.16.5.5", 443, 88.0, "")
        self.page._on_hit("104.16.5.6", 443, 90.0, "")
        self.page._on_verified("104.16.5.5", 443, 95.0, 5_000_000)
        self.page._on_rejected("104.16.5.6", 443)
        checked = self.page._checked_endpoints()
        # verified stays checked, rejected is unchecked
        self.assertIn(("104.16.5.5", 443), checked)
        self.assertNotIn(("104.16.5.6", 443), checked)

    def test_select_all_none(self):
        self.page._on_hit("104.16.5.5", 443, 88.0, "")
        self.page._on_hit("104.16.5.6", 443, 90.0, "")
        self.page._set_all_checked(False)
        self.assertEqual(self.page._checked_endpoints(), [])
        self.page._set_all_checked(True)
        self.assertEqual(len(self.page._checked_endpoints()), 2)

    def test_add_selected_builds_profiles_via_callback(self):
        self.page._on_hit("104.16.5.5", 443, 88.0, "")
        self.page._on_hit("104.16.5.6", 8443, 90.0, "")
        got = []
        self.page.on_add_profiles = lambda profs: got.extend(profs)
        self.page._add_selected()
        # one profile per clean IP, server address swapped to the clean IP
        addrs = sorted(getattr(x, "address", None) for x in got)
        self.assertEqual(addrs, ["104.16.5.5", "104.16.5.6"])
        # the built profiles keep the reference transport/sni
        for prof in got:
            self.assertEqual(getattr(prof, "sni", ""), "a.com")

    def test_add_selected_requires_reference_config(self):
        self.page.cmb_config.setCurrentIndex(0)  # «بدون کانفیگ»
        self.page._on_hit("104.16.5.5", 443, 88.0, "")
        got = []
        self.page.on_add_profiles = lambda profs: got.extend(profs)
        self.page._add_selected()
        self.assertEqual(got, [])               # nothing built without a config

    def test_focus_profile_selects_existing(self):
        p2 = _profile(port=2053, remark="r2")
        self.page.set_profiles([self.p, p2])
        self.page.focus_profile(p2)
        self.assertIs(self.page.current_profile(), p2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
