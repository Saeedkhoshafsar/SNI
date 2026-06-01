"""Cloudflare clean-IP scanner dialog (issue #3).

Opened from a profile row's 🔍 button. It takes that profile as the *reference*
config, sweeps a pool of Cloudflare edge IPs and — mirroring
``MatinSenPai/SenPaiScanner`` — validates each one with a **real HTTP edge
check** (``/cdn-cgi/trace``) plus, for WebSocket configs, a **WS upgrade** on
the config's Host + path. Only IPs that pass these honest checks are streamed
into the checkable list (a bare TLS handshake is *not* enough — that produced
the dozens of bogus "clean" IPs the user saw appear in seconds). The user then:

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
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from core.cf_scanner import (
    CFScanner, IPResult, ScanConfig, scan_config_from_profile, profile_with_ip,
    _fmt_speed,
)
from core.cf_xray_validator import XrayValidator, XrayValidation
from core.profile import Profile
from ui.i18n import tr


class ScanWorker(QThread):
    """Run the SenPaiScanner two-phase sweep on a worker thread.

    **Phase 1** (always) — the :class:`CFScanner` connectivity probe sweeps the
    Cloudflare IP pool and streams every clean IP via ``hit``.

    **Phase 2** (optional, when ``validate_xray`` is set) — the surviving clean
    IPs are then validated *end-to-end* through the bundled ``xray.exe`` running
    the user's real config; each IP that carries real traffic is re-emitted via
    ``verified`` with its proxied latency + download speed.

    Signals
    -------
    hit(ip, latency_ms, detail)        — a Phase-1 clean IP (with colo/speed in
                                          ``detail``).
    verified(ip, latency_ms, speed_bps)— a Phase-2 xray-validated IP.
    rejected(ip)                       — a Phase-1 IP that failed Phase 2.
    line(text)                         — progress log line.
    phase(name)                        — "phase1" / "phase2" transition.
    done(found, tested)                — finished (found = final clean count).
    """

    hit = Signal(str, float, str)        # ip, latency_ms, detail
    verified = Signal(str, float, float)  # ip, proxied_latency_ms, speed_bps
    rejected = Signal(str)               # ip that failed Phase 2
    line = Signal(str)
    phase = Signal(str)
    done = Signal(int, int)

    def __init__(self, profile, cfg: ScanConfig, parent=None,
                 *, validate_xray: bool = False):
        super().__init__(parent)
        self._profile = profile
        self._cfg = cfg
        self._validate_xray = validate_xray
        self._scanner: Optional[CFScanner] = None
        self._validator: Optional[XrayValidator] = None

    def stop(self):
        if self._scanner is not None:
            self._scanner.stop()
        if self._validator is not None:
            self._validator.stop()

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        self._scanner = CFScanner(
            on_log=self.line.emit,
            on_phase=self.phase.emit,
            on_result=lambda r: self.hit.emit(
                r.ip, r.latency_ms, getattr(r, "detail", "") or ""),
        )
        try:
            report = self._scanner.scan(self._cfg)
        except Exception as exc:
            self.line.emit(tr("خطا در اسکن (فاز ۱): {exc}").format(exc=exc))
            self.done.emit(0, 0)
            return

        clean_ips = [r.ip for r in report.clean]

        # --- Phase 2 — real xray end-to-end validation (optional) ---
        if self._validate_xray and clean_ips and not self._scanner._stopping():
            self.phase.emit("phase2")
            self._validator = XrayValidator(
                self._profile,
                on_log=self.line.emit,
                on_result=self._on_validation,
            )
            if not self._validator.is_available:
                self.line.emit(tr(
                    "هشدار: xray.exe یافت نشد — فاز ۲ (اعتبارسنجی واقعی) "
                    "نادیده گرفته شد؛ فقط نتایج فاز ۱ نمایش داده می‌شود."))
                self.done.emit(len(clean_ips), report.tested)
                return
            try:
                results = self._validator.validate_all(clean_ips,
                                                        concurrency=1)
                passed = sum(1 for r in results if r.success)
                self.done.emit(passed, report.tested)
                return
            except Exception as exc:
                self.line.emit(
                    tr("خطا در اسکن (فاز ۲): {exc}").format(exc=exc))

        self.done.emit(len(clean_ips), report.tested)

    def _on_validation(self, res: "XrayValidation"):  # pragma: no cover - Qt
        if res.success:
            self.verified.emit(res.ip, res.latency_ms, res.throughput_bps)
        else:
            self.rejected.emit(res.ip)


class ScannerDialog(QDialog):
    """Scan clean Cloudflare IPs for a reference config and build new configs."""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self._profile = profile
        self._worker: Optional[ScanWorker] = None
        # profiles the user accepted (read by the caller after exec())
        self.result_profiles: List[Profile] = []

        self.setObjectName("ScannerDialog")
        self.setWindowTitle(tr("اسکن IP تمیز کلودفلر"))
        self.setMinimumSize(620, 640)
        # The main window is frameless with a CUSTOM title bar, so a child
        # QDialog inherited an awkward geometry and opened with its (native)
        # title bar pushed off the top of the screen — the user couldn't grab it
        # to move the window (bug #4). Force a normal, OS-decorated, movable
        # dialog window and centre it on the active screen in showEvent() so the
        # title bar is always reachable.
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

        name = getattr(profile, "display_name", "") or tr("کانفیگ")
        sni = (getattr(profile, "sni", "") or getattr(profile, "host", "")
               or getattr(profile, "address", ""))
        port = int(getattr(profile, "port", 443) or 443)
        transport = (getattr(profile, "transport", "") or "tcp").lower()
        is_ws = transport in ("ws", "websocket", "httpupgrade")
        ws_note = (tr("  ·  بررسی WebSocket روی مسیر کانفیگ فعال است")
                   if is_ws else "")
        head = QLabel(tr(
            "اسکن IPهای تمیز کلودفلر با کانفیگ مرجع: «{name}»\n"
            "هر IP با یک درخواست واقعی HTTP به لبهٔ کلودفلر (و برای کانفیگ‌های "
            "WebSocket، یک upgrade واقعی) اعتبارسنجی می‌شود — فقط IPهایی که "
            "واقعاً برای این کانفیگ (SNI: {sni}، پورت: {port}) کار می‌کنند "
            "تمیز شمرده می‌شوند.{ws}").format(
                name=name, sni=sni or "—", port=port, ws=ws_note))
        head.setObjectName("Muted")
        head.setWordWrap(True)
        root.addWidget(head)

        # --- scan tunables ---
        # NOTE: every control gets its own label + tooltip so the user can find
        # the worker/thread count at a glance (previous feedback: "تعداد ورکر
        # پیدا نمیشه"). The worker count IS the «تعداد ورکر» field below.
        opts = QHBoxLayout()
        opts.setSpacing(10)

        lbl_count = QLabel(tr("تعداد IP برای تست:"))
        opts.addWidget(lbl_count)
        self.spin_count = QSpinBox()
        self.spin_count.setRange(20, 5000)
        self.spin_count.setValue(400)
        self.spin_count.setSingleStep(50)
        self.spin_count.setToolTip(
            tr("چند IP کلودفلر آزمایش شود (بیشتر = شانس بیشتر، اما کندتر)"))
        opts.addWidget(self.spin_count)

        lbl_results = QLabel(tr("سقف نتایج:"))
        opts.addWidget(lbl_results)
        self.spin_results = QSpinBox()
        self.spin_results.setRange(1, 200)
        self.spin_results.setValue(20)
        self.spin_results.setToolTip(
            tr("به محض پیدا شدن این تعداد IP تمیز، اسکن متوقف می‌شود"))
        opts.addWidget(self.spin_results)

        # the worker / thread count — labelled explicitly as «تعداد ورکر» so it
        # is impossible to miss, with a clear tooltip explaining what it does.
        lbl_conc = QLabel(tr("تعداد ورکر (هم‌زمانی):"))
        opts.addWidget(lbl_conc)
        self.spin_conc = QSpinBox()
        self.spin_conc.setRange(1, 256)
        self.spin_conc.setValue(64)
        self.spin_conc.setSingleStep(8)
        self.spin_conc.setToolTip(tr(
            "تعداد ورکرهای هم‌زمان (نخ‌ها). بیشتر = اسکن سریع‌تر ولی مصرف "
            "شبکه/سی‌پی‌یو بالاتر. پیشنهاد: ۶۴ تا ۱۲۸"))
        opts.addWidget(self.spin_conc)
        opts.addStretch(1)
        root.addLayout(opts)

        # --- Phase 2 toggle: real Xray end-to-end validation ---
        # This is what makes SenPaiScanner trustworthy: after the Phase-1
        # connectivity sweep, each clean IP is run through the bundled xray with
        # the user's REAL config and tested with real traffic (TTFB + download).
        # Only IPs that actually carry the config end-to-end survive.
        phase2_row = QHBoxLayout()
        phase2_row.setSpacing(8)
        self.chk_xray = QCheckBox(
            tr("اعتبارسنجی واقعی با xray (فاز ۲ — توصیه‌شده)"))
        self.chk_xray.setChecked(True)
        self.chk_xray.setToolTip(tr(
            "پس از فاز ۱، هر IP تمیز با کانفیگ واقعی شما از طریق xray اجرا و با "
            "ترافیک واقعی (تأخیر + سرعت دانلود) تست می‌شود. فقط IPهایی که "
            "واقعاً کانفیگ را سرتاسر منتقل می‌کنند تأیید می‌شوند. کندتر ولی "
            "دقیق‌تر — دقیقاً مثل SenPaiScanner."))
        phase2_row.addWidget(self.chk_xray)
        phase2_row.addStretch(1)
        root.addLayout(phase2_row)

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

        # --- live progress log (always visible, in the SAME screen) ---
        log_head = QHBoxLayout()
        log_head.addWidget(QLabel(tr("روند زندهٔ اسکن:")))
        log_head.addStretch(1)
        # live counter so the user sees activity even before the first clean IP
        self.lbl_progress = QLabel("")
        self.lbl_progress.setObjectName("Muted")
        log_head.addWidget(self.lbl_progress)
        root.addLayout(log_head)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        # taller so several log lines are visible without scrolling (#6)
        self.log.setMinimumHeight(150)
        self.log.setMaximumHeight(220)
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

    # -- window placement --------------------------------------------------
    def showEvent(self, event):  # noqa: N802 (Qt naming)
        """Centre on the active screen so the title bar is never off-screen.

        With a frameless parent the dialog used to appear partly above the top
        edge, hiding its drag handle (bug #4). We re-centre on first show and
        clamp the rect inside the available geometry so it's always grabbable.
        """
        super().showEvent(event)
        if getattr(self, "_centered_once", False):
            return
        self._centered_once = True
        try:
            screen = (self.screen()
                      or (self.parent().screen() if self.parent() else None)
                      or QGuiApplication.primaryScreen())
            avail = screen.availableGeometry()
            frame = self.frameGeometry()
            frame.moveCenter(avail.center())
            # clamp inside the available area so the title bar stays visible
            x = min(max(frame.left(), avail.left()),
                    avail.right() - frame.width())
            y = min(max(frame.top(), avail.top()),
                    avail.bottom() - frame.height())
            self.move(max(x, avail.left()), max(y, avail.top()))
        except Exception:
            pass

    # -- scan lifecycle ----------------------------------------------------
    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.list.clear()
        self.log.clear()
        self._found = 0
        self.lbl_progress.setText(
            tr("ورکرها: {n}").format(n=self.spin_conc.value()))
        cfg = scan_config_from_profile(
            self._profile,
            max_candidates=self.spin_count.value(),
            max_results=self.spin_results.value(),
            concurrency=self.spin_conc.value(),
        )
        # track which list rows correspond to which IP so Phase 2 can update /
        # prune them in place.
        self._row_for_ip = {}
        self._verified_ips = set()
        validate_xray = self.chk_xray.isChecked()
        self._busy(True)
        self.status.setText(tr("فاز ۱ — در حال پروب اتصال …"))
        self._worker = ScanWorker(self._profile, cfg, self,
                                  validate_xray=validate_xray)
        self._worker.hit.connect(self._on_hit)
        self._worker.verified.connect(self._on_verified)
        self._worker.rejected.connect(self._on_rejected)
        self._worker.line.connect(self._on_line)
        self._worker.phase.connect(self._on_phase)
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
        self.chk_xray.setEnabled(not busy)

    def _on_hit(self, ip: str, latency_ms: float, detail: str = ""):
        self._found = getattr(self, "_found", 0) + 1
        item = QListWidgetItem(self.list)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        # richer per-IP line: latency + a quick latency-quality tag + the probe
        # detail (e.g. "http ok" / colo) so the user understands WHY it's clean.
        tag = (tr("عالی") if latency_ms < 150 else
               tr("خوب") if latency_ms < 350 else tr("کند"))
        extra = f"   ·   {detail}" if detail else ""
        item.setText(tr("{ip}   ·   {ms:.0f}ms  ({tag}){extra}").format(
            ip=ip, ms=latency_ms, tag=tag, extra=extra))
        item.setToolTip(tr("IP: {ip}\nتأخیر: {ms:.0f}ms\nجزئیات: {d}").format(
            ip=ip, ms=latency_ms, d=detail or "—"))
        item.setData(Qt.UserRole, ip)
        self.list.addItem(item)
        # remember the row so Phase 2 can mark it verified / prune it
        self._row_for_ip[ip] = item
        self.lbl_progress.setText(tr("ورکرها: {n}  ·  پیداشده: {f}").format(
            n=self.spin_conc.value(), f=self._found))

    def _on_verified(self, ip: str, latency_ms: float, speed_bps: float):
        """A Phase-2 (xray) validated IP — annotate its row as ✓✓ confirmed."""
        self._verified_ips.add(ip)
        item = self._row_for_ip.get(ip)
        spd = _fmt_speed(speed_bps) if speed_bps > 0 else "—"
        label = tr("✅ {ip}   ·   {ms:.0f}ms واقعی   ·   {spd}   "
                   "(تأییدشده با xray)").format(ip=ip, ms=latency_ms, spd=spd)
        if item is not None:
            item.setText(label)
            item.setCheckState(Qt.Checked)
            item.setToolTip(tr(
                "IP: {ip}\nتأخیر واقعی پروکسی: {ms:.0f}ms\nسرعت دانلود: {spd}\n"
                "این IP با کانفیگ واقعی شما از طریق xray تست و تأیید شد.").format(
                    ip=ip, ms=latency_ms, spd=spd))
        else:
            item = QListWidgetItem(self.list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setText(label)
            item.setData(Qt.UserRole, ip)
            self.list.addItem(item)
            self._row_for_ip[ip] = item

    def _on_rejected(self, ip: str):
        """A Phase-1 clean IP that FAILED the Phase-2 xray validation.

        We dim it and uncheck it (so the user doesn't add a config that only
        passed the lightweight probe but doesn't carry real traffic).
        """
        item = self._row_for_ip.get(ip)
        if item is not None:
            item.setCheckState(Qt.Unchecked)
            item.setText(tr("⚠️ {ip}   ·   رد شد در فاز ۲ (xray)").format(ip=ip))
            item.setForeground(Qt.gray)
            item.setToolTip(tr(
                "این IP در فاز ۱ تمیز به‌نظر رسید اما در اعتبارسنجی واقعی با "
                "xray ترافیک را منتقل نکرد — توصیه نمی‌شود."))

    def _on_phase(self, name: str):
        if name == "phase1":
            self.status.setText(tr("فاز ۱ — در حال پروب اتصال …"))
        elif name == "phase2":
            self.status.setText(
                tr("فاز ۲ — اعتبارسنجی واقعی با xray (کندتر، دقیق‌تر) …"))
            self.lbl_progress.setText(
                tr("فاز ۲ — اجرای کانفیگ واقعی روی هر IP تمیز …"))

    def _on_line(self, text: str):
        self.log.appendPlainText(text)
        # keep the newest line in view (auto-scroll)
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum())

    def _on_done(self, found: int, tested: int):
        self._busy(False)
        xray_done = self.chk_xray.isChecked() and bool(self._verified_ips)
        if xray_done:
            self.lbl_progress.setText(
                tr("پایان  ·  تأییدشده با xray: {f}  ·  آزمایش‌شده: {t}").format(
                    f=found, t=tested))
            self.status.setText(
                tr("تمام شد — {found} IP با xray تأیید شد (از {tested} "
                   "آزمایش‌شده)").format(found=found, tested=tested))
        else:
            self.lbl_progress.setText(
                tr("پایان  ·  پیداشده: {f}  ·  آزمایش‌شده: {t}").format(
                    f=found, t=tested))
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
