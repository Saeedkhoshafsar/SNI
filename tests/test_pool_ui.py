"""UI tests for the multi-IP / multi-SNI route-pool surface.

Covers:
  * SettingsPage — the CONNECT_IPS / FAKE_SNIS textareas (load_from / collect,
    parsing, dedupe, the live "how many routes" hint).
  * PoolPage — the live status renderer for both the disabled (single-route)
    and enabled (multi-route) snapshots.

Headless Qt (offscreen); skipped automatically when PySide6 is unavailable.
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


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class SettingsPagePoolTest(unittest.TestCase):
    def _page(self):
        from ui.window import SettingsPage
        return SettingsPage()

    def test_pool_lists_roundtrip(self):
        page = self._page()
        page.load_from({
            "CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com",
            "CONNECT_IPS": ["9.9.9.9", "8.8.8.8"],
            "FAKE_SNIS": ["x.com", "y.com"],
        })
        out = page.collect()
        self.assertEqual(out["CONNECT_IPS"], ["9.9.9.9", "8.8.8.8"])
        self.assertEqual(out["FAKE_SNIS"], ["x.com", "y.com"])

    def test_empty_pool_lists_collect_empty(self):
        page = self._page()
        page.load_from({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        out = page.collect()
        self.assertEqual(out["CONNECT_IPS"], [])
        self.assertEqual(out["FAKE_SNIS"], [])

    def test_parse_lines_strips_and_dedupes(self):
        page = self._page()
        page.pool_ips.setPlainText(" 1.1.1.1 \n1.1.1.1\n\n2.2.2.2\n")
        self.assertEqual(page._pool_ip_list(), ["1.1.1.1", "2.2.2.2"])

    def test_hint_disabled_for_single_pair(self):
        # redesign: a single defined route is no longer "disabled" — we connect
        # with it first; the hint invites adding more for background testing.
        page = self._page()
        page.load_from({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        self.assertIn("تنها یک مسیر", page.pool_hint.text())

    def test_hint_optimize_off(self):
        # redesign: when the optimiser checkbox is off, the hint says testing is
        # off regardless of how many routes are defined.
        page = self._page()
        page.pool_ips.setPlainText("1.1.1.1\n2.2.2.2")
        page.pool_snis.setPlainText("a.com\nb.com")
        page.chk_pool_optimize.setChecked(False)
        page._update_pool_hint()
        self.assertIn("خاموش", page.pool_hint.text())

    def test_hint_enabled_and_counts_pairs(self):
        page = self._page()
        page.pool_ips.setPlainText("1.1.1.1\n2.2.2.2")
        page.pool_snis.setPlainText("a.com\nb.com\nc.com")
        page._update_pool_hint()
        # 2 × 3 = 6 routes
        self.assertIn("6", page.pool_hint.text())
        self.assertIn("فعال", page.pool_hint.text())

    def test_hint_uses_singular_fallback_for_count(self):
        # one pool IP + empty SNI list ⇒ falls back to single SNI ⇒ 1 route ⇒
        # "single route" hint (connect first; add more to test in background).
        page = self._page()
        page.pool_ips.setPlainText("1.1.1.1")
        page.pool_snis.setPlainText("")
        page._update_pool_hint()
        self.assertIn("تنها یک مسیر", page.pool_hint.text())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class PoolPageTest(unittest.TestCase):
    def _page(self):
        from ui.window import PoolPage
        return PoolPage()

    def test_no_provider_renders_disabled(self):
        page = self._page()
        page.refresh()
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_disabled_snapshot(self):
        page = self._page()
        page.set_provider(lambda: {"enabled": False})
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_enabled_snapshot_counts_and_check_age(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 6, "known": 4, "stable": 3,
            "weak": 1, "dead": 0, "unexplored": 2, "active": 2,
            "seconds_since_check": 12.0, "rows": [],
        })
        self.assertIn("فعال", page.lbl_state.text())
        self.assertIn("6", page.lbl_counts.text())
        self.assertIn("12", page.lbl_check.text())

    def test_enabled_snapshot_renders_rows(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 2, "known": 2, "stable": 1,
            "weak": 0, "dead": 1, "unexplored": 0, "active": 1,
            "seconds_since_check": 5.0,
            "rows": [
                {"ip": "9.9.9.9", "sni": "x.com", "loss": 0.05,
                 "alive": True, "active": 1, "in_pool": True},
                {"ip": "8.8.8.8", "sni": "y.com", "loss": 1.0,
                 "alive": False, "active": 0, "in_pool": False},
            ],
        })
        text = page.tbl.toPlainText()
        self.assertIn("9.9.9.9", text)
        self.assertIn("8.8.8.8", text)
        self.assertIn("★", text)        # active pair marker
        self.assertIn("مرده", text)     # dead pair state

    def test_seconds_none_shows_bootstrapping(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 4, "rows": [],
            "seconds_since_check": None,
        })
        self.assertIn("راه‌اندازی", page.lbl_check.text())

    def test_provider_exception_is_swallowed(self):
        page = self._page()

        def boom():
            raise RuntimeError("nope")

        page.set_provider(boom)        # must not raise
        self.assertIn("غیرفعال", page.lbl_state.text())

    def test_polling_start_stop(self):
        page = self._page()
        page.set_provider(lambda: {"enabled": False})
        page.start_polling()
        self.assertTrue(page._timer.isActive())
        page.stop_polling()
        self.assertFalse(page._timer.isActive())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class EngineBridgePoolSummaryTest(unittest.TestCase):
    def _bridge(self):
        from ui.engine_bridge import EngineBridge
        from core.engine import EngineController
        return EngineBridge(EngineController({}))

    def test_single_target_config_disabled(self):
        b = self._bridge()
        snap = b.pool_summary({"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"})
        self.assertFalse(snap.get("enabled"))

    def test_multi_target_config_enabled_static(self):
        b = self._bridge()
        snap = b.pool_summary({
            "CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com", "b.com"],
        })
        self.assertTrue(snap.get("enabled"))
        self.assertEqual(snap.get("total"), 4)

    def test_live_manager_summary_preferred(self):
        from core.pool import build_connection_manager
        b = self._bridge()
        mgr = build_connection_manager(
            {"CONNECT_IPS": ["1.1.1.1", "2.2.2.2"], "FAKE_SNIS": ["a.com"]},
            probe_fn=lambda ip, port, t: True,
        )
        # mark one pair probed/in-pool so the live summary differs from static
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        b.controller.conn_manager = mgr
        snap = b.pool_summary({})
        self.assertTrue(snap.get("enabled"))
        self.assertEqual(snap.get("total"), 2)
        self.assertGreaterEqual(snap.get("known", 0), 1)
        self.assertGreaterEqual(snap.get("active", 0), 1)

    def test_live_summary_includes_failover(self):
        from core.pool import ConnectionManager, FAILOVER_THRESHOLD
        from ui.engine_bridge import EngineBridge
        from core.engine import EngineController
        b = EngineBridge(EngineController({}))
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = ConnectionManager(combos, 443,
                                probe_fn=lambda ip, p, to: True)
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        for _ in range(FAILOVER_THRESHOLD):
            mgr.tracker.record_failure("1.1.1.1")
        b.controller.conn_manager = mgr
        snap = b.pool_summary({})
        self.assertIn("failover", snap)
        self.assertIn("1.1.1.1", snap.get("blocked_ips", []))


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class PoolPageFailoverExportTest(unittest.TestCase):
    def _page(self):
        from ui.window import PoolPage
        return PoolPage()

    def test_failover_line_lists_blocked(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 2, "known": 2, "stable": 1, "weak": 1,
            "dead": 0, "unexplored": 0, "active": 1, "seconds_since_check": 3,
            "rows": [{"ip": "1.1.1.1", "sni": "a.com", "loss": 0.0,
                      "alive": True, "active": 1, "in_pool": True}],
            "blocked_ips": ["9.9.9.9"],
        })
        self.assertIn("9.9.9.9", page.lbl_failover.text())

    def test_failover_line_all_healthy(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 2, "known": 2, "active": 1,
            "seconds_since_check": 1, "rows": [], "blocked_ips": [],
        })
        self.assertIn("سالم", page.lbl_failover.text())

    def test_export_button_disabled_when_pool_off(self):
        page = self._page()
        page.set_provider(lambda: {"enabled": False})
        self.assertFalse(page.btn_export.isEnabled())

    def test_export_button_enabled_when_pool_on(self):
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 1, "rows": [
                {"ip": "1.1.1.1", "sni": "a.com", "loss": 0.0,
                 "alive": True, "active": 0, "in_pool": True}],
        })
        self.assertTrue(page.btn_export.isEnabled())

    def test_export_writes_snis(self):
        import tempfile
        from unittest import mock
        page = self._page()
        page.set_provider(lambda: {
            "enabled": True, "total": 2, "rows": [
                {"ip": "1.1.1.1", "sni": "a.com", "loss": 0.0, "alive": True,
                 "active": 0, "in_pool": True},
                {"ip": "2.2.2.2", "sni": "b.com", "loss": 0.0, "alive": True,
                 "active": 0, "in_pool": True}],
        })
        path = os.path.join(tempfile.mkdtemp(), "out.txt")
        with mock.patch("ui.window.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("ui.window.QMessageBox.information"):
            page._on_export()
        body = open(path, encoding="utf-8").read()
        self.assertIn("a.com", body)
        self.assertIn("b.com", body)

    def test_export_no_snis_shows_info(self):
        from unittest import mock
        page = self._page()
        page.set_provider(lambda: {"enabled": True, "total": 0, "rows": []})
        with mock.patch("ui.window.QMessageBox.information") as info, \
             mock.patch("ui.window.QFileDialog.getSaveFileName") as save:
            page._on_export()
            info.assert_called_once()
            save.assert_not_called()


# ---------------------------------------------------------------------------
#  Inline SNI/IP scan (embedded in PoolPage — replaces the old dialog).
# ---------------------------------------------------------------------------

class _FakeSpoofProfile:
    is_spoof_config = True
    spoof_connect_ip = "127.0.0.1"
    spoof_connect_port = 40443
    spoof_fake_sni = "www.example.com"
    display_name = "Spoof Test Config"


class _FakePlainProfile:
    is_spoof_config = False
    display_name = "Plain Config"


class _FakeStore:
    def __init__(self, cfg, profiles):
        self.config = cfg
        self.profiles = profiles
        self.saved = 0

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value

    def save_config(self):
        self.saved += 1


def _scan_store(n_ips=40, n_snis=40, pairs=None):
    cfg = {
        "CONNECT_IPS": ["10.0.0.%d" % i for i in range(n_ips)],
        "FAKE_SNIS": ["sni%d.example.com" % i for i in range(n_snis)],
        "sni_ip_pairs": list(pairs or []),
        "probe_timeout": 2.0,
    }
    return _FakeStore(cfg, [_FakePlainProfile(), _FakeSpoofProfile()])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class PoolPageInlineScanTest(unittest.TestCase):
    def _page(self, store):
        from ui.window import PoolPage
        page = PoolPage()
        page.set_store(store)
        return page

    def test_toggle_shows_panel_and_lists_only_spoof_configs(self):
        page = self._page(_scan_store())
        # the page itself isn't shown in the headless test, so isVisible() is
        # always False; check the explicit hidden flag instead.
        self.assertTrue(page.scan_card.isHidden())
        page._toggle_scan_panel()
        self.assertFalse(page.scan_card.isHidden())
        datas = [page.scan_cmb.itemData(i)
                 for i in range(page.scan_cmb.count())]
        spoof = [d for d in datas if d is not None]
        self.assertEqual(len(spoof), 1)          # plain profile filtered out
        self.assertTrue(page.scan_btn_run.isEnabled())

    def test_no_spoof_config_disables_run(self):
        store = _FakeStore(
            {"CONNECT_IPS": ["1.1.1.1"], "FAKE_SNIS": ["a.com"],
             "sni_ip_pairs": []}, [_FakePlainProfile()])
        page = self._page(store)
        page._toggle_scan_panel()
        self.assertFalse(page.scan_btn_run.isEnabled())

    def test_candidates_capped(self):
        page = self._page(_scan_store(40, 40))
        page._toggle_scan_panel()
        prof = next(d for d in (page.scan_cmb.itemData(i)
                                for i in range(page.scan_cmb.count()))
                    if d is not None)
        cands = page._scan_candidate_pairs(prof)
        self.assertLessEqual(len(cands), page.MAX_CANDIDATES)
        self.assertEqual(len(cands), page.MAX_CANDIDATES)

    def test_seed_is_text_only_and_fast(self):
        import time
        import core.pool as pool
        page = self._page(_scan_store(40, 40))
        page._toggle_scan_panel()
        orig = pool.spoof_handshake_probe
        pool.spoof_handshake_probe = lambda ip, port, t, sni: False
        try:
            t0 = time.monotonic()
            page._scan_start()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self.assertGreater(page.scan_tbl.rowCount(), 0)
            self.assertLessEqual(page.scan_tbl.rowCount(), page.MAX_CANDIDATES)
            # NO per-row cell widgets (that froze the GUI in the old design)
            for r in range(page.scan_tbl.rowCount()):
                for c in range(page.scan_tbl.columnCount()):
                    self.assertIsNone(page.scan_tbl.cellWidget(r, c))
                    self.assertIsNotNone(page.scan_tbl.item(r, c))
            self.assertLess(elapsed_ms, 1500.0)
        finally:
            page.shutdown_scan()
            pool.spoof_handshake_probe = orig

    def _seed_rows(self, page, rows):
        from PySide6.QtWidgets import QTableWidgetItem
        page.scan_tbl.setRowCount(len(rows))
        page._row_for_key.clear()
        for i, (ip, sni, ok) in enumerate(rows):
            page.scan_tbl.setItem(i, page.SC_IP, QTableWidgetItem(ip))
            page.scan_tbl.setItem(i, page.SC_SNI, QTableWidgetItem(sni))
            page.scan_tbl.setItem(i, page.SC_LAT, QTableWidgetItem("10ms"))
            st = page._STATUS_FA["ok"] if ok else page._STATUS_FA["fail"]
            from ui.i18n import tr
            page.scan_tbl.setItem(i, page.SC_STATUS, QTableWidgetItem(tr(st)))
            page.scan_tbl.setItem(i, page.SC_SAVED, QTableWidgetItem(""))
            page._row_for_key[page._scan_key(ip, sni)] = i

    def test_add_all_ok_persists_only_healthy(self):
        store = _scan_store(pairs=[])
        page = self._page(store)
        page._toggle_scan_panel()
        self._seed_rows(page, [
            ("1.1.1.1", "good1.com", True),
            ("2.2.2.2", "bad.com", False),
            ("3.3.3.3", "good2.com", True),
        ])
        page._scan_add_all_ok()
        snis = {p["sni"] for p in store.get("sni_ip_pairs")}
        self.assertEqual(snis, {"good1.com", "good2.com"})
        self.assertGreaterEqual(store.saved, 1)

    def test_add_selected_then_remove_roundtrip(self):
        store = _scan_store(pairs=[])
        page = self._page(store)
        page._toggle_scan_panel()
        self._seed_rows(page, [
            ("1.1.1.1", "a.com", True),
            ("2.2.2.2", "b.com", False),
        ])
        page.scan_tbl.selectAll()
        page._scan_add_selected()
        self.assertEqual(len(store.get("sni_ip_pairs")), 2)
        page.scan_tbl.selectAll()
        page._scan_remove_selected()
        self.assertEqual(len(store.get("sni_ip_pairs")), 0)

    def test_add_is_idempotent(self):
        store = _scan_store(pairs=[{"ip": "1.1.1.1", "sni": "a.com"}])
        page = self._page(store)
        page._toggle_scan_panel()
        self._seed_rows(page, [("1.1.1.1", "a.com", True)])
        page.scan_tbl.selectAll()
        page._scan_add_selected()
        self.assertEqual(len(store.get("sni_ip_pairs")), 1)

    def test_pairs_changed_callback_fires_on_add(self):
        store = _scan_store(pairs=[])
        page = self._page(store)
        fired = []
        page.pairs_changed = lambda: fired.append(1)
        page._toggle_scan_panel()
        self._seed_rows(page, [("1.1.1.1", "a.com", True)])
        page.scan_tbl.selectAll()
        page._scan_add_selected()
        self.assertEqual(fired, [1])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class PoolPageImportTest(unittest.TestCase):
    def _page(self, store):
        from ui.window import PoolPage
        page = PoolPage()
        page.set_store(store)
        return page

    def _write(self, text):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "imp.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_import_merges_new_pairs_and_refreshes(self):
        from unittest import mock
        store = _scan_store(pairs=[{"ip": "1.1.1.1", "sni": "a.com"}])
        page = self._page(store)
        fired = []
        page.pairs_changed = lambda: fired.append(1)
        path = self._write("1.1.1.1\ta.com\n2.2.2.2\tb.com\n")
        with mock.patch("ui.window.QFileDialog.getOpenFileName",
                        return_value=(path, "")), \
             mock.patch("ui.window.QMessageBox.information") as info:
            page._on_import()
        # only b.com is new
        snis = {p["sni"] for p in store.get("sni_ip_pairs")}
        self.assertEqual(snis, {"a.com", "b.com"})
        self.assertGreaterEqual(store.saved, 1)
        self.assertEqual(fired, [1])
        info.assert_called_once()

    def test_import_nothing_new_shows_info_and_no_save(self):
        from unittest import mock
        store = _scan_store(pairs=[{"ip": "1.1.1.1", "sni": "a.com"}])
        page = self._page(store)
        path = self._write("1.1.1.1\tA.COM\n")        # dup (case-insensitive)
        with mock.patch("ui.window.QFileDialog.getOpenFileName",
                        return_value=(path, "")), \
             mock.patch("ui.window.QMessageBox.information") as info:
            page._on_import()
        self.assertEqual(store.saved, 0)
        self.assertEqual(len(store.get("sni_ip_pairs")), 1)
        info.assert_called_once()

    def test_import_cancelled_does_nothing(self):
        from unittest import mock
        store = _scan_store(pairs=[])
        page = self._page(store)
        with mock.patch("ui.window.QFileDialog.getOpenFileName",
                        return_value=("", "")):
            page._on_import()
        self.assertEqual(len(store.get("sni_ip_pairs")), 0)
        self.assertEqual(store.saved, 0)


if __name__ == "__main__":
    unittest.main()
