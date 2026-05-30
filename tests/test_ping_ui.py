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
class PingUITest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_panel_controls_exist(self):
        page, _eng, _store = _make_page(self._tmp)
        self.assertTrue(hasattr(page, "btn_ping_all"))
        self.assertTrue(hasattr(page, "btn_ping_one"))
        self.assertTrue(hasattr(page, "btn_test_strategies"))
        self.assertTrue(hasattr(page, "cmb_ping_strategy"))
        # combo has "all" + the 3 shipped strategies
        self.assertEqual(page.cmb_ping_strategy.count(), 4)
        self.assertEqual(page.cmb_ping_strategy.itemData(0), "")

    def test_ping_all_worker_formats_and_ranks(self):
        from ui.window import PingWorker
        page, engine, _store = _make_page(self._tmp)
        lines, summary = [], []
        w = PingWorker(engine, "ping_all", profiles=list(_store_profiles(page)))
        w.line.connect(lines.append)
        w.done.connect(summary.append)
        w.run()
        self.assertTrue(engine.ping_all_called)
        self.assertTrue(any("Fast" in l and "12ms" in l for l in lines))
        self.assertTrue(any("Dead" in l and "بدون پاسخ" in l for l in lines))
        self.assertTrue(any("بهترین سرور: Fast" in s for s in summary))

    def test_ping_one_worker(self):
        from ui.window import PingWorker
        page, engine, store = _make_page(self._tmp)
        prof = store.selected_profile
        lines, summary = [], []
        w = PingWorker(engine, "ping_one", profile=prof)
        w.line.connect(lines.append)
        w.done.connect(summary.append)
        w.run()
        self.assertTrue(engine.ping_one_called)
        self.assertTrue(any("One" in l and "20ms" in l for l in lines))

    def test_strategy_worker_marks_winner(self):
        from ui.window import PingWorker
        page, engine, store = _make_page(self._tmp)
        prof = store.selected_profile
        lines, summary = [], []
        w = PingWorker(engine, "strategy", profile=prof, strategy="")
        w.line.connect(lines.append)
        w.done.connect(summary.append)
        w.run()
        # wrong_seq failed (✖), fake_disorder connected (✔)
        self.assertTrue(any("✖" in l and "wrong_seq" in l for l in lines))
        self.assertTrue(any("✔" in l and "fake_disorder" in l for l in lines))
        self.assertTrue(any("بهترین استراتژی: fake_disorder" in s for s in summary))

    def test_strategy_worker_pins_single(self):
        from ui.window import PingWorker
        page, engine, store = _make_page(self._tmp)
        prof = store.selected_profile
        lines, summary = [], []
        w = PingWorker(engine, "strategy", profile=prof, strategy="multi_fake")
        w.line.connect(lines.append)
        w.done.connect(summary.append)
        w.run()
        self.assertEqual(engine.strategy_arg, "multi_fake")
        self.assertEqual(len([l for l in lines if "multi_fake" in l]), 1)

    def test_ping_buttons_guard_empty(self):
        page, engine, store = _make_page(self._tmp, with_profiles=False)
        # no profiles → _ping_all should not start a worker
        page._ping_all()
        self.assertFalse(engine.ping_all_called)
        # no selection → _ping_one should not start a worker
        page._ping_one()
        self.assertFalse(engine.ping_one_called)


def _store_profiles(page):
    return page._store.profiles


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
