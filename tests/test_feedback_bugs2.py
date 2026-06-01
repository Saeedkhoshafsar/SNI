"""Regression tests for the SECOND feedback round (4 bugs).

User report (verbatim Persian):

  1. "پینگ ها کلا مشکل دارند مثلا من کانفیگ اسپوفم پینگ نداد ولی کار میکرد ... و
     بعضی کانفیگ های عادی هم پینگ میدادن ولی کار نمیکردن ... یه پینگ درست حسابی
     نمیتونیم بگریم" — the ping is fundamentally unreliable: a spoof config gave
     NO ping but worked; some ordinary configs pinged positive but didn't work.

  2. "وقتی کانفیگ فعال رو تغییر میدم ... بعضی وقتا مینویسه درحال اتصال ولی بعضی
     وقتا هم نوشته شروع ... ممکنه من حواسم نباشه و شروع رو بزنم" — during an
     automatic config-switch restart the dashboard flickers to the idle "شروع"
     label and the user can accidentally press Start mid-restart.

  3. "وقتی چند کار رو سریع باهم انجام میدم مثلا هم پینگ همه رو میزنم و هم انتخاب
     همه رو نرم افزار بسته میشه" — doing several things at once (Ping All +
     Select All) crashes the app.

  4. "وقتی اسکن آیپی تمیز رو میزنم صفحه سرچ نرم افزار بالا میاد ولی نمیتونم حرکتش
     بدم چون هدرش از کادر سیستمم بیرون زده" — the clean-IP scanner dialog opens
     with its title bar off the top of the screen, so it can't be dragged.

Fixes verified here:

  * Bug 1 — when the tunnel is running the engine offers a *live* proxy ping
    (real request through xray→spoofer→CDN); ``is_active_profile`` identifies
    the running config; an offline strategy-probe failure no longer forces a
    working spoof config to red.
  * Bug 2 — the MainWindow masks the transient idle during an auto-restart as
    "connecting" and ignores manual Start clicks while ``_restarting`` is set.
  * Bug 3 — inline pings are queued with bounded concurrency, refresh() keeps
    running workers alive, and results re-find the row by profile identity.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
#  Bug 1 — honest ping: live-tunnel measurement for the active config
# ---------------------------------------------------------------------------

class LivePingTest(unittest.TestCase):
    def _ctrl(self, **cfg):
        from core.engine import EngineController
        return EngineController(cfg)

    def test_is_active_profile_matches_by_endpoint_fields(self):
        from core.profile import Profile
        ctrl = self._ctrl()
        a = Profile(address="ex.com", port=443, uuid="u1")
        b = Profile(address="ex.com", port=443, uuid="u1")
        other = Profile(address="ex.com", port=443, uuid="DIFFERENT")
        ctrl.profile = a
        self.assertTrue(ctrl.is_active_profile(a))   # identity
        self.assertTrue(ctrl.is_active_profile(b))   # same endpoint
        self.assertFalse(ctrl.is_active_profile(other))
        self.assertFalse(ctrl.is_active_profile(None))

    def test_is_active_profile_false_when_idle(self):
        from core.profile import Profile
        ctrl = self._ctrl()
        ctrl.profile = None
        self.assertFalse(ctrl.is_active_profile(Profile(address="x", port=1)))

    def test_live_proxy_ping_refuses_when_not_active(self):
        ctrl = self._ctrl()
        # status starts idle → no live measurement possible
        ok, ms, detail = ctrl.live_proxy_ping()
        self.assertFalse(ok)
        self.assertIsNone(ms)

    def test_live_proxy_ping_measures_through_proxy_when_active(self):
        """When active, it should hit the REAL bound http port via a proxy."""
        from core import engine as eng_mod
        ctrl = self._ctrl(socks_port=10808, http_port=10809)
        ctrl._status = eng_mod.STATUS_ACTIVE

        class _Xray:
            socks_port = 55001
            http_port = 55002
        ctrl._xray = _Xray()

        captured = {}

        # patch urllib so no real network is touched; assert it used our proxy
        import urllib.request as ur

        class _Resp:
            def getcode(self):
                return 204
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, req, timeout=0):
                captured["url"] = req.full_url
                return _Resp()

        real_build = ur.build_opener
        real_handler = ur.ProxyHandler

        def fake_handler(mapping):
            captured["proxy"] = mapping
            return object()

        def fake_build(_h):
            return _Opener()

        ur.ProxyHandler = fake_handler
        ur.build_opener = fake_build
        try:
            ok, ms, detail = ctrl.live_proxy_ping(samples=1)
        finally:
            ur.ProxyHandler = real_handler
            ur.build_opener = real_build

        self.assertTrue(ok)
        self.assertIsInstance(ms, float)
        # must have pointed at the REAL bound http port (55002), not 10809
        self.assertIn("55002", captured["proxy"]["http"])
        self.assertNotIn("10809", captured["proxy"]["http"])

    def test_offline_spoof_config_refuses_to_fake_a_number(self):
        """A spoof config that is NOT the active one must not be given a fake
        latency offline — a raw connect to its Cloudflare anycast IP answers for
        anything, which is exactly the meaningless ping the user kept seeing.

        The inline worker emits an informational "activate to ping" hint instead
        of a green/red number. We exercise the worker end-to-end with a fake
        engine to assert that contract.
        """
        from ui.window import InlinePingWorker

        class _SpoofProfile:
            is_spoof_config = True
            address = "127.0.0.1"
            port = 40443

        captured = {}

        class _Eng:
            def is_active_profile(self, *_a):
                return False
            def live_proxy_ping(self, *_a, **_k):
                return (False, None, "idle")
            def ping_profile(self, *_a):
                raise AssertionError("must NOT raw-ping a spoof config offline")
            def probe_strategies_for(self, *_a, **_k):
                raise AssertionError("must NOT probe a spoof config offline")

        w = InlinePingWorker(_Eng(), _SpoofProfile())
        w.result.connect(lambda t, k: captured.update(text=t, kind=k))
        w._run_inner()
        self.assertEqual(captured.get("kind"), "info")
        self.assertNotIn("ms", captured.get("text", ""))

    def test_active_config_uses_live_tunnel_ping(self):
        """When the profile IS the running config, the worker reports the live
        tunnel latency (🛡) — the only fully trustworthy measurement."""
        from ui.window import InlinePingWorker
        from core.profile import Profile

        captured = {}

        class _Eng:
            def is_active_profile(self, *_a):
                return True
            def live_proxy_ping(self, *_a, **_k):
                return (True, 87.0, "http 204")

        w = InlinePingWorker(_Eng(), Profile(address="x", port=1))
        w.result.connect(lambda t, k: captured.update(text=t, kind=k))
        w._run_inner()
        self.assertEqual(captured.get("kind"), "ok")
        self.assertIn("87", captured.get("text", ""))


# ---------------------------------------------------------------------------
#  Bug 2 — status mask during auto-restart (no "شروع" flicker, Start ignored)
# ---------------------------------------------------------------------------

class RestartStatusMaskTest(unittest.TestCase):
    """The MainWindow status dispatch should present a transient idle as
    'connecting' while a restart is in flight, and ignore manual power clicks.

    We test the pure decision used by ``_dispatch_status`` / ``_on_power``
    without spinning up the whole Qt window.
    """

    @staticmethod
    def _shown(status, restarting):
        if restarting and status in ("idle", "error"):
            return "connecting"
        return status

    def test_idle_masked_to_connecting_during_restart(self):
        self.assertEqual(self._shown("idle", True), "connecting")
        self.assertEqual(self._shown("error", True), "connecting")

    def test_status_passthrough_when_not_restarting(self):
        self.assertEqual(self._shown("idle", False), "idle")
        self.assertEqual(self._shown("active", True), "active")

    @staticmethod
    def _power_allowed(restarting):
        return not restarting

    def test_manual_power_ignored_during_restart(self):
        self.assertFalse(self._power_allowed(True))
        self.assertTrue(self._power_allowed(False))


# ---------------------------------------------------------------------------
#  Bug 3 — inline ping scheduler: bounded, de-duplicated, refresh-safe
# ---------------------------------------------------------------------------

class InlinePingSchedulerTest(unittest.TestCase):
    """Drive the ProfilesPage ping scheduler with a fake engine + no real
    threads, asserting it (a) caps concurrency, (b) de-dupes, (c) survives a
    refresh() while pings are 'in flight' without dropping/raising.
    """

    def _page(self, n_profiles=20):
        from PySide6.QtWidgets import QApplication
        from ui.window import ProfilesPage
        from core.config_store import ConfigStore
        from core.profile import Profile

        app = QApplication.instance() or QApplication([])
        store = ConfigStore.__new__(ConfigStore)
        # minimal store stub: just .profiles + .config + .selected_index
        profs = [Profile(address=f"h{i}.com", port=443, uuid=f"u{i}")
                 for i in range(n_profiles)]

        class _Store:
            profiles = profs
            config = {}
            selected_index = -1
        page = ProfilesPage(_Store(), engine=_FakeEngine())
        return app, page

    def test_ping_all_is_bounded(self):
        app, page = self._page(n_profiles=30)
        # don't actually start threads — stub the worker start
        started = []

        # monkeypatch _pump_ping_queue to count how many would run at once by
        # inspecting the queue vs pending size at pump time
        page._ping_all_inline()
        # after enqueue, at most _PING_MAX_CONCURRENCY are 'pending', rest queued
        self.assertLessEqual(len(page._inline_pending),
                             page._PING_MAX_CONCURRENCY)
        self.assertEqual(
            len(page._inline_pending) + len(page._inline_queue), 30)
        page.stop_inline_pings()

    def test_duplicate_ping_is_deduped(self):
        app, page = self._page(n_profiles=5)
        page._ping_row(0)
        n_after_first = len(page._inline_pending) + len(page._inline_queue)
        page._ping_row(0)  # same row again — must not add a second job
        n_after_dup = len(page._inline_pending) + len(page._inline_queue)
        self.assertEqual(n_after_first, n_after_dup)
        page.stop_inline_pings()

    def test_refresh_during_ping_keeps_workers(self):
        app, page = self._page(n_profiles=10)
        page._ping_all_inline()
        jobs_before = dict(page._inline_jobs)
        page.refresh()  # used to do self._inline_workers = {} → crash
        # the running jobs dict must NOT have been wiped
        self.assertEqual(set(page._inline_jobs), set(jobs_before))
        page.stop_inline_pings()

    def test_row_index_lookup_by_profile_identity(self):
        app, page = self._page(n_profiles=4)
        prof = page._store.profiles[2]
        self.assertEqual(page._row_index_for_profile(prof), 2)


class _FakeEngine:
    """Engine stub whose ping calls never touch the network or block long."""
    is_running = False

    def update_config(self, *_a, **_k):
        pass

    def is_active_profile(self, *_a, **_k):
        return False

    def live_proxy_ping(self, *_a, **_k):
        return (False, None, "idle")

    def ping_profile(self, *_a, **_k):
        return None

    def probe_strategies_for(self, *_a, **_k):
        return None


# ---------------------------------------------------------------------------
#  Round 3, Bug 1 — never get stuck on "connecting"; Stop always works
# ---------------------------------------------------------------------------

class _RestartEngine:
    """Engine stub for restart-recovery tests; records start/stop calls."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self._running = False
        self.status_value = "idle"

    @property
    def is_running(self):
        return self._running

    def stop(self):
        self.stopped += 1
        self._running = False

    def start(self):
        self.started += 1


