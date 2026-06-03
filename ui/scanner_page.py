"""Standalone **«اسکن IP تمیز»** page — a full SenPaiScanner-style workflow.

This replaces the cramped pop-up :class:`ui.scanner_dialog.ScannerDialog` with a
proper in-window section (the user asked for *no* pop-up — pressing the 🔍 button
on a config navigates here instead). It ports every SenPaiScanner setup option:

================  =========================================================
Source            تصادفی · دستی (paste) · از فایل  (آپلود یافته‌های دیگران)
Count             1000 / 5000 / 20000 / 100000 / دلخواه (random source only)
Workers           50 / 100 / 200 / دلخواه
Timeout           2s / 3s / 5s / دلخواه
Ports             چند انتخابی — Config / 443 / 8443 / 2053 / 2083 / 2087 / 2096
Config            کانفیگ مرجع (انتخاب از پروفایل‌ها یا «بدون کانفیگ» = فقط فاز۱)
Top N             10 / 25 / 50 / 100 / همه / دلخواه  (Phase-2 budget)
================  =========================================================

The heavy lifting still lives in :mod:`core.cf_scanner` (UI-agnostic, tested)
and :mod:`core.cf_xray_validator`. This module is the Qt layer: the setup rows,
a worker thread, a live results table, and the copy / export / add actions.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.cf_scanner import (
    CFScanner, IPResult, ScanConfig, scan_config_from_profile, profile_with_ip,
    parse_ip_list, format_endpoints, write_result_file, default_result_filename,
    CF_PORTS, COUNT_PRESETS, WORKER_PRESETS, TIMEOUT_PRESETS, TOPN_PRESETS,
    SOURCE_RANDOM, SOURCE_FILE, MAX_CANDIDATES_HARD, _fmt_speed,
)
from core.cf_xray_validator import XrayValidator, XrayValidation
from core.profile import Profile
from ui.i18n import tr


# ---------------------------------------------------------------------------
#  worker thread (two-phase SenPaiScanner sweep)
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    """Run the SenPaiScanner two-phase sweep on a worker thread.

    Phase 1 (always) sweeps the candidate IPs (random pool **or** an explicit
    list) across every selected port and streams each clean ``IP:port`` via
    ``hit``. Phase 2 (optional) validates the best ``top_n`` hits end-to-end
    through the bundled ``xray.exe`` and re-emits survivors via ``verified``.

    Signals carry the **port** too, so the UI can show / export ``IP:port``.
    """

    # ip, port, latency_ms, detail
    hit = Signal(str, int, float, str)
    # ip, port, proxied_latency_ms, speed_bps
    verified = Signal(str, int, float, float)
    # ip, port — a Phase-1 hit that failed Phase 2
    rejected = Signal(str, int)
    line = Signal(str)
    phase = Signal(str)
    p1_progress = Signal(int, int, int, str, bool)   # tested, total, found, ip, ok
    p2_progress = Signal(int, int, str, str)          # done, total, ip, stage
    done = Signal(int, int)                            # found, tested

    def __init__(self, profile, cfg: ScanConfig, parent=None, *,
                 validate_xray: bool = False,
                 ips: Optional[List[str]] = None,
                 result_path: Optional[str] = None):
        super().__init__(parent)
        self._profile = profile
        self._cfg = cfg
        self._validate_xray = validate_xray
        self._ips = ips
        self._result_path = result_path
        self._scanner: Optional[CFScanner] = None
        self._validator: Optional[XrayValidator] = None
        self._clean: List[IPResult] = []

    def stop(self):
        if self._scanner is not None:
            self._scanner.stop()
        if self._validator is not None:
            self._validator.stop()

    # -- live result-file writer (SenPaiScanner keeps updating ips.txt) ----
    def _flush_result_file(self):
        if self._result_path and self._clean:
            write_result_file(self._result_path, self._clean)

    def _on_hit(self, r: IPResult):
        self._clean.append(r)
        self._flush_result_file()
        self.hit.emit(r.ip, int(getattr(r, "port", 0) or self._cfg.port),
                      r.latency_ms, getattr(r, "detail", "") or "")

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        self._scanner = CFScanner(
            on_log=self.line.emit,
            on_phase=self.phase.emit,
            on_progress=self.p1_progress.emit,
            on_result=self._on_hit,
        )
        try:
            report = self._scanner.scan(self._cfg, ips=self._ips)
        except Exception as exc:
            self.line.emit(tr("خطا در اسکن (فاز ۱): {exc}").format(exc=exc))
            self.done.emit(0, 0)
            return

        clean = report.clean
        # Phase-2 budget: validate only the best ``top_n`` (0 = all).
        top_n = int(getattr(self._cfg, "top_n", 0) or 0)
        to_validate = clean if top_n <= 0 else clean[:top_n]
        clean_ips = [r.ip for r in to_validate]

        if (self._validate_xray and clean_ips
                and not self._scanner._stopping()):
            self.phase.emit("phase2")
            self._validator = XrayValidator(
                self._profile,
                on_log=self.line.emit,
                on_result=self._on_validation,
                on_progress=self.p2_progress.emit,
            )
            if not self._validator.is_available:
                self.line.emit(tr(
                    "هشدار: xray یافت نشد — فاز ۲ (اعتبارسنجی واقعی) نادیده "
                    "گرفته شد؛ فقط نتایج فاز ۱ نمایش داده می‌شود."))
                self.done.emit(len(clean), report.tested)
                return
            self.line.emit(tr(
                "فاز ۲ شروع شد — {n} IP برتر با xray واقعی تست می‌شوند.").format(
                    n=len(clean_ips)))
            try:
                results = self._validator.validate_all(clean_ips,
                                                       concurrency=1)
                passed = sum(1 for r in results if r.success)
                self.done.emit(passed, report.tested)
                return
            except Exception as exc:
                self.line.emit(
                    tr("خطا در اسکن (فاز ۲): {exc}").format(exc=exc))

        self.done.emit(len(clean), report.tested)

    def _on_validation(self, res: "XrayValidation"):  # pragma: no cover - Qt
        port = int(getattr(res, "port", 0) or self._cfg.port)
        if res.success:
            self.verified.emit(res.ip, port, res.latency_ms,
                               res.throughput_bps)
        else:
            self.rejected.emit(res.ip, port)


# ---------------------------------------------------------------------------
#  the page
# ---------------------------------------------------------------------------

# Results-table column indexes.
_COL_CHECK = 0
_COL_ENDPOINT = 1
_COL_LATENCY = 2
_COL_SPEED = 3
_COL_STATUS = 4


class ScannerPage(QWidget):
    """In-window «اسکن IP تمیز» section (replaces the old pop-up dialog).

    Drive it through :meth:`set_profiles` (the available reference configs) and
    optionally :meth:`focus_profile` (pre-select a config — used by the 🔍
    button). When the user adds clean IPs, :attr:`on_add_profiles` is invoked
    with the freshly-built :class:`~core.profile.Profile` list so the host can
    persist them, exactly like the old dialog's ``result_profiles``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ScannerPage")
        self._profiles: List[Profile] = []
        self._worker: Optional[ScanWorker] = None
        self._row_for_ep: dict[str, int] = {}   # "ip:port" -> table row
        self._found = 0
        self._result_path: Optional[str] = None
        self._uploaded_ips: List[str] = []      # parsed from an uploaded file
        # host callback: List[Profile] -> None
        self.on_add_profiles: Optional[Callable[[List[Profile]], None]] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        # --- header ---
        title = QLabel(tr("اسکن IP تمیز کلودفلر"))
        title.setObjectName("PageTitle")
        root.addWidget(title)
        sub = QLabel(tr(
            "IPهای تمیز کلودفلر را برای کانفیگ مرجع پیدا کنید — مثل "
            "SenPaiScanner: فاز ۱ اتصال را پروب می‌کند و فاز ۲ هر IP را با "
            "xray و کانفیگ واقعی شما تست می‌کند. می‌توانید IPهای تمیز خودتان را "
            "هم دستی وارد یا از فایل آپلود کنید."))
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        root.addWidget(self._build_setup_card())
        root.addWidget(self._build_controls())
        root.addWidget(self._build_results(), 1)
        root.addWidget(self._build_log())

        self._refresh_enabled_states()
        self._update_probe_hint()

    # ------------------------------------------------------------------ UI
    def _build_setup_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # --- row: reference config ---
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        r1.addWidget(QLabel(tr("کانفیگ مرجع:")))
        self.cmb_config = QComboBox()
        self.cmb_config.setMinimumWidth(220)
        self.cmb_config.setToolTip(tr(
            "کانفیگی که IPهای تمیز برایش پیدا می‌شود. «بدون کانفیگ» = فقط فاز ۱ "
            "(پروب اتصال استاندارد)."))
        self.cmb_config.currentIndexChanged.connect(self._on_config_changed)
        r1.addWidget(self.cmb_config, 1)
        lay.addLayout(r1)

        # --- row: source ---
        r2 = QHBoxLayout()
        r2.setSpacing(10)
        r2.addWidget(QLabel(tr("منبع IP:")))
        self.cmb_source = QComboBox()
        self.cmb_source.addItem(tr("تصادفی (رنج‌های کلودفلر)"), SOURCE_RANDOM)
        self.cmb_source.addItem(tr("دستی / از فایل (IPهای خودم)"), SOURCE_FILE)
        self.cmb_source.setToolTip(tr(
            "تصادفی: IPهای تصادفی از رنج‌های کلودفلر اسکن می‌شوند. "
            "دستی/فایل: IPهای تمیزی که خودتان دارید (یا یافته‌های دیگران) را "
            "وارد یا آپلود کنید."))
        self.cmb_source.currentIndexChanged.connect(
            self._refresh_enabled_states)
        self.cmb_source.currentIndexChanged.connect(self._update_probe_hint)
        r2.addWidget(self.cmb_source)

        r2.addSpacing(12)
        self.lbl_count = QLabel(tr("تعداد:"))
        r2.addWidget(self.lbl_count)
        self.cmb_count = QComboBox()
        for c in COUNT_PRESETS:
            self.cmb_count.addItem(f"{c:,}", c)
        self.cmb_count.addItem(tr("دلخواه…"), -1)
        self.cmb_count.setCurrentIndex(1)   # default 5,000 (SenPai default)
        self.cmb_count.setToolTip(tr(
            "چند IP تصادفی کلودفلر اسکن شود (در منبع تصادفی). تا "
            f"{MAX_CANDIDATES_HARD:,} پشتیبانی می‌شود."))
        self.cmb_count.currentIndexChanged.connect(self._refresh_enabled_states)
        self.cmb_count.currentIndexChanged.connect(self._update_probe_hint)
        r2.addWidget(self.cmb_count)
        self.spin_count = QSpinBox()
        self.spin_count.setRange(1, MAX_CANDIDATES_HARD)
        self.spin_count.setValue(5000)
        self.spin_count.setSingleStep(1000)
        self.spin_count.setVisible(False)
        self.spin_count.valueChanged.connect(self._update_probe_hint)
        r2.addWidget(self.spin_count)
        r2.addStretch(1)
        lay.addLayout(r2)

        # --- row: workers + timeout ---
        r3 = QHBoxLayout()
        r3.setSpacing(10)
        r3.addWidget(QLabel(tr("تعداد ورکر:")))
        self.cmb_workers = QComboBox()
        for w in WORKER_PRESETS:
            self.cmb_workers.addItem(str(w), w)
        self.cmb_workers.addItem(tr("دلخواه…"), -1)
        self.cmb_workers.setToolTip(tr(
            "تعداد پروب‌های هم‌زمان. بیشتر = سریع‌تر ولی پرفشارتر روی شبکه. "
            "پیش‌فرض ۵۰ روی خطوط محدود امن‌تر است."))
        self.cmb_workers.currentIndexChanged.connect(
            self._refresh_enabled_states)
        r3.addWidget(self.cmb_workers)
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 512)
        self.spin_workers.setValue(50)
        self.spin_workers.setVisible(False)
        r3.addWidget(self.spin_workers)

        r3.addSpacing(12)
        r3.addWidget(QLabel(tr("مهلت هر پروب:")))
        self.cmb_timeout = QComboBox()
        for t in TIMEOUT_PRESETS:
            self.cmb_timeout.addItem(tr("{s:.0f} ثانیه").format(s=t), t)
        self.cmb_timeout.addItem(tr("دلخواه…"), -1.0)
        self.cmb_timeout.setCurrentIndex(2)   # 5s default
        self.cmb_timeout.currentIndexChanged.connect(
            self._refresh_enabled_states)
        r3.addWidget(self.cmb_timeout)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(1, 30)
        self.spin_timeout.setValue(5)
        self.spin_timeout.setSuffix(tr(" ث"))
        self.spin_timeout.setVisible(False)
        r3.addWidget(self.spin_timeout)
        r3.addStretch(1)
        lay.addLayout(r3)

        # --- row: ports (multi-select pills) ---
        r4 = QHBoxLayout()
        r4.setSpacing(8)
        r4.addWidget(QLabel(tr("پورت‌ها:")))
        self.chk_port_config = QCheckBox(tr("پورت کانفیگ"))
        self.chk_port_config.setChecked(True)
        self.chk_port_config.setToolTip(tr(
            "همان پورتی که کانفیگ شما استفاده می‌کند."))
        r4.addWidget(self.chk_port_config)
        self._port_checks: dict[int, QCheckBox] = {}
        for p in CF_PORTS:
            cb = QCheckBox(str(p))
            # Default to the config port ONLY (no extra ports pre-checked) so the
            # probe count equals the IP count — e.g. 5,000 IPs = 5,000 probes,
            # not 10,000. Selecting extra ports multiplies the work; the live
            # hint below makes that explicit.
            cb.toggled.connect(self._update_probe_hint)
            r4.addWidget(cb)
            self._port_checks[p] = cb
        self.chk_port_config.toggled.connect(self._update_probe_hint)
        r4.addStretch(1)
        lay.addLayout(r4)

        # live "N IPs × M ports = K probes" hint so the header count and the
        # actual scan size never disagree (#3).
        self.lbl_probe_hint = QLabel("")
        self.lbl_probe_hint.setObjectName("hint")
        self.lbl_probe_hint.setWordWrap(True)
        lay.addWidget(self.lbl_probe_hint)

        # --- row: Phase-2 toggle + Top N ---
        r5 = QHBoxLayout()
        r5.setSpacing(10)
        self.chk_xray = QCheckBox(tr("اعتبارسنجی واقعی با xray (فاز ۲)"))
        self.chk_xray.setChecked(True)
        self.chk_xray.setToolTip(tr(
            "هر IP تمیز با کانفیگ واقعی شما از طریق xray تست می‌شود — کندتر "
            "ولی دقیق. بدون کانفیگ مرجع غیرفعال است."))
        self.chk_xray.toggled.connect(self._refresh_enabled_states)
        r5.addWidget(self.chk_xray)

        r5.addSpacing(12)
        self.lbl_topn = QLabel(tr("تعداد برتر برای فاز ۲ (Top N):"))
        r5.addWidget(self.lbl_topn)
        self.cmb_topn = QComboBox()
        for n in TOPN_PRESETS:
            self.cmb_topn.addItem(str(n), n)
        self.cmb_topn.addItem(tr("همه"), 0)
        self.cmb_topn.addItem(tr("دلخواه…"), -1)
        self.cmb_topn.setCurrentIndex(1)   # 25
        self.cmb_topn.setToolTip(tr(
            "چند IP برتر فاز ۱ با xray اعتبارسنجی شوند."))
        self.cmb_topn.currentIndexChanged.connect(self._refresh_enabled_states)
        r5.addWidget(self.cmb_topn)
        self.spin_topn = QSpinBox()
        self.spin_topn.setRange(1, 1000)
        self.spin_topn.setValue(25)
        self.spin_topn.setVisible(False)
        r5.addWidget(self.spin_topn)
        r5.addStretch(1)
        lay.addLayout(r5)

        # --- manual / file IP input (shown only for the File source) ---
        self.box_manual = QWidget()
        mlay = QVBoxLayout(self.box_manual)
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.setSpacing(6)
        mhead = QHBoxLayout()
        mhead.addWidget(QLabel(tr("IPهای تمیز شما (یکی در هر خط یا جداشده):")))
        mhead.addStretch(1)
        self.btn_upload = QPushButton(tr("📂  آپلود فایل…"))
        self.btn_upload.setObjectName("Ghost")
        self.btn_upload.clicked.connect(self._on_upload)
        mhead.addWidget(self.btn_upload)
        self.lbl_manual_count = QLabel("")
        self.lbl_manual_count.setObjectName("Muted")
        mhead.addWidget(self.lbl_manual_count)
        mlay.addLayout(mhead)
        self.txt_manual = QPlainTextEdit()
        self.txt_manual.setObjectName("ScanLog")
        self.txt_manual.setMaximumHeight(110)
        self.txt_manual.setPlaceholderText(tr(
            "104.16.1.1\n104.16.1.2:8443\n108.162.0.0/24  (CIDR هم پشتیبانی "
            "می‌شود)\n… یا یک فایل آپلود کنید"))
        self.txt_manual.textChanged.connect(self._on_manual_changed)
        mlay.addWidget(self.txt_manual)
        lay.addWidget(self.box_manual)

        return card

    def _build_controls(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self.btn_scan = QPushButton(tr("\U0001f50d  شروع اسکن"))
        self.btn_scan.setObjectName("Primary")
        self.btn_scan.clicked.connect(self._start)
        self.btn_stop = QPushButton(tr("توقف"))
        self.btn_stop.setObjectName("Ghost")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        ctrl.addWidget(self.btn_scan)
        ctrl.addWidget(self.btn_stop)
        ctrl.addStretch(1)
        self.status = QLabel(tr("آماده"))
        self.status.setObjectName("Muted")
        ctrl.addWidget(self.status)
        lay.addLayout(ctrl)

        self.progress = QProgressBar()
        self.progress.setObjectName("ScanProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setMinimumHeight(22)
        self.progress.setFormat(tr("آماده"))
        lay.addWidget(self.progress)
        return w

    def _build_results(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(QLabel(tr("نتایج (IP:port تمیز):")))
        head.addStretch(1)
        # --- sort by lowest ping (#4) ---
        self.btn_sort_ping = QPushButton(tr("⬆ مرتب‌سازی بر اساس کم‌ترین پینگ"))
        self.btn_sort_ping.setObjectName("Ghost")
        self.btn_sort_ping.setToolTip(tr(
            "نتایج را صعودی بر اساس تأخیر (کم‌ترین پینگ اول) مرتب می‌کند."))
        self.btn_sort_ping.clicked.connect(self._sort_by_latency)
        head.addWidget(self.btn_sort_ping)
        self.chk_autosort = QCheckBox(tr("مرتب‌سازی خودکار"))
        self.chk_autosort.setChecked(True)
        self.chk_autosort.setToolTip(tr(
            "نتایج را همان لحظه که پیدا می‌شوند بر اساس کم‌ترین پینگ مرتب نگه "
            "می‌دارد."))
        head.addWidget(self.chk_autosort)
        self.btn_check_all = QPushButton(tr("انتخاب همه"))
        self.btn_check_all.setObjectName("Ghost")
        self.btn_check_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_check_none = QPushButton(tr("لغو انتخاب"))
        self.btn_check_none.setObjectName("Ghost")
        self.btn_check_none.clicked.connect(
            lambda: self._set_all_checked(False))
        head.addWidget(self.btn_check_all)
        head.addWidget(self.btn_check_none)
        lay.addLayout(head)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("ScanTable")
        self.table.setHorizontalHeaderLabels([
            "", tr("IP:port"), tr("تأخیر"), tr("سرعت"), tr("وضعیت")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(180)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_ENDPOINT, QHeaderView.Stretch)
        hh.setSectionResizeMode(_COL_LATENCY, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_SPEED, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        act = QHBoxLayout()
        act.setSpacing(10)
        self.btn_copy = QPushButton(tr("📋  کپی IP:port"))
        self.btn_copy.setObjectName("Ghost")
        self.btn_copy.clicked.connect(self._copy_endpoints)
        self.btn_export = QPushButton(tr("💾  ذخیره در فایل"))
        self.btn_export.setObjectName("Ghost")
        self.btn_export.clicked.connect(self._export_file)
        act.addWidget(self.btn_copy)
        act.addWidget(self.btn_export)
        act.addStretch(1)
        self.btn_add_selected = QPushButton(tr("افزودن انتخاب‌شده‌ها به کانفیگ‌ها"))
        self.btn_add_selected.setObjectName("Primary")
        self.btn_add_selected.clicked.connect(self._add_selected)
        act.addWidget(self.btn_add_selected)
        lay.addLayout(act)
        return w

    def _build_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        h = QHBoxLayout()
        h.addWidget(QLabel(tr("روند زندهٔ اسکن:")))
        h.addStretch(1)
        self.lbl_progress = QLabel("")
        self.lbl_progress.setObjectName("Muted")
        h.addWidget(self.lbl_progress)
        lay.addLayout(h)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("ScanLog")
        self.log.setMaximumHeight(140)
        self.log.setPlaceholderText(tr("روند اسکن اینجا نمایش داده می‌شود …"))
        lay.addWidget(self.log)
        return w

    # ------------------------------------------------------------ host API
    def set_profiles(self, profiles: List[Profile],
                     *, selected: Optional[Profile] = None):
        """Populate the reference-config combo from the available profiles."""
        self._profiles = list(profiles or [])
        self.cmb_config.blockSignals(True)
        self.cmb_config.clear()
        self.cmb_config.addItem(tr("بدون کانفیگ (فقط فاز ۱)"), None)
        target_idx = 0
        for i, p in enumerate(self._profiles, start=1):
            name = getattr(p, "display_name", "") or tr("کانفیگ {n}").format(n=i)
            self.cmb_config.addItem(name, p)
            if selected is not None and p is selected:
                target_idx = i
        self.cmb_config.setCurrentIndex(target_idx)
        self.cmb_config.blockSignals(False)
        self._on_config_changed()

    def focus_profile(self, profile: Profile):
        """Pre-select *profile* as the reference config (used by 🔍)."""
        for i in range(self.cmb_config.count()):
            if self.cmb_config.itemData(i) is profile:
                self.cmb_config.setCurrentIndex(i)
                return
        # not in the list yet → add and select it
        name = getattr(profile, "display_name", "") or tr("کانفیگ")
        self.cmb_config.addItem(name, profile)
        self.cmb_config.setCurrentIndex(self.cmb_config.count() - 1)

    def current_profile(self) -> Optional[Profile]:
        return self.cmb_config.currentData()

    # ------------------------------------------------------------ helpers
    def _on_config_changed(self):
        has_cfg = self.current_profile() is not None
        # without a reference config there's nothing to validate end-to-end
        if not has_cfg:
            self.chk_xray.setChecked(False)
        self._refresh_enabled_states()
        self._update_probe_hint()

    def _refresh_enabled_states(self, *_):
        is_file = self.cmb_source.currentData() == SOURCE_FILE
        self.box_manual.setVisible(is_file)
        # count only applies to the random source
        self.lbl_count.setEnabled(not is_file)
        self.cmb_count.setEnabled(not is_file)
        self.spin_count.setVisible(self.cmb_count.currentData() == -1
                                   and not is_file)
        self.spin_workers.setVisible(self.cmb_workers.currentData() == -1)
        self.spin_timeout.setVisible(self.cmb_timeout.currentData() == -1.0)

        has_cfg = self.current_profile() is not None
        self.chk_xray.setEnabled(has_cfg)
        xray_on = self.chk_xray.isChecked() and has_cfg
        self.lbl_topn.setEnabled(xray_on)
        self.cmb_topn.setEnabled(xray_on)
        self.spin_topn.setVisible(self.cmb_topn.currentData() == -1)
        self.spin_topn.setEnabled(xray_on)

    def _selected_count(self) -> int:
        v = self.cmb_count.currentData()
        return self.spin_count.value() if v == -1 else int(v)

    def _selected_workers(self) -> int:
        v = self.cmb_workers.currentData()
        return self.spin_workers.value() if v == -1 else int(v)

    def _selected_timeout(self) -> float:
        v = self.cmb_timeout.currentData()
        return float(self.spin_timeout.value()) if v == -1.0 else float(v)

    def _selected_topn(self) -> int:
        v = self.cmb_topn.currentData()
        return self.spin_topn.value() if v == -1 else int(v)

    def _selected_extra_ports(self) -> tuple[int, ...]:
        return tuple(p for p, cb in self._port_checks.items()
                     if cb.isChecked())

    def _effective_ports(self) -> list[int]:
        """The de-duplicated port list a scan would actually probe each IP on.

        Mirrors :meth:`ScanConfig.all_ports`: the config port (when the «پورت
        کانفیگ» pill is checked) first, then any extra CDN ports, with the
        config port removed from the extras so it's never counted twice.
        """
        ports: list[int] = []
        cfg_port = self._config_port()
        if self.chk_port_config.isChecked():
            ports.append(cfg_port)
        for p in self._selected_extra_ports():
            if p not in ports:
                ports.append(p)
        return ports

    def _update_probe_hint(self, *_):
        """Keep the «N IP × M پورت = K پروب» hint in sync with the controls.

        This is what stops the header count (e.g. «۵۰۰۰ تصادفی») and the actual
        scan size from disagreeing: it always shows the real probe total.
        """
        if not hasattr(self, "lbl_probe_hint"):
            return
        is_file = self.cmb_source.currentData() == SOURCE_FILE
        ports = self._effective_ports()
        nports = max(1, len(ports))
        if is_file:
            n_ips = len(parse_ip_list(self.txt_manual.toPlainText()))
            src_txt = tr("از فایل/لیست شما")
        else:
            n_ips = self._selected_count()
            src_txt = tr("تصادفی")
        total = n_ips * nports
        ports_txt = "، ".join(str(p) for p in ports) if ports else "—"
        if nports == 1:
            self.lbl_probe_hint.setText(tr(
                "{n:,} IP {src} روی پورت {ports} ← مجموعاً {total:,} پروب"
            ).format(n=n_ips, src=src_txt, ports=ports_txt, total=total))
        else:
            self.lbl_probe_hint.setText(tr(
                "{n:,} IP {src} × {m} پورت ({ports}) ← مجموعاً {total:,} پروب"
            ).format(n=n_ips, src=src_txt, m=nports, ports=ports_txt,
                     total=total))

    def _config_port(self) -> int:
        prof = self.current_profile()
        if prof is not None:
            return int(getattr(prof, "port", 443) or 443)
        return 443

    # ------------------------------------------------------------ file I/O
    def _on_upload(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("انتخاب فایل IP"), "",
            tr("فایل‌های متنی (*.txt *.csv *.list);;همه فایل‌ها (*)"))
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception as exc:
            QMessageBox.warning(self, tr("خطا"),
                                tr("خواندن فایل ناموفق بود: {e}").format(e=exc))
            return
        existing = self.txt_manual.toPlainText().strip()
        merged = (existing + "\n" + text) if existing else text
        self.txt_manual.setPlainText(merged)
        self._on_line(tr("فایل بارگذاری شد: {p}").format(
            p=os.path.basename(path)))

    def _on_manual_changed(self):
        ips = parse_ip_list(self.txt_manual.toPlainText())
        self._uploaded_ips = ips
        self.lbl_manual_count.setText(
            tr("{n} IP معتبر").format(n=len(ips)) if ips else "")
        self._update_probe_hint()

    # ------------------------------------------------------------ scanning
    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        prof = self.current_profile()
        # A scan is only meaningful against a real config: the clean IP must
        # answer on the config's port/SNI and (for ws configs) carry its WS
        # upgrade. Without one we'd fall back to a generic edge probe that the
        # user can't actually use — so require the user to add/select a config
        # first instead of running a meaningless sweep.
        if prof is None:
            QMessageBox.information(
                self, tr("کانفیگ لازم است"),
                tr("برای اسکن باید یک کانفیگ انتخاب کنید. اگر کانفیگی ندارید، "
                   "ابتدا از بخش «کانفیگ‌ها» یک کانفیگ اضافه کنید، سپس آن را "
                   "از فهرست بالا انتخاب کنید."))
            return
        is_file = self.cmb_source.currentData() == SOURCE_FILE

        ips: Optional[List[str]] = None
        if is_file:
            ips = parse_ip_list(self.txt_manual.toPlainText())
            if not ips:
                QMessageBox.information(
                    self, tr("منبع خالی"),
                    tr("هیچ IP معتبری وارد نشده — IP وارد کنید یا فایل آپلود "
                       "کنید (یا منبع را روی «تصادفی» بگذارید)."))
                return

        extra_ports = self._selected_extra_ports()
        if not self.chk_port_config.isChecked() and not extra_ports:
            QMessageBox.information(
                self, tr("پورت انتخاب نشده"),
                tr("حداقل یک پورت را انتخاب کنید (پورت کانفیگ یا یکی از "
                   "پورت‌های کلودفلر)."))
            return

        # build the ScanConfig from the chosen reference profile. The clean IP
        # must satisfy the config's own port / SNI / (ws) upgrade.
        common = dict(
            timeout=self._selected_timeout(),
            concurrency=self._selected_workers(),
            max_candidates=self._selected_count(),
            max_results=0,           # never stop early — sweep the whole set
            ports=extra_ports,
            source=SOURCE_FILE if is_file else SOURCE_RANDOM,
            top_n=self._selected_topn(),
        )
        cfg = scan_config_from_profile(prof, **common)
        # the "پورت کانفیگ" pill toggles whether the config port is probed
        if not self.chk_port_config.isChecked() and extra_ports:
            cfg.port = extra_ports[0]
            cfg.ports = extra_ports[1:]

        validate_xray = self.chk_xray.isChecked()

        # live result file (SenPaiScanner keeps updating it during the scan)
        self._result_path = os.path.join(os.getcwd(), default_result_filename())

        # reset UI
        self.table.setRowCount(0)
        self.log.clear()
        self._row_for_ep = {}
        self._found = 0
        self.progress.setRange(0, 0)
        self.progress.setFormat(tr("در حال آماده‌سازی …"))
        self._busy(True)
        self.status.setText(tr("فاز ۱ — در حال پروب اتصال …"))

        self._worker = ScanWorker(
            prof, cfg, self, validate_xray=validate_xray, ips=ips,
            result_path=self._result_path)
        self._worker.hit.connect(self._on_hit)
        self._worker.verified.connect(self._on_verified)
        self._worker.rejected.connect(self._on_rejected)
        self._worker.line.connect(self._on_line)
        self._worker.phase.connect(self._on_phase)
        self._worker.p1_progress.connect(self._on_p1_progress)
        self._worker.p2_progress.connect(self._on_p2_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _stop(self):
        if self._worker is not None:
            self._worker.stop()
            self.status.setText(tr("در حال توقف …"))

    def shutdown_scan(self):
        """Stop any running scan (called when leaving the page / closing)."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)

    def _busy(self, busy: bool):
        self.btn_scan.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        for w in (self.cmb_config, self.cmb_source, self.cmb_count,
                  self.spin_count, self.cmb_workers, self.spin_workers,
                  self.cmb_timeout, self.spin_timeout, self.chk_xray,
                  self.cmb_topn, self.spin_topn, self.chk_port_config,
                  self.txt_manual, self.btn_upload):
            w.setEnabled(not busy)
        for cb in self._port_checks.values():
            cb.setEnabled(not busy)
        if not busy:
            self._refresh_enabled_states()

    # ------------------------------------------------------------ table ops
    def _row_endpoint(self, ip: str, port: int) -> str:
        return f"{ip}:{int(port)}"

    def _ensure_row(self, ip: str, port: int) -> int:
        ep = self._row_endpoint(ip, port)
        if ep in self._row_for_ep:
            return self._row_for_ep[ep]
        row = self.table.rowCount()
        self.table.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                     | Qt.ItemIsSelectable)
        chk.setCheckState(Qt.Checked)
        chk.setData(Qt.UserRole, (ip, int(port)))
        self.table.setItem(row, _COL_CHECK, chk)
        self.table.setItem(row, _COL_ENDPOINT, QTableWidgetItem(ep))
        lat_item = QTableWidgetItem("—")
        # numeric latency kept in UserRole (NOT EditRole) so the displayed text
        # ("50ms") is preserved while we still have a clean key to sort on; a
        # missing/unknown ping sorts last via a large sentinel.
        lat_item.setData(Qt.UserRole, 1e12)
        self.table.setItem(row, _COL_LATENCY, lat_item)
        spd_item = QTableWidgetItem("—")
        spd_item.setData(Qt.UserRole, 0.0)
        self.table.setItem(row, _COL_SPEED, spd_item)
        self.table.setItem(row, _COL_STATUS, QTableWidgetItem(tr("فاز ۱")))
        self._row_for_ep[ep] = row
        return row

    def _on_hit(self, ip: str, port: int, latency_ms: float, detail: str = ""):
        self._found += 1
        row = self._ensure_row(ip, port)
        lat_item = self.table.item(row, _COL_LATENCY)
        lat_item.setText(f"{latency_ms:.0f}ms")
        lat_item.setData(Qt.UserRole, float(latency_ms))
        tag = (tr("عالی") if latency_ms < 150 else
               tr("خوب") if latency_ms < 350 else tr("کند"))
        self.table.item(row, _COL_STATUS).setText(tr("تمیز · {t}").format(t=tag))
        if detail:
            self.table.item(row, _COL_ENDPOINT).setToolTip(detail)
        self.lbl_progress.setText(tr("پیداشده: {f}").format(f=self._found))
        if getattr(self, "chk_autosort", None) and self.chk_autosort.isChecked():
            self._sort_by_latency()

    def _on_verified(self, ip: str, port: int, latency_ms: float,
                     speed_bps: float):
        row = self._ensure_row(ip, port)
        lat_item = self.table.item(row, _COL_LATENCY)
        lat_item.setText(f"{latency_ms:.0f}ms")
        lat_item.setData(Qt.UserRole, float(latency_ms))
        spd = _fmt_speed(speed_bps) if speed_bps > 0 else "—"
        spd_item = self.table.item(row, _COL_SPEED)
        spd_item.setText(spd)
        spd_item.setData(Qt.UserRole, float(speed_bps))
        self.table.item(row, _COL_STATUS).setText(tr("✅ تأییدشده"))
        self.table.item(row, _COL_CHECK).setCheckState(Qt.Checked)
        if getattr(self, "chk_autosort", None) and self.chk_autosort.isChecked():
            self._sort_by_latency()

    def _sort_by_latency(self):
        """Reorder rows ascending by ping (best/lowest first) and re-index.

        Done manually (rather than ``QTableWidget.sortItems``) so the displayed
        latency text — e.g. "50ms" — is preserved: the numeric sort key lives in
        the latency item's ``UserRole``. After reordering we rebuild the cached
        endpoint→row map (read from each checkbox item's ``UserRole``) so live
        updates still write to the correct rows.
        """
        n = self.table.rowCount()
        if n < 2:
            return

        # snapshot every row: its sort key + the data needed to rewrite it
        snapshot = []
        for row in range(n):
            chk = self.table.item(row, _COL_CHECK)
            lat = self.table.item(row, _COL_LATENCY)
            key = lat.data(Qt.UserRole) if lat is not None else 1e12
            try:
                key = float(key)
            except (TypeError, ValueError):
                key = 1e12
            cells = []
            for col in range(self.table.columnCount()):
                cells.append(self.table.takeItem(row, col))
            check_state = (cells[_COL_CHECK].checkState()
                           if cells[_COL_CHECK] is not None else Qt.Checked)
            snapshot.append((key, cells, check_state))

        # stable ascending sort by ping (lowest first)
        order = sorted(range(len(snapshot)), key=lambda i: snapshot[i][0])

        new_map: dict[str, int] = {}
        for new_row, idx in enumerate(order):
            _, cells, check_state = snapshot[idx]
            for col, item in enumerate(cells):
                if item is not None:
                    self.table.setItem(new_row, col, item)
            chk = self.table.item(new_row, _COL_CHECK)
            if chk is not None:
                chk.setCheckState(check_state)
                data = chk.data(Qt.UserRole)
                if data:
                    ip, port = data
                    new_map[self._row_endpoint(ip, port)] = new_row
        self._row_for_ep = new_map

    def _on_rejected(self, ip: str, port: int):
        ep = self._row_endpoint(ip, port)
        row = self._row_for_ep.get(ep)
        if row is not None:
            self.table.item(row, _COL_STATUS).setText(tr("⚠️ رد شد (فاز ۲)"))
            self.table.item(row, _COL_CHECK).setCheckState(Qt.Unchecked)

    def _on_phase(self, name: str):
        if name == "phase1":
            self.status.setText(tr("فاز ۱ — در حال پروب اتصال …"))
        elif name == "phase2":
            self.status.setText(
                tr("فاز ۲ — اعتبارسنجی واقعی با xray …"))
            self.progress.setRange(0, 0)
            self.progress.setFormat(tr("فاز ۲ — آماده‌سازی xray …"))

    def _on_p1_progress(self, tested: int, total: int, found: int,
                        last_ip: str, last_ok: bool):
        if total > 0:
            if self.progress.maximum() != total:
                self.progress.setRange(0, total)
            self.progress.setValue(tested)
            pct = int(tested * 100 / total) if total else 0
            self.progress.setFormat(tr(
                "فاز ۱: {done}/{total} ({pct}%) · {found} تمیز").format(
                    done=tested, total=total, pct=pct, found=found))
        self.lbl_progress.setText(tr(
            "تست‌شده: {done}/{total}  ·  تمیز: {found}").format(
                done=tested, total=total, found=found))

    def _on_p2_progress(self, done: int, total: int, ip: str, stage: str):
        if total > 0 and self.progress.maximum() != total:
            self.progress.setRange(0, total)
        if total > 0:
            self.progress.setValue(done)
            pct = int(done * 100 / total) if total else 0
            if stage == "start":
                self.progress.setFormat(tr(
                    "فاز ۲: تست {ip} ({done}/{total} · {pct}%)").format(
                        ip=ip, done=done + 1, total=total, pct=pct))
            else:
                self.progress.setFormat(tr(
                    "فاز ۲: {done}/{total} ({pct}%)").format(
                        done=done, total=total, pct=pct))

    def _on_line(self, text: str):
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum())

    def _on_done(self, found: int, tested: int):
        self._busy(False)
        mx = self.progress.maximum()
        if mx <= 0:
            self.progress.setRange(0, 1)
            mx = 1
        self.progress.setValue(mx)
        self.progress.setFormat(tr("پایان — {f} IP تمیز").format(f=found))
        self.status.setText(tr(
            "تمام شد — {found} IP تمیز از {tested} پروب").format(
                found=found, tested=tested))
        if self._result_path:
            self._on_line(tr("نتایج در فایل ذخیره شد: {p}").format(
                p=self._result_path))
        if found == 0:
            self.status.setText(tr(
                "تمام شد — هیچ IP تمیزی پیدا نشد. تعداد را بیشتر کنید یا "
                "پورت‌های دیگری امتحان کنید."))

    # ------------------------------------------------------------ selection
    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            it = self.table.item(row, _COL_CHECK)
            if it is not None:
                it.setCheckState(state)

    def _checked_endpoints(self) -> List[tuple[str, int]]:
        out = []
        for row in range(self.table.rowCount()):
            it = self.table.item(row, _COL_CHECK)
            if it is not None and it.checkState() == Qt.Checked:
                data = it.data(Qt.UserRole)
                if data:
                    out.append((str(data[0]), int(data[1])))
        return out

    def _all_endpoints(self) -> List[tuple[str, int]]:
        out = []
        for row in range(self.table.rowCount()):
            it = self.table.item(row, _COL_CHECK)
            if it is not None:
                data = it.data(Qt.UserRole)
                if data:
                    out.append((str(data[0]), int(data[1])))
        return out

    # ------------------------------------------------------------ actions
    def _copy_endpoints(self):
        eps = self._checked_endpoints() or self._all_endpoints()
        if not eps:
            self.status.setText(tr("نتیجه‌ای برای کپی نیست"))
            return
        text = "\n".join(f"{ip}:{port}" for ip, port in eps)
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self.status.setText(tr("{n} IP:port در کلیپ‌بورد کپی شد").format(
            n=len(eps)))

    def _export_file(self):
        eps = self._checked_endpoints() or self._all_endpoints()
        if not eps:
            self.status.setText(tr("نتیجه‌ای برای ذخیره نیست"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("ذخیرهٔ نتایج"), default_result_filename(),
            tr("فایل متنی (*.txt)"))
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(f"{ip}:{port}" for ip, port in eps) + "\n")
            self.status.setText(tr("ذخیره شد: {p}").format(
                p=os.path.basename(path)))
        except Exception as exc:
            QMessageBox.warning(self, tr("خطا"),
                                tr("ذخیره ناموفق بود: {e}").format(e=exc))

    def _add_selected(self):
        prof = self.current_profile()
        if prof is None:
            QMessageBox.information(
                self, tr("کانفیگ مرجع لازم است"),
                tr("برای ساختن کانفیگ از IP تمیز، ابتدا یک کانفیگ مرجع "
                   "انتخاب کنید."))
            return
        eps = self._checked_endpoints()
        if not eps:
            self.status.setText(tr("هیچ IPی انتخاب نشده است"))
            return
        # de-duplicate by IP (one config per clean IP, on its best port)
        seen: set[str] = set()
        profiles: List[Profile] = []
        for ip, port in eps:
            if ip in seen:
                continue
            seen.add(ip)
            profiles.append(profile_with_ip(prof, ip, suffix=f"CF {ip}:{port}"))
        if self.on_add_profiles is not None:
            self.on_add_profiles(profiles)
        self.status.setText(tr("{n} کانفیگ از IPهای تمیز ساخته شد").format(
            n=len(profiles)))
