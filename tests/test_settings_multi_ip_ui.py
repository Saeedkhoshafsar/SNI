"""SettingsPage SNI ↔ multi-IP pairing.

Regression for the user-reported bug: "یک SNI با چند IP، فقط یکی ذخیره می‌شد".
A single fake SNI must be able to hold MANY connect IPs; picking the SNI must
list ALL of them in the IP combo so the user can choose which to use.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    _HAVE_QT = True
except Exception:
    _HAVE_QT = False

_app = None
if _HAVE_QT:
    _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class SettingsMultiIpTest(unittest.TestCase):
    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_add_pair_appends_multiple_ips_for_one_sni(self):
        page = self._page()
        page.load_from({"sni_ip_pairs": [], "FAKE_SNI": "chatgpt.com",
                        "CONNECT_IP": ""})
        page.sni.setCurrentText("chatgpt.com")
        page.connect_ip.setCurrentText("1.1.1.1")
        page._add_pair()
        page.connect_ip.setCurrentText("2.2.2.2")
        page._add_pair()
        page.connect_ip.setCurrentText("3.3.3.3")
        page._add_pair()
        ips = page._ips_for_sni("chatgpt.com")
        self.assertEqual(ips, ["1.1.1.1", "2.2.2.2", "3.3.3.3"])
        # all three persisted as distinct pairs
        self.assertEqual(len(page._sni_ip_pairs), 3)

    def test_choosing_sni_lists_all_its_ips_in_combo(self):
        page = self._page()
        page.load_from({"sni_ip_pairs": [
            {"sni": "chatgpt.com", "ip": "1.1.1.1"},
            {"sni": "chatgpt.com", "ip": "2.2.2.2"},
            {"sni": "other.com", "ip": "9.9.9.9"},
        ], "FAKE_SNI": "other.com", "CONNECT_IP": "9.9.9.9"})
        page.sni.setCurrentText("chatgpt.com")
        page._on_sni_chosen(0)
        combo_ips = [page.connect_ip.itemText(i)
                     for i in range(page.connect_ip.count())]
        self.assertEqual(combo_ips, ["1.1.1.1", "2.2.2.2"])
        # the first IP is auto-selected
        self.assertEqual(page.connect_ip.currentText(), "1.1.1.1")

    def test_add_duplicate_pair_is_ignored(self):
        page = self._page()
        page.load_from({"sni_ip_pairs": [
            {"sni": "chatgpt.com", "ip": "1.1.1.1"}],
            "FAKE_SNI": "chatgpt.com", "CONNECT_IP": "1.1.1.1"})
        page.sni.setCurrentText("chatgpt.com")
        page.connect_ip.setCurrentText("1.1.1.1")
        page._add_pair()
        self.assertEqual(len(page._sni_ip_pairs), 1)

    def test_remove_only_the_selected_ip_keeps_others(self):
        page = self._page()
        page.load_from({"sni_ip_pairs": [
            {"sni": "chatgpt.com", "ip": "1.1.1.1"},
            {"sni": "chatgpt.com", "ip": "2.2.2.2"}],
            "FAKE_SNI": "chatgpt.com", "CONNECT_IP": "1.1.1.1"})
        page.sni.setCurrentText("chatgpt.com")
        page.connect_ip.setCurrentText("1.1.1.1")
        page._remove_pair()
        ips = page._ips_for_sni("chatgpt.com")
        self.assertEqual(ips, ["2.2.2.2"])

    def test_collect_round_trips_all_pairs(self):
        page = self._page()
        pairs = [
            {"sni": "chatgpt.com", "ip": "1.1.1.1"},
            {"sni": "chatgpt.com", "ip": "2.2.2.2"},
        ]
        page.load_from({"sni_ip_pairs": pairs, "FAKE_SNI": "chatgpt.com",
                        "CONNECT_IP": "1.1.1.1"})
        out = page.collect()
        got = {(p["sni"], p["ip"]) for p in out["sni_ip_pairs"]}
        self.assertEqual(
            got, {("chatgpt.com", "1.1.1.1"), ("chatgpt.com", "2.2.2.2")})

    def test_pair_count_reports_pairs_and_snis(self):
        page = self._page()
        page.load_from({"sni_ip_pairs": [
            {"sni": "a.com", "ip": "1.1.1.1"},
            {"sni": "a.com", "ip": "2.2.2.2"},
            {"sni": "b.com", "ip": "3.3.3.3"}],
            "FAKE_SNI": "a.com", "CONNECT_IP": "1.1.1.1"})
        txt = page.lbl_pair_count.text()
        self.assertIn("3", txt)   # 3 pairs
        self.assertIn("2", txt)   # 2 SNIs


if __name__ == "__main__":
    unittest.main()