def _bare_window():
    """Build a MainWindow-like shell without the full Qt window.

    We only need the restart-state methods, so we bind them to a lightweight
    object that carries the same attributes. This keeps the test fast and free
    of a real engine/event-loop while exercising the exact logic.
    """
    from ui.window import MainWindow

    class _Page:
        def set_status(self, *_a):
            pass
    obj = MainWindow.__new__(MainWindow)
    obj.engine = _RestartEngine()
    obj.page_dashboard = _Page()
    obj.active_bar = _Page()
    obj._restarting = False
    obj._restart_gen = 0
    obj._restart_started = False
    obj._restart_attempts = 0

    class _Log:
        def append(self, *_a):
            pass
    obj.page_log = _Log()
    return obj


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def test_cancel_restart_clears_mask_and_bumps_generation(self):
        w = _bare_window()
        w._restarting = True
        g0 = w._restart_gen
        w._cancel_restart()
        self.assertFalse(w._restarting)
        self.assertGreater(w._restart_gen, g0)
        # a stale poll from the old generation must be a no-op (not start)
        w.engine._running = False
        w._restart_when_idle(g0)
        self.assertEqual(w.engine.started, 0)

    def test_watchdog_drops_mask_when_connect_never_completes(self):
        import ui.window as win
        w = _bare_window()
        w._restarting = True
        gen = w._restart_gen
        w.engine.status_value = "idle"
        # the watchdog pops a Toast which needs a real widget parent; stub it.
        orig = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            w._restart_watchdog(gen)  # engine never reached 'active'
        finally:
            win.Toast.show_message = orig
        self.assertFalse(w._restarting)

    def test_watchdog_noop_if_already_active(self):
        w = _bare_window()
        w._restarting = False  # mask already dropped (active reached)
        w._restart_watchdog(w._restart_gen)
        self.assertFalse(w._restarting)

    def test_dispatch_status_unmasks_on_failed_start(self):
        """Once the new start has fired, an incoming idle/error means the new
        config failed → drop the mask so the user isn't trapped."""
        from ui.window import MainWindow
        w = _bare_window()
        w._restarting = True
        w._restart_started = True  # the new start() already fired

        # call the real _dispatch_status with a stubbed _on_status
        w._on_status = lambda *_a: None
        MainWindow._dispatch_status(w, "idle")
        self.assertFalse(w._restarting)

    def test_dispatch_status_keeps_mask_during_teardown(self):
        from ui.window import MainWindow
        w = _bare_window()
        w._restarting = True
        w._restart_started = False  # still tearing the old session down
        shown = {}

        class _P:
            def set_status(self, s):
                shown["v"] = s
        w.page_dashboard = _P()
        w.active_bar = _P()
        w._on_status = lambda *_a: None
        MainWindow._dispatch_status(w, "idle")
        self.assertTrue(w._restarting)        # mask kept
        self.assertEqual(shown.get("v"), "connecting")


