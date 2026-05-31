"""Headless tests for the ping / strategy-test UI panel (step 18).

Verifies ProfilesPage exposes the ping controls, that the PingWorker formats
latency / strategy results into readable lines, and that the engine bridge is
called. The worker's ``run`` is invoked directly (synchronously) so there is no
thread-timing flakiness. Skipped where Qt is absent.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

from core.config_store import ConfigStore
from core.profile import Profile
from core.ping import PingResult, StrategyPing, StrategyPingReport
from core.prober import OK, RST

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


class FakeEngine:
    """Stand-in for EngineBridge exposing the ping passthroughs."""
    def __init__(self):
        self.config_updates = 0
        self.ping_all_called = False
        self.ping_one_called = False
        self.strategy_arg = None

    def update_config(self, cfg):
        self.config_updates += 1

    def ping_profiles(self, profiles):
        self.ping_all_called = True
        return [
            PingResult("Fast", "fast", 443, samples_sent=1, latencies=[12.0],
                       download_kbps=800.0),
            PingResult("Dead", "dead", 443, samples_sent=1, latencies=[]),
        ]

    def ping_profile(self, profile):
        self.ping_one_called = True
        return PingResult("One", "one", 443, samples_sent=2,
                          latencies=[20.0, 30.0])

    def probe_strategies_for(self, profile, *, strategy=None):
        self.strategy_arg = strategy
        rep = StrategyPingReport("S", "h", 443)
        keys = [strategy] if strategy else ["wrong_seq", "fake_disorder"]
        for k in keys:
            outcome = OK if k != "wrong_seq" else RST
            lat = 18.0 if outcome == OK else 0.0
            score = 0.9 if outcome == OK else 0.0
            rep.results.append(StrategyPing(k, k, outcome, latency_ms=lat,
                                            score=score))
        return rep


def _make_page(tmpdir, with_profiles=True):
    from ui.window import ProfilesPage
    store = ConfigStore(runtime_dir=tmpdir)
    if with_profiles:
        store.profiles = [
            Profile(protocol="vless", address="fast", port=443, remark="Fast",
                    uuid="x"),
            Profile(protocol="vless", address="dead", port=443, remark="Dead",
                    uuid="y"),
        ]
        store.selected_index = 0
    engine = FakeEngine()
    page = ProfilesPage(store, engine=engine)
    return page, engine, store


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class ProfilesToolbarTest(unittest.TestCase):
    """The redesigned compact icon toolbar on the profiles page (#4).

    The old standalone "سنجش پیش از اتصال" ping/strategy-test panel (and its
    PingWorker) were removed; per-server measurements now happen inline. This
    test pins down the new toolbar so the controls don't silently disappear.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_bulk_toolbar_buttons_exist(self):
        page, _eng, _store = _make_page(self._tmp)
        for attr in ("btn_select_all", "btn_clear_sel", "btn_ping_all_rows",
                     "btn_ping_selected", "btn_copy_selected", "btn_edit",
                     "btn_delete_selected"):
            self.assertTrue(hasattr(page, attr), attr)

    def test_pre_connect_panel_removed(self):
        page, _eng, _store = _make_page(self._tmp)
        # the removed panel's widgets must no longer exist (#4)
        for attr in ("btn_ping_all", "btn_ping_one", "btn_test_strategies",
                     "cmb_ping_strategy", "ping_output", "ping_status"):
            self.assertFalse(hasattr(page, attr), attr)

    def test_worker_class_removed(self):
        import ui.window as w
        self.assertFalse(hasattr(w, "PingWorker"))


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class BulkImportTest(unittest.TestCase):
    """ProfilesPage bulk import: paste many links, add them all at once (#7)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_split_links_one_per_line(self):
        from ui.window import ProfilesPage
        links = ProfilesPage._split_links(
            "vless://a@h1:443#A\ntrojan://pw@h2:443#B\n")
        self.assertEqual(len(links), 2)

    def test_split_links_glued_on_one_line(self):
        from ui.window import ProfilesPage
        links = ProfilesPage._split_links(
            "vless://a@h1:443#A trojan://pw@h2:443#B")
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("vless://"))
        self.assertTrue(links[1].startswith("trojan://"))

    def test_split_links_empty(self):
        from ui.window import ProfilesPage
        self.assertEqual(ProfilesPage._split_links("   \n  "), [])

    def test_bulk_import_adds_all_without_dialog(self):
        page, _eng, store = _make_page(self._tmp, with_profiles=False)
        blob = ("vless://u1@h1.example:443?security=tls&sni=h1#A\n"
                "trojan://pw@h2.example:443#B\n"
                "vless://u3@h3.example:8443?type=ws#C")
        page.input.setPlainText(blob)
        page._import_link()           # 3 links → bulk path, no dialog
        self.assertEqual(len(store.profiles), 3)
        self.assertEqual(page.input.toPlainText(), "")   # cleared on success

    def test_bulk_import_skips_invalid_lines(self):
        page, _eng, store = _make_page(self._tmp, with_profiles=False)
        blob = ("vless://u1@h1.example:443#A\n"
                "this-is-not-a-link\n"
                "trojan://pw@h2.example:443#B")
        page.input.setPlainText(blob)
        page._import_link()
        # the 2 valid links are added, the junk line is skipped
        self.assertEqual(len(store.profiles), 2)


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class MultiSelectTest(unittest.TestCase):
    """ProfilesPage multi-select + bulk delete / copy-links (#7)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _page_with_three(self):
        from ui.window import ProfilesPage
        store = ConfigStore(runtime_dir=self._tmp)
        store.profiles = [
            Profile(protocol="vless", address="h1.example", port=443,
                    remark="A", uuid="11111111-1111-1111-1111-111111111111"),
            Profile(protocol="vless", address="h2.example", port=443,
                    remark="B", uuid="22222222-2222-2222-2222-222222222222"),
            Profile(protocol="vless", address="h3.example", port=443,
                    remark="C", uuid="33333333-3333-3333-3333-333333333333"),
        ]
        store.selected_index = 0
        return ProfilesPage(store, engine=FakeEngine()), store

    def test_row_has_independent_select_checkbox(self):
        page, _store = self._page_with_three()
        self.assertTrue(hasattr(page._rows[0], "chk_select"))
        # checking the box must NOT change the active profile
        page._rows[1].chk_select.setChecked(True)
        self.assertEqual(page._store.selected_index, 0)
        self.assertIn(1, page._checked)

    def test_checkbox_does_not_activate(self):
        page, store = self._page_with_three()
        page._rows[2].chk_select.setChecked(True)
        # selection (active server) stays put — checking is selection only
        self.assertEqual(store.selected_index, 0)

    def test_select_all_and_clear(self):
        page, _store = self._page_with_three()
        page._select_all()
        self.assertEqual(page._checked, {0, 1, 2})
        page._clear_selection()
        self.assertEqual(page._checked, set())

    def test_bulk_delete_checked(self):
        page, store = self._page_with_three()
        page._checked = {0, 2}
        # bypass the confirmation dialog by calling the store method the
        # handler delegates to, then mirror the handler's post-steps
        removed = store.remove_profiles(page._checked)
        page._checked = set()
        page.refresh()
        self.assertEqual(removed, 2)
        self.assertEqual([p.remark for p in store.profiles], ["B"])
        self.assertEqual(page._checked, set())

    def test_bulk_copy_links_joins_with_newline(self):
        page, store = self._page_with_three()
        page._checked = {0, 2}
        page._copy_selected_links()
        from PySide6.QtGui import QGuiApplication
        text = QGuiApplication.clipboard().text()
        self.assertEqual(len(text.splitlines()), 2)

    def test_checked_indexes_cleaned_after_external_shrink(self):
        page, store = self._page_with_three()
        page._checked = {0, 1, 2}
        store.remove_profile(2)   # list now has 2 entries
        page.refresh()            # refresh prunes stale checked indexes
        self.assertTrue(page._checked.issubset({0, 1}))


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class LanSettingsTest(unittest.TestCase):
    """SettingsPage LAN-sharing toggle (share proxy to a phone)."""

    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_lan_toggle_roundtrips_through_config(self):
        page = self._page()
        page.load_from({"allow_lan": True})
        self.assertTrue(page.chk_lan.isChecked())
        self.assertTrue(page.collect()["allow_lan"])
        page.chk_lan.setChecked(False)
        self.assertFalse(page.collect()["allow_lan"])

    def test_lan_off_by_default(self):
        page = self._page()
        page.load_from({})
        self.assertFalse(page.chk_lan.isChecked())
        self.assertIn("127.0.0.1", page.lan_hint.text())

    def test_lan_hint_shows_address_when_on(self):
        page = self._page()
        page.chk_lan.setChecked(True)   # toggled → hint updates
        hint = page.lan_hint.text()
        self.assertIn("SOCKS5", hint)
        self.assertIn(str(page.spin_socks.value()), hint)


class SystemProxySettingsTest(unittest.TestCase):
    """SettingsPage tunnel-vs-system-proxy toggle (feedback 7)."""

    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_system_proxy_toggle_roundtrips_through_config(self):
        page = self._page()
        page.load_from({"system_proxy": True})
        self.assertTrue(page.chk_system_proxy.isChecked())
        self.assertTrue(page.collect()["system_proxy"])
        page.chk_system_proxy.setChecked(False)
        self.assertFalse(page.collect()["system_proxy"])

    def test_system_proxy_off_by_default(self):
        page = self._page()
        page.load_from({})
        self.assertFalse(page.chk_system_proxy.isChecked())
        # default (tunnel) hint mentions the tunnel wording
        self.assertIn("تونل", page.proxy_hint.text())

    def test_system_proxy_hint_changes_when_on(self):
        page = self._page()
        page.chk_system_proxy.setChecked(True)   # toggled → hint updates
        self.assertIn("پروکسی سیستم", page.proxy_hint.text())


if __name__ == "__main__":
    unittest.main()
