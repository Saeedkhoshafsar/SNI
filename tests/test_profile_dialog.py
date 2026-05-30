"""Headless tests for the profile editor dialog (step 15).

Runs offscreen; skipped gracefully where PySide6 / a Qt platform plugin is
unavailable. Verifies the dialog pre-fills every field from a parsed share
link and round-trips edits back into a validated Profile — the v2rayN-style
"paste link → review/edit → add" flow the user asked for.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox, QComboBox
    from ui.profile_dialog import ProfileDialog
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.share_link import parse_link
from core.profile import Profile

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class ProfileDialogTest(unittest.TestCase):

    VLESS = (
        "vless://11111111-2222-3333-4444-555555555555@example.com:8443"
        "?type=ws&security=tls&sni=cdn.example.com&host=cdn.example.com"
        "&path=%2Fws&fp=chrome&flow=xtls-rprx-vision#MyServer"
    )

    def test_prefills_every_parsed_field(self):
        prof = parse_link(self.VLESS)
        dlg = ProfileDialog(prof)
        w = dlg._widgets
        self.assertEqual(w["remark"].text(), "MyServer")
        self.assertEqual(w["address"].text(), "example.com")
        self.assertEqual(w["port"].value(), 8443)
        self.assertEqual(w["uuid"].text(), prof.uuid)
        self.assertEqual(w["protocol"].currentText(), "vless")
        self.assertEqual(w["transport"].currentText(), "ws")
        self.assertEqual(w["security"].currentText(), "tls")
        self.assertEqual(w["sni"].text(), "cdn.example.com")
        self.assertEqual(w["path"].text(), "/ws")
        self.assertEqual(w["fingerprint"].text(), "chrome")
        self.assertEqual(w["flow"].text(), "xtls-rprx-vision")

    def test_collect_round_trips_edits(self):
        prof = parse_link(self.VLESS)
        dlg = ProfileDialog(prof)
        # user edits the display name and port
        dlg._widgets["remark"].setText("Edited Name")
        dlg._widgets["port"].setValue(443)
        dlg._widgets["sni"].setText("new.sni.example")
        out = dlg.collect()
        self.assertIsInstance(out, Profile)
        self.assertEqual(out.remark, "Edited Name")
        self.assertEqual(out.port, 443)
        self.assertEqual(out.sni, "new.sni.example")
        # untouched credential survives
        self.assertEqual(out.uuid, prof.uuid)
        # original raw link is preserved for debugging
        self.assertEqual(out.raw, prof.raw)

    def test_xhttp_mode_prefills_and_survives_edit(self):
        prof = parse_link(
            "vless://u-9@127.0.0.1:40443?encryption=none&security=tls"
            "&sni=w.example.dev&fp=chrome&type=xhttp&host=w.example.dev"
            "&path=%2Fvless-xhttp&mode=auto#X")
        dlg = ProfileDialog(prof)
        # xhttp is a selectable transport and mode is shown
        self.assertEqual(dlg._widgets["transport"].currentText(), "xhttp")
        self.assertEqual(dlg._widgets["mode"].text(), "auto")
        # editing an unrelated field keeps xhttp + mode intact
        dlg._widgets["remark"].setText("Renamed")
        out = dlg.collect()
        self.assertEqual(out.transport, "xhttp")
        self.assertEqual(out.mode, "auto")
        self.assertEqual(out.path, "/vless-xhttp")

    def test_accept_blocks_on_invalid_then_succeeds(self):
        prof = parse_link(self.VLESS)
        dlg = ProfileDialog(prof)
        # blank the UUID → vless is invalid → accept must not close
        dlg._widgets["uuid"].setText("")
        dlg._on_accept()
        self.assertNotEqual(dlg.result(), ProfileDialog.Accepted)
        self.assertTrue(dlg._err.text())  # an error message is shown
        # restore a valid uuid → accept succeeds
        dlg._widgets["uuid"].setText("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        dlg._on_accept()
        self.assertEqual(dlg.result(), ProfileDialog.Accepted)
        self.assertEqual(dlg.result_profile.uuid,
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_trojan_credentials_prefill(self):
        prof = parse_link(
            "trojan://secretpass@host.example:443?sni=host.example#TJ")
        dlg = ProfileDialog(prof)
        self.assertEqual(dlg._widgets["protocol"].currentText(), "trojan")
        self.assertEqual(dlg._widgets["password"].text(), "secretpass")
        self.assertEqual(dlg._widgets["address"].text(), "host.example")


if __name__ == "__main__":
    unittest.main()
