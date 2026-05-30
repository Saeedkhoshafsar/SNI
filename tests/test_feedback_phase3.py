"""Tests for the phase-3 feedback fixes.

Covers the four behaviours that don't need a live Qt event loop:

* **#1** — adding a profile while one is already active must NOT steal the
  active selection (only the very first profile auto-activates).
* **#3** — :func:`core.ping.target_from_profile` pings the *real* CDN endpoint
  (SNI/Host on the TLS port) for SNI-spoof configs, so ping works while the
  tunnel is down instead of hitting the dead ``127.0.0.1`` spoofer port.
* **#6** — the lightweight runtime i18n layer translates Persian source
  strings to English and falls back to the original for unknown keys.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore
from core.profile import Profile
from core.ping import target_from_profile
from ui import i18n


def _profile(remark="srv", addr="example.com", port=443, **extra):
    return Profile(protocol="vless", address=addr, port=port,
                   uuid="11111111-1111-1111-1111-111111111111",
                   remark=remark, **extra)


class AddProfileSelectionTest(unittest.TestCase):
    """#1: a newly added profile must not auto-activate over an active one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ConfigStore(runtime_dir=self.tmp)

    def test_first_profile_auto_selected(self):
        # the very first profile becomes active so the app is usable out of box
        self.store.add_profile(_profile("first"))
        self.assertEqual(self.store.selected_index, 0)

    def test_second_profile_does_not_steal_selection(self):
        self.store.add_profile(_profile("first"))
        self.store.select(0)
        self.store.add_profile(_profile("second"))
        # still pointing at the original active profile, not the new one
        self.assertEqual(self.store.selected_index, 0)
        self.assertEqual(self.store.selected_profile.display_name,
                         _profile("first").display_name)

    def test_explicit_select_true_overrides(self):
        self.store.add_profile(_profile("first"))
        self.store.add_profile(_profile("second"), select=True)
        self.assertEqual(self.store.selected_index, 1)


class PingTargetTest(unittest.TestCase):
    """#3: spoof configs ping the real CDN endpoint, not the local spoofer."""

    def test_plain_profile_pings_its_own_address(self):
        prof = _profile("plain", addr="real.example.com", port=8443)
        tgt = target_from_profile(prof)
        self.assertEqual(tgt.host, "real.example.com")
        self.assertEqual(tgt.port, 8443)

    def test_spoof_config_pings_connect_ip_with_decoy_sni(self):
        # #1: the honest offline test of a spoof config's bypass path is a TLS
        # handshake to the *connect IP* the spoofer dials, presenting the
        # **decoy** SNI it injects — NOT the real worker SNI directly (DPI would
        # block that). So if the connect IP is censored, the TLS probe resets →
        # honest red instead of a misleading green TCP-only ping.
        prof = _profile("spoof", addr="127.0.0.1", port=40443,
                        sni="worker.example.workers.dev", security="tls")
        self.assertTrue(prof.is_spoof_config)
        tgt = target_from_profile(prof)
        self.assertEqual(tgt.host, prof.spoof_connect_ip)
        self.assertEqual(tgt.port, prof.spoof_connect_port)
        self.assertEqual(tgt.server_name, prof.spoof_fake_sni)
        self.assertTrue(tgt.tls)


class I18nTest(unittest.TestCase):
    """#6: runtime translation layer."""

    def setUp(self):
        # always start from the product default so test order is irrelevant
        i18n._lang = "fa"

    def tearDown(self):
        i18n._lang = "fa"

    def test_persian_is_identity(self):
        self.assertEqual(i18n.tr("داشبورد"), "داشبورد")

    def test_english_lookup(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("داشبورد"), "Dashboard")
        self.assertEqual(i18n.tr("لاگ"), "Log")

    def test_unknown_key_falls_back(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("یک رشته‌ی ترجمه‌نشده"),
                         "یک رشته‌ی ترجمه‌نشده")

    def test_toggle_round_trips(self):
        self.assertEqual(i18n.language(), "fa")
        self.assertEqual(i18n.toggle_language(), "en")
        self.assertEqual(i18n.toggle_language(), "fa")

    def test_observer_fires_on_change(self):
        seen = []
        i18n.on_language_changed(lambda lang: seen.append(lang))
        i18n.set_language("en")
        self.assertIn("en", seen)

    def test_format_strings_translate_with_placeholders(self):
        i18n.set_language("en")
        msg = i18n.tr("سرور فعال شد: {name}").format(name="MyServer")
        self.assertEqual(msg, "Server activated: MyServer")


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
