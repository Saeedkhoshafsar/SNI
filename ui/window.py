"""The frameless main window for the professional SNI Spoofer UI.

Assembles:
  * a custom :class:`~ui.widgets.TitleBar` (drag + minimise / theme / close)
  * a side-navigation column of :class:`~ui.widgets.NavItem`s
  * a :class:`QStackedWidget` of content pages (Dashboard / Settings /
    Strategy / Log)
  * a translucent Mica/acrylic backdrop (Windows) with a graceful opaque
    fallback elsewhere.

The window is intentionally *decoupled from the core*: pages are populated
with rich, meaningful placeholder content so the product never looks "dry".
Real start/stop wiring to ``ProxyServer`` / ``TransparentSpoofServer`` lands in
step 3; dynamic animations land in step 2.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ui import win_effects
from ui.theme import get_palette, build_qss
from ui.widgets import Card, NavItem, PowerButton, TitleBar, Toast
from ui.animations import CountUp, PulseDot, stagger_in

from core.config_store import ConfigStore
from core.engine import EngineController
from core.profile import Profile
from core.share_link import parse_link, parse_subscription, ShareLinkError
from ui.engine_bridge import EngineBridge


# ---------------------------------------------------------------------------
#  Reference data (mirrors gui.py so pages feel "real", not empty)
# ---------------------------------------------------------------------------

DEFAULT_SNIS = [
    "www.speedtest.net",
    "www.google.com",
    "www.cloudflare.com",
    "fonts.googleapis.com",
    "www.bing.com",
]

MODES = [
    "SNI Only",
    "SNI + Warp",
    "SNI + Psiphon",
    "SNI + Warp-in-Warp",
    "Gaming Mode",
]

STRATEGIES = [
    ("wrong_seq", "Wrong Sequence", "تزریق ClientHello جعلی با seq خارج از پنجره"),
    ("wrong_checksum", "Wrong Checksum", "چک‌سام نامعتبر تا سرور دور بریزد"),
    ("fake_ttl", "Fake TTL", "TTL کوتاه تا فقط به DPI برسد"),
    ("multi_fake", "Multi Fake", "چند بسته جعلی پشت‌سرهم"),
    ("fake_disorder", "Fake Disorder", "بی‌نظمی عمدی در ترتیب بسته‌ها"),
]


# ---------------------------------------------------------------------------
#  Page builders
# ---------------------------------------------------------------------------

def _section_title(text: str, sub: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    h = QLabel(text)
    h.setObjectName("H1")
    lay.addWidget(h)
    if sub:
        s = QLabel(sub)
        s.setObjectName("Muted")
        lay.addWidget(s)
    return w


def _stat_card(value: str, label: str, accent_color: str | None = None) -> Card:
    c = Card(object_name="CardAlt")
    b = c.body()
    v = QLabel(value)
    v.setObjectName("H1")
    if accent_color:
        # inline colour overrides the #H1 rule reliably
        v.setStyleSheet(f"color: {accent_color};")
    cap = QLabel(label)
    cap.setObjectName("Muted")
    b.addWidget(v)
    b.addWidget(cap)
    c.value_label = v   # exposed so callers can animate it (CountUp)
    return c


class DashboardPage(QWidget):
    """Hero status + quick stats + the big animated Start/Stop control.

    Wired to the real engine in step 5: the power button asks the host window
    to start/stop the :class:`~core.engine.EngineController`; status, live
    connection count and the active strategy are pushed back in via
    :meth:`set_status`, :meth:`on_count` and :meth:`set_active_strategy`.
    """

    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self._palette = palette
        # host window assigns this; called with "start" / "stop"
        self.power_handler = None
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        self.header = _section_title(
            "داشبورد", "وضعیت زنده‌ی تونل و کنترل سریع روشن/خاموش")
        root.addWidget(self.header)

        # --- status hero card ---
        hero = Card()
        hb = hero.body()
        row = QHBoxLayout()
        row.setSpacing(14)

        self.status_dot = PulseDot(diameter=12)
        self.status_label = QLabel("آماده — متوقف")
        self.status_label.setObjectName("H2")
        row.addWidget(self.status_dot)
        row.addWidget(self.status_label)
        row.addStretch(1)

        self.btn_start = PowerButton(palette)
        self.btn_start.request.connect(self._on_power)
        row.addWidget(self.btn_start)
        hb.addLayout(row)
        self.hero = hero
        root.addWidget(hero)

        # --- quick stats row ---
        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_conns = _stat_card("0", "اتصالات فعال",
                                     accent_color=palette.accent)
        self.stat_mode = _stat_card("SNI Only", "حالت")
        self.stat_strategy = _stat_card("wrong_seq", "استراتژی فعال")
        self.stat_cards = [self.stat_conns, self.stat_mode, self.stat_strategy]
        for c in self.stat_cards:
            stats.addWidget(c)
        root.addLayout(stats)

        root.addStretch(1)

        self._count = CountUp(self.stat_conns.value_label)
        self._sim_timers: list = []

    # -- entrance animation (called when page becomes visible) -------------
    def play_intro(self):
        stagger_in([self.header, self.hero, *self.stat_cards], step=70)

    # -- power button → delegate to the engine via the host window ---------
    def _on_power(self, action: str):
        if self.power_handler:
            self.power_handler(action)

    # -- live updates pushed in from the engine bridge ---------------------
    def set_status(self, state: str):
        self.status_dot.set_state(state)
        self.btn_start.set_state(state)
        self.status_label.setText({
            "idle": "آماده — متوقف",
            "connecting": "در حال اتصال…",
            "active": "متصل — تونل فعال",
            "error": "خطا — تلاش دوباره",
        }.get(state, "آماده — متوقف"))

    def on_count(self, active: int, total: int):
        """Slot for the engine's connection-count signal."""
        self._count.to(active)

    def set_active_strategy(self, key: str):
        self.stat_strategy.value_label.setText(key)

    def set_mode(self, mode: str):
        self.stat_mode.value_label.setText(mode)

    def _toast(self, text: str, kind: str):
        win = self.window()
        Toast.show_message(win, text, kind)

    def set_palette(self, palette):
        self._palette = palette
        self.btn_start.set_palette(palette)
        self.stat_conns.value_label.setStyleSheet(f"color:{palette.accent};")


