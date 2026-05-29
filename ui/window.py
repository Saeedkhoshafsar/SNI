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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout,
    QWidget,
)

from ui import win_effects
from ui.theme import get_palette, build_qss
from ui.widgets import Card, NavItem, PowerButton, TitleBar, Toast
from ui.animations import CountUp, PulseDot, stagger_in


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
    """Hero status + quick stats + the big animated Start/Stop control."""

    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self._palette = palette
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

    # -- power button flow (preview/simulation until core wiring in step 5)-
    def _on_power(self, action: str):
        for t in self._sim_timers:
            t.stop()
        self._sim_timers.clear()
        if action == "start":
            self.set_status("connecting")
            t1 = QTimer(self); t1.setSingleShot(True)
            t1.timeout.connect(lambda: self._activate())
            t1.start(1400)
            self._sim_timers.append(t1)
        else:
            self.set_status("idle")
            self._count.to(0)
            self._toast("اتصال قطع شد", "warn")

    def _activate(self):
        self.set_status("active")
        self._count.to(7)
        self._toast("اتصال برقرار شد — spoofing فعال", "ok")

    def set_status(self, state: str):
        self.status_dot.set_state(state)
        self.btn_start.set_state(state)
        self.status_label.setText({
            "idle": "آماده — متوقف",
            "connecting": "در حال اتصال…",
            "active": "متصل — تونل فعال",
            "error": "خطا — تلاش دوباره",
        }.get(state, "آماده — متوقف"))

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


class StrategyPage(QWidget):
    """The 'final boss' surface — arsenal of bypass strategies + auto-prober."""

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.btn_autoprobe = QPushButton("فعال‌سازی")
        self.btn_autoprobe.setObjectName("Ghost")
        row.addWidget(self.btn_autoprobe)
        apb.addLayout(row)
        root.addWidget(ap)

        # strategy list
        for key, name, desc in STRATEGIES:
            root.addWidget(self._strategy_row(key, name, desc))

        root.addStretch(1)

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
        root.addWidget(card, 1)


# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------

class MainWindow(QWidget):

    def __init__(self, theme: str = "dark"):
        super().__init__()
        self._theme = theme
        self._palette = get_palette(theme)
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
        self.page_settings = SettingsPage()
        self.page_strategy = StrategyPage()
        self.page_log = LogPage()
        for p in (self.page_dashboard, self.page_settings,
                  self.page_strategy, self.page_log):
            self.stack.addWidget(p)
        self.stack.currentChanged.connect(self._on_page_changed)
        body.addWidget(self.stack, 1)

        outer.addLayout(body, 1)

        self._apply_theme()
        # play the dashboard entrance once the window is up
        QTimer.singleShot(60, self.page_dashboard.play_intro)

    def _on_page_changed(self, index: int):
        # replay the dashboard intro when navigating back to it
        if self.stack.widget(index) is self.page_dashboard:
            self.page_dashboard.play_intro()

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
            ("داشبورد", "\u25c9"),   # fisheye
            ("تنظیمات", "\u2699"),    # gear
            ("استراتژی", "\u29bf"),   # circled bullet
            ("لاگ", "\u2261"),        # identical-to (log lines)
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
