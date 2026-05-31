"""Cloudflare clean-IP scanner dialog (issue #3).

Opened from a profile row's 🔍 button. It takes that profile as the *reference*
config, sweeps a pool of Cloudflare edge IPs (validating a real TLS handshake
with the config's SNI on the config's port), and streams the clean IPs into a
checkable list. The user then:

  * picks one / several / all clean IPs, and
  * clicks **افزودن** → the dialog builds new profiles identical to the
    reference config except their server address is the chosen clean IP, and
    hands them back to the caller to store.

The heavy lifting lives in :mod:`core.cf_scanner` (UI-agnostic, testable). This
file is the thin Qt layer: a worker thread + a results table + the add buttons.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from core.cf_scanner import (
    CFScanner, IPResult, ScanConfig, scan_config_from_profile, profile_with_ip,
)
from core.profile import Profile
from ui.i18n import tr


class ScanWorker(QThread):
    """Run the Cloudflare sweep on a worker thread (keeps the GUI responsive).

    Emits ``hit(ip, latency_ms)`` for each clean IP found, ``line(text)`` for
    progress, and ``done(found, tested)`` once finished. Cancellable via
    :meth:`stop`.
    """

    hit = Signal(str, float)
    line = Signal(str)
    done = Signal(int, int)

    def __init__(self, profile, cfg: ScanConfig, parent=None):
        super().__init__(parent)
        self._profile = profile
        self._cfg = cfg
        self._scanner: Optional[CFScanner] = None

    def stop(self):
        if self._scanner is not None:
            self._scanner.stop()

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        self._scanner = CFScanner(
            on_log=self.line.emit,
            on_result=lambda r: self.hit.emit(r.ip, r.latency_ms),
        )
        try:
            report = self._scanner.scan(self._cfg)
            self.done.emit(len(report.clean), report.tested)
        except Exception as exc:
            self.line.emit(tr("خطا در اسکن: {exc}").format(exc=exc))
            self.done.emit(0, 0)


class ScannerDialog(QDialog):
    """Scan clean Cloudflare IPs for a reference config and build new configs."""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self._profile = profile
        self._worker: Optional[ScanWorker] = None
        # profiles the user accepted (read by the caller after exec())
        self.result_profiles: List[Profile] = []

        self.setWindowTitle(tr("اسکن IP تمیز کلودفلر"))
        self.setMinimumSize(560, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        name = getattr(profile, "display_name", "") or tr("کانفیگ")
        sni = (getattr(profile, "sni", "") or getattr(profile, "host", "")
               or getattr(profile, "address", ""))
        port = int(getattr(profile, "port", 443) or 443)
        head = QLabel(tr(
            "اسکن IPهای تمیز کلودفلر با کانفیگ مرجع: «{name}»\n"
            "IPهایی که با این کانفیگ (SNI: {sni}، پورت: {port}) دست‌دادن TLS "
            "موفق بدهند، تمیز شمرده می‌شوند.").format(
                name=name, sni=sni or "—", port=port))
        head.setObjectName("Muted")
        head.setWordWrap(True)
        root.addWidget(head)

        # --- scan tunables ---
        opts = QHBoxLayout()
        opts.setSpacing(10)
        opts.addWidget(QLabel(tr("تعداد IP برای تست:")))
        self.spin_count = QSpinBox()
        self.spin_count.setRange(20, 5000)
        self.spin_count.setValue(400)
        self.spin_count.setSingleStep(50)
        opts.addWidget(self.spin_count)
        opts.addWidget(QLabel(tr("سقف نتایج:")))
        self.spin_results = QSpinBox()
        self.spin_results.setRange(1, 200)
        self.spin_results.setValue(20)
        opts.addWidget(self.spin_results)
        opts.addWidget(QLabel(tr("هم‌زمانی:")))
        self.spin_conc = QSpinBox()
        self.spin_conc.setRange(1, 256)
        self.spin_conc.setValue(64)
        opts.addWidget(self.spin_conc)
        opts.addStretch(1)
        root.addLayout(opts)

        # --- start/stop ---
        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self.btn_scan = QPushButton(tr("\U0001f50d  شروع اسکن"))
        self.btn_scan.setObjectName("Primary")
        self.btn_stop = QPushButton(tr("توقف"))
        self.btn_stop.setObjectName("Ghost")
        self.btn_stop.setEnabled(False)
        ctrl.addWidget(self.btn_scan)
        ctrl.addWidget(self.btn_stop)
        ctrl.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        ctrl.addWidget(self.status)
        root.addLayout(ctrl)

        # --- results list (checkable) ---
        root.addWidget(QLabel(tr("IPهای تمیز پیداشده (تیک بزنید):")))
        self.list = QListWidget()
        self.list.setObjectName("ScanList")
        self.list.setMinimumHeight(180)
        root.addWidget(self.list, 1)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(10)
        self.btn_check_all = QPushButton(tr("انتخاب همه"))
        self.btn_check_all.setObjectName("Ghost")
        self.btn_check_none = QPushButton(tr("لغو انتخاب"))
        self.btn_check_none.setObjectName("Ghost")
        sel_row.addWidget(self.btn_check_all)
        sel_row.addWidget(self.btn_check_none)
        sel_row.addStretch(1)
        root.addLayout(sel_row)

        # --- progress log ---
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setObjectName("ScanLog")
        self.log.setPlaceholderText(tr("روند اسکن اینجا نمایش داده می‌شود …"))
        root.addWidget(self.log)

        # --- add / cancel ---
        act = QHBoxLayout()
        act.addStretch(1)
        self.btn_add_selected = QPushButton(tr("افزودن انتخاب‌شده‌ها"))
        self.btn_add_selected.setObjectName("Primary")
        self.btn_add_all = QPushButton(tr("افزودن همه"))
        self.btn_add_all.setObjectName("Ghost")
        self.btn_close = QPushButton(tr("بستن"))
        self.btn_close.setObjectName("Ghost")
        act.addWidget(self.btn_add_selected)
        act.addWidget(self.btn_add_all)
        act.addWidget(self.btn_close)
        root.addLayout(act)

        # wiring
        self.btn_scan.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_check_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_check_none.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_add_selected.clicked.connect(self._add_selected)
        self.btn_add_all.clicked.connect(self._add_all)
        self.btn_close.clicked.connect(self.reject)

    # -- scan lifecycle ----------------------------------------------------
    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.list.clear()
        self.log.clear()
        cfg = scan_config_from_profile(
            self._profile,
            max_candidates=self.spin_count.value(),
            max_results=self.spin_results.value(),
            concurrency=self.spin_conc.value(),
        )
        self._busy(True)
        self.status.setText(tr("در حال اسکن …"))
        self._worker = ScanWorker(self._profile, cfg, self)
        self._worker.hit.connect(self._on_hit)
        self._worker.line.connect(self._on_line)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _stop(self):
        if self._worker is not None:
            self._worker.stop()
            self.status.setText(tr("در حال توقف …"))

    def _busy(self, busy: bool):
        self.btn_scan.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.spin_count.setEnabled(not busy)
        self.spin_results.setEnabled(not busy)
        self.spin_conc.setEnabled(not busy)

    def _on_hit(self, ip: str, latency_ms: float):
        item = QListWidgetItem(self.list)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        item.setText(tr("{ip}   ·   {ms:.0f}ms").format(ip=ip, ms=latency_ms))
        item.setData(Qt.UserRole, ip)
        self.list.addItem(item)

    def _on_line(self, text: str):
        self.log.appendPlainText(text)

    def _on_done(self, found: int, tested: int):
        self._busy(False)
        self.status.setText(
            tr("تمام شد — {found} IP تمیز از {tested} آزمایش‌شده").format(
                found=found, tested=tested))

    # -- selection helpers -------------------------------------------------
    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _checked_ips(self) -> List[str]:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                ip = it.data(Qt.UserRole)
                if ip:
                    out.append(str(ip))
        return out

    def _all_ips(self) -> List[str]:
        return [str(self.list.item(i).data(Qt.UserRole))
                for i in range(self.list.count())
                if self.list.item(i).data(Qt.UserRole)]

    # -- accept ------------------------------------------------------------
    def _build_and_accept(self, ips: List[str]):
        if not ips:
            self.status.setText(tr("هیچ IPی انتخاب نشده است"))
            return
        self.result_profiles = [profile_with_ip(self._profile, ip)
                                for ip in ips]
        self.accept()

    def _add_selected(self):
        self._build_and_accept(self._checked_ips())

    def _add_all(self):
        self._build_and_accept(self._all_ips())

    # ensure the worker is stopped if the dialog is closed mid-scan
    def reject(self):  # noqa: D401
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        super().reject()