class SettingsPage(QWidget):
    """Connection mode, SNI, ports — pre-filled with sane real values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "تنظیمات", "حالت اتصال، SNI و پورت‌ها"))

        card = Card()
        form = card.body()

        form.addWidget(self._field_label("حالت اتصال"))
        self.mode = QComboBox()
        self.mode.addItems(MODES)
        form.addWidget(self.mode)

        form.addWidget(self._field_label("SNI جعلی"))
        self.sni = QComboBox()
        self.sni.setEditable(True)
        self.sni.addItems(DEFAULT_SNIS)
        form.addWidget(self.sni)

        ports = QHBoxLayout()
        ports.setSpacing(14)
        ports.addWidget(self._labelled_spin("پورت گوش‌دادن", 40443, out="listen"))
        ports.addWidget(self._labelled_spin("پورت SOCKS", 10808, out="socks"))
        form.addLayout(ports)

        form.addWidget(self._field_label("IP اتصال"))
        self.connect_ip = QLineEdit("www.speedtest.net")
        form.addWidget(self.connect_ip)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save = QPushButton("ذخیره")
        self.btn_save.setObjectName("Primary")
        save_row.addWidget(self.btn_save)
        form.addLayout(save_row)

        root.addWidget(card)
        root.addStretch(1)

    # -- config <-> widgets ------------------------------------------------
    def load_from(self, cfg: dict) -> None:
        """Populate the widgets from a config dict."""
        mode = cfg.get("connection_mode", "SNI Only")
        i = self.mode.findText(mode)
        if i >= 0:
            self.mode.setCurrentIndex(i)
        self.sni.setCurrentText(cfg.get("FAKE_SNI", "www.speedtest.net"))
        self.spin_listen.setValue(int(cfg.get("LISTEN_PORT", 40443)))
        self.spin_socks.setValue(int(cfg.get("socks_port", 10808)))
        self.connect_ip.setText(str(cfg.get("CONNECT_IP", "")))

    def collect(self) -> dict:
        """Read the widgets back into a config dict fragment."""
        return {
            "connection_mode": self.mode.currentText(),
            "FAKE_SNI": self.sni.currentText().strip(),
            "LISTEN_PORT": self.spin_listen.value(),
            "socks_port": self.spin_socks.value(),
            "CONNECT_IP": self.connect_ip.text().strip(),
        }

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(t)
        lbl.setObjectName("Muted")
        return lbl

    def _labelled_spin(self, t: str, val: int, out: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self._field_label(t))
        sp = QSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(val)
        lay.addWidget(sp)
        setattr(self, f"spin_{out}", sp)
        return w


class ProfilesPage(QWidget):
    """Import + manage server profiles (share links / subscriptions).

    v2rayN-style: the user pastes a ``vless://`` / ``vmess://`` / ``trojan://``
    / ``ss://`` link or a subscription URL; the page parses it via
    :mod:`core.share_link` and stores the resulting :class:`Profile`(s). The
    selected profile is what the engine chains xray + spoofing under.
    """

    def __init__(self, store: ConfigStore, parent=None):
        super().__init__(parent)
        self._store = store
        # host window assigns this; called when the selected profile changes
        self.on_selection_changed = None

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "پروفایل‌ها", "وارد کردن لینک اشتراک‌گذاری یا سابسکریپشن (vless/vmess/trojan/ss)"))

        # --- import card ---
        imp = Card()
        ib = imp.body()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "vless://… یا trojan://… یا لینک سابسکریپشن را اینجا بچسبانید")
        ib.addWidget(self.input)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.btn_import = QPushButton("افزودن لینک")
        self.btn_import.setObjectName("Primary")
        self.btn_paste = QPushButton("از کلیپ‌بورد")
        self.btn_paste.setObjectName("Ghost")
        self.btn_sub = QPushButton("افزودن سابسکریپشن")
        self.btn_sub.setObjectName("Ghost")
        btn_row.addWidget(self.btn_import)
        btn_row.addWidget(self.btn_paste)
        btn_row.addWidget(self.btn_sub)
        btn_row.addStretch(1)
        ib.addLayout(btn_row)
        root.addWidget(imp)

        # --- profiles list card ---
        listc = Card()
        lb = listc.body()
        lb.addWidget(self._field_label("سرورهای ذخیره‌شده"))
        self.list = QListWidget()
        self.list.setObjectName("ProfileList")
        lb.addWidget(self.list)

        del_row = QHBoxLayout()
        del_row.addStretch(1)
        self.btn_delete = QPushButton("حذف انتخاب‌شده")
        self.btn_delete.setObjectName("Ghost")
        del_row.addWidget(self.btn_delete)
        lb.addLayout(del_row)
        root.addWidget(listc, 1)

        # wiring
        self.btn_import.clicked.connect(self._import_link)
        self.input.returnPressed.connect(self._import_link)
        self.btn_paste.clicked.connect(self._paste)
        self.btn_sub.clicked.connect(self._import_subscription)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.list.currentRowChanged.connect(self._row_changed)

        self.refresh()

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(t)
        lbl.setObjectName("Muted")
        return lbl

    # -- list rendering ----------------------------------------------------
    def refresh(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for p in self._store.profiles:
            badge = p.protocol.upper()
            QListWidgetItem(f"[{badge}]  {p.display_name}", self.list)
        if 0 <= self._store.selected_index < self.list.count():
            self.list.setCurrentRow(self._store.selected_index)
        self.list.blockSignals(False)

    # -- actions -----------------------------------------------------------
    def _toast(self, text: str, kind: str = "info"):
        Toast.show_message(self.window(), text, kind)

    def _import_link(self):
        text = self.input.text().strip()
        if not text:
            return
        try:
            profile = parse_link(text)
        except ShareLinkError as exc:
            self._toast(f"لینک نامعتبر: {exc}", "err")
            return
        errs = profile.validate()
        if errs:
            self._toast("؛ ".join(errs), "err")
            return
        self._store.add_profile(profile, select=True)
        self.input.clear()
        self.refresh()
        self._toast(f"پروفایل افزوده شد: {profile.display_name}", "ok")
        self._emit_selection()

    def _import_subscription(self):
        text = self.input.text().strip()
        if not text:
            self._toast("ابتدا متن/URL سابسکریپشن را وارد کنید", "warn")
            return
        blob = text
        if text.startswith("http://") or text.startswith("https://"):
            blob = self._fetch(text)
            if blob is None:
                return
        profiles = parse_subscription(blob)
        if not profiles:
            self._toast("هیچ پروفایل معتبری در سابسکریپشن یافت نشد", "warn")
            return
        added = self._store.add_profiles(profiles)
        self.input.clear()
        self.refresh()
        self._toast(f"{added} پروفایل از سابسکریپشن افزوده شد", "ok")
        self._emit_selection()

    def _fetch(self, url: str) -> str | None:
        import urllib.request
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self._toast(f"واکشی سابسکریپشن ناموفق: {exc}", "err")
            return None

    def _paste(self):
        cb = QGuiApplication.clipboard()
        self.input.setText(cb.text().strip())

    def _delete_selected(self):
        row = self.list.currentRow()
        if row < 0:
            return
        self._store.remove_profile(row)
        self.refresh()
        self._emit_selection()
        self._toast("پروفایل حذف شد", "warn")

    def _row_changed(self, row: int):
        if 0 <= row < len(self._store.profiles):
            self._store.select(row)
            self._emit_selection()

    def _emit_selection(self):
        if self.on_selection_changed:
            self.on_selection_changed(self._store.selected_profile)


class StrategyPage(QWidget):
    """The 'final boss' surface — arsenal of bypass strategies + auto-prober."""

    # emitted when the auto-prober toggle changes (True == enabled)
    auto_prober_changed = Signal(bool)

    def __init__(self, store: "ConfigStore | None" = None, parent=None):
        super().__init__(parent)
        self.store = store
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "استراتژی عبور", "زرادخانه‌ی روش‌های دور زدن DPI + پراب خودکار (غول مرحله آخر)"))

        # auto-prober toggle card
        ap = Card()
        apb = ap.body()
        row = QHBoxLayout()
        t = QLabel("پراب خودکار")
        t.setObjectName("H2")
        desc = QLabel("بهترین استراتژی را خودکار آزمایش، رتبه‌بندی و قفل می‌کند")
        desc.setObjectName("Faint")
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(t)
        col.addWidget(desc)
        row.addLayout(col)
        row.addStretch(1)
        self.btn_autoprobe = QPushButton()
        self.btn_autoprobe.setObjectName("Ghost")
        self.btn_autoprobe.setCheckable(True)
        enabled = bool(store.get("auto_prober", False)) if store else False
        self.btn_autoprobe.setChecked(enabled)
        self._sync_autoprobe_label(enabled)
        self.btn_autoprobe.toggled.connect(self._on_autoprobe_toggled)
        row.addWidget(self.btn_autoprobe)
        apb.addLayout(row)
        root.addWidget(ap)

        # strategy list
        for key, name, desc in STRATEGIES:
            root.addWidget(self._strategy_row(key, name, desc))

        root.addStretch(1)

    def _sync_autoprobe_label(self, enabled: bool) -> None:
        self.btn_autoprobe.setText("فعال ✓" if enabled else "فعال‌سازی")

    def _on_autoprobe_toggled(self, enabled: bool) -> None:
        self._sync_autoprobe_label(enabled)
        if self.store is not None:
            self.store.set("auto_prober", bool(enabled))
        self.auto_prober_changed.emit(bool(enabled))

    def _strategy_row(self, key: str, name: str, desc: str) -> Card:
        c = Card(object_name="CardAlt")
        b = c.body()
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel(name)
        nm.setObjectName("H2")
        ds = QLabel(desc)
        ds.setObjectName("Faint")
        col.addWidget(nm)
        col.addWidget(ds)
        row.addLayout(col)
        row.addStretch(1)
        badge = QLabel(key)
        badge.setObjectName("Mono")
        row.addWidget(badge)
        b.addLayout(row)
        return c


class DiagnosticsPage(QWidget):
    """Live picture of the auto-prober + resilience layer (step 12).

    Pure renderer: it polls ``engine.diagnostics()`` (a plain
    :class:`core.diagnostics.DiagnosticsSnapshot`) on a timer and repaints. No
    engine internals are touched here, so the GUI stays decoupled from the core.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider = None          # callable -> DiagnosticsSnapshot
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "تشخیص", "وضعیت زنده‌ی پراب خودکار و تاب‌آوری"))

        # --- summary card: active strategy + status ---
        summary = Card()
        sb = summary.body()
        self.lbl_active = QLabel("استراتژی فعال: —")
        self.lbl_active.setObjectName("H2")
        self.lbl_status = QLabel("وضعیت: بی‌کار")
        self.lbl_status.setObjectName("Faint")
        sb.addWidget(self.lbl_active)
        sb.addWidget(self.lbl_status)
        root.addWidget(summary)

        # --- throughput / throttle card ---
        tp = Card()
        tb = tp.body()
        h = QLabel("توان عبوری (throughput)")
        h.setObjectName("H2")
        tb.addWidget(h)
        self.bar_tp = QProgressBar()
        self.bar_tp.setRange(0, 100)
        self.bar_tp.setTextVisible(False)
        tb.addWidget(self.bar_tp)
        self.lbl_tp = QLabel("بدون داده")
        self.lbl_tp.setObjectName("Faint")
        tb.addWidget(self.lbl_tp)
        self.lbl_rst = QLabel("RST جعلی: —")
        self.lbl_rst.setObjectName("Faint")
        tb.addWidget(self.lbl_rst)
        self.lbl_chain = QLabel("زنجیره‌ی fallback: —")
        self.lbl_chain.setObjectName("Faint")
        self.lbl_chain.setWordWrap(True)
        tb.addWidget(self.lbl_chain)
        root.addWidget(tp)

        # --- candidate health table card ---
        cand = Card()
        cb = cand.body()
        ch = QLabel("کاندیداها (probe)")
        ch.setObjectName("H2")
        cb.addWidget(ch)
        self.tbl = QPlainTextEdit()
        self.tbl.setObjectName("Log")
        self.tbl.setReadOnly(True)
        self.tbl.setMinimumHeight(170)
        cb.addWidget(self.tbl)
        root.addWidget(cand, 1)

        # poll timer (started/stopped when the page becomes visible)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)

    def set_provider(self, provider) -> None:
        """Give the page a zero-arg callable returning a DiagnosticsSnapshot."""
        self._provider = provider
        self.refresh()

    def start_polling(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.refresh()

    def stop_polling(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        if self._provider is None:
            return
        try:
            snap = self._provider()
        except Exception:
            return
        self._render(snap)

    # -- rendering --------------------------------------------------------
    _STATUS_FA = {
        "idle": "بی‌کار", "connecting": "در حال اتصال",
        "active": "فعال", "error": "خطا",
    }

    def _render(self, snap) -> None:
        self.lbl_active.setText(
            f"استراتژی فعال: {snap.active_strategy or '—'}")
        st = self._STATUS_FA.get(snap.status, snap.status)
        port = f" · پورت {snap.spoof_port}" if snap.spoof_port else ""
        self.lbl_status.setText(f"وضعیت: {st}{port}")

        # throughput bar = recent/baseline ratio (clamped to 100%)
        ratio = snap.throttle_ratio
        if snap.baseline_bps > 0:
            pct = max(0, min(100, int(ratio * 100)))
            self.bar_tp.setValue(pct)
            tag = " — throttle!" if snap.throttled else ""
            self.lbl_tp.setText(
                f"{self._fmt_bps(snap.recent_bps)} از "
                f"{self._fmt_bps(snap.baseline_bps)} ({pct}%){tag}")
        else:
            self.bar_tp.setValue(0)
            self.lbl_tp.setText("بدون داده")

        if snap.resilience_on:
            self.lbl_rst.setText(
                f"RST جعلی: {snap.forged_rst_count} / بودجه {snap.rst_budget}")
            chain = " → ".join(snap.strategy_chain) or "—"
            ips = " → ".join(snap.ip_chain) or "—"
            self.lbl_chain.setText(
                f"زنجیره‌ی استراتژی: {chain}\nزنجیره‌ی IP: {ips}")
        else:
            self.lbl_rst.setText("تاب‌آوری غیرفعال است")
            self.lbl_chain.setText("زنجیره‌ی fallback: —")

        self.tbl.setPlainText(self._candidate_table(snap))

    @staticmethod
    def _fmt_bps(bps: float) -> str:
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.1f} MB/s"
        if bps >= 1000:
            return f"{bps / 1000:.0f} KB/s"
        return f"{bps:.0f} B/s"

    @staticmethod
    def _candidate_table(snap) -> str:
        if not snap.has_probe_data:
            return "هنوز probe انجام نشده — هنگام اتصال با «پراب خودکار» پر می‌شود."
        lines = [f"{'استراتژی':<22}{'امتیاز':>8}{'موفقیت':>9}{'نمونه':>7}  وضعیت"]
        for c in snap.candidates:
            mark = "★ " if c.selected else "  "
            lines.append(
                f"{mark}{c.key:<20}{c.mean_score:>8.2f}"
                f"{c.success_rate*100:>8.0f}%{c.samples:>7}  {c.last_outcome}")
        return "\n".join(lines)


class LogPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title("لاگ", "رویدادهای زنده‌ی موتور"))

        card = Card()
        b = card.body()
        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setPlainText(
            "[init] SNI Spoofer UI بارگذاری شد\n"
            "[init] منتظر شروع تونل…\n"
        )
        b.addWidget(self.log)

        clr = QHBoxLayout()
        clr.addStretch(1)
        self.btn_clear = QPushButton("پاک‌سازی")
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.clicked.connect(lambda: self.log.clear())
        clr.addWidget(self.btn_clear)
        b.addLayout(clr)

        root.addWidget(card, 1)

    def append(self, line: str) -> None:
        """Slot for the engine's log signal (thread-safe via Qt queued conn)."""
        # keep the buffer bounded so long sessions stay responsive
        doc = self.log.document()
        if doc.blockCount() > 1500:
            self.log.clear()
        self.log.appendPlainText(line)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())


# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------

class MainWindow(QWidget):

    def __init__(self, theme: str | None = None):
        super().__init__()
        # --- core: persistent store + engine bridge ---
        self.store = ConfigStore()
        self._theme = theme or self.store.get("theme", "dark")
        self.engine = EngineBridge(EngineController(self.store.config))
        self.engine.set_profile(self.store.selected_profile)

        self._palette = get_palette(self._theme)
        self.setObjectName("RootBackdrop")
        self.setWindowTitle("SNI Spoofer")
        self.resize(940, 620)
        self.setMinimumSize(820, 540)

        # frameless + translucent so the Mica backdrop can show through
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- title bar ---
        self.title_bar = TitleBar(self)
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.close_clicked.connect(self.close)
        self.title_bar.theme_toggled.connect(self.toggle_theme)
        outer.addWidget(self.title_bar)

        # --- body: nav + pages ---
        body = QHBoxLayout()
        body.setContentsMargins(14, 6, 14, 14)
        body.setSpacing(14)

        body.addWidget(self._build_nav())

        self.stack = QStackedWidget()
        self.page_dashboard = DashboardPage(self._palette)
        self.page_profiles = ProfilesPage(self.store)
        self.page_settings = SettingsPage()
        self.page_strategy = StrategyPage(self.store)
        self.page_strategy.auto_prober_changed.connect(self._on_auto_prober_changed)
        self.page_diagnostics = DiagnosticsPage()
        self.page_diagnostics.set_provider(self.engine.diagnostics)
        self.page_log = LogPage()
        for p in (self.page_dashboard, self.page_profiles, self.page_settings,
                  self.page_strategy, self.page_diagnostics, self.page_log):
            self.stack.addWidget(p)
        self.stack.currentChanged.connect(self._on_page_changed)
        body.addWidget(self.stack, 1)

        outer.addLayout(body, 1)

        self._wire_core()
        self._apply_theme()
        # play the dashboard entrance once the window is up
        QTimer.singleShot(60, self.page_dashboard.play_intro)

    # ------------------------------------------------------------------ core
    def _wire_core(self):
        """Connect UI pages to the engine bridge + config store (step 5)."""
        # engine → UI (signals are marshalled to the GUI thread by Qt)
        self.engine.log.connect(self.page_log.append)
        self.engine.status.connect(self.page_dashboard.set_status)
        self.engine.status.connect(self._on_status)
        self.engine.count.connect(self.page_dashboard.on_count)

        # UI → engine
        self.page_dashboard.power_handler = self._on_power
        self.page_profiles.on_selection_changed = self._on_profile_selected
        self.page_settings.btn_save.clicked.connect(self._save_settings)

        # initialise widgets from persisted state
        self.page_settings.load_from(self.store.config)
        self.page_dashboard.set_mode(
            self.store.get("connection_mode", "SNI Only"))
        self.page_dashboard.set_active_strategy(
            self.store.get("bypass_method", "wrong_seq"))
        sel = self.store.selected_profile
        if sel:
            self.page_log.append(f"[init] پروفایل فعال: {sel.display_name}")
        else:
            self.page_log.append("[init] پروفایلی انتخاب نشده — حالت SNI Only")

    def _on_power(self, action: str):
        if action == "start":
            # push the freshest settings + profile into the engine first
            self.engine.update_config(self.store.config)
            self.engine.set_profile(self.store.selected_profile)
            if (self.store.get("connection_mode") != "SNI Only"
                    and self.store.selected_profile is None):
                Toast.show_message(
                    self, "ابتدا یک پروفایل وارد و انتخاب کنید", "warn")
                self.page_dashboard.set_status("idle")
                return
            self.engine.start()
        else:
            self.engine.stop()

    def _on_status(self, status: str):
        if status == "active":
            Toast.show_message(self, "اتصال برقرار شد — spoofing فعال", "ok")
        elif status == "idle":
            Toast.show_message(self, "اتصال قطع شد", "warn")
        elif status == "error":
            Toast.show_message(self, "خطا در اتصال — لاگ را ببینید", "err")

    def _on_profile_selected(self, profile):
        self.engine.set_profile(profile)
        if profile:
            self.page_log.append(f"[profile] انتخاب شد: {profile.display_name}")

    def _on_auto_prober_changed(self, enabled: bool):
        # the StrategyPage already persisted the flag; push it to the live engine
        self.store.save_config()
        self.engine.update_config(self.store.config)
        self.page_log.append(
            f"[auto-prober] {'فعال شد' if enabled else 'غیرفعال شد'}")
        Toast.show_message(
            self, "پراب خودکار فعال شد" if enabled else "پراب خودکار غیرفعال شد",
            "ok")

    def _save_settings(self):
        self.store.update(**self.page_settings.collect())
        self.store.save_config()
        self.engine.update_config(self.store.config)
        self.page_dashboard.set_mode(
            self.store.get("connection_mode", "SNI Only"))
        Toast.show_message(self, "تنظیمات ذخیره شد", "ok")

    def _on_page_changed(self, index: int):
        # replay the dashboard intro when navigating back to it
        if self.stack.widget(index) is self.page_dashboard:
            self.page_dashboard.play_intro()
        # only poll diagnostics while its page is visible (saves cycles)
        if self.stack.widget(index) is self.page_diagnostics:
            self.page_diagnostics.start_polling()
        else:
            self.page_diagnostics.stop_polling()

    # --- navigation -------------------------------------------------------
    def _build_nav(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("Card")
        rail.setFixedWidth(196)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(10, 14, 10, 14)
        lay.setSpacing(6)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        items = [
            ("داشبورد", "\u25c9"),    # fisheye
            ("پروفایل‌ها", "\u2630"),  # trigram (list)
            ("تنظیمات", "\u2699"),     # gear
            ("استراتژی", "\u29bf"),    # circled bullet
            ("تشخیص", "\u2295"),       # circled plus (diagnostics)
            ("لاگ", "\u2261"),         # identical-to (log lines)
        ]
        for idx, (text, icon) in enumerate(items):
            btn = NavItem(text, icon)
            btn.clicked.connect(lambda _=False, i=idx: self.stack.setCurrentIndex(i))
            self.nav_group.addButton(btn, idx)
            lay.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)

        lay.addStretch(1)
        ver = QLabel("v3.0 · Windows")
        ver.setObjectName("Faint")
        lay.addWidget(ver)
        return rail

    # --- theming ----------------------------------------------------------
    def _apply_theme(self):
        palette = get_palette(self._theme)
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        # propagate the palette to widgets that paint inline (not via QSS)
        self.page_dashboard.set_palette(palette)
        try:
            hwnd = int(self.winId())
            win_effects.set_dark_titlebar(hwnd, palette.is_dark)
            win_effects.apply_backdrop(
                hwnd,
                win_effects.BACKDROP_MICA if palette.is_dark
                else win_effects.BACKDROP_ACRYLIC,
            )
        except Exception:
            pass

    def toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme()
        self.store.set("theme", self._theme)
        self.store.save_config()

    def closeEvent(self, event):
        """Stop the engine cleanly so no subprocess / thread is orphaned."""
        try:
            self.engine.stop()
        except Exception:
            pass
        super().closeEvent(event)
