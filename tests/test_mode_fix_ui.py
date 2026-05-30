"""Tests for the connection-mode fixes (feedback: 'still need V2RayTun').

Covers:
  * engine.uses_core / wants_core_but_no_profile semantics
  * SettingsPage mode hint + set_mode
  * MainWindow auto-switches SNI Only -> Tunnel when a profile is selected
  * the window is NOT translucent and HAS the minimise flag (drag/min fix)
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


# --- pure engine semantics (no Qt needed) --------------------------------
class EngineModeTest(unittest.TestCase):
    def _engine(self, mode, profile):
        from core.engine import EngineController
        e = EngineController()
        e.config["connection_mode"] = mode
        e.set_profile(profile)
        return e

    def test_sni_only_without_profile_no_core(self):
        # No profile selected → SNI Only runs the standalone raw forwarder
        # (the only case that legitimately needs no xray core).
        e = self._engine("SNI Only", None)
        self.assertFalse(e.uses_core)
        self.assertFalse(e.wants_core_but_no_profile)

    def test_sni_only_with_profile_still_uses_core(self):
        # A selected profile ALWAYS needs xray (it speaks VLESS/VMess/Trojan,
        # which a raw forwarder can't). "SNI Only" only means "no Warp/Psiphon
        # outer layer", not "no xray" — so the core must run regardless.
        e = self._engine("SNI Only", object())
        self.assertTrue(e.uses_core)
        self.assertFalse(e.wants_core_but_no_profile)

    def test_tunnel_with_profile_uses_core(self):
        e = self._engine("Tunnel", object())
        self.assertTrue(e.uses_core)
        self.assertFalse(e.wants_core_but_no_profile)

    def test_tunnel_without_profile_flags_warning(self):
        e = self._engine("Tunnel", None)
        self.assertFalse(e.uses_core)
        self.assertTrue(e.wants_core_but_no_profile)

    def test_plain_tunnel_ordinary_profile_does_not_chain_spoofer(self):
        # plain Tunnel + an ORDINARY (non-spoof) profile = direct xray
        # (V2RayTun-like), no spoofer mangling. object() has no is_spoof_config
        # attribute, so it's treated as a routable server.
        e = self._engine("Tunnel", object())
        self.assertTrue(e.uses_core)
        self.assertFalse(e.chains_spoofer)

    def test_sni_combo_chains_spoofer(self):
        # explicit SNI-bypass combos DO chain the spoofer under xray
        e = self._engine("SNI + Warp", object())
        self.assertTrue(e.uses_core)
        self.assertTrue(e.chains_spoofer)

    def test_sni_only_ordinary_profile_chains_spoofer(self):
        # SNI Only + a profile = the user wants DPI evasion on the outer hop,
        # so the spoofer is chained under xray.
        e = self._engine("SNI Only", object())
        self.assertTrue(e.uses_core)
        self.assertTrue(e.chains_spoofer)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class SettingsModeHintTest(unittest.TestCase):
    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_mode_hint_updates(self):
        from ui.window import MODE_HINTS
        page = self._page()
        page.set_mode("SNI Only")
        self.assertEqual(page.mode_hint.text(), MODE_HINTS["SNI Only"])
        page.set_mode("Tunnel")
        self.assertEqual(page.mode_hint.text(), MODE_HINTS["Tunnel"])

    def test_tunnel_is_first_mode(self):
        from ui.window import MODES
        self.assertEqual(MODES[0], "Tunnel")


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class WindowFlagsTest(unittest.TestCase):
    def test_not_translucent_and_has_minimize(self):
        from PySide6.QtCore import Qt
        from ui.window import MainWindow
        w = MainWindow()
        self.assertFalse(w.testAttribute(Qt.WA_TranslucentBackground))
        self.assertTrue(bool(w.windowFlags() & Qt.WindowMinimizeButtonHint))
        self.assertTrue(bool(w.windowFlags() & Qt.FramelessWindowHint))

    def test_profile_selection_autoswitches_to_tunnel(self):
        from ui.window import MainWindow
        w = MainWindow()
        w.store.set("connection_mode", "SNI Only")

        class _P:
            display_name = "test"
        w._on_profile_selected(_P())
        self.assertEqual(w.store.get("connection_mode"), "Tunnel")

    def test_profile_selection_keeps_explicit_nonsni_mode(self):
        from ui.window import MainWindow
        w = MainWindow()
        w.store.set("connection_mode", "Gaming Mode")

        class _P:
            display_name = "test"
        w._on_profile_selected(_P())
        # should not clobber a deliberate non-SNI mode
        self.assertEqual(w.store.get("connection_mode"), "Gaming Mode")


if __name__ == "__main__":
    unittest.main()
