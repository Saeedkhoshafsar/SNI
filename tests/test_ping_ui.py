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
        keys = [strategy] if strategy else ["wrong_seq", "fake_ttl"]
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
        # combo has "all" + the 5 strategies
        self.assertEqual(page.cmb_ping_strategy.count(), 6)
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
        # wrong_seq failed (✖), fake_ttl connected (✔)
        self.assertTrue(any("✖" in l and "wrong_seq" in l for l in lines))
        self.assertTrue(any("✔" in l and "fake_ttl" in l for l in lines))
        self.assertTrue(any("بهترین استراتژی: fake_ttl" in s for s in summary))

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


if __name__ == "__main__":
    unittest.main()
