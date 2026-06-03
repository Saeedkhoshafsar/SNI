"""Headless regression tests for the manual "شروع تست" scan dialog.

These lock in the fixes for the "0% + frozen mouse + crash on click" hang the
user reported. The root causes were:

  1. Emitting Qt signals from raw probe worker threads (undefined behaviour).
  2. Building a QWidget + two QPushButtons **per candidate row** on the GUI
     thread while seeding hundreds/thousands of rows — this froze the window
     before a single probe ran.

So we assert the invariants that prevent regressions:

  * The candidate set is capped at ``MAX_CANDIDATES`` so a huge IP×SNI product
    can't seed an unbounded table.
  * The results table holds **plain ``QTableWidgetItem`` cells only** — no
    per-row cell widgets.
  * Seeding the table on click (``_on_start``) is cheap and does not block.
  * add/remove operate on the selection and persist to ``sni_ip_pairs``.

Skipped where Qt is unavailable.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    import core.pool as pool
    from ui.sni_scan_dialog import SniScanDialog, MAX_CANDIDATES
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


class _FakeProfile:
    is_spoof_config = True
    spoof_connect_ip = "127.0.0.1"
    spoof_connect_port = 40443
    spoof_fake_sni = "www.example.com"
    display_name = "Spoof Test Config"


class _PlainProfile:
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


def _make_store(n_ips=40, n_snis=40, pairs=None):
    cfg = {
        "CONNECT_IPS": ["10.0.0.%d" % i for i in range(n_ips)],
        "FAKE_SNIS": ["sni%d.example.com" % i for i in range(n_snis)],
        "sni_ip_pairs": list(pairs or []),
        "probe_timeout": 2.0,
    }
    return _FakeStore(cfg, [_PlainProfile(), _FakeProfile()])


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScanDialogConfigPickerTest(unittest.TestCase):
    def test_only_spoof_configs_are_listed(self):
        store = _make_store()
        dlg = SniScanDialog(store)
        # one spoof profile -> exactly one selectable entry (the plain one is
        # filtered out) and start is enabled.
        datas = [dlg.cmb_config.itemData(i)
                 for i in range(dlg.cmb_config.count())]
        spoof = [d for d in datas if d is not None]
        self.assertEqual(len(spoof), 1)
        self.assertTrue(dlg.btn_start.isEnabled())

    def test_no_spoof_config_disables_start(self):
        store = _FakeStore(
            {"CONNECT_IPS": ["1.1.1.1"], "FAKE_SNIS": ["a.com"],
             "sni_ip_pairs": []},
            [_PlainProfile()])
        dlg = SniScanDialog(store)
        self.assertFalse(dlg.btn_start.isEnabled())


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScanDialogCandidateCapTest(unittest.TestCase):
    def test_candidates_capped(self):
        # 40 x 40 = 1600 product, must be clamped to MAX_CANDIDATES
        store = _make_store(40, 40)
        dlg = SniScanDialog(store)
        prof = next(d for d in (dlg.cmb_config.itemData(i)
                                for i in range(dlg.cmb_config.count()))
                    if d is not None)
        cands = dlg._candidate_pairs(prof)
        self.assertLessEqual(len(cands), MAX_CANDIDATES)
        self.assertEqual(len(cands), MAX_CANDIDATES)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScanDialogSeedTest(unittest.TestCase):
    """Seeding the table on click must be cheap and use TEXT-ONLY cells."""

    def setUp(self):
        # make the worker thread a no-op so we only measure GUI-thread seeding
        self._orig = pool.spoof_handshake_probe
        pool.spoof_handshake_probe = lambda ip, port, t, sni: False

    def tearDown(self):
        pool.spoof_handshake_probe = self._orig

    def test_seed_is_text_only_and_fast(self):
        store = _make_store(40, 40)
        dlg = SniScanDialog(store)
        t0 = time.monotonic()
        dlg._on_start()
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        # rows seeded, capped
        self.assertGreater(dlg.tbl.rowCount(), 0)
        self.assertLessEqual(dlg.tbl.rowCount(), MAX_CANDIDATES)

        # NO per-row cell widgets anywhere (this is what used to freeze the GUI)
        for r in range(dlg.tbl.rowCount()):
            for c in range(dlg.tbl.columnCount()):
                self.assertIsNone(
                    dlg.tbl.cellWidget(r, c),
                    "row %d col %d unexpectedly has a cell widget" % (r, c))
                self.assertIsNotNone(
                    dlg.tbl.item(r, c),
                    "row %d col %d missing a text item" % (r, c))

        # seeding hundreds of text rows is cheap; generous bound for CI
        self.assertLess(elapsed_ms, 1500.0,
                        "seeding blocked the GUI for %dms" % elapsed_ms)

        # stop the worker so the test doesn't leak a running thread
        if dlg._worker is not None:
            dlg._worker.stop()
            dlg._worker.wait(1500)


@unittest.skipUnless(_HAVE_QT, "PySide6 not available")
class ScanDialogAddRemoveTest(unittest.TestCase):
    """add/remove act on rows and persist to sni_ip_pairs."""

    def _seed(self, store, statuses):
        """Build a dialog with rows manually marked OK/FAIL (no worker)."""
        from PySide6.QtWidgets import QTableWidgetItem
        dlg = SniScanDialog(store)
        dlg.tbl.setRowCount(len(statuses))
        dlg._row_for_key.clear()
        for i, (ip, sni, ok) in enumerate(statuses):
            dlg.tbl.setItem(i, dlg.C_IP, QTableWidgetItem(ip))
            dlg.tbl.setItem(i, dlg.C_SNI, QTableWidgetItem(sni))
            dlg.tbl.setItem(i, dlg.C_LAT, QTableWidgetItem("10ms"))
            st = dlg._STATUS_FA["ok"] if ok else dlg._STATUS_FA["fail"]
            dlg.tbl.setItem(i, dlg.C_STATUS, QTableWidgetItem(st))
            dlg.tbl.setItem(i, dlg.C_SAVED, QTableWidgetItem(""))
            dlg._row_for_key[dlg._key(ip, sni)] = i
        return dlg

    def test_add_all_ok_persists_only_healthy(self):
        store = _make_store(pairs=[])
        dlg = self._seed(store, [
            ("1.1.1.1", "good1.com", True),
            ("2.2.2.2", "bad.com", False),
            ("3.3.3.3", "good2.com", True),
        ])
        dlg._add_all_ok()
        pairs = store.get("sni_ip_pairs")
        snis = {p["sni"] for p in pairs}
        self.assertEqual(snis, {"good1.com", "good2.com"})
        self.assertGreaterEqual(store.saved, 1)

    def test_add_selected_then_remove_roundtrip(self):
        store = _make_store(pairs=[])
        dlg = self._seed(store, [
            ("1.1.1.1", "a.com", True),
            ("2.2.2.2", "b.com", False),
        ])
        # select both rows and add (selection-based, ignores status)
        dlg.tbl.selectAll()
        dlg._add_selected()
        self.assertEqual(len(store.get("sni_ip_pairs")), 2)

        # now remove the selection
        dlg.tbl.selectAll()
        dlg._remove_selected()
        self.assertEqual(len(store.get("sni_ip_pairs")), 0)

    def test_add_is_idempotent(self):
        store = _make_store(pairs=[{"ip": "1.1.1.1", "sni": "a.com"}])
        dlg = self._seed(store, [("1.1.1.1", "a.com", True)])
        dlg.tbl.selectAll()
        dlg._add_selected()
        # already present -> not duplicated
        self.assertEqual(len(store.get("sni_ip_pairs")), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
