"""Manual SNI/IP scan dialog ("شروع تست").

The user's redesign (see the issue thread):

  * Previously the pool tested **per spoof config, from scratch**, every time a
    spoof config connected — wasteful, since a route that survives DPI for one
    config works for ~90 % of the others.
  * Now there is a single **manual** scan. The user clicks "شروع تست" next to
    the pool IPs, **picks ONE spoof config** (the ``127.0.0.1:40443`` links) as
    the reference, and we sweep every candidate ``(IP, SNI)`` pair **once**,
    streaming each ✓/✗ verdict live.
  * The scan **never swaps the live route**. Instead the user selects good rows
    (or "add all healthy") and appends them to their reusable ``sni_ip_pairs``
    list (the same list shown in Settings), so they can pick it later. Nothing
    is auto-applied — the user stays in control.

Performance / responsiveness notes (this is what fixes the "0 % + frozen
mouse + crash on click" hang):

  * The probe sweep runs entirely on a worker :class:`QThread`; the GUI thread
    only receives **queued signals**, so it never blocks.
  * The results table uses **plain text cells only** — no per-row QWidgets /
    buttons. Building thousands of button widgets on the GUI thread was what
    froze the window before a single probe ran. Add/remove is driven by two
    cheap buttons under the table that act on the selected rows.
  * The candidate set is capped so a huge IP×SNI product can't seed tens of
    thousands of rows in one go.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QHeaderView, QAbstractItemView,
)

from core.pool import SniIpScanner, build_scan_candidates, export_sni_pairs
from ui.i18n import tr

# Hard cap on how many (IP, SNI) pairs a single scan will queue, so a large
# CONNECT_IPS × FAKE_SNIS product can't seed an unbounded table / sweep.
MAX_CANDIDATES = 600


class _ScanWorker(QThread):
    """Run :class:`SniIpScanner` on a worker thread, marshalling callbacks.

    The scanner's callbacks are wired to this QThread's **signals**, so every
    update reaches the GUI as a queued cross-thread signal (safe) — the GUI
    thread is never touched directly from a probe worker.
    """

    result = Signal(dict)          # one candidate verdict (live)
    progress = Signal(int, int)    # done, total
    finished_scan = Signal(int, int)  # ok_count, total
    log = Signal(str)

    def __init__(self, candidates: List[Tuple[str, str]], *, port: int,
                 timeout: float, parent=None):
        # NOTE: no parent — a QThread parented to a widget can inherit the GUI
        # thread's affinity in some teardown paths; keep it standalone.
        super().__init__()
        self._scanner: Optional[SniIpScanner] = None
        self._candidates = candidates
        self._port = port
        self._timeout = timeout

    def stop(self):
        if self._scanner is not None:
            self._scanner.stop()

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        self._scanner = SniIpScanner(
            self._candidates,
            port=self._port,
            timeout=self._timeout,
            workers=16,
            on_result=self.result.emit,
            on_progress=self.progress.emit,
            on_done=self.finished_scan.emit,
            on_log=self.log.emit,
        )
        try:
            self._scanner.run()
        except Exception as exc:  # never let the thread crash the GUI
            try:
                self.log.emit(tr("خطا در آزمایش: {e}").format(e=exc))
            except Exception:
                pass
            self.finished_scan.emit(0, len(self._candidates))


class SniScanDialog(QDialog):
    """Pick a spoof config, sweep (IP, SNI) pairs, add good ones to the list."""

    _STATUS_FA = {
        "pending": "در صف",
        "testing": "در حال آزمایش…",
        "ok": "✓ سالم",
        "fail": "✗ ناموفق",
    }

    # column indices
    C_IP, C_SNI, C_LAT, C_STATUS, C_SAVED = range(5)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._worker: Optional[_ScanWorker] = None
        self._row_for_key: dict = {}        # (ip,sni) -> table row index
        self._existing: set = {
            (str(p.get("sni", "")).strip().lower(),
             str(p.get("ip", "")).strip().lower())
            for p in (store.get("sni_ip_pairs", []) or [])
        }

        self.setObjectName("SniScanDialog")
        self.setWindowTitle(tr("آزمایش جفت‌های SNI/IP"))
        self.setMinimumSize(680, 620)
        self.setWindowFlags(
            Qt.Dialog
            | Qt.CustomizeWindowHint
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowCloseButtonHint
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        intro = QLabel(tr(
            "یک کانفیگ اسپوف (۱۲۷.۰.۰.۱:۴۰۴۴۳) را به‌عنوان مرجع انتخاب کنید، "
            "سپس «شروع تست» را بزنید. همهٔ جفت‌های IP/SNI یک‌بار آزمایش می‌شوند "
            "(برای همهٔ کانفیگ‌ها مشترک است). مسیر فعال جابه‌جا نمی‌شود — "
            "ردیف‌های سالم را انتخاب کنید و با دکمه‌های پایین به فهرست SNI/IP خود "
            "اضافه کنید تا بعداً در «تنظیمات» انتخابشان کنید."))
        intro.setObjectName("Faint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # --- config picker row -------------------------------------------
        pick = QHBoxLayout()
        pick.setSpacing(8)
        pick.addWidget(QLabel(tr("کانفیگ اسپوف:")))
        self.cmb_config = QComboBox()
        self.cmb_config.setObjectName("ConfigPicker")
        pick.addWidget(self.cmb_config, 1)
        self.btn_start = QPushButton(tr("شروع تست"))
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._on_start)
        pick.addWidget(self.btn_start)
        self.btn_stop = QPushButton(tr("توقف"))
        self.btn_stop.setObjectName("Ghost")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        pick.addWidget(self.btn_stop)
        root.addLayout(pick)

        self._populate_configs()

        # --- progress -----------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        # --- results table (TEXT ONLY — no per-row widgets) ---------------
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(
            [tr("IP"), tr("SNI"), tr("تأخیر"), tr("وضعیت"), tr("در فهرست؟")])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        # let the user multi-select healthy rows to add them
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(self.C_IP, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.C_SNI, QHeaderView.Stretch)
        hh.setSectionResizeMode(self.C_LAT, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.C_STATUS, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.C_SAVED, QHeaderView.ResizeToContents)
        root.addWidget(self.tbl, 1)

        # --- add/remove action row (cheap, operate on the selection) ------
        act = QHBoxLayout()
        act.setSpacing(8)
        self.btn_add_sel = QPushButton(tr("افزودن انتخاب‌شده‌ها به فهرست"))
        self.btn_add_sel.setObjectName("Ghost")
        self.btn_add_sel.clicked.connect(self._add_selected)
        act.addWidget(self.btn_add_sel)
        self.btn_add_ok = QPushButton(tr("افزودن همهٔ سالم‌ها"))
        self.btn_add_ok.setObjectName("Ghost")
        self.btn_add_ok.clicked.connect(self._add_all_ok)
        act.addWidget(self.btn_add_ok)
        self.btn_rem_sel = QPushButton(tr("حذف انتخاب‌شده‌ها از فهرست"))
        self.btn_rem_sel.setObjectName("Ghost")
        self.btn_rem_sel.clicked.connect(self._remove_selected)
        act.addWidget(self.btn_rem_sel)
        act.addStretch(1)
        root.addLayout(act)

        # --- footer -------------------------------------------------------
        foot = QHBoxLayout()
        self.lbl_status = QLabel(tr("آماده"))
        self.lbl_status.setObjectName("Faint")
        foot.addWidget(self.lbl_status, 1)
        self.btn_export = QPushButton(tr("خروجی فهرست…"))
        self.btn_export.setObjectName("Ghost")
        self.btn_export.clicked.connect(self._on_export)
        self.btn_export.setEnabled(False)
        foot.addWidget(self.btn_export)
        self.btn_close = QPushButton(tr("بستن"))
        self.btn_close.setObjectName("Ghost")
        self.btn_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_close)
        root.addLayout(foot)

    # ------------------------------------------------------------------
    # config picker
    # ------------------------------------------------------------------
    def _spoof_profiles(self) -> List:
        out = []
        for prof in getattr(self._store, "profiles", []) or []:
            try:
                if bool(getattr(prof, "is_spoof_config", False)):
                    out.append(prof)
            except Exception:
                continue
        return out

    def _populate_configs(self) -> None:
        self.cmb_config.clear()
        profs = self._spoof_profiles()
        if not profs:
            self.cmb_config.addItem(
                tr("هیچ کانفیگ اسپوفی یافت نشد (۱۲۷.۰.۰.۱:۴۰۴۴۳)"), None)
            self.cmb_config.setEnabled(False)
            self.btn_start.setEnabled(False)
            return
        self.cmb_config.setEnabled(True)
        self.btn_start.setEnabled(True)
        for prof in profs:
            name = getattr(prof, "display_name", "") or tr("کانفیگ")
            self.cmb_config.addItem(name, prof)

    # ------------------------------------------------------------------
    # candidate building
    # ------------------------------------------------------------------
    def _candidate_pairs(self, profile) -> List[Tuple[str, str]]:
        cfg = self._store.config
        extra: List[Tuple[str, str]] = []
        for p in (cfg.get("sni_ip_pairs", []) or []):
            ip = str(p.get("ip", "")).strip()
            sni = str(p.get("sni", "")).strip()
            if ip and sni:
                extra.append((ip, sni))

        ips = list(cfg.get("CONNECT_IPS", []) or [])
        snis = list(cfg.get("FAKE_SNIS", []) or [])
        if cfg.get("CONNECT_IP"):
            ips.append(str(cfg["CONNECT_IP"]))
        if cfg.get("FAKE_SNI"):
            snis.append(str(cfg["FAKE_SNI"]))
        try:
            if getattr(profile, "spoof_connect_ip", ""):
                ips.append(str(profile.spoof_connect_ip))
            if getattr(profile, "spoof_fake_sni", ""):
                snis.append(str(profile.spoof_fake_sni))
        except Exception:
            pass
        cands = build_scan_candidates(ips, snis, extra_pairs=extra)
        # cap so a huge product can't freeze the table / sweep
        if len(cands) > MAX_CANDIDATES:
            cands = cands[:MAX_CANDIDATES]
        return cands

    def _scan_port(self, profile) -> int:
        try:
            return int(getattr(profile, "spoof_connect_port", 0) or 443)
        except Exception:
            return 443

    # ------------------------------------------------------------------
    # scan lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        profile = self.cmb_config.currentData()
        if profile is None:
            QMessageBox.information(
                self, tr("آزمایش"),
                tr("هیچ کانفیگ اسپوفی برای آزمایش انتخاب نشده است."))
            return
        candidates = self._candidate_pairs(profile)
        if not candidates:
            QMessageBox.information(
                self, tr("آزمایش"),
                tr("هیچ جفت IP/SNI برای آزمایش وجود ندارد. در «تنظیمات» "
                   "IP و SNI اضافه کنید."))
            return

        # reset table + seed rows (text-only is cheap; we also disable sorting/
        # updates while bulk-inserting so the GUI never stalls).
        self.tbl.setSortingEnabled(False)
        self.tbl.setUpdatesEnabled(False)
        self.tbl.setRowCount(0)
        self._row_for_key.clear()
        self.tbl.setRowCount(len(candidates))
        for i, (ip, sni) in enumerate(candidates):
            self.tbl.setItem(i, self.C_IP, QTableWidgetItem(ip))
            self.tbl.setItem(i, self.C_SNI, QTableWidgetItem(sni))
            self.tbl.setItem(i, self.C_LAT, QTableWidgetItem("—"))
            self.tbl.setItem(i, self.C_STATUS,
                             QTableWidgetItem(tr(self._STATUS_FA["pending"])))
            saved = self._key(ip, sni) in self._existing
            self.tbl.setItem(i, self.C_SAVED,
                             QTableWidgetItem("✓" if saved else ""))
            self._row_for_key[self._key(ip, sni)] = i
        self.tbl.setUpdatesEnabled(True)
        self.progress.setRange(0, len(candidates))
        self.progress.setValue(0)

        timeout = min(float(self._store.get("probe_timeout", 3.0) or 3.0), 3.0)
        port = self._scan_port(profile)
        self._worker = _ScanWorker(candidates, port=port, timeout=timeout)
        self._worker.result.connect(self._on_result)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_scan.connect(self._on_finished)
        self._worker.log.connect(self._on_log)
        self.btn_start.setEnabled(False)
        self.cmb_config.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText(
            tr("در حال آزمایش {n} جفت…").format(n=len(candidates)))
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText(tr("در حال توقف…"))

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_finished(self, ok: int, total: int) -> None:
        self.btn_start.setEnabled(True)
        self.cmb_config.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_export.setEnabled(self.tbl.rowCount() > 0)
        self.lbl_status.setText(
            tr("پایان — {ok} از {total} جفت سالم بود.").format(
                ok=ok, total=total))

    def _on_log(self, _text: str) -> None:
        pass

    # ------------------------------------------------------------------
    # table rendering (text only)
    # ------------------------------------------------------------------
    def _key(self, ip: str, sni: str) -> tuple:
        return (str(ip).strip().lower(), str(sni).strip().lower())

    def _on_result(self, cand: dict) -> None:
        ip = str(cand.get("ip", "")).strip()
        sni = str(cand.get("sni", "")).strip()
        status = str(cand.get("status", "pending"))
        latency = cand.get("latency_ms")
        row = self._row_for_key.get(self._key(ip, sni))
        if row is None:
            return
        lat_item = self.tbl.item(row, self.C_LAT)
        if lat_item is not None:
            lat_item.setText("—" if latency is None
                             else "%.0fms" % float(latency))
        st_item = self.tbl.item(row, self.C_STATUS)
        if st_item is not None:
            st_item.setText(tr(self._STATUS_FA.get(status, status)))
            if status == "ok":
                st_item.setForeground(QColor("#3ddc97"))
            elif status == "fail":
                st_item.setForeground(QColor("#ff6b6b"))

    # ------------------------------------------------------------------
    # add / remove to the saved sni_ip_pairs list
    # ------------------------------------------------------------------
    def _selected_rows(self) -> List[int]:
        return sorted({idx.row() for idx in self.tbl.selectedIndexes()})

    def _row_pair(self, row: int) -> Tuple[str, str]:
        ip = self.tbl.item(row, self.C_IP)
        sni = self.tbl.item(row, self.C_SNI)
        return (ip.text() if ip else "", sni.text() if sni else "")

    def _row_status_ok(self, row: int) -> bool:
        st = self.tbl.item(row, self.C_STATUS)
        return st is not None and st.text() == tr(self._STATUS_FA["ok"])

    def _persist_pairs(self, pairs) -> None:
        self._store.set("sni_ip_pairs", pairs)
        try:
            self._store.save_config()
        except Exception:
            pass

    def _add_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.lbl_status.setText(tr("هیچ ردیفی انتخاب نشده است."))
            return
        added = self._add_rows(rows, ok_only=False)
        self.lbl_status.setText(tr("{n} جفت به فهرست افزوده شد.").format(n=added))

    def _add_all_ok(self) -> None:
        rows = [r for r in range(self.tbl.rowCount()) if self._row_status_ok(r)]
        if not rows:
            self.lbl_status.setText(tr("هیچ جفت سالمی برای افزودن نیست."))
            return
        added = self._add_rows(rows, ok_only=True)
        self.lbl_status.setText(
            tr("{n} جفت سالم به فهرست افزوده شد.").format(n=added))

    def _add_rows(self, rows: List[int], *, ok_only: bool) -> int:
        pairs = list(self._store.get("sni_ip_pairs", []) or [])
        existing_keys = {self._key(p.get("ip", ""), p.get("sni", ""))
                         for p in pairs}
        added = 0
        for row in rows:
            if ok_only and not self._row_status_ok(row):
                continue
            ip, sni = self._row_pair(row)
            if not ip or not sni:
                continue
            key = self._key(ip, sni)
            if key in existing_keys:
                continue
            pairs.append({"sni": sni, "ip": ip})
            existing_keys.add(key)
            self._existing.add(key)
            saved_item = self.tbl.item(row, self.C_SAVED)
            if saved_item is not None:
                saved_item.setText("✓")
            added += 1
        if added:
            self._persist_pairs(pairs)
        return added

    def _remove_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.lbl_status.setText(tr("هیچ ردیفی انتخاب نشده است."))
            return
        remove_keys = {self._key(*self._row_pair(r)) for r in rows}
        pairs = [
            p for p in (self._store.get("sni_ip_pairs", []) or [])
            if self._key(p.get("ip", ""), p.get("sni", "")) not in remove_keys
        ]
        self._persist_pairs(pairs)
        for r in rows:
            self._existing.discard(self._key(*self._row_pair(r)))
            saved_item = self.tbl.item(r, self.C_SAVED)
            if saved_item is not None:
                saved_item.setText("")
        self.lbl_status.setText(
            tr("{n} جفت از فهرست حذف شد.").format(n=len(rows)))

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def _on_export(self) -> None:
        rows: List[Tuple[str, str, str]] = []
        for r in range(self.tbl.rowCount()):
            ip, sni = self._row_pair(r)
            status = self.tbl.item(r, self.C_STATUS)
            rows.append((ip, sni, status.text() if status else ""))
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("ذخیرهٔ فهرست SNI/IP"), "sni_ip_list.txt",
            tr("فایل متنی (*.txt)"))
        if not path:
            return
        try:
            n = export_sni_pairs(rows, path)
            QMessageBox.information(
                self, tr("خروجی"),
                tr("{n} مورد ذخیره شد.").format(n=n))
        except Exception as exc:
            QMessageBox.warning(
                self, tr("خروجی"),
                tr("ذخیره ناموفق بود: {e}").format(e=exc))

    # ------------------------------------------------------------------
    def closeEvent(self, ev):  # pragma: no cover - Qt
        # Closing must be instant even mid-scan. Signal stop (the scanner breaks
        # out of its drain loop at once) and give the QThread a short grace
        # window. Probe sockets run on daemon threads, so we never block the GUI
        # waiting for a stuck connect.
        w = self._worker
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
            try:
                if w.isRunning():
                    w.wait(800)
            except Exception:
                pass
        super().closeEvent(ev)

    def showEvent(self, ev):  # pragma: no cover - Qt
        super().showEvent(ev)
        try:
            scr = QGuiApplication.screenAt(self.pos()) or \
                QGuiApplication.primaryScreen()
            if scr is not None:
                geo = scr.availableGeometry()
                self.move(geo.center() - self.rect().center())
        except Exception:
            pass