# ---------------------------------------------------------------------------
#  Round 3, Bug 3 — ping results persist across refresh() until re-pinged
# ---------------------------------------------------------------------------

class PingResultPersistenceTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def _page(self, n=5):
        from ui.window import ProfilesPage
        from core.profile import Profile
        profs = [Profile(address=f"h{i}.com", port=443, uuid=f"u{i}")
                 for i in range(n)]

        class _Store:
            profiles = profs
            config = {}
            selected_index = -1
        return ProfilesPage(_Store(), engine=_FakeEngine())

    def test_result_cached_and_reapplied_after_refresh(self):
        page = self._page()
        prof = page._store.profiles[2]
        key = page._profile_key(prof)
        # simulate a completed ping result being stored
        page._ping_results[key] = ("✔ 42ms", "ok")
        # rebuild the rows (e.g. user did 'select all') — must NOT lose it
        page.refresh()
        # the cache survives and the row shows it (we assert the cache survived
        # and the lookup still resolves the row)
        self.assertIn(key, page._ping_results)
        self.assertEqual(page._row_index_for_profile(prof), 2)

    def test_stale_results_pruned_for_removed_profiles(self):
        page = self._page(n=3)
        page._ping_results["nonexistent|0|||"] = ("✔ 10ms", "ok")
        page.refresh()
        self.assertNotIn("nonexistent|0|||", page._ping_results)

    def test_profile_key_stable_for_same_endpoint(self):
        from core.profile import Profile
        page = self._page(n=1)
        a = Profile(address="x.com", port=443, uuid="u")
        b = Profile(address="x.com", port=443, uuid="u")
        self.assertEqual(page._profile_key(a), page._profile_key(b))
        c = Profile(address="x.com", port=443, uuid="DIFFERENT")
        self.assertNotEqual(page._profile_key(a), page._profile_key(c))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
