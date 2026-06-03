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

from PySide6.QtCore import Qt, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizeGrip, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QColor


def _scrollable(page: QWidget) -> QScrollArea:
    """Wrap a content page in a vertical scroll area.

    Without this, when the window is short or the content is tall, Qt squeezes
    the widgets on top of one another (the "overlapping / clipped fields" bug
    seen on the built Windows app). A resizable scroll area keeps every widget
    at its natural size and adds a scrollbar instead of overlapping.
    """
    sa = QScrollArea()
    sa.setObjectName("PageScroll")
    sa.setWidget(page)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    # transparent viewport so the themed backdrop shows through
    sa.viewport().setAutoFillBackground(False)
    sa.setStyleSheet("QScrollArea#PageScroll{background:transparent;}"
                     "QScrollArea#PageScroll>QWidget>QWidget{background:transparent;}")
    return sa

from ui import icons as _icons
from ui import win_effects
from ui.theme import get_palette, build_qss, ACCENT2_DARK, ACCENT2_LIGHT
from ui.widgets import (
    ActiveConfigBar, Card, NavItem, NoScrollComboBox, NoScrollSpinBox,
    PowerButton, ProfileRow, Sparkline, TitleBar, Toast,
)
from ui.animations import CountUp, PulseDot, WaveBackdrop, stagger_in
from ui.i18n import tr

from core.config_store import ConfigStore
from core.engine import EngineController
from core.logbuffer import LEVELS, SOURCES, LogBuffer
from core.profile import Profile
from core.share_link import parse_link, parse_subscription, ShareLinkError
from ui.engine_bridge import EngineBridge
from ui.profile_dialog import ProfileDialog


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

# #5: only the two modes that actually work are kept. The Warp / Psiphon /
# Warp-in-Warp / Gaming experiments were removed — they were never wired to a
# working backend and only confused the UI.
MODES = [
    "Tunnel",          # default: VLESS/xray chained under the spoofer (needs a profile)
    "SNI Only",        # spoofer; if a profile is selected, xray runs chained under it
]

# human-readable, Persian hint shown under the mode selector
MODE_HINTS = {
    "Tunnel": "اتصال کامل از طریق کانفیگ انتخاب‌شده (VLESS/VMess/Trojan) با هسته‌ی xray + اسپوف SNI. برای استفاده از کانفیگ‌ها این حالت را انتخاب کنید.",
    "SNI Only": "اسپوف SNI بدون لایه‌ی بیرونی Warp/Psiphon. اگر کانفیگی انتخاب شده باشد، xray هم اجرا و زیر اسپوفر زنجیر می‌شود (کانفیگ VLESS کار می‌کند). فقط وقتی هیچ کانفیگی انتخاب نشده باشد، صرفاً فورواردر خام برای دور زدن DPI روی HTTPS عادی اجرا می‌شود.",
}

STRATEGIES = [
    ("wrong_seq", "Wrong Sequence", "تزریق ClientHello جعلی با seq خارج از پنجره"),
    ("multi_fake", "Multi Fake", "چند بسته جعلی پشت‌سرهم"),
    ("fake_disorder", "Fake Disorder", "بی‌نظمی عمدی در ترتیب بسته‌ها"),
]


# ---------------------------------------------------------------------------
#  Page builders
# ---------------------------------------------------------------------------

def _section_title(text: str, sub: str = "") -> QWidget:
    # translate centrally (#6) so every page heading is bilingual at once
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    h = QLabel(tr(text))
    h.setObjectName("H1")
    h.setWordWrap(True)
    lay.addWidget(h)
    if sub:
        s = QLabel(tr(sub))
        s.setObjectName("Muted")
        # #5: wrap the subtitle so a long bilingual heading can never force the
        # page wider than the scroll viewport (which clipped the settings page).
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def fmt_bytes(n: float) -> str:
    """Human-readable byte total (e.g. 1.4 MB)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_rate(bps: float) -> str:
    """Human-readable throughput (e.g. 320 KB/s)."""
    return fmt_bytes(bps) + "/s"


# which connection modes are full tunnels vs. local proxy only
def mode_kind(mode: str) -> str:
    """Classify a connection mode for the dashboard badge.

    Only two modes remain (#5): ``"Tunnel"`` is a full tunnel through the
    selected config (→ ``"tunnel"``); everything else (``"SNI Only"`` / empty)
    is the local SNI-spoof proxy (→ ``"proxy"``).
    """
    return "tunnel" if (mode or "").strip().lower() == "tunnel" else "proxy"


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
    c.value_label = v       # exposed so callers can animate it (CountUp)
    c.caption_label = cap   # exposed so the caption can be retitled (#6)
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
        # #6: whether the selected config actually runs the SNI spoofer (and so
        # a bypass *strategy* is meaningful). For ordinary/direct configs the
        # spoofer + strategy are NOT in the path, so the dashboard must not claim
        # a strategy is "فعال". Updated by set_spoof_active() from the host.
        self._spoof_active = True
        # remember the last real strategy key so we can restore it when the user
        # switches back to a spoof config.
        self._strategy_key = "wrong_seq"
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        self.header = _section_title(
            "کنترل‌مرکز", "وضعیت زنده‌ی تونل، مصرف و کنترل سریع روشن/خاموش")
        root.addWidget(self.header)

        # --- status hero card ---
        hero = Card()
        hb = hero.body()
        row = QHBoxLayout()
        row.setSpacing(14)

        self.status_dot = PulseDot(diameter=12)
        self.status_label = QLabel(tr("آماده — متوقف"))
        self.status_label.setObjectName("H2")
        row.addWidget(self.status_dot)
        row.addWidget(self.status_label)
        row.addStretch(1)
        # tunnel / proxy badge — answers feedback 7 ("is this a tunnel or proxy?")
        self.mode_badge = QLabel(tr("پروکسی محلی"))
        self.mode_badge.setObjectName("ModeBadge")
        self.mode_badge.setProperty("kind", "proxy")
        row.addWidget(self.mode_badge)

        self.btn_start = PowerButton(palette)
        self.btn_start.request.connect(self._on_power)
        row.addWidget(self.btn_start)
        hb.addLayout(row)
        self.hero = hero
        root.addWidget(hero)

        # --- live throughput card (download / upload sparkline) ---
        traffic = Card()
        tb = traffic.body()
        thead = QHBoxLayout()
        thead.setSpacing(14)
        tlabel = QLabel(tr("مصرف زنده"))
        tlabel.setObjectName("H2")
        thead.addWidget(tlabel)
        thead.addStretch(1)
        self.rate_down = QLabel("↓ 0 B/s")
        self.rate_down.setObjectName("RateDown")
        self.rate_up = QLabel("↑ 0 B/s")
        self.rate_up.setObjectName("RateUp")
        thead.addWidget(self.rate_down)
        thead.addWidget(self.rate_up)
        tb.addLayout(thead)
        self.spark = Sparkline(capacity=60)
        self.spark.set_colors(palette.accent, palette.success)
        tb.addWidget(self.spark)
        self.traffic_card = traffic
        root.addWidget(traffic)

        # --- quick stats row ---
        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_conns = _stat_card("0", tr("اتصالات فعال"),
                                     accent_color=palette.accent)
        self.stat_total = _stat_card("0 B", tr("مصرف کل (↓/↑)"))
        self.stat_mode = _stat_card("Tunnel", tr("حالت"))
        self.stat_strategy = _stat_card("wrong_seq", tr("استراتژی فعال"))
        self.stat_cards = [self.stat_conns, self.stat_total,
                           self.stat_mode, self.stat_strategy]
        for c in self.stat_cards:
            stats.addWidget(c)
        root.addLayout(stats)

        # --- resilience strip (live fallback state) ---
        self.lbl_resilience = QLabel(tr("تاب‌آوری: —"))
        self.lbl_resilience.setObjectName("Muted")
        root.addWidget(self.lbl_resilience)

        root.addStretch(1)

        self._count = CountUp(self.stat_conns.value_label)
        self._sim_timers: list = []

    # -- entrance animation (called when page becomes visible) -------------
    def play_intro(self):
        stagger_in([self.header, self.hero, self.traffic_card,
                    *self.stat_cards], step=60)

    # -- power button → delegate to the engine via the host window ---------
    def _on_power(self, action: str):
        if self.power_handler:
            self.power_handler(action)

    # -- live updates pushed in from the engine bridge ---------------------
    def set_status(self, state: str):
        self.status_dot.set_state(state)
        self.btn_start.set_state(state)
        self.status_label.setText(tr({
            "idle": "آماده — متوقف",
            "connecting": "در حال اتصال…",
            "active": "متصل — تونل فعال",
            "error": "خطا — تلاش دوباره",
        }.get(state, "آماده — متوقف")))
        # track the live state so on_traffic can reject stray bytes that arrive
        # after the session ends (see on_traffic).
        self._live_state = state
        # Reset the live usage picture whenever the session is NOT actively
        # carrying traffic. Previously only "idle" cleared it, so a config that
        # FAILED (error) — e.g. a sabotaged spoof config demoted by the self-test
        # — left the last rate/total/sparkline frozen on screen, which the user
        # read as "data is still flowing even though it's broken / not
        # connected". Clearing on error/idle makes the dashboard honest.
        if state in ("idle", "error"):
            self.spark.clear()
            self.rate_down.setText("↓ 0 B/s")
            self.rate_up.setText("↑ 0 B/s")
            self.stat_total.value_label.setText("0 B / 0 B")
            self.lbl_resilience.setText(tr("تاب‌آوری: —"))

    def on_count(self, active: int, total: int):
        """Slot for the engine's connection-count signal."""
        self._count.to(active)

    def on_traffic(self, up_bytes: int, down_bytes: int,
                   up_bps: float, down_bps: float):
        """Slot for the engine's live traffic signal (step 20).

        Ignore traffic that arrives while the session is NOT active. A worker
        thread (stats poller / spoofer) can emit one last sample just after the
        engine demotes to error / stops, which would otherwise repaint the
        usage card we just cleared — making a broken/disconnected config look
        like it's "still exchanging data". Only an active session feeds the
        live picture.
        """
        if getattr(self, "_live_state", "idle") != "active":
            return
        self.spark.push(down_bps, up_bps)
        self.rate_down.setText(f"↓ {fmt_rate(down_bps)}")
        self.rate_up.setText(f"↑ {fmt_rate(up_bps)}")
        self.stat_total.value_label.setText(
            f"{fmt_bytes(down_bytes)} / {fmt_bytes(up_bytes)}")

    def set_resilience(self, text: str):
        """Slot for the live resilience/fallback summary line."""
        # #6: resilience/fallback only exists when the spoofer is in the path
        if not self._spoof_active:
            self.lbl_resilience.setText(tr("تاب‌آوری: غیرفعال (کانفیگ عادی)"))
            return
        self.lbl_resilience.setText(tr("تاب‌آوری: {text}").format(text=text))

    def set_active_strategy(self, key: str):
        # keep the real key so we can show it again on a spoof config (#6)
        if key:
            self._strategy_key = key
        self._render_strategy()

    def set_spoof_active(self, active: bool):
        """#6: tell the dashboard whether the active config uses the spoofer.

        For ordinary configs the bypass strategy + resilience layer are not
        engaged, so the dashboard shows «غیرفعال» instead of falsely claiming a
        strategy is running. The strategy stat-card caption also switches to
        make the meaning explicit.
        """
        active = bool(active)
        if active == self._spoof_active:
            self._render_strategy()
            return
        self._spoof_active = active
        self._render_strategy()
        # reset the resilience strip when spoofing isn't applicable
        if not active:
            self.lbl_resilience.setText(tr("تاب‌آوری: غیرفعال (کانفیگ عادی)"))
        else:
            self.lbl_resilience.setText(tr("تاب‌آوری: —"))

    def _render_strategy(self):
        """Paint the strategy stat-card according to spoof applicability (#6)."""
        if self._spoof_active:
            self.stat_strategy.value_label.setText(self._strategy_key)
            self.stat_strategy.caption_label.setText(tr("استراتژی فعال"))
        else:
            # ordinary config: no spoofing/strategy in the path
            self.stat_strategy.value_label.setText(tr("غیرفعال"))
            self.stat_strategy.caption_label.setText(tr("استراتژی (کانفیگ عادی)"))

    def set_mode(self, mode: str):
        self.stat_mode.value_label.setText(mode)
        kind = mode_kind(mode)
        self.mode_badge.setProperty("kind", kind)
        self.mode_badge.setText(
            tr("تونل کامل") if kind == "tunnel" else tr("پروکسی محلی"))
        # re-polish so the QSS property selector re-applies
        self.mode_badge.style().unpolish(self.mode_badge)
        self.mode_badge.style().polish(self.mode_badge)

    def _toast(self, text: str, kind: str):
        win = self.window()
        Toast.show_message(win, text, kind)

    def set_palette(self, palette):
        self._palette = palette
        self.btn_start.set_palette(palette)
        self.stat_conns.value_label.setStyleSheet(f"color:{palette.accent};")
        self.spark.set_colors(palette.accent, palette.success)


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
        form.setSpacing(8)

        form.addWidget(self._field_label("حالت اتصال"))
        self.mode = NoScrollComboBox()
        self.mode.addItems(MODES)
        form.addWidget(self.mode)
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Faint")
        self.mode_hint.setWordWrap(True)
        form.addWidget(self.mode_hint)
        self.mode.currentTextChanged.connect(self._update_mode_hint)
        self._update_mode_hint(self.mode.currentText())

        # --- SNI ↔ connect-IP pair manager (issue #3) ----------------------
        # Each fake SNI can be paired with the connect IP that is known to work
        # with it. Picking a saved SNI auto-fills its paired IP, and an "add"
        # button stores the current SNI+IP as a reusable pair.
        self._sni_ip_pairs: list[dict] = []

        form.addWidget(self._field_label("SNI جعلی"))
        self.sni = NoScrollComboBox()
        self.sni.setEditable(True)
        self.sni.addItems(DEFAULT_SNIS)
        # picking an existing item auto-fills the paired connect IP (#3)
        self.sni.activated.connect(self._on_sni_chosen)
        form.addWidget(self.sni)

        form.addWidget(self._field_label("IP اتصال"))
        # A SINGLE fake SNI can work with MANY connect IPs. This is now an
        # editable combo, not a line-edit: when you pick a saved SNI above, it
        # is repopulated with EVERY connect IP saved for that SNI so you can
        # choose which one to use right now (issue: "یک SNI با چند IP، فقط یکی
        # ذخیره می‌شد"). You can still type a fresh IP.
        self.connect_ip = NoScrollComboBox()
        self.connect_ip.setEditable(True)
        self.connect_ip.setInsertPolicy(NoScrollComboBox.NoInsert)
        self.connect_ip.setCurrentText("104.19.229.21")
        form.addWidget(self.connect_ip)

        # add / remove pair row
        pair_row = QHBoxLayout()
        pair_row.setSpacing(8)
        self.btn_add_pair = QPushButton(tr("افزودن جفت SNI/IP"))
        self.btn_add_pair.setObjectName("Ghost")
        self.btn_add_pair.setIcon(_icons.icon("plus", size=16))
        self.btn_add_pair.setIconSize(QSize(16, 16))
        self.btn_add_pair.clicked.connect(self._add_pair)
        self.btn_remove_pair = QPushButton(tr("حذف این جفت"))
        self.btn_remove_pair.setObjectName("Ghost")
        self.btn_remove_pair.setIcon(_icons.icon("trash", size=16))
        self.btn_remove_pair.setIconSize(QSize(16, 16))
        self.btn_remove_pair.clicked.connect(self._remove_pair)
        pair_row.addWidget(self.btn_add_pair)
        pair_row.addWidget(self.btn_remove_pair)
        pair_row.addStretch(1)
        self.lbl_pair_count = QLabel("")
        self.lbl_pair_count.setObjectName("Faint")
        pair_row.addWidget(self.lbl_pair_count)
        form.addLayout(pair_row)
        self.pair_hint = QLabel("")
        self.pair_hint.setObjectName("Muted")
        self.pair_hint.setWordWrap(True)
        self.pair_hint.setText(tr(
            "هر SNI جعلی می‌تواند چند IP اتصال داشته باشد. IP را وارد و «افزودن "
            "جفت» را بزنید (هر بار یک IP تازه به همان SNI اضافه می‌شود). وقتی "
            "همان SNI را انتخاب کنید، همهٔ IPهای ذخیره‌شده‌اش در فهرست «IP "
            "اتصال» می‌آیند تا یکی را انتخاب کنید."))
        form.addWidget(self.pair_hint)
        form.addSpacing(6)

        # NOTE: the multi-IP / multi-SNI route-pool controls (CONNECT_IPS /
        # FAKE_SNIS / optimise toggle / worker count) used to live here. They
        # have moved to their own card on the «استخر» (Pool) page so that
        # everything pool-related — the IP/SNI pool *and* the tester — sits
        # together. SettingsPage now only owns the single direct route
        # (CONNECT_IP / FAKE_SNI) and the saved sni_ip_pairs picker above.
        form.addSpacing(4)

        ports_wrap = QWidget()
        ports = QHBoxLayout(ports_wrap)
        ports.setContentsMargins(0, 0, 0, 0)
        ports.setSpacing(14)
        ports.addWidget(self._labelled_spin("پورت گوش‌دادن", 40443, out="listen"))
        ports.addWidget(self._labelled_spin("پورت SOCKS", 10808, out="socks"))
        form.addWidget(ports_wrap)
        form.addSpacing(6)

        # --- LAN sharing (use the proxy from a phone on the same Wi-Fi) ---
        # #5: keep the checkbox LABEL short (QCheckBox never word-wraps, so a
        # long label forced the whole page wider than the scroll viewport and
        # the right side got clipped). The full explanation lives in the
        # wrapping hint label right below each checkbox.
        form.addSpacing(8)
        self.chk_lan = QCheckBox(tr("اشتراک LAN (برای گوشی)"))
        form.addWidget(self.chk_lan)
        self.lan_hint = QLabel("")
        self.lan_hint.setObjectName("Muted")
        self.lan_hint.setWordWrap(True)
        form.addWidget(self.lan_hint)
        self.chk_lan.toggled.connect(self._update_lan_hint)

        # --- system proxy vs. tunnel (feedback 7) ---
        form.addSpacing(8)
        self.chk_system_proxy = QCheckBox(tr("پروکسی سیستم"))
        form.addWidget(self.chk_system_proxy)
        self.proxy_hint = QLabel("")
        self.proxy_hint.setObjectName("Muted")
        self.proxy_hint.setWordWrap(True)
        form.addWidget(self.proxy_hint)
        self.chk_system_proxy.toggled.connect(self._update_proxy_hint)

        # --- force SNI-spoof for ordinary configs (issue #1) ---
        form.addSpacing(8)
        self.chk_force_spoof = QCheckBox(tr("اسپوف SNI اجباری"))
        form.addWidget(self.chk_force_spoof)
        self.force_spoof_hint = QLabel("")
        self.force_spoof_hint.setObjectName("Muted")
        self.force_spoof_hint.setWordWrap(True)
        form.addWidget(self.force_spoof_hint)
        self.chk_force_spoof.toggled.connect(self._update_force_spoof_hint)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save = QPushButton(tr("ذخیره"))
        self.btn_save.setObjectName("Primary")
        save_row.addWidget(self.btn_save)
        form.addLayout(save_row)

        root.addWidget(card)
        root.addStretch(1)

    # -- config <-> widgets ------------------------------------------------
    def load_from(self, cfg: dict) -> None:
        """Populate the widgets from a config dict."""
        mode = cfg.get("connection_mode", "Tunnel")
        i = self.mode.findText(mode)
        if i >= 0:
            self.mode.setCurrentIndex(i)
        # load saved SNI↔IP pairs before populating the combo (issue #3)
        raw_pairs = cfg.get("sni_ip_pairs", []) or []
        self._sni_ip_pairs = [
            {"sni": str(p.get("sni", "")).strip(),
             "ip": str(p.get("ip", "")).strip()}
            for p in raw_pairs
            if isinstance(p, dict) and str(p.get("sni", "")).strip()
        ]
        self._rebuild_sni_combo()
        self.sni.setCurrentText(cfg.get("FAKE_SNI", "www.hcaptcha.com"))
        self.spin_listen.setValue(int(cfg.get("LISTEN_PORT", 40443)))
        self.spin_socks.setValue(int(cfg.get("socks_port", 10808)))
        self.connect_ip.setCurrentText(str(cfg.get("CONNECT_IP", "")))
        # populate the IP combo with every IP saved for the loaded SNI
        self._rebuild_ip_combo(self.sni.currentText().strip(),
                               prefer=str(cfg.get("CONNECT_IP", "")).strip())
        # NOTE: the multi-IP / multi-SNI pool lists (CONNECT_IPS / FAKE_SNIS /
        # POOL_OPTIMIZE_ENABLED) are now loaded by PoolPage.load_pool_settings,
        # not here — they moved to the «استخر» page.
        self.chk_lan.setChecked(bool(cfg.get("allow_lan", False)))
        self._update_lan_hint(self.chk_lan.isChecked())
        self.chk_system_proxy.setChecked(bool(cfg.get("system_proxy", False)))
        self._update_proxy_hint(self.chk_system_proxy.isChecked())
        self.chk_force_spoof.setChecked(bool(cfg.get("force_spoof", False)))
        self._update_force_spoof_hint(self.chk_force_spoof.isChecked())

    def collect(self) -> dict:
        """Read the widgets back into a config dict fragment."""
        return {
            "connection_mode": self.mode.currentText(),
            "FAKE_SNI": self.sni.currentText().strip(),
            "LISTEN_PORT": self.spin_listen.value(),
            "socks_port": self.spin_socks.value(),
            "CONNECT_IP": self.connect_ip.currentText().strip(),
            # CONNECT_IPS / FAKE_SNIS / POOL_OPTIMIZE_ENABLED are now collected
            # by PoolPage.collect_pool_settings (the pool controls moved there).
            "sni_ip_pairs": list(self._sni_ip_pairs),
            "allow_lan": self.chk_lan.isChecked(),
            "system_proxy": self.chk_system_proxy.isChecked(),
            "force_spoof": self.chk_force_spoof.isChecked(),
        }

    def set_mode(self, mode: str) -> None:
        """Programmatically select a connection mode (keeps hint in sync)."""
        i = self.mode.findText(mode)
        if i >= 0:
            self.mode.setCurrentIndex(i)

    def set_mode_applicable(self, applicable: bool) -> None:
        """Enable/disable the connection-mode selector (#6).

        The Tunnel / SNI-Only modes only matter for **spoof** configs (loopback
        share links that need our SNI spoofer). For an ordinary, routable config
        the app connects directly like a normal client, so the selector is
        disabled and an explanatory hint is shown instead — no spoofer is spun
        up and no system resources are wasted.
        """
        self._mode_applicable = bool(applicable)
        self.mode.setEnabled(applicable)
        if applicable:
            self._update_mode_hint(self.mode.currentText())
        else:
            self.mode_hint.setText(tr(
                "این کانفیگ آدرس سرور معمولی (غیرلوکال) دارد و مثل یک کلاینت "
                "معمولی مستقیماً وصل می‌شود؛ حالت تونل/SNI Only فقط برای "
                "کانفیگ‌های اسپوف (با IP لوکال) کاربرد دارد."))

    def _update_mode_hint(self, mode: str) -> None:
        # honour the "not applicable" state set by set_mode_applicable (#6)
        if getattr(self, "_mode_applicable", True) is False:
            return
        self.mode_hint.setText(tr(MODE_HINTS.get(mode, "")))

    def _update_lan_hint(self, on: bool) -> None:
        """Show the LAN address the phone should use when sharing is on."""
        if not on:
            self.lan_hint.setText(
                tr("خاموش — پروکسی فقط روی همین کامپیوتر (127.0.0.1) در دسترس است"))
            return
        try:
            from core.xray_manager import lan_ip_address
            ip = lan_ip_address()
        except Exception:
            ip = tr("<IP این کامپیوتر>")
        port = self.spin_socks.value()
        self.lan_hint.setText(
            tr("روشن — در گوشی، پروکسی SOCKS5 را روی {ip}:{port} تنظیم کنید "
               "(هر دو دستگاه باید روی یک شبکه/Wi-Fi باشند)").format(ip=ip, port=port))

    def _update_proxy_hint(self, on: bool) -> None:
        """Explain the tunnel-vs-system-proxy choice (feedback 7)."""
        if on:
            self.proxy_hint.setText(tr(
                "حالت «پروکسی سیستم»: هنگام اتصال، پروکسی ویندوز روی پورت HTTP "
                "محلی تنظیم می‌شود و با قطع اتصال خودکار برمی‌گردد. فقط در "
                "حالت‌های دارای xray (نه SNI Only) و روی ویندوز کار می‌کند."))
        else:
            self.proxy_hint.setText(tr(
                "حالت «تونل»: فقط برنامه‌هایی که دستی روی پروکسی محلی تنظیم "
                "شده‌اند رد می‌شوند؛ تنظیمات ویندوز دست‌نخورده می‌ماند."))

    def _update_force_spoof_hint(self, on: bool) -> None:
        """Explain the force-SNI-spoof option for ordinary configs (issue #1)."""
        if on:
            self.force_spoof_hint.setText(tr(
                "روشن — کانفیگ‌های معمولی (با IP/دامنه‌ی واقعی) هم به‌جای اتصال "
                "مستقیم، از طریق اسپوفر وصل می‌شوند: xray → اسپوفر → همان "
                "IP/پورت کانفیگ، با تزریق ClientHello جعلی برای دور زدن DPI. "
                "اگر کانفیگ تمیزی در V2RayTun کار می‌کند ولی اینجا مستقیم وصل "
                "نمی‌شود، این گزینه را روشن کنید (نیازمند دسترسی Administrator "
                "و درایور WinDivert)."))
        else:
            self.force_spoof_hint.setText(tr(
                "خاموش — کانفیگ‌های معمولی مستقیماً وصل می‌شوند (مثل V2RayTun)؛ "
                "فقط کانفیگ‌های اسپوف (لینک‌های لوکال) از اسپوفر رد می‌شوند."))

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(tr(t))
        lbl.setObjectName("Muted")
        return lbl

    def _labelled_spin(self, t: str, val: int, out: str) -> QWidget:
        from PySide6.QtWidgets import QSizePolicy
        w = QWidget()
        # let the VBox drive the height (label + spinbox + spacing). A fixed
        # min-height combined with addStretch was what squeezed the spinbox and
        # let the next row overlap it on the built app — use a content-driven
        # size policy instead so the field is always exactly as tall as it needs.
        w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lab = self._field_label(t)
        lay.addWidget(lab)
        sp = NoScrollSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(val)
        sp.setMinimumHeight(40)          # never clipped (the spinbox bug)
        sp.setButtonSymbols(QSpinBox.UpDownArrows)
        lay.addWidget(sp)
        setattr(self, f"spin_{out}", sp)
        return w

    # -- SNI ↔ IP pair manager (issue #3) ---------------------------------
    def _rebuild_sni_combo(self) -> None:
        """Repopulate the SNI combo from defaults + saved pairs, preserving
        the current text (so editing isn't disrupted)."""
        current = self.sni.currentText().strip()
        seen = set()
        names: list[str] = []
        # saved pairs first (most relevant to the user), then defaults
        for pair in self._sni_ip_pairs:
            s = (pair.get("sni") or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                names.append(s)
        for s in DEFAULT_SNIS:
            if s and s.lower() not in seen:
                seen.add(s.lower())
                names.append(s)
        self.sni.blockSignals(True)
        self.sni.clear()
        self.sni.addItems(names)
        if current:
            self.sni.setCurrentText(current)
        self.sni.blockSignals(False)
        self._update_pair_count()

    def _find_pair(self, sni: str) -> dict | None:
        sni = (sni or "").strip().lower()
        for pair in self._sni_ip_pairs:
            if (pair.get("sni") or "").strip().lower() == sni:
                return pair
        return None

    def _ips_for_sni(self, sni: str) -> list[str]:
        """Every connect IP saved for *sni* (de-duped, order preserved).

        A single fake SNI can front many IPs; this collects all of them so the
        IP combo can offer the full set for the user to pick from."""
        sni = (sni or "").strip().lower()
        out: list[str] = []
        seen: set = set()
        for pair in self._sni_ip_pairs:
            if (pair.get("sni") or "").strip().lower() != sni:
                continue
            ip = (pair.get("ip") or "").strip()
            if ip and ip.lower() not in seen:
                seen.add(ip.lower())
                out.append(ip)
        return out

    def _has_pair(self, sni: str, ip: str) -> bool:
        s = (sni or "").strip().lower()
        i = (ip or "").strip().lower()
        return any(
            (p.get("sni") or "").strip().lower() == s
            and (p.get("ip") or "").strip().lower() == i
            for p in self._sni_ip_pairs)

    def _rebuild_ip_combo(self, sni: str, prefer: str = "") -> None:
        """Repopulate the connect-IP combo with every IP saved for *sni*.

        Keeps the current/typed text when possible so the user's choice (or a
        freshly typed IP) is preserved."""
        ips = self._ips_for_sni(sni)
        current = (prefer or self.connect_ip.currentText()).strip()
        self.connect_ip.blockSignals(True)
        self.connect_ip.clear()
        self.connect_ip.addItems(ips)
        # keep selection/typed value
        if current:
            self.connect_ip.setCurrentText(current)
        elif ips:
            self.connect_ip.setCurrentText(ips[0])
        self.connect_ip.blockSignals(False)

    def _on_sni_chosen(self, _index: int) -> None:
        """When the user picks an SNI, list ALL its saved connect IPs in the IP
        combo and auto-select the first (issue: one SNI ↔ many IPs)."""
        sni = self.sni.currentText().strip()
        ips = self._ips_for_sni(sni)
        # rebuild the combo from the saved IPs; prefer the first saved IP
        self._rebuild_ip_combo(sni, prefer=(ips[0] if ips else ""))
        if len(ips) > 1:
            self.pair_hint.setText(tr(
                "این SNI با {n} IP ذخیره شده — از فهرست «IP اتصال» یکی را "
                "انتخاب کنید.").format(n=len(ips)))
        elif ips:
            self.pair_hint.setText(tr(
                "IP جفت‌شده پر شد: {ip}").format(ip=ips[0]))

    def _add_pair(self) -> None:
        """Save the current SNI + connect-IP as a reusable pair.

        Fix: a SNI can now hold MANY IPs — we APPEND a new (SNI, IP) instead of
        overwriting the SNI's single IP. Duplicates (same SNI+IP) are ignored."""
        sni = self.sni.currentText().strip()
        ip = self.connect_ip.currentText().strip()
        if not sni or not ip:
            self.pair_hint.setText(tr(
                "برای افزودن جفت، هم SNI جعلی و هم IP اتصال را پر کنید."))
            return
        if self._has_pair(sni, ip):
            self.pair_hint.setText(tr(
                "این جفت از قبل ذخیره شده است: «{sni}» ← {ip}").format(
                    sni=sni, ip=ip))
            return
        self._sni_ip_pairs.append({"sni": sni, "ip": ip})
        self._rebuild_sni_combo()
        self.sni.setCurrentText(sni)
        self._rebuild_ip_combo(sni, prefer=ip)
        n = len(self._ips_for_sni(sni))
        self.pair_hint.setText(tr(
            "جفت ذخیره شد: «{sni}» ← {ip} (این SNI اکنون {n} IP دارد)").format(
                sni=sni, ip=ip, n=n))

    def _remove_pair(self) -> None:
        """Remove the (SNI, currently-selected IP) pair.

        With multi-IP SNIs we remove only the EXACT pair shown (SNI + the IP in
        the combo), not every IP of that SNI."""
        sni = self.sni.currentText().strip()
        ip = self.connect_ip.currentText().strip()
        if not self._has_pair(sni, ip):
            self.pair_hint.setText(tr(
                "این جفت (SNI + IP انتخابی) ذخیره نشده است."))
            return
        s, i = sni.lower(), ip.lower()
        self._sni_ip_pairs = [
            p for p in self._sni_ip_pairs
            if not ((p.get("sni") or "").strip().lower() == s
                    and (p.get("ip") or "").strip().lower() == i)]
        self._rebuild_sni_combo()
        self.sni.setCurrentText(sni)
        self._rebuild_ip_combo(sni)
        left = self._ips_for_sni(sni)
        if left:
            self.pair_hint.setText(tr(
                "جفت «{sni}» ← {ip} حذف شد ({n} IP باقی ماند).").format(
                    sni=sni, ip=ip, n=len(left)))
        else:
            self.pair_hint.setText(tr(
                "جفت «{sni}» ← {ip} حذف شد.").format(sni=sni, ip=ip))

    def _update_pair_count(self) -> None:
        n = len(self._sni_ip_pairs)
        snis = len({(p.get("sni") or "").strip().lower()
                    for p in self._sni_ip_pairs if (p.get("sni") or "").strip()})
        if n:
            self.lbl_pair_count.setText(
                tr("{n} جفت در {s} SNI").format(n=n, s=snis))
        else:
            self.lbl_pair_count.setText("")


class _ViewportWidthListWidget(QListWidget):
    """A QListWidget whose item widgets are pinned to the viewport width.

    Plain QListWidget sizes each row to its *sizeHint* width, which can be
    wider than a narrow viewport — the row then renders at its natural width
    and its right-hand content (badges / action buttons) spills outside the
    visible box and gets clipped. This is the "از کادر زده بیرون که برش
    خورده" responsive bug (issue #2).

    By forcing every item's width to match ``viewport().width()`` on every
    resize, each ProfileRow is told *exactly* how much space it has, so its
    own ``_apply_responsive`` / elision logic can collapse decoration to fit
    and nothing ever overflows the frame.
    """

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_item_widths()

    def _sync_item_widths(self) -> None:
        vw = self.viewport().width()
        if vw <= 0:
            return
        for i in range(self.count()):
            item = self.item(i)
            if item is None:
                continue
            hint = item.sizeHint()
            if hint.width() != vw:
                hint.setWidth(vw)
                item.setSizeHint(hint)
            w = self.itemWidget(item)
            if w is not None:
                w.setMaximumWidth(vw)


class ProfilesPage(QWidget):
    """Import + manage server profiles (share links / subscriptions).

    v2rayN-style: the user pastes a ``vless://`` / ``vmess://`` / ``trojan://``
    / ``ss://`` link or a subscription URL; the page parses it via
    :mod:`core.share_link` and stores the resulting :class:`Profile`(s). The
    selected profile is what the engine chains xray + spoofing under.
    """

    # Bug-fix: cap how many inline pings run at once. "Ping all" used to spawn
    # one QThread *per profile* simultaneously — with a big list that flooded the
    # CPU/network and (combined with refresh() dropping references) crashed the
    # app. Pings now queue and run at most this many at a time.
    #
    # Kept deliberately LOW (3): each inactive-config ping now starts a REAL,
    # throwaway xray core (+ a spoofer for spoof configs) the v2rayNG way and
    # fetches through it. That is far heavier than the old hand-rolled socket
    # probe, so running too many at once would spawn a swarm of xray processes
    # and exhaust ports/CPU. v2rayNG throttles its real-delay sweep for the same
    # reason; 3 keeps "ping all" responsive without flooding the machine.
    _PING_MAX_CONCURRENCY = 3

    def __init__(self, store: ConfigStore, engine=None, parent=None):
        super().__init__(parent)
        self._store = store
        self._engine = engine          # EngineBridge — used for ping (optional)
        # Inline-ping job scheduler (rewritten for bug-fix: rapid "ping all" +
        # "select all" used to CRASH the app). The old design kept one worker per
        # *row index* in a dict that refresh() reset to ``{}`` — which dropped the
        # last Python reference to QThreads that were STILL RUNNING, so Qt tore
        # them down mid-run ("QThread: Destroyed while thread is still running")
        # and the process died. It also span an *unbounded* number of threads on
        # "ping all" (one per profile), flooding the machine.
        #
        # New model:
        #   * workers are keyed by a monotonic JOB id (never reused), and each job
        #     remembers which PROFILE it is pinging — not a fragile row index or a
        #     stale widget pointer. The result handler re-finds the live row by
        #     looking the profile up in the current store, so a refresh() that
        #     rebuilds every row never breaks (or crashes) an in-flight ping.
        #   * refresh() NO LONGER discards running workers — they stay referenced
        #     here until they emit their result, then clean themselves up.
        #   * concurrency is BOUNDED: at most ``_PING_MAX_CONCURRENCY`` workers run
        #     at once; the rest queue and start as slots free up. "Ping all" now
        #     enqueues every profile instead of spawning N threads immediately.
        self._inline_jobs: dict[int, "InlinePingWorker"] = {}
        self._inline_job_seq: int = 0
        # profiles currently pinging or queued (by identity) — used to skip
        # double-firing the same config and to map results back to a row.
        self._inline_pending: list[tuple[int, object]] = []   # (job_id, profile)
        self._inline_queue: list[tuple[object, str]] = []     # (profile, mode) waiting
        # per-profile measurement mode for the items currently in flight:
        # "delay" (real-delay ping) or "download" (sustained speed test).
        self._inline_modes: dict[int, str] = {}               # job_id -> mode
        # Completed ping results, keyed by a stable profile key so they SURVIVE
        # a refresh()/row-rebuild (bug: "بعد از پینگ هر کاری کنم پینگ‌ها میرن").
        # refresh() re-applies these to the rebuilt rows; only a fresh ping (or
        # an edit/removal of that profile) replaces the stored value.
        self._ping_results: dict[str, tuple[str, str]] = {}   # key -> (text,kind)
        # #7: indexes that are *checked* for bulk actions (delete / copy links).
        # This is independent of the active profile — checking a row never
        # activates it. Stored as a set so order doesn't matter.
        self._checked: set[int] = set()
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
        # multi-line box so several links can be pasted at once (#7). One link
        # per line — exactly what users copy out of channels/sub pages.
        self.input = QPlainTextEdit()
        self.input.setObjectName("ImportBox")
        self.input.setPlaceholderText(
            "یک یا چند لینک را اینجا بچسبانید — هر لینک در یک خط\n"
            "vless://…\ntrojan://…\nیا یک لینک سابسکریپشن")
        self.input.setMaximumHeight(96)
        self.input.setTabChangesFocus(True)
        ib.addWidget(self.input)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.btn_import = QPushButton(tr("افزودن لینک‌ها"))
        self.btn_import.setObjectName("Primary")
        self.btn_paste = QPushButton(tr("از کلیپ‌بورد"))
        self.btn_paste.setObjectName("Ghost")
        self.btn_sub = QPushButton(tr("افزودن سابسکریپشن"))
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

        # header row: title (left) + live selection-count (right)
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head_row.addWidget(self._field_label("سرورهای ذخیره‌شده"))
        head_row.addStretch(1)
        self.lbl_sel_count = QLabel("")
        self.lbl_sel_count.setObjectName("Muted")
        head_row.addWidget(self.lbl_sel_count)
        lb.addLayout(head_row)

        # #4: compact ICON toolbar ABOVE the list (was a stack of wide text
        # buttons below it). Each action is a single tinted icon button with a
        # tooltip, so the toolbar stays on one line at any width and the list
        # itself gets all the remaining vertical space.
        tools = QHBoxLayout()
        tools.setSpacing(7)

        # track icon toolbar buttons so a theme change can recolour them (#1)
        self._tool_buttons: list[tuple[QPushButton, str]] = []

        def _tool(icon_name: str, obj: str, tip: str) -> QPushButton:
            b = QPushButton()
            b.setObjectName(obj)
            b.setProperty("class", "ToolBtn")
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(34, 30)
            b.setIconSize(QSize(18, 18))
            b.setIcon(_icons.icon(icon_name, size=18))
            b.setToolTip(tr(tip))
            self._tool_buttons.append((b, icon_name))
            return b

        # bulk-selection actions (operate on the checkboxes — never activate)
        self.btn_select_all = _tool("check_all", "Ghost", "انتخاب همه")
        self.btn_clear_sel = _tool("uncheck_all", "Ghost", "لغو انتخاب")
        self.btn_ping_all_rows = _tool("ping", "Ghost",
                                       "پینگ واقعی همه (تأخیر — v2rayNG)")
        self.btn_speed_all_rows = _tool("download", "Ghost",
                                        "تست سرعت دانلود همه (مطمئن‌تر، اتصال مدت‌دار)")
        self.btn_ping_selected = _tool("broadcast", "Ghost",
                                       "پینگ کانفیگ‌های انتخاب‌شده")
        self.btn_speed_selected = _tool("download", "Ghost",
                                        "تست سرعت دانلود کانفیگ‌های انتخاب‌شده")
        self.btn_copy_selected = _tool("link", "Ghost",
                                       "کپی لینک کانفیگ‌های انتخاب‌شده")
        self.btn_edit = _tool("edit", "Ghost", "ویرایش کانفیگ انتخاب‌شده")
        self.btn_delete_selected = _tool("trash", "Danger",
                                         "حذف کانفیگ‌های انتخاب‌شده")

        for b in (self.btn_select_all, self.btn_clear_sel):
            tools.addWidget(b)
        sep = QFrame(); sep.setObjectName("ToolSep"); sep.setFixedWidth(1)
        tools.addWidget(sep)
        for b in (self.btn_ping_all_rows, self.btn_speed_all_rows,
                  self.btn_ping_selected, self.btn_speed_selected,
                  self.btn_copy_selected, self.btn_edit):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self.btn_delete_selected)
        lb.addLayout(tools)

        self.list = _ViewportWidthListWidget()
        self.list.setObjectName("ProfileList")
        # give the list real breathing room so several servers are visible and
        # rows never get vertically squeezed (the "cramped / clipped" feedback).
        # With the pre-connect panel removed (#4) the list now owns all the free
        # vertical space (stretch=1 below) so many configs are visible at once.
        self.list.setMinimumHeight(260)
        self.list.setSpacing(6)
        from PySide6.QtWidgets import QAbstractItemView, QSizePolicy
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(False)
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lb.addWidget(self.list, 1)

        # let the list card take all the remaining height of the page
        root.addWidget(listc, 1)

        # wiring
        self.btn_import.clicked.connect(self._import_link)
        self.btn_paste.clicked.connect(self._paste)
        self.btn_sub.clicked.connect(self._import_subscription)
        self.btn_edit.clicked.connect(self._edit_selected)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemDoubleClicked.connect(lambda *_: self._edit_selected())
        self.btn_ping_all_rows.clicked.connect(self._ping_all_inline)
        self.btn_speed_all_rows.clicked.connect(self._speed_all_inline)
        # #7: bulk-selection wiring
        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_clear_sel.clicked.connect(self._clear_selection)
        self.btn_ping_selected.clicked.connect(self._ping_selected)
        self.btn_speed_selected.clicked.connect(self._speed_selected)
        self.btn_copy_selected.clicked.connect(self._copy_selected_links)
        self.btn_delete_selected.clicked.connect(self._delete_checked)

        self.refresh()

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(tr(t))
        lbl.setObjectName("Muted")
        return lbl

    # -- list rendering ----------------------------------------------------
    def refresh(self) -> None:
        # Rows are about to be rebuilt. We must NOT touch ``self._inline_jobs``
        # here: those QThreads may still be RUNNING, and dropping the last
        # reference to a running QThread crashes the whole app (this was the
        # "ping all + select all → نرم‌افزار بسته میشه" crash). The jobs are keyed
        # by job-id and remember their *profile*, so when each one finishes its
        # result handler re-finds the (new) row widget by looking the profile up
        # in the freshly-rebuilt list — no stale widget pointer, no crash.
        # recolour the icon toolbar for the active theme (#1)
        for b, name in getattr(self, "_tool_buttons", []):
            try:
                b.setIcon(_icons.icon(name, size=18))
            except Exception:
                pass
        self.list.blockSignals(True)
        self.list.clear()
        sel = self._store.selected_index
        # #7: drop any checked indexes that no longer exist (list shrank)
        self._checked = {i for i in self._checked
                         if 0 <= i < len(self._store.profiles)}
        self._rows = []
        for i, p in enumerate(self._store.profiles):
            item = QListWidgetItem(self.list)
            row = ProfileRow(p, active=(i == sel), checked=(i in self._checked))
            row.edit.connect(lambda _=False, idx=i: self._edit_index(idx))
            # one-click activation straight from the row (#8)
            row.activate.connect(lambda _=False, idx=i: self._activate_index(idx))
            # inline per-row ping (#3)
            row.ping.connect(lambda _=False, idx=i: self._ping_row(idx))
            # inline per-row DOWNLOAD speed test (PR #34)
            row.download.connect(lambda _=False, idx=i: self._download_row(idx))
            # inline per-row single delete (PR #34)
            row.delete_one.connect(
                lambda _=False, idx=i: self._delete_one_index(idx))
            # copy this config back to a share link (issue #2)
            row.share.connect(lambda _=False, idx=i: self._share_index(idx))
            # scan clean Cloudflare IPs using this config as reference (issue #3)
            row.scan.connect(lambda _=False, idx=i: self._scan_index(idx))
            # #7: multi-select checkbox toggled — track for bulk actions
            row.selection_toggled.connect(
                lambda checked, idx=i: self._on_row_checked(idx, checked))
            self._rows.append(row)
            # use a guaranteed row height so the active "● فعال" pill + badges
            # never get clipped (sizeHint can under-report before layout), but
            # CLAMP it to the row's own max height so the cell can't grow taller
            # than the widget and leave it overflowing/cut off when the window
            # enlarges (#3 "از کادر زده بیرون که برش خورده").
            hint = row.sizeHint()
            hint.setHeight(min(max(hint.height(), 62), 64))
            # pin the width to the viewport so the row is told exactly how much
            # horizontal space it has and never overflows its box (#2)
            vw = self.list.viewport().width()
            if vw > 0:
                hint.setWidth(vw)
                row.setMaximumWidth(vw)
            item.setSizeHint(hint)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        if 0 <= sel < self.list.count():
            self.list.setCurrentRow(sel)
        # empty-state hint
        if self.list.count() == 0:
            ph = QListWidgetItem(tr("هنوز پروفایلی اضافه نشده — یک لینک بچسبانید"))
            ph.setFlags(Qt.NoItemFlags)
            self.list.addItem(ph)
        self.list.blockSignals(False)
        # final pass to pin widths to the current viewport (#2)
        self.list._sync_item_widths()
        # re-apply any COMPLETED ping result first, then overlay the busy state
        # for rows still pinging. Order matters: a row that's both cached AND
        # re-pinging should show the spinner, not the stale number.
        self._restore_ping_results()
        self._restore_pinging_rows()
        self._update_selection_ui()

    def _restore_ping_results(self) -> None:
        """Re-paint cached ping results onto the freshly rebuilt rows.

        Keeps a measured ping visible until the user explicitly re-pings (or
        edits/removes that profile) — instead of clearing the moment any other
        action triggers a refresh().
        """
        results = getattr(self, "_ping_results", {})
        if not results:
            return
        live_keys = set()
        for i, prof in enumerate(self._store.profiles):
            key = self._profile_key(prof)
            live_keys.add(key)
            if i >= len(self._rows):
                continue
            cached = results.get(key)
            if not cached:
                continue
            text, kind = cached
            try:
                self._rows[i].set_ping_state(text, kind)
                self._rows[i].set_ping_idle()
            except RuntimeError:
                pass
        # prune results for profiles that no longer exist (edited/removed) so
        # the cache can't grow without bound.
        stale = [k for k in results if k not in live_keys]
        for k in stale:
            results.pop(k, None)

    def _restore_pinging_rows(self) -> None:
        pending_profiles = [p for _, p in getattr(self, "_inline_pending", [])]
        pending_profiles += [p for p, _ in getattr(self, "_inline_queue", [])]
        if not pending_profiles:
            return
        for i, prof in enumerate(self._store.profiles):
            if i < len(self._rows) and any(p is prof for p in pending_profiles):
                try:
                    self._rows[i].set_pinging()
                except RuntimeError:
                    pass

    # -- multi-select / bulk actions (#7) ---------------------------------
    def _on_row_checked(self, index: int, checked: bool) -> None:
        """Track a row's multi-select checkbox without activating it (#7)."""
        if checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        """Refresh the selection count label + enable/disable bulk buttons."""
        n = len(self._checked)
        if n:
            self.lbl_sel_count.setText(
                tr("{n} مورد انتخاب شده").format(n=n))
        else:
            self.lbl_sel_count.setText(tr("هیچ موردی انتخاب نشده"))
        for b in (self.btn_ping_selected, self.btn_speed_selected,
                  self.btn_copy_selected, self.btn_delete_selected):
            b.setEnabled(n > 0)
        has_rows = bool(self._store.profiles)
        self.btn_select_all.setEnabled(has_rows)
        self.btn_clear_sel.setEnabled(n > 0)

    def _select_all(self) -> None:
        self._checked = set(range(len(self._store.profiles)))
        self.refresh()

    def _clear_selection(self) -> None:
        self._checked = set()
        self.refresh()

    def _ping_selected(self) -> None:
        """Ping ONLY the checked rows, concurrently (#1 follow-up)."""
        if self._engine is None:
            self._toast(tr("موتور در دسترس نیست"), "err")
            return
        if not self._checked:
            self._toast(tr("هیچ کانفیگی انتخاب نشده"), "warn")
            return
        self._toast(tr("در حال پینگ کانفیگ‌های انتخاب‌شده …"), "info")
        for row in sorted(self._checked):
            self._ping_row(row)

    def _speed_selected(self) -> None:
        """Download-speed test ONLY the checked rows, concurrently (PR #34)."""
        if self._engine is None:
            self._toast(tr("موتور در دسترس نیست"), "err")
            return
        if not self._checked:
            self._toast(tr("هیچ کانفیگی انتخاب نشده"), "warn")
            return
        self._toast(tr("در حال تست سرعت دانلود کانفیگ‌های انتخاب‌شده …"), "info")
        for row in sorted(self._checked):
            self._ping_row(row, mode="download")

    def _copy_selected_links(self) -> None:
        """Copy the share links of every checked profile, one per line (#7)."""
        if not self._checked:
            self._toast(tr("هیچ کانفیگی انتخاب نشده"), "warn")
            return
        from core.share_link import profile_to_link
        links: list[str] = []
        failed = 0
        for i in sorted(self._checked):
            if not (0 <= i < len(self._store.profiles)):
                continue
            try:
                links.append(profile_to_link(self._store.profiles[i]))
            except Exception:
                failed += 1
        if not links:
            self._toast(tr("ساخت لینک برای موارد انتخاب‌شده ناموفق بود"), "err")
            return
        QGuiApplication.clipboard().setText("\n".join(links))
        if failed:
            self._toast(
                tr("{n} لینک کپی شد ({f} مورد ناموفق)").format(
                    n=len(links), f=failed), "warn")
        else:
            self._toast(
                tr("{n} لینک کپی شد").format(n=len(links)), "ok")

    def _delete_checked(self) -> None:
        """Delete every checked profile in one batch (#7 bulk delete)."""
        if not self._checked:
            self._toast(tr("هیچ کانفیگی انتخاب نشده"), "warn")
            return
        from PySide6.QtWidgets import QMessageBox
        n = len(self._checked)
        resp = QMessageBox.question(
            self.window(),
            tr("حذف دسته‌ای"),
            tr("آیا {n} کانفیگ انتخاب‌شده حذف شود؟").format(n=n),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        removed = self._store.remove_profiles(self._checked)
        self._checked = set()
        self.refresh()
        self._emit_selection()
        self._toast(tr("{n} کانفیگ حذف شد").format(n=removed), "warn")

    # -- actions -----------------------------------------------------------
    def _toast(self, text: str, kind: str = "info"):
        Toast.show_message(self.window(), text, kind)

    @staticmethod
    def _split_links(text: str) -> list[str]:
        """Split a pasted blob into individual share links.

        Accepts one-per-line *and* several links crammed on one line (we split
        on whitespace before each ``scheme://``). Blank lines are dropped.
        """
        import re
        text = (text or "").strip()
        if not text:
            return []
        # put a newline before every scheme:// so glued links separate too
        text = re.sub(r"\s+(?=[a-zA-Z][a-zA-Z0-9+.\-]*://)", "\n", text)
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                out.append(line)
        return out

    def _import_link(self):
        """Paste one or many links → parse → add (bulk-aware, #7).

        * A single link still opens the editable dialog pre-filled so the user
          can review/tweak fields before adding.
        * Multiple links are added in one go (bulk) — no per-link dialog — so
          importing a whole list is one paste + one click. Lines that fail to
          parse are reported but never abort the rest.
        """
        links = self._split_links(self.input.toPlainText())
        if not links:
            return

        # single link → keep the familiar review-then-add dialog flow
        if len(links) == 1:
            try:
                profile = parse_link(links[0])
            except ShareLinkError as exc:
                self._toast(tr("لینک نامعتبر: {exc}").format(exc=exc), "err")
                return
            dlg = ProfileDialog(profile, self.window(),
                                title=tr("افزودن پروفایل جدید"))
            if dlg.exec() != ProfileDialog.Accepted:
                self._toast(tr("افزودن لغو شد"), "info")
                return
            edited = dlg.result_profile
            # #1: do not auto-activate the newly added profile if one is
            # already active — only the first-ever profile becomes active.
            self._store.add_profile(edited, select=False)
            self.input.clear()
            self.refresh()
            self._toast(tr("پروفایل افزوده شد: {name}").format(name=edited.display_name), "ok")
            self._emit_selection()
            return

        # multiple links → bulk add, skipping (and counting) bad ones
        parsed: list[Profile] = []
        bad = 0
        for link in links:
            try:
                parsed.append(parse_link(link))
            except ShareLinkError:
                bad += 1
        if not parsed:
            self._toast(tr("هیچ لینک معتبری یافت نشد"), "err")
            return
        added = self._store.add_profiles(parsed)
        self.input.clear()
        self.refresh()
        if bad:
            self._toast(tr("{added} پروفایل افزوده شد ({bad} لینک نامعتبر رد شد)")
                        .format(added=added, bad=bad), "warn")
        else:
            self._toast(tr("{added} پروفایل افزوده شد").format(added=added), "ok")
        self._emit_selection()

    def _edit_selected(self):
        """Open the editor on the currently selected profile and save edits."""
        row = self.list.currentRow()
        if not (0 <= row < len(self._store.profiles)):
            self._toast(tr("ابتدا یک پروفایل را انتخاب کنید"), "warn")
            return
        self._edit_index(row)

    def _edit_index(self, row: int):
        """Open the editor on a specific profile row and save edits."""
        if not (0 <= row < len(self._store.profiles)):
            return
        current = self._store.profiles[row]
        dlg = ProfileDialog(current, self.window(), title=tr("ویرایش پروفایل"))
        if dlg.exec() != ProfileDialog.Accepted:
            return
        self._store.profiles[row] = dlg.result_profile
        self._store.save_profiles()
        self.refresh()
        # re-emit so the engine picks up edits to the active profile
        if row == self._store.selected_index:
            self._emit_selection()
        self._toast(tr("پروفایل به‌روزرسانی شد"), "ok")

    def _import_subscription(self):
        text = self.input.toPlainText().strip()
        if not text:
            self._toast(tr("ابتدا متن/URL سابسکریپشن را وارد کنید"), "warn")
            return
        blob = text
        if text.startswith("http://") or text.startswith("https://"):
            blob = self._fetch(text)
            if blob is None:
                return
        profiles = parse_subscription(blob)
        if not profiles:
            self._toast(tr("هیچ پروفایل معتبری در سابسکریپشن یافت نشد"), "warn")
            return
        added = self._store.add_profiles(profiles)
        self.input.clear()
        self.refresh()
        self._toast(tr("{added} پروفایل از سابسکریپشن افزوده شد").format(added=added), "ok")
        self._emit_selection()

    def _fetch(self, url: str) -> str | None:
        import urllib.request
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self._toast(tr("واکشی سابسکریپشن ناموفق: {exc}").format(exc=exc), "err")
            return None

    def _paste(self):
        cb = QGuiApplication.clipboard()
        self.input.setPlainText(cb.text().strip())

    def _delete_selected(self):
        row = self.list.currentRow()
        if row < 0:
            return
        self._store.remove_profile(row)
        self.refresh()
        self._emit_selection()
        self._toast(tr("پروفایل حذف شد"), "warn")

    def _row_changed(self, row: int):
        # Highlighting a row no longer activates it (#1/#2): activation is an
        # explicit action via the row's «فعال‌سازی» button or _activate_index.
        # This keeps the running server stable while the user browses the list.
        pass

    def _activate_index(self, row: int):
        """One-click activation: select this profile as the active server (#8).

        No dialog, no extra steps — exactly what the user asked for. Refreshes
        the list so the green ● فعال pill moves to the chosen row immediately.
        """
        if not (0 <= row < len(self._store.profiles)):
            return
        self._store.select(row)
        self.refresh()
        self._emit_selection()
        prof = self._store.profiles[row]
        self._toast(tr("سرور فعال شد: {name}").format(name=prof.display_name), "ok")

    # -- share / export to link (issue #2) --------------------------------
    def _share_index(self, row: int):
        """Re-serialise a profile back to a share link and copy it (issue #2)."""
        if not (0 <= row < len(self._store.profiles)):
            return
        prof = self._store.profiles[row]
        try:
            from core.share_link import profile_to_link
            link = profile_to_link(prof)
        except Exception as exc:
            self._toast(tr("ساخت لینک ناموفق: {exc}").format(exc=exc), "err")
            return
        QGuiApplication.clipboard().setText(link)
        self._toast(
            tr("لینک کانفیگ کپی شد — حالا می‌توانید به اشتراک بگذارید"), "ok")

    # -- Cloudflare clean-IP scanner (issue #3) ---------------------------
    def _scan_index(self, row: int):
        """Open the clean-IP scanner using this profile as the reference (#3).

        Clean IPs found by the scan are turned into new profiles — byte-for-byte
        identical to the reference config except their server address is the
        chosen clean IP — and added to the store.
        """
        if not (0 <= row < len(self._store.profiles)):
            return
        prof = self._store.profiles[row]
        try:
            from ui.scanner_dialog import ScannerDialog
        except Exception as exc:
            self._toast(tr("اسکنر در دسترس نیست: {exc}").format(exc=exc), "err")
            return
        dlg = ScannerDialog(prof, self.window())
        if dlg.exec() != ScannerDialog.Accepted:
            return
        new_profiles = list(dlg.result_profiles)
        if not new_profiles:
            return
        added = self._store.add_profiles(new_profiles)
        self.refresh()
        self._emit_selection()
        self._toast(
            tr("{n} کانفیگ با IP تمیز افزوده شد").format(n=added), "ok")

    # -- inline per-row ping (bounded, crash-safe) ------------------------
    def _profile_pending(self, prof) -> bool:
        """True if this exact profile already has a queued / running ping."""
        if any(p is prof for _, p in self._inline_pending):
            return True
        if any(p is prof for p, _ in self._inline_queue):
            return True
        return False

    def _enqueue_ping(self, prof, mode: str = "delay") -> None:
        """Queue a profile for an inline measurement (de-duplicated).

        ``mode`` is "delay" (real-delay ping) or "download" (speed test).
        """
        if prof is None or self._profile_pending(prof):
            return
        self._inline_queue.append((prof, mode))

    def _ping_row(self, row: int, mode: str = "delay"):
        """Ping a single profile and show the result inline on its row.

        ``mode`` is "delay" (latency in ms — the default 📡 button) or
        "download" (sustained download-speed test in Mbps — the ⇩ button,
        PR #34).

        Pings are now QUEUED and run with bounded concurrency (see
        ``_PING_MAX_CONCURRENCY``). A profile that's already queued/running is
        skipped, so double-clicking 📡 or hitting "ping all" repeatedly can't
        pile up duplicate threads.
        """
        if not (0 <= row < len(self._store.profiles)):
            return
        if self._engine is None:
            self._toast(tr("موتور در دسترس نیست"), "err")
            return
        prof = self._store.profiles[row]
        if self._profile_pending(prof):
            return
        rows = getattr(self, "_rows", [])
        if 0 <= row < len(rows):
            try:
                rows[row].set_pinging()
            except RuntimeError:
                pass
        self._enqueue_ping(prof, mode)
        self._pump_ping_queue()

    def _download_row(self, row: int):
        """Run a DOWNLOAD speed test on a single row (PR #34)."""
        self._ping_row(row, mode="download")

    def _delete_one_index(self, idx: int) -> None:
        """Delete a single config straight from its row trash button (PR #34)."""
        if not (0 <= idx < len(self._store.profiles)):
            return
        from PySide6.QtWidgets import QMessageBox
        prof = self._store.profiles[idx]
        remark = getattr(prof, "remark", "") or tr("بدون نام")
        resp = QMessageBox.question(
            self.window(),
            tr("حذف کانفیگ"),
            tr("آیا کانفیگ «{name}» حذف شود؟").format(name=remark),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        self._store.remove_profiles({idx})
        # keep the checked set consistent with the now-shifted indexes
        self._checked = {(i if i < idx else i - 1)
                         for i in self._checked if i != idx}
        self.refresh()
        self._emit_selection()
        self._toast(tr("کانفیگ حذف شد"), "warn")

    def _ping_all_inline(self):
        """Queue an inline ping on **every** row (#4 — "ping all").

        Bug-fix: this used to spawn one QThread per profile *immediately*. It now
        enqueues every profile and lets ``_pump_ping_queue`` start them a few at
        a time, so a big list (or "ping all" + "select all" fired together) can
        no longer flood threads and crash the app.
        """
        if self._engine is None:
            self._toast(tr("موتور در دسترس نیست"), "err")
            return
        if not self._store.profiles:
            self._toast(tr("هیچ پروفایلی برای پینگ نیست"), "warn")
            return
        self._toast(tr("در حال پینگ همهٔ سرورها …"), "info")
        self._enqueue_all_inline("delay")

    def _speed_all_inline(self):
        """Queue a DOWNLOAD speed test on every row (v2rayNG-style).

        A sustained download is far more reliable than a one-shot delay ping —
        a config that can't actually carry traffic simply won't stream bytes,
        so fast false-negatives (the user's "first configs ping red") are much
        less likely.
        """
        if self._engine is None:
            self._toast(tr("موتور در دسترس نیست"), "err")
            return
        if not self._store.profiles:
            self._toast(tr("هیچ پروفایلی برای تست نیست"), "warn")
            return
        self._toast(tr("در حال تست سرعت دانلود همهٔ سرورها …"), "info")
        self._enqueue_all_inline("download")

    def _enqueue_all_inline(self, mode: str):
        rows = getattr(self, "_rows", [])
        for row, prof in enumerate(self._store.profiles):
            if self._profile_pending(prof):
                continue
            if 0 <= row < len(rows):
                try:
                    rows[row].set_pinging()
                except RuntimeError:
                    pass
            self._enqueue_ping(prof, mode)
        self._pump_ping_queue()

    def _pump_ping_queue(self) -> None:
        """Start queued pings up to the concurrency cap."""
        if self._engine is None:
            return
        # config is shared by all probes — push it once per pump, not per row
        try:
            self._engine.update_config(self._store.config)
        except Exception:
            pass
        while (self._inline_queue
               and len(self._inline_pending) < self._PING_MAX_CONCURRENCY):
            prof, mode = self._inline_queue.pop(0)
            self._inline_job_seq += 1
            job_id = self._inline_job_seq
            self._inline_modes[job_id] = mode
            worker = InlinePingWorker(self._engine, prof, parent=self,
                                      mode=mode)
            worker.result.connect(
                lambda text, kind, jid=job_id:
                    self._inline_ping_done(jid, text, kind))
            self._inline_jobs[job_id] = worker
            self._inline_pending.append((job_id, prof))
            worker.start()

    def _row_index_for_profile(self, prof) -> int:
        """Find the CURRENT row index of a profile by identity (refresh-safe)."""
        for i, p in enumerate(self._store.profiles):
            if p is prof:
                return i
        return -1

    @staticmethod
    def _profile_key(prof) -> str:
        """Stable identity for caching a ping result across row rebuilds.

        Keyed by the config-defining fields (address/port/uuid/password/sni/
        path) so the cached result follows the *config*, not a transient Python
        object — a re-parsed Profile for the same link still matches.
        """
        try:
            return "|".join(str(getattr(prof, f, "") or "") for f in (
                "address", "port", "uuid", "password", "sni", "path"))
        except Exception:
            return repr(prof)

    def _inline_ping_done(self, job_id: int, text: str, kind: str):
        # locate the profile this job was pinging
        prof = None
        for jid, p in self._inline_pending:
            if jid == job_id:
                prof = p
                break
        # persist the result so it SURVIVES any later refresh()/row rebuild
        # (bug: results vanished the moment the user did anything else).
        if prof is not None:
            self._ping_results[self._profile_key(prof)] = (text, kind)
        # re-find the LIVE row widget by profile identity — never a stale pointer
        if prof is not None:
            row = self._row_index_for_profile(prof)
            rows = getattr(self, "_rows", [])
            if 0 <= row < len(rows):
                try:
                    rows[row].set_ping_state(text, kind)
                    rows[row].set_ping_idle()
                except RuntimeError:
                    pass
        # retire the worker safely: wait for the thread to actually finish before
        # dropping our reference, so we never GC a still-running QThread (crash).
        worker = self._inline_jobs.pop(job_id, None)
        if worker is not None:
            try:
                worker.wait(2000)
            except Exception:
                pass
            try:
                worker.deleteLater()
            except Exception:
                pass
        self._inline_pending = [
            (jid, p) for jid, p in self._inline_pending if jid != job_id]
        self._inline_modes.pop(job_id, None)
        # a slot just freed up — start the next queued ping
        self._pump_ping_queue()

    def stop_inline_pings(self) -> None:
        """Drain the queue and wait for running ping workers to finish.

        Called on shutdown so the app never tears down a QThread that's still
        running (which would crash). Safe to call multiple times.
        """
        self._inline_queue = []
        for worker in list(self._inline_jobs.values()):
            try:
                if worker.isRunning():
                    worker.wait(3000)
            except Exception:
                pass
            try:
                worker.deleteLater()
            except Exception:
                pass
        self._inline_jobs = {}
        self._inline_pending = []
        self._inline_modes = {}

    def _emit_selection(self):
        if self.on_selection_changed:
            self.on_selection_changed(self._store.selected_profile)

    # NOTE: the standalone "سنجش پیش از اتصال" panel (batch ping / strategy-test
    # with its own output box) was removed (#4): it no longer served a purpose,
    # took space away from the server list, and duplicated the per-row inline
    # 📡 ping. Per-server and "ping all" measurements now happen inline on each
    # row via _ping_row / _ping_all_inline (InlinePingWorker below).


class InlinePingWorker(QThread):
    """Ping ONE profile on a worker thread and emit a compact inline result.

    Emits ``result(text, kind)`` once, where ``kind`` ∈ {"ok","err"} so the
    row can tint the inline text. Used by the per-row 📡 button (#3).
    """

    result = Signal(str, str)

    def __init__(self, engine, profile, parent=None, mode="delay"):
        super().__init__(parent)
        self._engine = engine
        self._profile = profile
        self._mode = mode  # "delay" (real-delay ping) or "download" (speed)

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        # Bug #1 — make the ping HONEST.
        #
        # The hard truth: no *offline* probe can faithfully tell whether a config
        # works, because spoof configs only succeed when the running spoofer
        # injects a DECOY SNI to slip past DPI. An offline probe that presents
        # the config's REAL SNI to the CDN edge gets DPI-blocked → it shows no
        # ping even though the config works (the user's "اسپوف پینگ نداد ولی کار
        # میکرد"). Conversely a /cdn-cgi/trace to any live anycast IP answers for
        # ANY SNI, so an ordinary config can look green yet not route ("پینگ
        # میدادن ولی کار نمیکردن").
        #
        # So we measure differently depending on what we can actually observe:
        #
        #   1. If the tunnel is RUNNING this exact config → send a real request
        #      THROUGH the live proxy. That travels the genuine chain (with the
        #      spoofer's decoy injection) and is the single most trustworthy
        #      "does it work + how fast" answer. Marked with 🛡 (تونل زنده).
        #   2. Otherwise → an offline reachability estimate that is clearly
        #      labelled as such (≈) and never over-claims. We only assert
        #      "blocked" when even the raw transport is unreachable; a reachable
        #      transport is reported as a tentative latency, not a guarantee.
        try:
            self._run_inner()
        except Exception as exc:
            try:
                self.result.emit(tr("خطا: {exc}").format(exc=exc), "err")
            except Exception:
                pass

    def _run_inner(self):
        # --- 1) live tunnel measurement for the active config (definitive) ---
        try:
            is_active = bool(self._engine.is_active_profile(self._profile))
        except Exception:
            is_active = False
        if is_active:
            # Respect the requested mode even for the active config: a download
            # test on the connected server must measure DOWNLOAD through the
            # live tunnel — not fall back to a latency ms (the user's bug:
            # "پینگ دانلود روی کانفیگ فعال، ms می‌داد").
            if self._mode == "download":
                try:
                    ok, mbps, _detail = self._engine.live_proxy_download(
                        duration=8.0)
                except Exception:
                    ok, mbps = False, None
                if ok and mbps is not None:
                    self.result.emit(
                        tr("🛡⇩ {mbps:.1f} Mbps (تونل زنده)").format(mbps=mbps),
                        "ok")
                    return
                self.result.emit(tr("✖ دانلود تونل زنده انجام نشد"), "err")
                return
            try:
                ok, ms, _detail = self._engine.live_proxy_ping(samples=2)
            except Exception:
                ok, ms = False, None
            if ok and ms is not None:
                self.result.emit(
                    tr("🛡 {ms:.0f}ms (تونل زنده)").format(ms=ms), "ok")
                return
            # tunnel is up but the live request failed — that IS meaningful:
            # this config is selected yet not actually carrying traffic.
            self.result.emit(tr("✖ تونل زنده پاسخ نداد"), "err")
            return

        # --- 2) ANY inactive config → REAL delay, the v2rayNG way -----------
        # The hand-rolled offline probes (manual TLS handshake + /cdn-cgi/trace
        # + WS upgrade) were fundamentally unreliable and produced every bug the
        # user kept hitting:
        #   * AYYILDIZ7 (relay path /stars/http://user:pass@vps…) pinged red even
        #     though it connects — a bare WS upgrade can't validate a relay route.
        #   * a deliberately-BROKEN vls-cf-xhttp pinged GREEN — a trace to a live
        #     Cloudflare anycast IP answers for ANY config, working or not.
        #   * spoof configs got no number at all.
        #
        # v2rayNG gets this right by NOT guessing: it starts the REAL core with
        # the config's own outbound on a throwaway local proxy, fetches a known
        # URL THROUGH it, and times the round-trip. A broken config fails the
        # fetch (honest red); a working one returns the real body (honest green)
        # and the elapsed time IS the real delay. ``measure_profile_delay`` does
        # exactly that — chaining the spoofer underneath for spoof configs, so
        # the decoy-SNI injection is in the path just like a real connect. One
        # code path now serves relay / xhttp / spoof / plain configs identically.

        # --- DOWNLOAD speed test mode (sustained connection) ---------------
        # The more reliable test the user asked for: pull real bytes through
        # the config's core for a window and report throughput. A route that
        # can't carry traffic simply won't stream — no fast false-negative.
        if self._mode == "download":
            try:
                # The engine picks the right wall-clock budget per config type:
                # ordinary configs are judged fast (~8 s), while SPOOF configs —
                # which are slow to ESTABLISH — get a longer window + an
                # automatic second chance (modeled on v2rayN's failed-part
                # retest) so a working-but-slow-to-wake spoof isn't false-red,
                # yet a genuinely dead config still bails in seconds instead of
                # hanging for a minute. We DON'T pass an explicit deadline here
                # so that per-config logic applies.
                ok, mbps, _detail = self._engine.measure_profile_download(
                    self._profile, duration=6.0)
            except Exception as exc:
                self.result.emit(tr("خطا: {exc}").format(exc=exc), "err")
                return
            if ok and mbps is not None:
                self.result.emit(
                    tr("⇩ {mbps:.1f} Mbps (دانلود)").format(mbps=mbps), "ok")
                return
            self.result.emit(tr("✖ دانلودی انجام نشد"), "err")
            return

        # --- REAL delay mode (default) -------------------------------------
        try:
            # Same per-config budget logic as download: ordinary configs answer
            # fast (~6 s cap), spoof configs get a longer budget + warm-up + one
            # automatic retry so a slow-to-establish spoof isn't false-red. No
            # explicit deadline → the engine applies the right per-config cap.
            ok, ms, _detail = self._engine.measure_profile_delay(
                self._profile, timeout=15.0)
        except Exception as exc:
            self.result.emit(tr("خطا: {exc}").format(exc=exc), "err")
            return
        if ok and ms is not None:
            # a real, body-verified round-trip through the config's own core.
            self.result.emit(tr("✔ {ms:.0f}ms (واقعی)").format(ms=ms), "ok")
            return
        # the fetch through the config's real core failed → it genuinely does
        # not carry traffic right now (broken route / blocked / dead Worker).
        self.result.emit(tr("✖ بدون پاسخ (کار نمی‌کند)"), "err")


class StrategyPage(QWidget):
    """The 'final boss' surface — arsenal of bypass strategies + auto-prober."""

    # emitted when the auto-prober toggle changes (True == enabled)
    auto_prober_changed = Signal(bool)
    # emitted when the user clicks a strategy card to select it manually
    strategy_selected = Signal(str)

    def __init__(self, store: "ConfigStore | None" = None, parent=None):
        super().__init__(parent)
        self.store = store
        self._cards: dict[str, QFrame] = {}
        self._selected = (str(store.get("bypass_method", "wrong_seq"))
                          if store else "wrong_seq")

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "استراتژی عبور", "زرادخانه‌ی روش‌های دور زدن DPI + پراب خودکار (غول مرحله آخر)"))

        # auto-prober toggle card
        ap = Card()
        apb = ap.body()
        row = QHBoxLayout()
        t = QLabel(tr("پراب خودکار"))
        t.setObjectName("H2")
        desc = QLabel(tr("بهترین استراتژی را خودکار آزمایش، رتبه‌بندی و قفل می‌کند"))
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

        # manual-pick hint
        self.pick_hint = QLabel("")
        self.pick_hint.setObjectName("Faint")
        self.pick_hint.setWordWrap(True)
        root.addWidget(self.pick_hint)
        self._sync_pick_hint(enabled)

        # clickable strategy list
        for key, name, desc in STRATEGIES:
            root.addWidget(self._strategy_row(key, name, desc))

        self._refresh_selection()
        root.addStretch(1)

    def _sync_autoprobe_label(self, enabled: bool) -> None:
        self.btn_autoprobe.setText(tr("فعال ✓") if enabled else tr("فعال‌سازی"))

    def _sync_pick_hint(self, auto_enabled: bool) -> None:
        if auto_enabled:
            self.pick_hint.setText(
                tr("پراب خودکار روشن است؛ انتخاب دستی نادیده گرفته می‌شود. ")
                + tr("برای انتخاب دستی، ابتدا پراب خودکار را خاموش کنید."))
        else:
            self.pick_hint.setText(
                tr("روی هر استراتژی کلیک کنید تا به‌صورت دستی انتخاب/قفل شود."))

    def _on_autoprobe_toggled(self, enabled: bool) -> None:
        self._sync_autoprobe_label(enabled)
        self._sync_pick_hint(enabled)
        self._refresh_selection()
        if self.store is not None:
            self.store.set("auto_prober", bool(enabled))
        self.auto_prober_changed.emit(bool(enabled))

    def _strategy_row(self, key: str, name: str, desc: str) -> QFrame:
        c = Card(object_name="StrategyCard")
        c.setProperty("selected", False)
        c.setCursor(Qt.PointingHandCursor)
        b = c.body()
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel(tr(name))
        nm.setObjectName("H2")
        ds = QLabel(tr(desc))
        ds.setObjectName("Faint")
        col.addWidget(nm)
        col.addWidget(ds)
        row.addLayout(col)
        row.addStretch(1)
        check = QLabel("")
        check.setObjectName("StrategyCheck")
        row.addWidget(check)
        badge = QLabel(key)
        badge.setObjectName("Mono")
        row.addWidget(badge)
        b.addLayout(row)
        # make the whole card clickable
        c.mousePressEvent = lambda ev, k=key: self._on_card_clicked(k)
        # hover-lift: deepen the shadow + raise the card on enter (3D feel)
        c.enterEvent = lambda ev, card=c: card.set_shadow(
            blur=46, y=16, color="rgba(0,0,0,0.6)")
        c.leaveEvent = lambda ev, card=c: card.set_shadow(
            blur=34, y=10, color="rgba(0,0,0,0.55)")
        c._check_label = check  # stash for selection rendering
        self._cards[key] = c
        return c

    def _on_card_clicked(self, key: str) -> None:
        # manual pick disables auto-prober (the two are mutually exclusive)
        if self.btn_autoprobe.isChecked():
            self.btn_autoprobe.setChecked(False)  # fires _on_autoprobe_toggled
        self._selected = key
        self._refresh_selection()
        if self.store is not None:
            self.store.set("bypass_method", key)
        self.strategy_selected.emit(key)

    def _refresh_selection(self) -> None:
        """Repaint cards so the active one stands out (and re-polish QSS)."""
        auto = self.btn_autoprobe.isChecked()
        for key, card in self._cards.items():
            is_sel = (not auto) and (key == self._selected)
            card.setProperty("selected", is_sel)
            if hasattr(card, "_check_label"):
                card._check_label.setText(tr("✓ انتخاب‌شده") if is_sel else "")
            # re-polish so the [selected="true"] QSS applies immediately
            card.style().unpolish(card)
            card.style().polish(card)


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
        self.lbl_active = QLabel(tr("استراتژی فعال: —"))
        self.lbl_active.setObjectName("H2")
        self.lbl_status = QLabel(tr("وضعیت: بی‌کار"))
        self.lbl_status.setObjectName("Faint")
        sb.addWidget(self.lbl_active)
        sb.addWidget(self.lbl_status)
        root.addWidget(summary)

        # --- throughput / throttle card ---
        tp = Card()
        tb = tp.body()
        h = QLabel(tr("توان عبوری (throughput)"))
        h.setObjectName("H2")
        tb.addWidget(h)
        # a plain-language explanation so the user knows exactly what this
        # number means and why it may be empty (feedback #4 — "نمی‌فهمم چیه و
        # هیچ کاری نمی‌کنه"). Throughput = how many bytes/sec are flowing right
        # now; the bar compares that to the connection's own baseline to flag
        # active throttling by the censor.
        self.lbl_tp_help = QLabel(tr(
            "سرعت لحظه‌ای عبور داده از تونل را نشان می‌دهد. نوار، سرعت فعلی را با "
            "«خط پایه‌ی» همین اتصال مقایسه می‌کند تا اگر سانسورچی سرعت را خفه کرد "
            "(throttle) معلوم شود. تا وقتی متصل نشده‌اید یا ترافیکی رد و بدل نشده، "
            "داده‌ای برای نمایش نیست."))
        self.lbl_tp_help.setObjectName("Faint")
        self.lbl_tp_help.setWordWrap(True)
        tb.addWidget(self.lbl_tp_help)
        # live current throughput (always shown while connected, even before a
        # baseline exists — this is the "it does nothing" fix)
        self.lbl_tp_live = QLabel(tr("سرعت فعلی: —"))
        self.lbl_tp_live.setObjectName("H2")
        tb.addWidget(self.lbl_tp_live)
        self.bar_tp = QProgressBar()
        self.bar_tp.setRange(0, 100)
        self.bar_tp.setTextVisible(False)
        tb.addWidget(self.bar_tp)
        self.lbl_tp = QLabel(tr("بدون داده"))
        self.lbl_tp.setObjectName("Faint")
        self.lbl_tp.setWordWrap(True)
        tb.addWidget(self.lbl_tp)
        self.lbl_rst = QLabel(tr("RST جعلی: —"))
        self.lbl_rst.setObjectName("Faint")
        tb.addWidget(self.lbl_rst)
        self.lbl_chain = QLabel(tr("زنجیره‌ی fallback: —"))
        self.lbl_chain.setObjectName("Faint")
        self.lbl_chain.setWordWrap(True)
        tb.addWidget(self.lbl_chain)
        root.addWidget(tp)

        # --- candidate health table card ---
        cand = Card()
        cb = cand.body()
        ch = QLabel(tr("کاندیداها (probe)"))
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
            tr("استراتژی فعال: {s}").format(s=snap.active_strategy or '—'))
        st = tr(self._STATUS_FA.get(snap.status, snap.status))
        port = tr(" · پورت {p}").format(p=snap.spoof_port) if snap.spoof_port else ""
        self.lbl_status.setText(tr("وضعیت: {st}{port}").format(st=st, port=port))

        # live current throughput — always shown so the card never looks dead
        # while connected (the "هیچ کاری نمی‌کند" complaint, #4).
        if snap.recent_bps > 0:
            self.lbl_tp_live.setText(
                tr("سرعت فعلی: {v}").format(v=self._fmt_bps(snap.recent_bps)))
        elif snap.status == "active":
            self.lbl_tp_live.setText(tr("سرعت فعلی: در انتظار ترافیک…"))
        else:
            self.lbl_tp_live.setText(tr("سرعت فعلی: — (متصل نیست)"))

        # throughput bar = recent/baseline ratio (clamped to 100%). The
        # baseline is the best sustained speed this connection has reached;
        # a sharp drop below it ⇒ likely throttling.
        ratio = snap.throttle_ratio
        if snap.baseline_bps > 0:
            pct = max(0, min(100, int(ratio * 100)))
            self.bar_tp.setValue(pct)
            tag = tr("  ⚠ احتمال throttle!") if snap.throttled else ""
            self.lbl_tp.setText(
                tr("{pct}% از خط پایه — {recent} از {base}{tag}").format(
                    pct=pct, recent=self._fmt_bps(snap.recent_bps),
                    base=self._fmt_bps(snap.baseline_bps), tag=tag))
        elif snap.status == "active":
            self.bar_tp.setValue(0)
            self.lbl_tp.setText(
                tr("در حال ساختن خط پایه… (برای سنجش throttle کمی ترافیک لازم است)"))
        else:
            self.bar_tp.setValue(0)
            self.lbl_tp.setText(tr("بدون داده — پس از اتصال و عبور ترافیک پر می‌شود"))

        if snap.resilience_on:
            self.lbl_rst.setText(
                tr("RST جعلی: {n} / بودجه {b}").format(
                    n=snap.forged_rst_count, b=snap.rst_budget))
            chain = " → ".join(snap.strategy_chain) or "—"
            ips = " → ".join(snap.ip_chain) or "—"
            self.lbl_chain.setText(
                tr("زنجیره‌ی استراتژی: {chain}\nزنجیره‌ی IP: {ips}").format(
                    chain=chain, ips=ips))
        else:
            self.lbl_rst.setText(tr("تاب‌آوری غیرفعال است"))
            self.lbl_chain.setText(tr("زنجیره‌ی fallback: —"))

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
            return tr("هنوز probe انجام نشده — هنگام اتصال با «پراب خودکار» پر می‌شود.")
        lines = [f"{tr('استراتژی'):<22}{tr('امتیاز'):>8}{tr('موفقیت'):>9}{tr('نمونه'):>7}  {tr('وضعیت')}"]
        for c in snap.candidates:
            mark = "★ " if c.selected else "  "
            lines.append(
                f"{mark}{c.key:<20}{c.mean_score:>8.2f}"
                f"{c.success_rate*100:>8.0f}%{c.samples:>7}  {c.last_outcome}")
        return "\n".join(lines)


class _PoolScanWorker(QThread):
    """Run the SNI/IP sweep off the GUI thread for the inline pool scanner.

    The scanner's probe sockets live on daemon worker threads; this QThread only
    *drains* the result queue and re-emits each verdict as a **queued Qt signal**
    so the GUI thread is never touched from a raw probe thread (that was the
    original freeze/crash). The thread is intentionally unparented.
    """

    # results_batch carries a LIST of verdict dicts so the GUI repaints in
    # bulk (a few times per second) instead of once per probe — this is the
    # freeze fix. ``result`` is kept for any single-shot callers/tests.
    result = Signal(dict)
    results_batch = Signal(list)
    progress = Signal(int, int)
    finished_scan = Signal(int, int)

    def __init__(self, candidates, *, port: int, timeout: float,
                 workers: int = 8):
        super().__init__()
        self._candidates = candidates
        self._port = port
        self._timeout = timeout
        self._workers = max(1, int(workers))
        self._scanner = None

    def stop(self):
        if self._scanner is not None:
            self._scanner.stop()

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        from core.pool import SniIpScanner
        # Bounded concurrency (8 by default, not 16): 16 simultaneous TLS
        # handshakes per cycle generated a verdict storm the GUI couldn't keep
        # up with. Combined with batched emits below, the UI stays responsive.
        self._scanner = SniIpScanner(
            self._candidates, port=self._port, timeout=self._timeout,
            workers=self._workers,
            on_results_batch=self.results_batch.emit,
            on_progress=self.progress.emit, on_done=self.finished_scan.emit)
        try:
            self._scanner.run()
        except Exception:
            self.finished_scan.emit(0, len(self._candidates))


class PoolPage(QWidget):
    """Live picture of the multi-IP / multi-SNI route pool (core.pool).

    Pure renderer: polls a zero-arg ``provider`` that returns a plain dict
    snapshot (the shape produced by
    :meth:`core.pool.CombinationExplorer.summary` plus a couple of extra
    keys) on a timer and repaints. No engine/pool internals are touched here,
    so the GUI stays fully decoupled from the core — and tests can feed a fake
    provider.

    Expected snapshot keys (all optional, sane fallbacks)::

        enabled            bool   — is the pool active for the current config?
        total/known/stable/weak/dead/unexplored  int  — pair counts
        seconds_since_check  float|None  — age of the last health-check cycle
        active             int    — pairs currently serving connections
        rows               list[dict]  — per-pair rows from summary()
                            {ip, sni, loss, alive, active, in_pool}
    """

    # column indices for the inline scan results table
    SC_IP, SC_SNI, SC_LAT, SC_STATUS, SC_SAVED = range(5)
    MAX_CANDIDATES = 600
    _STATUS_FA = {"pending": "در صف", "testing": "در حال آزمایش…",
                  "ok": "✓ سالم", "fail": "✗ ناموفق"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider = None          # callable -> snapshot dict
        self._last_snap = None         # latest snapshot (for the export action)
        self._store = None             # ConfigStore (wired by the host)
        self._scan_worker = None       # _PoolScanWorker | None
        self._row_for_key = {}         # (ip,sni) -> scan table row
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title(
            "استخر مسیرها", "وضعیت زنده‌ی استخر چند-IP / چند-SNI"))

        # --- summary card -------------------------------------------------
        summary = Card()
        sb = summary.body()
        self.lbl_state = QLabel(tr("استخر: —"))
        self.lbl_state.setObjectName("H2")
        self.lbl_counts = QLabel(tr("مسیرها: —"))
        self.lbl_counts.setObjectName("Faint")
        self.lbl_check = QLabel(tr("آخرین سلامت‌سنجی: —"))
        self.lbl_check.setObjectName("Faint")
        # Redesign: live active route vs best candidate found by the optimiser.
        self.lbl_route = QLabel(tr("مسیر فعال: —"))
        self.lbl_route.setObjectName("Faint")
        self.lbl_route.setWordWrap(True)
        # per-IP rapid-failover state (7.8): shows any IP currently blocked
        self.lbl_failover = QLabel(tr("بازیابی خودکار: —"))
        self.lbl_failover.setObjectName("Faint")
        self.lbl_failover.setWordWrap(True)
        self.lbl_help = QLabel(tr(
            "اول با مسیر فعلی (که می‌دانیم وصل می‌شود) متصل می‌شویم؛ سپس استخر "
            "چند-IP/چند-SNI در پس‌زمینه تست می‌کند و اگر مسیری «به‌روشنی بهتر» "
            "پیدا کرد، آن را بدون قطع اتصال جایگزین می‌کند. بهترین نتیجه برای هر "
            "کانفیگ ذخیره می‌شود تا دفعهٔ بعد از همان شروع کنیم. با تیکِ "
            "«بهینه‌سازی مسیر در پس‌زمینه» در تنظیمات می‌توانید تست را خاموش کنید."))
        self.lbl_help.setObjectName("Faint")
        self.lbl_help.setWordWrap(True)
        sb.addWidget(self.lbl_state)
        sb.addWidget(self.lbl_counts)
        sb.addWidget(self.lbl_check)
        sb.addWidget(self.lbl_route)
        sb.addWidget(self.lbl_failover)
        sb.addWidget(self.lbl_help)
        root.addWidget(summary)

        # --- pool IP/SNI settings card (moved here from «تنظیمات») --------
        # Everything that defines the route pool now lives on this page so the
        # pool definition and the tester sit together. The widgets keep the same
        # config keys (CONNECT_IPS / FAKE_SNIS / POOL_OPTIMIZE_ENABLED) plus the
        # new scan_workers control for the inline tester.
        root.addWidget(self._build_pool_settings_card())

        # --- saved SNI/IP list card --------------------------------------
        # The user's reusable sni_ip_pairs list — the thing the import/export
        # buttons actually operate on. This is ALWAYS shown (independent of the
        # live pool) so the buttons never act on an "empty" view and the user
        # can see exactly what they have saved, how many, and remove pairs.
        saved = Card()
        sv = saved.body()
        shead = QHBoxLayout()
        sh = QLabel(tr("فهرست ذخیره‌شده‌ی من (IP × SNI)"))
        sh.setObjectName("H2")
        shead.addWidget(sh)
        self.lbl_saved_count = QLabel(tr("۰ جفت"))
        self.lbl_saved_count.setObjectName("Faint")
        shead.addWidget(self.lbl_saved_count)
        shead.addStretch(1)
        # Inline manual scan ("شروع تست"): toggles the scan panel below (no
        # separate window — the dialog used to feel like it froze on open). The
        # sweep tests every (IP, SNI) pair once against the DPI directly and the
        # user adds the good ones to their reusable sni_ip_pairs list.
        self.btn_scan = QPushButton(tr("شروع تست"))
        self.btn_scan.setObjectName("Primary")
        self.btn_scan.clicked.connect(self._toggle_scan_panel)
        shead.addWidget(self.btn_scan)
        # 7.10 — export the saved SNI/IP list (and any live routes) to a file.
        self.btn_export = QPushButton(tr("خروجی فهرست SNI/IP…"))
        self.btn_export.setObjectName("Ghost")
        self.btn_export.clicked.connect(self._on_export)
        shead.addWidget(self.btn_export)
        # import a previously-exported SNI/IP list straight into sni_ip_pairs,
        # so the user never has to copy-paste a list back in by hand.
        self.btn_import = QPushButton(tr("وارد کردن فهرست…"))
        self.btn_import.setObjectName("Ghost")
        self.btn_import.clicked.connect(self._on_import)
        shead.addWidget(self.btn_import)
        # clear the whole saved list (with confirmation) — there was no way to
        # do this from the UI before.
        self.btn_clear_saved = QPushButton(tr("حذف فهرست"))
        self.btn_clear_saved.setObjectName("Ghost")
        self.btn_clear_saved.clicked.connect(self._on_clear_saved)
        shead.addWidget(self.btn_clear_saved)
        sv.addLayout(shead)
        self.saved_tbl = QPlainTextEdit()
        self.saved_tbl.setObjectName("Log")
        self.saved_tbl.setReadOnly(True)
        self.saved_tbl.setMinimumHeight(140)
        sv.addWidget(self.saved_tbl)
        root.addWidget(saved)

        # --- live pool routes card (only meaningful while connected) ------
        pairs = Card()
        pb = pairs.body()
        head = QHBoxLayout()
        ph = QLabel(tr("مسیرهای زنده‌ی استخر (هنگام اتصال)"))
        ph.setObjectName("H2")
        head.addWidget(ph)
        head.addStretch(1)
        pb.addLayout(head)
        self.tbl = QPlainTextEdit()
        self.tbl.setObjectName("Log")
        self.tbl.setReadOnly(True)
        self.tbl.setMinimumHeight(150)
        pb.addWidget(self.tbl)
        root.addWidget(pairs)

        # --- inline scan panel (hidden until "شروع تست") ------------------
        self.scan_card = self._build_scan_card()
        self.scan_card.setVisible(False)
        root.addWidget(self.scan_card, 1)

        # poll timer (started/stopped when the page becomes visible)
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------------
    #  pool IP/SNI settings card (moved from SettingsPage)
    # ------------------------------------------------------------------
    def _build_pool_settings_card(self) -> "Card":
        """Build the multi-IP / multi-SNI route-pool settings card.

        Owns CONNECT_IPS / FAKE_SNIS / POOL_OPTIMIZE_ENABLED (previously on the
        Settings page) plus the new ``scan_workers`` control for the inline
        tester. A dedicated «ذخیره استخر» button persists just these keys via
        the host callback so the user does not have to hop to Settings to save.
        """
        card = Card()
        form = card.body()
        form.setSpacing(8)

        title = QLabel(tr("تنظیمات استخر مسیر (چند IP / چند SNI)"))
        title.setObjectName("H2")
        form.addWidget(title)

        # opt-in checkbox. OFF → single fixed route, no background testing.
        # ON → pool tests in the background and swaps in a strictly-better
        # route losslessly. Default ON.
        self.chk_pool_optimize = QCheckBox(tr("بهینه‌سازی مسیر در پس‌زمینه"))
        self.chk_pool_optimize.setChecked(True)
        form.addWidget(self.chk_pool_optimize)
        opt_hint = QLabel(tr(
            "روشن: اول با مسیر فعلی وصل می‌شویم، استخر در پس‌زمینه تست می‌کند و "
            "مسیر بهتر را بدون قطع جایگزین می‌کند (بهترین نتیجه برای هر کانفیگ "
            "ذخیره می‌شود).\nخاموش: فقط همان SNI/IP تکی ثابت می‌ماند، هیچ تستی "
            "انجام نمی‌شود."))
        opt_hint.setObjectName("Muted")
        opt_hint.setWordWrap(True)
        form.addWidget(opt_hint)
        form.addSpacing(4)

        lbl_ips = QLabel(tr("IPهای استخر — هر خط یک IP"))
        lbl_ips.setObjectName("Muted")
        form.addWidget(lbl_ips)
        self.pool_ips = QPlainTextEdit()
        self.pool_ips.setObjectName("PoolList")
        self.pool_ips.setPlaceholderText(tr(
            "خالی = فقط همان «IP اتصال» تنظیمات.\n"
            "172.66.41.252\n108.162.196.145\n172.65.13.230"))
        self.pool_ips.setFixedHeight(78)
        form.addWidget(self.pool_ips)

        lbl_snis = QLabel(tr("SNIهای استخر — هر خط یک SNI"))
        lbl_snis.setObjectName("Muted")
        form.addWidget(lbl_snis)
        self.pool_snis = QPlainTextEdit()
        self.pool_snis.setObjectName("PoolList")
        self.pool_snis.setPlaceholderText(tr(
            "خالی = فقط همان «SNI جعلی» تنظیمات.\n"
            "apple.com\ngithub.com\nmicrosoft.com"))
        self.pool_snis.setFixedHeight(78)
        form.addWidget(self.pool_snis)

        self.pool_hint = QLabel("")
        self.pool_hint.setObjectName("Muted")
        self.pool_hint.setWordWrap(True)
        form.addWidget(self.pool_hint)
        self.pool_ips.textChanged.connect(self._update_pool_hint)
        self.pool_snis.textChanged.connect(self._update_pool_hint)
        self.chk_pool_optimize.toggled.connect(self._update_pool_hint)
        form.addSpacing(6)

        # scan_workers: parallel probes used by the inline tester ("شروع تست").
        # Fewer workers = a calmer GUI during a sweep (the freeze fix); more =
        # a faster sweep. Clamped 1..32.
        wrow = QHBoxLayout()
        wrow.setSpacing(8)
        wlbl = QLabel(tr("تعداد کارگرهای آزمایش (موازی):"))
        wlbl.setObjectName("Muted")
        wrow.addWidget(wlbl)
        self.spin_workers = NoScrollSpinBox()
        self.spin_workers.setRange(1, 32)
        self.spin_workers.setValue(8)
        self.spin_workers.setMinimumHeight(36)
        self.spin_workers.setButtonSymbols(QSpinBox.UpDownArrows)
        wrow.addWidget(self.spin_workers)
        wrow.addStretch(1)
        form.addLayout(wrow)
        wkr_hint = QLabel(tr(
            "کمتر = رابط نرم‌تر هنگام آزمایش؛ بیشتر = آزمایش سریع‌تر. اگر برنامه "
            "هنگام تست کند می‌شود این عدد را کم کنید."))
        wkr_hint.setObjectName("Muted")
        wkr_hint.setWordWrap(True)
        form.addWidget(wkr_hint)
        form.addSpacing(6)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save_pool = QPushButton(tr("ذخیره استخر"))
        self.btn_save_pool.setObjectName("Primary")
        self.btn_save_pool.clicked.connect(self._save_pool_settings)
        save_row.addWidget(self.btn_save_pool)
        form.addLayout(save_row)

        self._update_pool_hint()
        return card

    # -- multi-IP / multi-SNI pool helpers (moved from SettingsPage) ------
    @staticmethod
    def _parse_lines(text: str) -> list:
        """Split a textarea into a clean, de-duplicated list (one per line)."""
        seen = set()
        out = []
        for raw in (text or "").splitlines():
            v = raw.strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        return out

    def _pool_ip_list(self) -> list:
        return self._parse_lines(self.pool_ips.toPlainText())

    def _pool_sni_list(self) -> list:
        return self._parse_lines(self.pool_snis.toPlainText())

    def _update_pool_hint(self) -> None:
        """Live preview of how many (IP, SNI) routes the pool will build."""
        if not hasattr(self, "pool_hint"):
            return
        ips = self._pool_ip_list()
        snis = self._pool_sni_list()
        # mirror config_store.pool_enabled(): empty lists fall back to the
        # single CONNECT_IP / FAKE_SNI, so the pool needs >1 resulting pair.
        eff_ips = len(ips) or 1
        eff_snis = len(snis) or 1
        pairs = eff_ips * eff_snis
        optimize = self.chk_pool_optimize.isChecked()
        if not optimize:
            self.pool_hint.setText(tr(
                "بهینه‌سازی خاموش — فقط مسیر تکیِ ثابت استفاده می‌شود "
                "(بدون تست پس‌زمینه)."))
        elif pairs <= 1:
            self.pool_hint.setText(tr(
                "تنها یک مسیر تعریف شده — اول با همین وصل می‌شویم؛ برای جایگزینی "
                "بدون قطع، چند IP/SNI اضافه کنید تا در پس‌زمینه تست شوند."))
        else:
            self.pool_hint.setText(tr(
                "بهینه‌ساز فعال — {ips} IP × {snis} SNI = {pairs} مسیر در "
                "پس‌زمینه تست می‌شوند؛ مسیر بهتر بدون قطع جایگزین می‌شود.").format(
                    ips=eff_ips, snis=eff_snis, pairs=pairs))

    def load_pool_settings(self, cfg: dict) -> None:
        """Populate the pool-settings widgets from a config dict."""
        if not hasattr(self, "pool_ips"):
            return
        pool_ips = cfg.get("CONNECT_IPS", []) or []
        pool_snis = cfg.get("FAKE_SNIS", []) or []
        self.pool_ips.setPlainText(
            "\n".join(str(x).strip() for x in pool_ips if str(x).strip()))
        self.pool_snis.setPlainText(
            "\n".join(str(x).strip() for x in pool_snis if str(x).strip()))
        self.chk_pool_optimize.setChecked(
            bool(cfg.get("POOL_OPTIMIZE_ENABLED", True)))
        try:
            w = int(cfg.get("scan_workers", 8))
        except (TypeError, ValueError):
            w = 8
        self.spin_workers.setValue(max(1, min(32, w)))
        self._update_pool_hint()

    def collect_pool_settings(self) -> dict:
        """Read the pool-settings widgets back into a config fragment."""
        return {
            "CONNECT_IPS": self._pool_ip_list(),
            "FAKE_SNIS": self._pool_sni_list(),
            "POOL_OPTIMIZE_ENABLED": bool(self.chk_pool_optimize.isChecked()),
            "scan_workers": int(self.spin_workers.value()),
        }

    def _save_pool_settings(self) -> None:
        """Persist just the pool settings via the host callback (set by
        _wire_core). Falls back to writing the store directly if no callback."""
        cb = getattr(self, "save_pool_settings", None)
        if callable(cb):
            try:
                cb()
                self._toast(tr("تنظیمات استخر ذخیره شد"), "ok")
                return
            except Exception:
                pass
        # fallback: write straight to the store if we have one
        if self._store is not None:
            try:
                self._store.update(**self.collect_pool_settings())
                self._store.save_config()
                self._toast(tr("تنظیمات استخر ذخیره شد"), "ok")
            except Exception:
                self._toast(tr("ذخیره تنظیمات استخر ناموفق بود"), "err")

    # ------------------------------------------------------------------
    #  inline scan panel
    # ------------------------------------------------------------------
    def _build_scan_card(self) -> "Card":
        card = Card()
        b = card.body()

        title = QLabel(tr("آزمایش جفت‌های SNI/IP"))
        title.setObjectName("H2")
        b.addWidget(title)

        intro = QLabel(tr(
            "یک کانفیگ اسپوف (۱۲۷.۰.۰.۱:۴۰۴۴۳) را انتخاب کنید و «اجرای آزمایش» "
            "را بزنید. آزمایش مستقیماً DPI را می‌سنجد و به اتصال فعلی شما کاری "
            "ندارد — حتی وقتی متصل هستید نتیجه واقعی است. مسیر فعال جابه‌جا "
            "نمی‌شود؛ ردیف‌های سالم را انتخاب کنید و با دکمه‌های پایین به فهرست "
            "SNI/IP خود بیفزایید تا بعداً در «تنظیمات» انتخابشان کنید."))
        intro.setObjectName("Faint")
        intro.setWordWrap(True)
        b.addWidget(intro)

        pick = QHBoxLayout()
        pick.setSpacing(8)
        lbl = QLabel(tr("کانفیگ اسپوف:"))
        lbl.setObjectName("Muted")
        pick.addWidget(lbl)
        self.scan_cmb = NoScrollComboBox()
        pick.addWidget(self.scan_cmb, 1)
        self.scan_btn_run = QPushButton(tr("اجرای آزمایش"))
        self.scan_btn_run.setObjectName("Primary")
        self.scan_btn_run.clicked.connect(self._scan_start)
        pick.addWidget(self.scan_btn_run)
        self.scan_btn_stop = QPushButton(tr("توقف"))
        self.scan_btn_stop.setObjectName("Ghost")
        self.scan_btn_stop.setEnabled(False)
        self.scan_btn_stop.clicked.connect(self._scan_stop)
        pick.addWidget(self.scan_btn_stop)
        b.addLayout(pick)

        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setValue(0)
        b.addWidget(self.scan_progress)

        self.scan_tbl = QTableWidget(0, 5)
        self.scan_tbl.setObjectName("ScanResults")
        self.scan_tbl.setHorizontalHeaderLabels(
            [tr("IP"), tr("SNI"), tr("تأخیر"), tr("وضعیت"), tr("در فهرست؟")])
        self.scan_tbl.verticalHeader().setVisible(False)
        self.scan_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.scan_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scan_tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.scan_tbl.setAlternatingRowColors(True)
        self.scan_tbl.setMinimumHeight(240)
        hh = self.scan_tbl.horizontalHeader()
        hh.setSectionResizeMode(self.SC_IP, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.SC_SNI, QHeaderView.Stretch)
        hh.setSectionResizeMode(self.SC_LAT, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.SC_STATUS, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.SC_SAVED, QHeaderView.ResizeToContents)
        b.addWidget(self.scan_tbl, 1)

        act = QHBoxLayout()
        act.setSpacing(8)
        self.scan_btn_add_sel = QPushButton(tr("افزودن انتخاب‌شده‌ها"))
        self.scan_btn_add_sel.setObjectName("Ghost")
        self.scan_btn_add_sel.clicked.connect(self._scan_add_selected)
        act.addWidget(self.scan_btn_add_sel)
        self.scan_btn_add_ok = QPushButton(tr("افزودن همهٔ سالم‌ها"))
        self.scan_btn_add_ok.setObjectName("Ghost")
        self.scan_btn_add_ok.clicked.connect(self._scan_add_all_ok)
        act.addWidget(self.scan_btn_add_ok)
        self.scan_btn_rem_sel = QPushButton(tr("حذف انتخاب‌شده‌ها"))
        self.scan_btn_rem_sel.setObjectName("Ghost")
        self.scan_btn_rem_sel.clicked.connect(self._scan_remove_selected)
        act.addWidget(self.scan_btn_rem_sel)
        # Export the HEALTHY scan results straight to a file — the user's actual
        # ask: "می‌خوام IPهای سالمِ کشف‌شده را خروجی بگیرم". No need to add them
        # to the saved list first.
        self.scan_btn_export_ok = QPushButton(tr("خروجی سالم‌ها…"))
        self.scan_btn_export_ok.setObjectName("Ghost")
        self.scan_btn_export_ok.clicked.connect(self._scan_export_ok)
        act.addWidget(self.scan_btn_export_ok)
        act.addStretch(1)
        self.scan_status = QLabel(tr("آماده"))
        self.scan_status.setObjectName("Faint")
        act.addWidget(self.scan_status)
        b.addLayout(act)
        return card

    def set_store(self, store) -> None:
        """Wire the live ConfigStore so the inline scan can read configs and
        persist added sni_ip_pairs (called by the host in _wire_core)."""
        self._store = store
        self.refresh_saved_list()

    # ------------------------------------------------------------------
    #  saved sni_ip_pairs list (always visible, independent of live pool)
    # ------------------------------------------------------------------
    def _saved_pairs(self) -> list:
        """The user's reusable ``sni_ip_pairs`` as a list of ``(ip, sni)``."""
        store = self._store
        raw = (store.get("sni_ip_pairs", []) or []) if store else []
        out = []
        for p in raw:
            ip = str(p.get("ip", "")).strip()
            sni = str(p.get("sni", "")).strip()
            if ip and sni:
                out.append((ip, sni))
        return out

    def refresh_saved_list(self) -> None:
        """Re-render the saved SNI/IP list card + its count label."""
        if not hasattr(self, "saved_tbl"):
            return
        pairs = self._saved_pairs()
        n = len(pairs)
        self.lbl_saved_count.setText(
            tr("{n} جفت").format(n=n) if n else tr("خالی"))
        # export / clear only make sense when there is something saved
        has_any = n > 0
        if hasattr(self, "btn_clear_saved"):
            self.btn_clear_saved.setEnabled(has_any)
        if not pairs:
            self.saved_tbl.setPlainText(tr(
                "فهرست SNI/IP شما خالی است. با «شروع تست» جفت‌های سالم را پیدا "
                "و اضافه کنید، یا با «وارد کردن فهرست…» یک فایل را بارگذاری "
                "کنید. این جفت‌ها در «تنظیمات» قابل انتخاب‌اند."))
            return
        header = f"{tr('IP'):<20}{tr('SNI')}"
        lines = [header, "─" * 44]
        for ip, sni in pairs:
            lines.append(f"{ip:<20}{sni}")
        self.saved_tbl.setPlainText("\n".join(lines))

    def _on_clear_saved(self) -> None:
        """Clear the whole saved SNI/IP list (after confirmation)."""
        if self._store is None:
            return
        pairs = self._saved_pairs()
        if not pairs:
            self._toast(tr("فهرست از قبل خالی است."), "info")
            return
        resp = QMessageBox.question(
            self, tr("حذف فهرست SNI/IP"),
            tr("کل فهرست ذخیره‌شده ({n} جفت) حذف شود؟ این کار قابل بازگشت "
               "نیست.").format(n=len(pairs)),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        n = len(pairs)
        self._scan_persist([])
        self.refresh_saved_list()
        self._toast(tr("{n} جفت حذف شد — فهرست خالی شد.").format(n=n), "warn")

    def _toast(self, text: str, kind: str = "info") -> None:
        """Show a transient toast on the top-level window (clear feedback)."""
        try:
            from ui.widgets import Toast
            win = self.window() or self
            Toast.show_message(win, text, kind)
        except Exception:
            pass

    def _toggle_scan_panel(self) -> None:
        # use isHidden() (the explicit hide flag) rather than isVisible(), which
        # also reflects ancestor visibility and would mis-toggle before the page
        # is shown / in headless tests.
        show = self.scan_card.isHidden()
        self.scan_card.setVisible(show)
        if show:
            self._scan_populate_configs()
            self.btn_scan.setText(tr("بستن آزمایش"))
            # nudge the scroll so the panel is visible
            try:
                self.scan_card.setFocus()
            except Exception:
                pass
        else:
            self._scan_stop()
            self.btn_scan.setText(tr("شروع تست"))

    def set_provider(self, provider) -> None:
        """Give the page a zero-arg callable returning a snapshot dict."""
        self._provider = provider
        self.refresh()

    def start_polling(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.refresh()

    def stop_polling(self) -> None:
        self._timer.stop()

    def shutdown_scan(self) -> None:
        """Stop any running inline scan and give the worker a short grace
        window. Daemon probe sockets unwind on their own timeout, so this never
        blocks the GUI (used on page-leave and app close)."""
        w = self._scan_worker
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

    def refresh(self) -> None:
        if self._provider is None:
            self._render(None)
            return
        try:
            snap = self._provider()
        except Exception:
            snap = None
        self._render(snap)

    # -- rendering --------------------------------------------------------
    def _render(self, snap) -> None:
        self._last_snap = snap if isinstance(snap, dict) else None
        # keep the saved-list card in sync on every poll so it reflects any
        # external changes (e.g. edits made in Settings)
        self.refresh_saved_list()
        # export is driven by the SAVED list, not the live pool, so it is
        # enabled whenever the user has anything saved (or live routes exist).
        self.btn_export.setEnabled(
            bool(self._saved_pairs())
            or bool((snap or {}).get("rows") if isinstance(snap, dict) else []))
        if not snap or not snap.get("enabled"):
            self.lbl_state.setText(tr("استخر: غیرفعال (حالت تک‌مسیره)"))
            self.lbl_counts.setText(tr("مسیرها: —"))
            self.lbl_check.setText(tr("آخرین سلامت‌سنجی: —"))
            self.lbl_route.setText(tr("مسیر فعال: —"))
            self.lbl_failover.setText(tr("بازیابی خودکار: —"))
            self.tbl.setPlainText(tr(
                "استخر فقط هنگام اتصال زنده می‌شود. در «تنظیمات» بیش از یک IP یا "
                "SNI وارد کنید تا استخر چند-مسیره ساخته شود؛ مسیرهای زنده اینجا "
                "نمایش داده می‌شوند."))
            return

        total = int(snap.get("total", 0))
        known = int(snap.get("known", 0))
        stable = int(snap.get("stable", 0))
        weak = int(snap.get("weak", 0))
        dead = int(snap.get("dead", 0))
        unexplored = int(snap.get("unexplored", 0))
        active = int(snap.get("active", 0))

        self.lbl_state.setText(
            tr("استخر: فعال — {active} مسیر در حال سرویس").format(active=active))
        self.lbl_counts.setText(tr(
            "کل {total} · سالم {stable} · ضعیف {weak} · مرده {dead} · "
            "کشف‌نشده {unexplored} (آزموده {known})").format(
                total=total, stable=stable, weak=weak, dead=dead,
                unexplored=unexplored, known=known))

        secs = snap.get("seconds_since_check")
        if secs is None:
            self.lbl_check.setText(tr("آخرین سلامت‌سنجی: در حال راه‌اندازی…"))
        else:
            self.lbl_check.setText(
                tr("آخرین سلامت‌سنجی: {s} ثانیه پیش").format(s=int(secs)))

        # Redesign: active route vs best candidate found by the optimiser.
        active_route = snap.get("active_route") or {}
        best_route = snap.get("best_route") or {}
        if active_route.get("ip"):
            txt = tr("مسیر فعال: {ip} (SNI: {sni})").format(
                ip=active_route.get("ip"), sni=active_route.get("sni"))
            if best_route.get("ip"):
                same = (best_route.get("ip") == active_route.get("ip")
                        and best_route.get("sni") == active_route.get("sni"))
                if same:
                    txt += tr(" — بهترین مسیر همین است ✓")
                else:
                    txt += tr(" · بهترین یافته‌شده: {ip} (SNI: {sni}، افت "
                              "{loss:.0f}%)").format(
                        ip=best_route.get("ip"), sni=best_route.get("sni"),
                        loss=float(best_route.get("loss", 0.0)) * 100)
            self.lbl_route.setText(txt)
        else:
            self.lbl_route.setText(tr("مسیر فعال: —"))

        # per-IP failover line (7.8): list any IP currently in rapid-failover.
        blocked = snap.get("blocked_ips") or []
        if blocked:
            self.lbl_failover.setText(tr(
                "بازیابی خودکار: {n} IP موقتاً کنار گذاشته شد — {ips}").format(
                    n=len(blocked), ips="، ".join(str(x) for x in blocked)))
        else:
            self.lbl_failover.setText(tr("بازیابی خودکار: همه‌ی IPها سالم‌اند"))

        self.tbl.setPlainText(self._pair_table(snap.get("rows", []) or []))

    # -- inline scan: config picker + candidate building ------------------
    def _scan_spoof_profiles(self) -> list:
        out = []
        store = self._store
        if store is None:
            return out
        for prof in getattr(store, "profiles", []) or []:
            try:
                if bool(getattr(prof, "is_spoof_config", False)):
                    out.append(prof)
            except Exception:
                continue
        return out

    def _scan_populate_configs(self) -> None:
        self.scan_cmb.clear()
        profs = self._scan_spoof_profiles()
        if not profs:
            self.scan_cmb.addItem(
                tr("هیچ کانفیگ اسپوفی یافت نشد (۱۲۷.۰.۰.۱:۴۰۴۴۳)"), None)
            self.scan_cmb.setEnabled(False)
            self.scan_btn_run.setEnabled(False)
            return
        self.scan_cmb.setEnabled(True)
        self.scan_btn_run.setEnabled(True)
        for prof in profs:
            name = getattr(prof, "display_name", "") or tr("کانفیگ")
            self.scan_cmb.addItem(name, prof)

    def _scan_key(self, ip: str, sni: str) -> tuple:
        return (str(ip).strip().lower(), str(sni).strip().lower())

    def _scan_existing_keys(self) -> set:
        store = self._store
        pairs = (store.get("sni_ip_pairs", []) or []) if store else []
        return {self._scan_key(p.get("ip", ""), p.get("sni", "")) for p in pairs}

    def _scan_candidate_pairs(self, profile) -> list:
        from core.pool import build_scan_candidates
        store = self._store
        cfg = store.config if store else {}
        extra = []
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
        if len(cands) > self.MAX_CANDIDATES:
            cands = cands[:self.MAX_CANDIDATES]
        return cands

    def _scan_port(self, profile) -> int:
        try:
            return int(getattr(profile, "spoof_connect_port", 0) or 443)
        except Exception:
            return 443

    # -- inline scan: lifecycle -------------------------------------------
    def _scan_start(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        if self._store is None:
            return
        profile = self.scan_cmb.currentData()
        if profile is None:
            self.scan_status.setText(tr("کانفیگ اسپوفی انتخاب نشده است."))
            return
        candidates = self._scan_candidate_pairs(profile)
        if not candidates:
            self.scan_status.setText(tr(
                "هیچ جفت IP/SNI برای آزمایش نیست — در «تنظیمات» اضافه کنید."))
            return

        existing = self._scan_existing_keys()
        self.scan_tbl.setSortingEnabled(False)
        self.scan_tbl.setUpdatesEnabled(False)
        self.scan_tbl.setRowCount(0)
        self._row_for_key.clear()
        self.scan_tbl.setRowCount(len(candidates))
        for i, (ip, sni) in enumerate(candidates):
            self.scan_tbl.setItem(i, self.SC_IP, QTableWidgetItem(ip))
            self.scan_tbl.setItem(i, self.SC_SNI, QTableWidgetItem(sni))
            self.scan_tbl.setItem(i, self.SC_LAT, QTableWidgetItem("—"))
            self.scan_tbl.setItem(
                i, self.SC_STATUS,
                QTableWidgetItem(tr(self._STATUS_FA["pending"])))
            saved = self._scan_key(ip, sni) in existing
            self.scan_tbl.setItem(
                i, self.SC_SAVED, QTableWidgetItem("✓" if saved else ""))
            self._row_for_key[self._scan_key(ip, sni)] = i
        self.scan_tbl.setUpdatesEnabled(True)
        self.scan_progress.setRange(0, len(candidates))
        self.scan_progress.setValue(0)

        timeout = min(float(self._store.get("probe_timeout", 3.0) or 3.0), 3.0)
        port = self._scan_port(profile)
        # Concurrency is bounded and user-configurable (default 8). Fewer
        # workers + batched UI updates keep the window responsive during a
        # scan (the old 16-worker firehose froze the GUI on any mouse move).
        # Prefer the live spin widget on this page (so a just-changed value
        # takes effect without a save round-trip); fall back to the store.
        try:
            if hasattr(self, "spin_workers"):
                workers = int(self.spin_workers.value())
            else:
                workers = int(self._store.get("scan_workers", 8) or 8)
        except Exception:
            workers = 8
        workers = max(1, min(workers, 32))
        self._scan_worker = _PoolScanWorker(
            candidates, port=port, timeout=timeout, workers=workers)
        # use the BATCHED signal (coalesced verdicts) — not per-probe result —
        # so a fast scan repaints the table a few times/sec, never per row.
        self._scan_worker.results_batch.connect(self._scan_on_results_batch)
        self._scan_worker.progress.connect(self._scan_on_progress)
        self._scan_worker.finished_scan.connect(self._scan_on_finished)
        self.scan_btn_run.setEnabled(False)
        self.scan_cmb.setEnabled(False)
        self.scan_btn_stop.setEnabled(True)
        self.scan_status.setText(
            tr("در حال آزمایش {n} جفت…").format(n=len(candidates)))
        self._scan_worker.start()

    def _scan_stop(self) -> None:
        w = self._scan_worker
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
        self.scan_btn_stop.setEnabled(False)

    def _scan_on_progress(self, done: int, total: int) -> None:
        self.scan_progress.setRange(0, total)
        self.scan_progress.setValue(done)

    def _scan_on_finished(self, ok: int, total: int) -> None:
        self.scan_btn_run.setEnabled(True)
        self.scan_cmb.setEnabled(True)
        self.scan_btn_stop.setEnabled(False)
        msg = tr("پایان آزمایش — {ok} از {total} جفت سالم بود.").format(
            ok=ok, total=total)
        self.scan_status.setText(msg)
        # clear end-of-scan feedback so the user knows the sweep finished and
        # how many routes are usable (then they can hit "افزودن همهٔ سالم‌ها").
        if ok:
            self._toast(
                tr("آزمایش تمام شد — {ok} جفت سالم پیدا شد. حالا می‌توانید "
                   "آن‌ها را به فهرست بیفزایید.").format(ok=ok), "ok")
        else:
            self._toast(
                tr("آزمایش تمام شد — هیچ جفت سالمی پیدا نشد."), "warn")

    def _apply_scan_verdict(self, cand: dict) -> None:
        """Update a single table row from a verdict dict (no repaint control)."""
        ip = str(cand.get("ip", "")).strip()
        sni = str(cand.get("sni", "")).strip()
        status = str(cand.get("status", "pending"))
        latency = cand.get("latency_ms")
        row = self._row_for_key.get(self._scan_key(ip, sni))
        if row is None:
            return
        lat_item = self.scan_tbl.item(row, self.SC_LAT)
        if lat_item is not None:
            lat_item.setText("—" if latency is None
                             else "%.0fms" % float(latency))
        st_item = self.scan_tbl.item(row, self.SC_STATUS)
        if st_item is not None:
            st_item.setText(tr(self._STATUS_FA.get(status, status)))
            if status == "ok":
                st_item.setForeground(QColor("#3ddc97"))
            elif status == "fail":
                st_item.setForeground(QColor("#ff6b6b"))

    def _scan_on_result(self, cand: dict) -> None:
        # single-verdict path (back-compat / tests)
        self._apply_scan_verdict(cand)

    def _scan_on_results_batch(self, cands: list) -> None:
        """Apply a BATCH of verdicts in one bulk repaint (freeze fix).

        Disabling table updates while we mutate every row, then re-enabling
        once, collapses N individual repaints into a single one — the GUI stays
        smooth even with hundreds of candidates and a fast scan.
        """
        if not cands:
            return
        self.scan_tbl.setUpdatesEnabled(False)
        try:
            for cand in cands:
                self._apply_scan_verdict(cand)
        finally:
            self.scan_tbl.setUpdatesEnabled(True)

    # -- inline scan: add / remove to sni_ip_pairs ------------------------
    def _scan_selected_rows(self) -> list:
        return sorted({idx.row() for idx in self.scan_tbl.selectedIndexes()})

    def _scan_row_pair(self, row: int) -> tuple:
        ip = self.scan_tbl.item(row, self.SC_IP)
        sni = self.scan_tbl.item(row, self.SC_SNI)
        return (ip.text() if ip else "", sni.text() if sni else "")

    def _scan_row_ok(self, row: int) -> bool:
        st = self.scan_tbl.item(row, self.SC_STATUS)
        return st is not None and st.text() == tr(self._STATUS_FA["ok"])

    def _scan_persist(self, pairs) -> None:
        if self._store is None:
            return
        self._store.set("sni_ip_pairs", pairs)
        try:
            self._store.save_config()
        except Exception:
            pass
        # keep the always-visible saved-list card in sync immediately
        self.refresh_saved_list()
        # let the host refresh Settings so the new pairs are selectable there
        if callable(getattr(self, "pairs_changed", None)):
            try:
                self.pairs_changed()
            except Exception:
                pass

    def _scan_add_rows(self, rows: list, *, ok_only: bool) -> tuple:
        """Add scan rows to ``sni_ip_pairs``.

        Returns ``(added, duplicates, skipped)`` so the caller can give the user
        precise feedback — "how many were added, how many were already in the
        list, how many were skipped (not OK / blank)".
        """
        if self._store is None:
            return (0, 0, 0)
        pairs = list(self._store.get("sni_ip_pairs", []) or [])
        keys = {self._scan_key(p.get("ip", ""), p.get("sni", "")) for p in pairs}
        added = 0
        duplicates = 0
        skipped = 0
        for row in rows:
            if ok_only and not self._scan_row_ok(row):
                skipped += 1
                continue
            ip, sni = self._scan_row_pair(row)
            if not ip or not sni:
                skipped += 1
                continue
            key = self._scan_key(ip, sni)
            if key in keys:
                duplicates += 1
                continue
            pairs.append({"sni": sni, "ip": ip})
            keys.add(key)
            saved_item = self.scan_tbl.item(row, self.SC_SAVED)
            if saved_item is not None:
                saved_item.setText("✓")
            added += 1
        if added:
            self._scan_persist(pairs)
        return (added, duplicates, skipped)

    def _add_feedback(self, added: int, duplicates: int, skipped: int) -> None:
        """Surface a clear status line + a toast describing what happened."""
        parts = []
        if added:
            parts.append(tr("{n} جفت تازه افزوده شد").format(n=added))
        if duplicates:
            parts.append(tr("{n} مورد از قبل بود").format(n=duplicates))
        if skipped:
            parts.append(tr("{n} مورد رد شد").format(n=skipped))
        if not parts:
            msg = tr("چیزی برای افزودن نبود.")
            self.scan_status.setText(msg)
            self._toast(msg, "warn")
            return
        msg = " · ".join(parts)
        self.scan_status.setText(msg)
        # toast kind: success when something new landed, otherwise informative
        self._toast(msg, "ok" if added else "info")

    def _scan_add_selected(self) -> None:
        rows = self._scan_selected_rows()
        if not rows:
            self.scan_status.setText(tr("هیچ ردیفی انتخاب نشده است."))
            self._toast(tr("ابتدا چند ردیف را انتخاب کنید."), "warn")
            return
        added, dups, skipped = self._scan_add_rows(rows, ok_only=False)
        self._add_feedback(added, dups, skipped)

    def _scan_add_all_ok(self) -> None:
        rows = [r for r in range(self.scan_tbl.rowCount())
                if self._scan_row_ok(r)]
        if not rows:
            msg = tr("هیچ جفت سالمی برای افزودن نیست — اول آزمایش را اجرا کنید.")
            self.scan_status.setText(msg)
            self._toast(msg, "warn")
            return
        added, dups, skipped = self._scan_add_rows(rows, ok_only=True)
        self._add_feedback(added, dups, skipped)

    def _scan_remove_selected(self) -> None:
        rows = self._scan_selected_rows()
        if not rows or self._store is None:
            self.scan_status.setText(tr("هیچ ردیفی انتخاب نشده است."))
            self._toast(tr("ابتدا چند ردیف را انتخاب کنید."), "warn")
            return
        remove_keys = {self._scan_key(*self._scan_row_pair(r)) for r in rows}
        before = self._saved_pairs()
        pairs = [
            p for p in (self._store.get("sni_ip_pairs", []) or [])
            if self._scan_key(p.get("ip", ""), p.get("sni", "")) not in remove_keys
        ]
        removed = max(0, len(before) - len(pairs))
        self._scan_persist(pairs)
        for r in rows:
            saved_item = self.scan_tbl.item(r, self.SC_SAVED)
            if saved_item is not None:
                saved_item.setText("")
        if removed:
            msg = tr("{n} جفت از فهرست حذف شد.").format(n=removed)
            self.scan_status.setText(msg)
            self._toast(msg, "warn")
        else:
            msg = tr("هیچ‌کدام از ردیف‌های انتخابی در فهرست نبودند.")
            self.scan_status.setText(msg)
            self._toast(msg, "info")

    def _scan_ok_pairs(self) -> list:
        """Every (ip, sni, 'ok') from the scan table whose verdict is healthy."""
        out = []
        seen = set()
        for r in range(self.scan_tbl.rowCount()):
            if not self._scan_row_ok(r):
                continue
            ip, sni = self._scan_row_pair(r)
            ip, sni = ip.strip(), sni.strip()
            if not ip or not sni:
                continue
            key = (ip.lower(), sni.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((ip, sni, "ok"))
        return out

    def _scan_export_ok(self) -> None:
        """Export ONLY the healthy scan results to a file (the user's ask:
        'خروجی IPهای سالمِ کشف‌شده'). No need to add them to the saved list
        first — the scan results are exported directly."""
        pairs = self._scan_ok_pairs()
        if not pairs:
            msg = tr("هیچ جفت سالمی برای خروجی نیست — اول آزمایش را اجرا کنید.")
            self.scan_status.setText(msg)
            self._toast(msg, "warn")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("ذخیره‌ی IPهای سالم"), "sni_ip_healthy.txt",
            tr("فایل متنی (*.txt)"))
        if not path:
            return
        try:
            from core.pool import export_sni_pairs
            n = export_sni_pairs(pairs, path, header="Healthy SNI/IP pairs")
            self._toast(
                tr("{n} جفت سالم در فایل ذخیره شد ✓").format(n=n), "ok")
            QMessageBox.information(
                self, tr("خروجی سالم‌ها"),
                tr("{n} جفت سالم با موفقیت ذخیره شد.\n\n{p}").format(
                    n=n, p=path))
        except Exception as exc:
            self._toast(tr("خروجی ناموفق بود"), "err")
            QMessageBox.warning(
                self, tr("خروجی سالم‌ها"),
                tr("ذخیره ناموفق بود: {e}").format(e=exc))

    # -- export (7.10) ----------------------------------------------------
    def _on_export(self) -> None:
        """Write the user's SNI/IP pairs (with status) to a text file.

        The export is IP-paired (issue: "خروجیِ فهرستِ SNI، IP مناسب ندارد"):
        each line is ``IP <TAB> SNI <TAB> status`` so every SNI carries the
        connect IP it was proven against, not a bare SNI string.

        Fix: the export now ALWAYS works as long as the user has anything saved.
        Previously it only read the *live* pool snapshot rows, which are empty
        whenever the tunnel isn't running, so the user could never get a file
        out of their saved list. We now merge:
          * every pair in the saved ``sni_ip_pairs`` list (status ``saved``), and
          * any live pool rows (status active/ok/dead) — these override the
            saved status when the same pair is currently in the pool.
        """
        # start from the saved list so export works even when disconnected
        pairs = []
        seen = set()
        for ip, sni in self._saved_pairs():
            key = (ip.lower(), sni.lower())
            if key in seen:
                continue
            seen.add(key)
            pairs.append((ip, sni, "saved"))

        # overlay/append live pool rows with their richer status
        snap = self._last_snap
        rows = (snap or {}).get("rows", []) if isinstance(snap, dict) else []
        status_by_key = {}
        for r in rows:
            ip = str(r.get("ip", "")).strip()
            sni = str(r.get("sni", "")).strip()
            if not ip or not sni:
                continue
            if not r.get("alive", True):
                status = "dead"
            elif r.get("in_pool"):
                status = "active"
            else:
                status = "ok"
            key = (ip.lower(), sni.lower())
            status_by_key[key] = (ip, sni, status)
        # update statuses of already-listed pairs, append new live-only ones
        for i, (ip, sni, _st) in enumerate(pairs):
            k = (ip.lower(), sni.lower())
            if k in status_by_key:
                pairs[i] = status_by_key[k]
        for key, triple in status_by_key.items():
            if key not in seen:
                seen.add(key)
                pairs.append(triple)

        if not pairs:
            QMessageBox.information(
                self, tr("خروجی SNI/IP"),
                tr("فهرستی برای خروجی‌گرفتن وجود ندارد. ابتدا با «شروع تست» "
                   "جفت‌های سالم را اضافه کنید یا فهرستی وارد کنید."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("ذخیره‌ی فهرست SNI/IP"), "sni_ip_list.txt",
            tr("فایل متنی (*.txt)"))
        if not path:
            return
        try:
            from core.pool import export_sni_pairs
            n = export_sni_pairs(pairs, path)
            self._toast(
                tr("{n} جفت SNI/IP در فایل ذخیره شد ✓").format(n=n), "ok")
            QMessageBox.information(
                self, tr("خروجی SNI/IP"),
                tr("{n} جفت SNI/IP با موفقیت در فایل ذخیره شد.\n\n{p}").format(
                    n=n, p=path))
        except Exception as exc:
            self._toast(tr("خروجی ناموفق بود"), "err")
            QMessageBox.warning(
                self, tr("خروجی SNI/IP"),
                tr("ذخیره ناموفق بود: {e}").format(e=exc))

    # -- import -----------------------------------------------------------
    def _on_import(self) -> None:
        """Import a previously-exported SNI/IP list file into sni_ip_pairs.

        Lets the user load a list they (or someone else) produced with the
        export button — no copy-pasting. The file is parsed leniently
        (TAB/comma/space, comments ignored, IP/SNI order auto-detected) and only
        NEW pairs are appended to the user's reusable ``sni_ip_pairs`` list. The
        Settings page is refreshed so the imported pairs are immediately
        selectable there.
        """
        if self._store is None:
            QMessageBox.warning(
                self, tr("وارد کردن SNI/IP"),
                tr("امکان وارد کردن نیست — فروشگاه تنظیمات در دسترس نیست."))
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("انتخاب فایل فهرست SNI/IP"), "",
            tr("فایل متنی (*.txt);;همهٔ فایل‌ها (*)"))
        if not path:
            return
        try:
            from core.pool import import_sni_pairs
            existing = list(self._store.get("sni_ip_pairs", []) or [])
            merged, added = import_sni_pairs(path, existing=existing)
        except Exception as exc:
            QMessageBox.warning(
                self, tr("وارد کردن SNI/IP"),
                tr("وارد کردن ناموفق بود: {e}").format(e=exc))
            return
        if added <= 0:
            self._toast(
                tr("جفت تازه‌ای نبود — همه از قبل در فهرست بودند."), "info")
            QMessageBox.information(
                self, tr("وارد کردن SNI/IP"),
                tr("جفت تازه‌ای پیدا نشد — همهٔ موارد فایل از قبل در فهرست بودند."))
            return
        self._store.set("sni_ip_pairs", merged)
        try:
            self._store.save_config()
        except Exception:
            pass
        self.refresh_saved_list()
        if callable(getattr(self, "pairs_changed", None)):
            try:
                self.pairs_changed()
            except Exception:
                pass
        self._toast(
            tr("{n} جفت تازه به فهرست افزوده شد ✓").format(n=added), "ok")
        QMessageBox.information(
            self, tr("وارد کردن SNI/IP"),
            tr("{n} جفت تازه از فایل به فهرست SNI/IP افزوده شد.").format(n=added))

    @staticmethod
    def _pair_table(rows: list) -> str:
        if not rows:
            return tr("هنوز مسیری آزموده نشده — اولین سلامت‌سنجی در حال انجام است…")
        header = (f"{tr('IP'):<18}{tr('SNI'):<24}"
                  f"{tr('افت'):>7}{tr('اتصال'):>7}  {tr('وضعیت')}")
        lines = [header]
        for r in rows:
            if not r.get("alive", True):
                state = tr("مرده")
            elif r.get("in_pool"):
                state = tr("★ فعال")
            else:
                state = tr("سالم")
            lines.append(
                f"{str(r.get('ip', '')):<18}{str(r.get('sni', '')):<24}"
                f"{float(r.get('loss', 0.0)) * 100:>6.0f}%"
                f"{int(r.get('active', 0)):>7}  {state}")
        return "\n".join(lines)


class LogPage(QWidget):
    """Professional log console (step 23).

    * each line is timestamped + classified (info/ok/warn/err) and coloured
    * a level filter + a text search narrow what's shown (re-rendered from the
      backing :class:`~core.logbuffer.LogBuffer`, which stays bounded)
    * a live per-level counter strip ("info 12 · ok 3 · warn 1 · err 0")
    The classification/filter/count logic is pure (``core.logbuffer``); this
    widget only renders it.
    """

    # per-level text colours (kept here so the QSS file stays theme-only)
    _COLORS = {
        "info": "#9fb3c8",
        "ok":   "#3ddc97",
        "warn": "#f4b740",
        "err":  "#ff6b6b",
    }
    _LEVEL_FA = {"all": "همه", "info": "اطلاع", "ok": "موفق",
                 "warn": "هشدار", "err": "خطا"}
    # log-source labels + chip colours (issue #4) — so spoofer/WinDivert lines
    # are visually separated from ordinary xray-core lines.
    _SOURCE_FA = {"all": "همه‌ی منابع", "engine": "موتور",
                  "spoof": "اسپوف SNI", "core": "هسته xray"}
    _SOURCE_COLORS = {"engine": "#7c8aa0", "spoof": "#c792ea", "core": "#56b6f7"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffer = LogBuffer(capacity=2000)
        # theme-dependent text colours (#4): default to the dark palette; the
        # host calls set_palette() so the log message + timestamp are always
        # readable — never white-on-white in the light theme.
        self._msg_color = "#d8e2ec"
        self._stamp_color = "#5b6b7b"

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)

        root.addWidget(_section_title("لاگ", "رویدادهای زنده‌ی موتور"))

        card = Card()
        b = card.body()

        # --- toolbar: filter + search + counters ---
        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(self._field_label("سطح"))
        self.cmb_level = NoScrollComboBox()
        self.cmb_level.setObjectName("LogFilter")
        for lv in ("all",) + LEVELS:
            self.cmb_level.addItem(tr(self._LEVEL_FA.get(lv, lv)), lv)
        self.cmb_level.currentIndexChanged.connect(self._rerender)
        bar.addWidget(self.cmb_level)

        # source filter (issue #4): all / engine / spoof / xray-core
        bar.addWidget(self._field_label("منبع"))
        self.cmb_source = NoScrollComboBox()
        self.cmb_source.setObjectName("LogFilter")
        for src in ("all",) + SOURCES:
            self.cmb_source.addItem(tr(self._SOURCE_FA.get(src, src)), src)
        self.cmb_source.currentIndexChanged.connect(self._rerender)
        bar.addWidget(self.cmb_source)

        self.search = QLineEdit()
        self.search.setObjectName("LogSearch")
        self.search.setPlaceholderText(tr("جستجو در لاگ…"))
        self.search.textChanged.connect(self._rerender)
        bar.addWidget(self.search, 1)

        self.counters = QLabel("")
        self.counters.setObjectName("LogCounters")
        bar.addWidget(self.counters)
        b.addLayout(bar)

        # --- the console itself (rich text so each line can be coloured) ---
        self.log = QTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        b.addWidget(self.log)

        clr = QHBoxLayout()
        clr.addStretch(1)
        self.btn_clear = QPushButton(tr("پاک‌سازی"))
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.clicked.connect(self.clear)
        clr.addWidget(self.btn_clear)
        b.addLayout(clr)

        root.addWidget(card, 1)

        # seed lines so the page never looks empty
        self.append(tr("SNI Spoofer UI بارگذاری شد"))
        self.append(tr("منتظر شروع تونل…"))

    # -- helpers ----------------------------------------------------------
    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(tr(t))
        lbl.setObjectName("Muted")
        return lbl

    def _current_filter(self) -> str:
        data = self.cmb_level.currentData()
        return data if data else "all"

    def _current_source(self) -> str:
        data = self.cmb_source.currentData() if hasattr(self, "cmb_source") else None
        return data if data else "all"

    def _row_html(self, entry) -> str:
        color = self._COLORS.get(entry.level, self._COLORS["info"])
        src = getattr(entry, "source", "engine")
        src_col = self._SOURCE_COLORS.get(src, self._SOURCE_COLORS["engine"])
        src_label = self._SOURCE_FA.get(src, src)
        # the source is already encoded as a leading [tag]; strip it from the
        # body so it isn't shown twice — render it as a coloured chip instead.
        body = entry.message
        b = body.lstrip()
        if b.startswith("["):
            end = b.find("]")
            if end > 0:
                body = b[end + 1:].lstrip()
        # escape minimal HTML so messages can't break the markup
        msg = (body.replace("&", "&amp;")
                   .replace("<", "&lt;").replace(">", "&gt;"))
        return (f'<span style="color:{self._stamp_color}">[{entry.stamp}]</span> '
                f'<span style="color:{color};font-weight:600">'
                f'{entry.level.upper():<4}</span> '
                f'<span style="color:{src_col};font-weight:600">'
                f'[{src_label}]</span> '
                f'<span style="color:{self._msg_color}">{msg}</span>')

    def set_palette(self, palette) -> None:
        """Adopt the active theme's text/timestamp colours and re-render (#4)."""
        self._msg_color = palette.text
        self._stamp_color = palette.text_faint
        self._rerender()

    def _update_counters(self) -> None:
        c = self._buffer.counts
        parts = []
        for lv in LEVELS:
            col = self._COLORS[lv]
            parts.append(f'<span style="color:{col}">{self._LEVEL_FA[lv]} '
                         f'{c.get(lv, 0)}</span>')
        self.counters.setText(" · ".join(parts))

    # -- public API (slots) ----------------------------------------------
    def append(self, line: str) -> None:
        """Slot for the engine's log signal (thread-safe via Qt queued conn)."""
        entry = self._buffer.add(line)
        self._update_counters()
        # if the new entry passes the active filter, append it incrementally
        from core.logbuffer import matches
        if matches(entry, level=self._current_filter(),
                   query=self.search.text(), source=self._current_source()):
            self.log.append(self._row_html(entry))
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _rerender(self, *args) -> None:
        """Rebuild the visible console from the buffer under current filters."""
        rows = self._buffer.filtered(level=self._current_filter(),
                                     query=self.search.text(),
                                     source=self._current_source())
        html = "<br>".join(self._row_html(e) for e in rows)
        self.log.setHtml(html)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        self._buffer.clear()
        self.log.clear()
        self._update_counters()


# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------

class MainWindow(QWidget):

    def __init__(self, theme: str | None = None):
        super().__init__()
        # --- core: persistent store + engine bridge ---
        self.store = ConfigStore()
        self._theme = theme or self.store.get("theme", "dark")
        # --- language (#6): restore persisted choice and apply it before any
        # widget text is built, so tr() returns the right language everywhere.
        from ui import i18n
        lang = str(self.store.get("language", "fa"))
        if lang not in ("fa", "en"):
            lang = "fa"
        # set the module language directly (no observers yet)
        i18n._lang = lang
        self.engine = EngineBridge(EngineController(self.store.config))
        self.engine.set_profile(self.store.selected_profile)
        # Let the background optimiser persist a newly-found best route into
        # config.json (per-config best-result persistence). The controller writes
        # into the shared config dict, so saving the store flushes it to disk.
        try:
            self.engine.controller.on_save_config = self.store.save_config
        except Exception:
            pass

        self._palette = get_palette(self._theme)
        self.setObjectName("RootBackdrop")
        self.setWindowTitle("SNI Spoofer")
        self.resize(940, 620)
        self.setMinimumSize(760, 520)
        # #2: explicit maximized-state bookkeeping. On a frameless window the
        # platform's own normal-geometry restore is unreliable (minimising a
        # window and re-opening it from the taskbar could leave it stuck
        # maximized), so we track the state + last normal geometry ourselves.
        self._is_maximized = False
        self._normal_geometry = None
        # #3: track the mouse so the cursor turns into a resize arrow when it
        # hovers a window edge (interactive edge/corner resize for the frameless
        # window). startSystemResize then does the actual native resize.
        self.setMouseTracking(True)

        # Frameless, but keep the window a *real* top-level window so the OS
        # still gives us minimise + taskbar entry + native system-move. We do
        # NOT use WA_TranslucentBackground: on Windows it broke showMinimized()
        # and startSystemMove() and made the UI look "scattered" (feedback 2/3).
        # Instead the RootBackdrop paints a solid 3-D gradient (feedback 6).
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowSystemMenuHint
        )

        # --- living mathematical-wave backdrop (#10) ---
        # A child widget that paints animated superposed sine waves *behind*
        # all content. It is transparent to mouse events and kept lowered in
        # the z-order, so the layout/content sit on top unchanged.
        self.wave_bg = WaveBackdrop(self)
        self.wave_bg.lower()

        # #1: a visible grab handle in the bottom corner so users immediately
        # see that the window is resizable (the invisible edge band alone was
        # not discoverable — "فلش تنظیم ابعاد نداره"). The edge/corner band
        # still works too (mousePressEvent → startSystemResize) for grabbing any
        # side. The grip follows the layout-direction corner (bottom-left for
        # RTL Persian, bottom-right for LTR English).
        self.size_grip = QSizeGrip(self)
        self.size_grip.setObjectName("SizeGrip")
        self.size_grip.setFixedSize(18, 18)
        self.size_grip.raise_()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- title bar ---
        self.title_bar = TitleBar(self)
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.maximize_clicked.connect(self.toggle_maximize)
        self.title_bar.close_clicked.connect(self.close)
        self.title_bar.theme_toggled.connect(self.toggle_theme)
        self.title_bar.language_toggled.connect(self.toggle_language)
        outer.addWidget(self.title_bar)

        # --- persistent active-config status bar (visible on every tab, #9) ---
        self.active_bar = ActiveConfigBar(self)
        self.active_bar.set_profile(self.store.selected_profile)
        outer.addWidget(self.active_bar)

        # --- body: nav + pages ---
        body = QHBoxLayout()
        body.setContentsMargins(14, 6, 14, 14)
        body.setSpacing(14)

        body.addWidget(self._build_nav())

        self.stack = QStackedWidget()
        self.page_dashboard = DashboardPage(self._palette)
        self.page_profiles = ProfilesPage(self.store, engine=self.engine)
        self.page_settings = SettingsPage()
        self.page_strategy = StrategyPage(self.store)
        self.page_strategy.auto_prober_changed.connect(self._on_auto_prober_changed)
        self.page_strategy.strategy_selected.connect(self._on_strategy_selected)
        # #5: the standalone "تشخیص" (Diagnostics) page was removed at the user's
        # request — its live resilience numbers already surface on the dashboard
        # strip via _pump_resilience(self.engine.diagnostics), so the dedicated
        # tab was redundant. The engine's diagnostics provider stays wired.
        self.page_log = LogPage()
        # live route-pool status page (multi-IP / multi-SNI). It polls a
        # zero-arg provider so it stays decoupled from the engine/pool; the
        # provider is wired in _wire_core once the engine is known.
        self.page_pool = PoolPage()
        # wrap every page in a scroll area so tall content scrolls instead of
        # overlapping/clipping when the window is short (the layout bug on the
        # built Windows app). ``_scroll`` maps page -> its scroll wrapper so the
        # page-change / nav logic can still reason about which page is shown.
        self._scroll: dict[QWidget, QScrollArea] = {}
        for p in (self.page_dashboard, self.page_profiles, self.page_settings,
                  self.page_strategy, self.page_pool, self.page_log):
            wrap = _scrollable(p)
            self._scroll[p] = wrap
            self.stack.addWidget(wrap)
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
        # restart state machine (config/strategy switch while connected). Init
        # explicitly so the phase-driven dispatcher never relies on getattr
        # defaults firing in the right order.
        self._restarting = False
        self._restart_phase = "idle"      # "stopping" | "starting" | "idle"
        self._restart_gen = 0
        self._restart_attempts = 0
        self._restart_settle = 0

        # engine → UI (signals are marshalled to the GUI thread by Qt)
        self.engine.log.connect(self.page_log.append)
        # All status updates funnel through _dispatch_status so that, while an
        # automatic config-switch restart is in flight, a transient ``idle`` (the
        # stop half of stop→start) is presented as ``connecting`` instead of
        # flashing the "شروع" idle label — which used to let the user press Start
        # mid-restart and break the connection (bug #2).
        self.engine.status.connect(self._dispatch_status)
        self.engine.count.connect(self.page_dashboard.on_count)
        self.engine.traffic.connect(self.page_dashboard.on_traffic)
        # feed the persistent status bar's live rate (down_bps, up_bps)
        self.engine.traffic.connect(
            lambda up, down, up_bps, down_bps:
                self.active_bar.set_rate(down_bps, up_bps))
        # live bypass method → dashboard stays in sync with Diagnostics
        self.engine.strategy.connect(self.page_dashboard.set_active_strategy)
        self.engine.strategy.connect(self._on_strategy_changed)

        # poll the resilience layer for the dashboard strip while active
        self._resilience_timer = QTimer(self)
        self._resilience_timer.setInterval(1500)
        self._resilience_timer.timeout.connect(self._pump_resilience)

        # UI → engine
        self.page_dashboard.power_handler = self._on_power
        self.page_profiles.on_selection_changed = self._on_profile_selected
        self.page_settings.btn_save.clicked.connect(self._save_settings)

        # live route-pool status: feed the page a zero-arg provider that reads
        # the engine's live manager (when present) or falls back to a static
        # config-derived snapshot. Bound to the current store so it always
        # reflects saved settings.
        self.page_pool.set_provider(
            lambda: self.engine.pool_summary(self.store.config))
        # inline SNI/IP scan ("شروع تست"): the scan panel is embedded in the
        # pool page (no separate window). Give it the live store so it can list
        # spoof configs and persist added sni_ip_pairs, and a callback so the
        # Settings page refreshes when the user adds/removes pairs.
        self.page_pool.set_store(self.store)
        self.page_pool.pairs_changed = self._refresh_after_pairs_changed
        # the pool IP/SNI/optimise/workers settings moved onto this page; load
        # them and give the page a save callback so its «ذخیره استخر» button
        # persists those keys through the same path as the Settings save.
        self.page_pool.load_pool_settings(self.store.config)
        self.page_pool.save_pool_settings = self._save_pool_settings

        # initialise widgets from persisted state
        self.page_settings.load_from(self.store.config)
        self.page_dashboard.set_mode(
            self.store.get("connection_mode", "Tunnel"))
        self.page_dashboard.set_active_strategy(
            self.store.get("bypass_method", "wrong_seq"))
        sel = self.store.selected_profile
        # #6: gate the mode selector on the initially-selected profile too
        self._sync_mode_applicability(sel)
        if sel:
            self.page_log.append(
                "[init] " + tr("پروفایل فعال: {name}").format(name=sel.display_name))
        else:
            self.page_log.append(
                "[init] " + tr("پروفایلی انتخاب نشده — حالت SNI Only"))

    def _on_power(self, action: str):
        # While an automatic config-switch restart is in flight the engine is
        # briefly idle; a stray Start click here used to kick off a second,
        # conflicting start ("موتور از قبل در حال اجراست" / a half torn-down
        # session). So we ignore a manual *Start* until the restart settles.
        #
        # BUT we must ALWAYS honour a *Stop* — otherwise if the new config never
        # comes up the user is trapped on "در حال اتصال…" with no way out (the
        # reported "gets stuck connecting, can't stop or do anything" bug). Stop
        # cancels the pending restart and tears the engine down for real.
        if action == "stop":
            self._cancel_restart()
            try:
                self.engine.stop()
            except Exception:
                pass
            # reflect the stop in the UI IMMEDIATELY. engine.stop() is
            # synchronous and emits idle, but during a restart we were masking
            # idle→connecting; since _cancel_restart() just dropped the mask the
            # next idle would show through, yet a stale "connecting" could still
            # be on screen for a beat. Force idle now so the button never looks
            # stuck on "در حال اتصال…" after the user pressed Stop.
            try:
                self.page_dashboard.set_status("idle")
                self.active_bar.set_status("idle")
            except Exception:
                pass
            return
        if getattr(self, "_restarting", False):
            Toast.show_message(
                self, tr("در حال جابه‌جایی سرور… چند لحظه صبر کنید"), "info")
            return
        if action == "start":
            # push the freshest settings + profile into the engine first
            self.engine.update_config(self.store.config)
            self.engine.set_profile(self.store.selected_profile)
            if (self.store.get("connection_mode") != "SNI Only"
                    and self.store.selected_profile is None):
                Toast.show_message(
                    self, tr("ابتدا یک پروفایل وارد و انتخاب کنید"), "warn")
                self.page_dashboard.set_status("idle")
                return
            # «تلاش دوباره» = a CLEAN restart (user request, نکته ۲).
            #
            # The button shows "شروع" from idle and "تلاش دوباره" from error,
            # but BOTH emit "start". When we start out of an error (or any
            # not-fully-idle state), a previous attempt may have left worker
            # threads (spoofer / xray / stats poller) half-alive — so a bare
            # start() landed ON TOP of that debris and "sometimes connects,
            # sometimes won't, gets tangled". The user asked for exactly this:
            # make تلاش‌دوباره behave like a fresh شروع — first tear EVERYTHING
            # down, then start clean. So unless the engine is already cleanly
            # idle, issue a full stop() (kills any lingering attempt) before
            # the new start.
            try:
                needs_clean = bool(self.engine.is_running) or \
                    (self.engine.status_value != "idle")
            except Exception:
                needs_clean = True
            if needs_clean:
                try:
                    self.engine.stop()      # synchronous — kills all workers
                except Exception:
                    pass
            self.engine.start()

    # Restart state machine (config / strategy switch while connected).
    #
    # The tricky part is that engine teardown emits a *late* ``idle`` from a
    # WORKER thread (the proxy listen-loop reporting down), which reaches the GUI
    # via a queued connection AFTER we've already fired the new start(). The old
    # boolean flag misread that stale idle as "the new config failed" and flipped
    # the dashboard to «شروع» / «تلاش دوباره» mid-restart (the reported bug). We
    # now drive an explicit phase so stale signals can't fool us:
    #
    #   "stopping" — stop() issued, waiting for the engine to reach idle. ALL
    #                idle/error are the dying old session → mask as connecting.
    #   "starting" — new start() fired. We IGNORE idle/error until we've actually
    #                observed the NEW session move (connecting→active). Only an
    #                idle/error seen *after* that counts as a genuine failure.
    #
    # ``_restarting`` is True for both phases (mask on); a generation counter
    # retires stale timers; a watchdog guarantees the mask can never wedge.

    def _cancel_restart(self):
        """Abort any in-flight auto-restart and drop the connecting mask.

        Called when the user hits Stop (so they can always escape a stuck
        "در حال اتصال…") and by the watchdog. Bumps the generation so pending
        poll/watchdog timers from this cycle become no-ops.
        """
        self._restarting = False
        self._restart_phase = "idle"
        self._restart_gen = getattr(self, "_restart_gen", 0) + 1
        self._restart_attempts = 0
        self._restart_settle = 0

    def _begin_restart(self):
        """Kick off a fresh stop→idle→start restart cycle (phase-driven)."""
        self._restarting = True
        self._restart_phase = "stopping"
        self._restart_gen = getattr(self, "_restart_gen", 0) + 1
        self._restart_attempts = 0
        self._restart_settle = 0
        self.page_dashboard.set_status("connecting")
        self.active_bar.set_status("connecting")
        try:
            self.engine.stop()
        except Exception:
            pass
        self._restart_when_idle(self._restart_gen)

    def _dispatch_status(self, status: str):
        """Fan a single engine status out to every UI consumer, masking the
        transient idle/connecting churn of an auto-restart so the dashboard
        shows a steady "در حال اتصال…" instead of flickering to «شروع» (bug #2).
        """
        shown = status
        if getattr(self, "_restarting", False):
            phase = getattr(self, "_restart_phase", "idle")
            if status == "active":
                # new session is up — restart complete, drop the mask.
                self._restarting = False
                self._restart_phase = "idle"
            elif phase == "stopping":
                # still tearing the old session down (or its late worker-thread
                # idle is arriving) — keep a steady "connecting".
                shown = "connecting"
            elif phase == "starting":
                if status == "connecting":
                    # the NEW start is handshaking — exactly what we expect.
                    shown = "connecting"
                else:
                    # idle/error AFTER we fired the new start. This may still be
                    # a *stale* teardown idle queued from a worker thread, so we
                    # DON'T trust it to mean failure — keep masking. The watchdog
                    # (and a real, later active) resolve the true outcome. This
                    # is what stops the mid-restart flip to «شروع»/«تلاش دوباره».
                    shown = "connecting"
        self.page_dashboard.set_status(shown)
        self.active_bar.set_status(shown)
        self._on_status(status)

    def _on_status(self, status: str):
        # never act on the *masked* status here — use the raw engine status, but
        # suppress the transient "اتصال قطع شد" / "خطا" toasts during an
        # intentional restart (they're just the old session dying / stale signal
        # churn; the resolver shows the real outcome when the restart settles).
        if getattr(self, "_restarting", False) and status in ("idle", "error"):
            return
        if status == "active":
            Toast.show_message(self, tr("اتصال برقرار شد — spoofing فعال"), "ok")
            self._resilience_timer.start()
            self._pump_resilience()
        elif status == "idle":
            Toast.show_message(self, tr("اتصال قطع شد"), "warn")
            self._resilience_timer.stop()
        elif status == "error":
            Toast.show_message(self, tr("خطا در اتصال — لاگ را ببینید"), "err")
            self._resilience_timer.stop()

    def _pump_resilience(self):
        """Push a concise live resilience summary into the dashboard strip."""
        try:
            snap = self.engine.diagnostics()
        except Exception:
            return
        if not getattr(snap, "resilience_on", False):
            self.page_dashboard.set_resilience(tr("غیرفعال"))
            return
        chain = " → ".join(snap.strategy_chain) or (snap.active_strategy or "—")
        throttle = " · throttle!" if snap.throttled else ""
        self.page_dashboard.set_resilience(
            tr("RST {n}/{b} · زنجیره {chain}{throttle}").format(
                n=snap.forged_rst_count, b=snap.rst_budget,
                chain=chain, throttle=throttle))

    def _on_strategy_changed(self, method: str):
        self.page_log.append(
            "[strategy] " + tr("استراتژی فعال: {m}").format(m=method))

    def _on_profile_selected(self, profile):
        # #2: if the engine is already running when the user activates a
        # different server, transparently restart it on the new profile so the
        # switch takes effect immediately — no manual stop/start needed.
        # NOTE: ``is_running`` is a *property* on both EngineBridge and the
        # controller — calling it like a method raised TypeError (swallowed by
        # the except), so the auto-restart never fired and the engine stayed
        # stuck on the previous config (feedback #2).
        try:
            was_running = bool(self.engine.is_running)
        except Exception:
            was_running = False
        # Also restart when the engine is sitting in ERROR (the «تلاش دوباره»
        # button is showing): the user picking a new config clearly wants to
        # connect to it, so switching the active config must kick off a fresh
        # connect — not leave the stale «تلاش دوباره» button (reported bug). We
        # only do this when a real profile is being selected (not a deselect).
        try:
            in_error = (self.engine.status_value == "error")
        except Exception:
            in_error = False
        # Guard against a NEEDLESS restart when the user re-activates the config
        # that is ALREADY the running one (e.g. clicking «فعال‌سازی» on the
        # already-active row, or a spurious selection signal). Tearing down and
        # rebuilding a perfectly working tunnel here is exactly the "I switch the
        # active config and it resets itself / sometimes breaks" surprise the
        # user hit. If the engine is up AND this profile is the active endpoint,
        # there is nothing to switch to — keep the live session untouched.
        same_active = False
        if was_running and profile is not None:
            try:
                same_active = bool(self.engine.is_active_profile(profile))
            except Exception:
                same_active = False
        should_restart = (was_running or (in_error and profile is not None)) \
            and not same_active

        # --- 1) apply the new profile + any mode change FIRST ---------------
        # so the (re)start below already sees the new server *and* the right
        # connection mode. Doing the mode switch after start() was part of why
        # the engine stayed stuck on the previous config.
        self.engine.set_profile(profile)
        # keep the persistent status bar in sync with the active server (#9)
        self.active_bar.set_profile(profile)
        # #6: the connection-mode selector only applies to spoof (local-IP)
        # configs; ordinary configs connect directly like a normal client.
        self._sync_mode_applicability(profile)

        if profile:
            self.page_log.append(
                "[profile] " + tr("انتخاب شد: {name}").format(name=profile.display_name))
            # auto-switch to Tunnel so the VLESS/VMess/Trojan config is actually
            # used: in "SNI Only" the profile is ignored (the "still need
            # V2RayTun" bug). Only nudge when the user is on the no-core default.
            if self.store.get("connection_mode", "Tunnel") == "SNI Only":
                self.store.set("connection_mode", "Tunnel")
                self.store.save_config()
                self.engine.update_config(self.store.config)
                # keep the Settings combo + Dashboard badge in sync
                if hasattr(self, "page_settings"):
                    self.page_settings.set_mode("Tunnel")
                self.page_dashboard.set_mode("Tunnel")
                self.page_log.append(
                    "[mode] " + tr("حالت به «Tunnel» تغییر کرد تا کانفیگ انتخاب‌شده واقعاً استفاده شود"))
                Toast.show_message(
                    self, tr("حالت به «Tunnel» تغییر کرد (برای استفاده از کانفیگ)"),
                    "ok")

        # --- 2) (re)start the live engine if it was running OR errored ------
        if should_restart:
            self.page_log.append(
                "[profile] " + tr("راه‌اندازی مجدد خودکار برای اعمال سرور جدید…"))
            # _begin_restart() masks the transient idle as "connecting", drives
            # the phase state machine and arms a resolver so the dashboard shows
            # «در حال اتصال…» immediately (not «شروع»/«تلاش دوباره») and the user
            # can't accidentally fire a conflicting start mid-switch.
            self._begin_restart()
            try:
                Toast.show_message(
                    self, tr("سرور جدید فعال شد — اتصال بازنشانی شد"), "ok")
            except Exception:
                pass

    def _sync_mode_applicability(self, profile):
        """Enable the mode selector only for spoof (local-IP) configs (#6).

        Ordinary configs connect directly like a normal client, so the
        Tunnel / SNI-Only selector is irrelevant for them and is greyed out with
        an explanatory hint. Spoof configs keep the selector active because they
        genuinely need the SNI spoofer.
        """
        is_spoof = bool(getattr(profile, "is_spoof_config", False)) if profile \
            else True  # no profile selected → SNI-Only forwarder still relevant
        if hasattr(self, "page_settings"):
            try:
                self.page_settings.set_mode_applicable(is_spoof)
            except Exception:
                pass
        # #6: reflect spoof-applicability on the dashboard so the "استراتژی فعال"
        # card / resilience strip don't falsely claim a strategy runs for an
        # ordinary (direct) config where the spoofer isn't in the path.
        if hasattr(self, "page_dashboard"):
            try:
                self.page_dashboard.set_spoof_active(is_spoof)
            except Exception:
                pass

    def _restart_when_idle(self, gen: int | None = None):
        """Phase "stopping": wait for the old session to fully stop, then start.

        Polls engine status every 150 ms (≈12 s cap). Starting only after the
        previous session reached idle guarantees the spoofer port + xray
        subprocess are released, so the new profile actually connects instead of
        the engine appearing "active" while stuck on the old config.

        ``gen`` pins the restart cycle this timer belongs to; if the user hit
        Stop (or a newer restart began) the generation moved on and this timer
        silently retires so it can't resurrect a cancelled start.
        """
        if gen is None:
            gen = getattr(self, "_restart_gen", 0)
        if gen != getattr(self, "_restart_gen", 0):
            return  # cancelled / superseded — do nothing
        if getattr(self, "_restart_phase", "idle") != "stopping":
            return  # already moved on (started / cancelled)
        try:
            running = bool(self.engine.is_running)
        except Exception:
            running = False
        self._restart_attempts = getattr(self, "_restart_attempts", 0) + 1
        timed_out = self._restart_attempts > 80
        if not running or timed_out:
            # the old session is down — fire the new start and switch to the
            # "starting" phase so the resolver (not fragile signal timing) judges
            # success/failure.
            self._restart_phase = "starting"
            self._restart_attempts = 0
            try:
                self.engine.start()
            except Exception:
                # start failed outright — drop the mask, surface reality.
                self._restarting = False
                self._restart_phase = "idle"
                self._surface_restart_failure(gen)
                return
            QTimer.singleShot(200, lambda g=gen: self._restart_resolve(g))
            return
        QTimer.singleShot(150, lambda g=gen: self._restart_when_idle(g))

    def _restart_resolve(self, gen: int):
        """Phase "starting": decide success/failure from the ENGINE's own state.

        Polls the live engine status rather than trusting (possibly stale,
        out-of-order) status signals. Resolves when the engine is genuinely
        ``active`` (success) or has settled on ``idle``/``error`` for a sustained
        stretch after the new start (real failure). A hard cap frees the UI no
        matter what so it can never wedge on "در حال اتصال…".
        """
        if gen != getattr(self, "_restart_gen", 0):
            return  # superseded / cancelled
        if getattr(self, "_restart_phase", "idle") != "starting":
            return
        try:
            status = self.engine.status_value
        except Exception:
            status = "idle"
        self._restart_attempts = getattr(self, "_restart_attempts", 0) + 1

        if status == "active":
            # success — drop the mask; _dispatch_status already shows "active".
            self._restarting = False
            self._restart_phase = "idle"
            self.page_dashboard.set_status("active")
            self.active_bar.set_status("active")
            return

        # Count consecutive non-connecting settles. The new start should reach
        # "connecting" quickly; if instead it sits at idle/error for ~2.4s, the
        # new config genuinely failed.
        if status in ("idle", "error"):
            self._restart_settle = getattr(self, "_restart_settle", 0) + 1
        else:  # connecting — still working, reset the settle counter
            self._restart_settle = 0

        # hard cap (~24s) OR a sustained settle (~16 polls × 150ms ≈ 2.4s).
        if self._restart_attempts > 160 or self._restart_settle >= 16:
            self._restarting = False
            self._restart_phase = "idle"
            self._restart_settle = 0
            self._surface_restart_failure(gen)
            return
        QTimer.singleShot(150, lambda g=gen: self._restart_resolve(g))

    def _surface_restart_failure(self, gen: int):
        """Show the engine's real (failed) status and free the controls."""
        try:
            status = self.engine.status_value
        except Exception:
            status = "idle"
        if status not in ("idle", "error"):
            status = "error"
        self.page_dashboard.set_status(status)
        self.active_bar.set_status(status)
        self.page_log.append(
            "[restart] " + tr("اتصال مجدد برقرار نشد — کنترل آزاد شد"))
        try:
            Toast.show_message(
                self, tr("اتصال مجدد ناموفق بود — می‌توانید دوباره تلاش کنید"),
                "warn")
        except Exception:
            pass

    # back-compat shim: older callers/tests referenced _restart_watchdog.
    def _restart_watchdog(self, gen: int):
        if gen != getattr(self, "_restart_gen", 0):
            return
        if not getattr(self, "_restarting", False):
            return
        self._restarting = False
        self._restart_phase = "idle"
        self._surface_restart_failure(gen)

    def _on_auto_prober_changed(self, enabled: bool):
        # the StrategyPage already persisted the flag; push it to the live engine
        self.store.save_config()
        self.engine.update_config(self.store.config)
        self.page_log.append(
            "[auto-prober] " + (tr("فعال شد") if enabled else tr("غیرفعال شد")))
        Toast.show_message(
            self,
            tr("پراب خودکار فعال شد") if enabled else tr("پراب خودکار غیرفعال شد"),
            "ok")

    def _on_strategy_selected(self, key: str):
        # StrategyPage already persisted bypass_method (and cleared auto_prober);
        # push to the live engine so the next connection uses it.
        self.store.save_config()
        self.engine.update_config(self.store.config)
        # find the human-readable name for the toast/log
        name = next((n for k, n, _ in STRATEGIES if k == key), key)
        self.page_log.append(
            "[strategy] " + tr("انتخاب دستی: {name} ({key})").format(name=name, key=key))

        # #4: reflect the new strategy on the dashboard immediately. The engine
        # only emits its ``strategy`` signal on start / auto-probe, so a manual
        # pick never reached the dashboard badge before — it kept showing the
        # old strategy until the next connect.
        try:
            self.page_dashboard.set_active_strategy(key)
        except Exception:
            pass

        # #3: if the engine is running, restart the active config so the new
        # strategy actually takes effect now (same transparent stop→idle→start
        # mechanism as the config-switch restart, #2). ``is_running`` is a
        # *property* on both EngineBridge and the controller.
        try:
            was_running = bool(self.engine.is_running)
        except Exception:
            was_running = False
        if was_running:
            self.page_log.append(
                "[strategy] " + tr("راه‌اندازی مجدد خودکار برای اعمال استراتژی جدید…"))
            # mask the stop→start idle as "connecting" (bug #2) + watchdog, same
            # as the config-switch restart above.
            self._begin_restart()
            Toast.show_message(
                self,
                tr("استراتژی «{name}» اعمال شد — اتصال بازنشانی شد").format(name=name),
                "ok")
        else:
            Toast.show_message(
                self, tr("استراتژی انتخاب شد: {name}").format(name=name), "ok")

    def _refresh_after_pairs_changed(self):
        """The inline pool scan added/removed sni_ip_pairs; reload Settings so
        its SNI/IP pair manager + combo immediately reflect the change."""
        try:
            self.page_settings.load_from(self.store.config)
        except Exception:
            pass

    def _save_settings(self):
        self.store.update(**self.page_settings.collect())
        self.store.save_config()
        self.engine.update_config(self.store.config)
        self.page_dashboard.set_mode(
            self.store.get("connection_mode", "Tunnel"))
        Toast.show_message(self, tr("تنظیمات ذخیره شد"), "ok")

    def _save_pool_settings(self):
        """Persist the pool IP/SNI/optimise/workers settings (these widgets
        live on the Pool page now). Pushes the new config to the engine so a
        running session picks up the changed pool immediately."""
        self.store.update(**self.page_pool.collect_pool_settings())
        self.store.save_config()
        try:
            self.engine.update_config(self.store.config)
        except Exception:
            pass

    def _on_page_changed(self, index: int):
        current = self.stack.widget(index)
        # replay the dashboard intro when navigating back to it
        if current is self._scroll.get(self.page_dashboard):
            self.page_dashboard.play_intro()
        # only poll the route-pool page while it is visible (saves probes/CPU)
        if current is self._scroll.get(self.page_pool):
            self.page_pool.start_polling()
        else:
            self.page_pool.stop_polling()
            # leaving the page: stop any in-flight inline scan so it never runs
            # probes in the background after the user navigated away.
            self.page_pool.shutdown_scan()

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

        # #5: the "تشخیص" (Diagnostics) entry was removed — the page no longer
        # exists. Nav indexes still map 1:1 onto the stack order above.
        # Each entry now carries a crisp 3-D vector icon-name (issue #1).
        items = [
            ("داشبورد", "dashboard"),
            ("پروفایل‌ها", "servers"),
            ("تنظیمات", "settings"),
            ("استراتژی", "strategy"),
            ("استخر", "strategy"),
            ("لاگ", "logs"),
        ]
        self._nav_buttons: list[NavItem] = []
        for idx, (text, icon_name) in enumerate(items):
            btn = NavItem(tr(text), icon_name)
            btn.clicked.connect(lambda _=False, i=idx: self.stack.setCurrentIndex(i))
            # recolour the icon whenever the selection state flips (#1)
            btn.toggled.connect(lambda checked, b=btn: b.refresh_icon(checked))
            self.nav_group.addButton(btn, idx)
            lay.addWidget(btn)
            self._nav_buttons.append(btn)
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
        # pull the icon colours from the active palette BEFORE building any
        # icons so nav/row/toolbar glyphs are tinted for this theme (#1).
        _icons.apply_palette(palette)
        qss = build_qss(palette)
        # Apply the theme at the *application* level so EVERY top-level window —
        # including dialogs (scanner, QMessageBox confirms, …) — inherits it.
        # Previously the QSS was set only on MainWindow, so dialogs popped up in
        # the blinding OS-default white that clashed with dark/light mode (#5).
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        self.setStyleSheet(qss)
        # propagate the palette to widgets that paint inline (not via QSS)
        self.page_dashboard.set_palette(palette)
        # log console text must follow the theme so it's never white-on-white (#4)
        if hasattr(self, "page_log"):
            self.page_log.set_palette(palette)
        # re-tint every Card's drop shadow so the light theme gets a soft, clean
        # shadow instead of the heavy near-black one (#4)
        from ui.widgets import Card as _Card
        for card in self.findChildren(_Card):
            try:
                card.tune_shadow_for(palette.is_dark)
            except Exception:
                pass
        # recolour every 3-D nav icon for the new palette (#1)
        for b in getattr(self, "_nav_buttons", []):
            try:
                b.refresh_icon()
            except Exception:
                pass
        # rebuild the profiles list so its row glyphs + toolbar icons re-tint
        if hasattr(self, "page_profiles"):
            try:
                self.page_profiles.refresh()
            except Exception:
                pass
        # recolour the living wave backdrop (accent → secondary gaming accent)
        accent2 = ACCENT2_DARK if palette.is_dark else ACCENT2_LIGHT
        self.wave_bg.set_palette(palette.accent, accent2)
        self.wave_bg.lower()
        try:
            hwnd = int(self.winId())
            # only keep the dark immersive title region; we paint our own solid
            # 3-D gradient backdrop now (no Mica/Acrylic — see __init__ note).
            win_effects.set_dark_titlebar(hwnd, palette.is_dark)
        except Exception:
            pass

    def toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme()
        self.store.set("theme", self._theme)
        self.store.save_config()

    def toggle_maximize(self):
        """Maximize the window, or restore it if already maximized (#6).

        The layout is fully responsive (scroll areas + stretch factors), so
        growing to the full screen never breaks or overlaps the content; the
        title-bar glyph is kept in sync via changeEvent → _sync_max_button.

        We remember the *normal* geometry before maximizing and restore it
        explicitly. This is the fix for the bug where minimising a maximized
        (or even a normal) window and then re-opening it from the taskbar left
        the window stuck at full size: on a frameless window Qt's own
        normal-geometry bookkeeping is unreliable, so we drive it ourselves.
        """
        if self.isMaximized() or self.isFullScreen():
            self._restore_normal()
        else:
            # remember where we were so restore puts the window back exactly
            self._normal_geometry = self.geometry()
            self._is_maximized = True
            self.showMaximized()

    def _restore_normal(self):
        """Return the window to its remembered normal size/position."""
        self._is_maximized = False
        self.showNormal()
        geo = getattr(self, "_normal_geometry", None)
        if geo is not None and geo.isValid():
            self.setGeometry(geo)

    def _sync_max_button(self):
        """Keep the maximize/restore glyph + rounded corners match the state."""
        # while minimised, keep whatever state we had so the glyph doesn't flap
        if self.isMinimized():
            maxed = self._is_maximized
        else:
            maxed = self.isMaximized() or self.isFullScreen()
        try:
            self.title_bar.update_max_label(maxed)
        except Exception:
            pass
        # #4: drop the rounded corners + border while maximized so the window
        # fills the screen edge-to-edge with no gap/rounded-corner artefacts;
        # restore them when back to normal. Toggled via a dynamic property the
        # QSS keys off (RootBackdrop[maximized="1"]).
        try:
            self.setProperty("maximized", "1" if maxed else "0")
            self.style().unpolish(self)
            self.style().polish(self)
        except Exception:
            pass
        # keep the resize grip hidden while maximized, visible otherwise (#1)
        self._position_size_grip()

    # -- interactive edge / corner resize for the frameless window (#3) -----
    _RESIZE_MARGIN = 9   # px band around the window edges that starts a resize

    def _edge_at(self, pos):
        """Return the Qt edges under *pos* (within the grip margin), or None."""
        from PySide6.QtCore import Qt as _Qt
        m = self._RESIZE_MARGIN
        r = self.rect()
        left = pos.x() <= m
        right = pos.x() >= r.width() - m
        top = pos.y() <= m
        bottom = pos.y() >= r.height() - m
        edges = _Qt.Edges()
        if left:
            edges |= _Qt.LeftEdge
        if right:
            edges |= _Qt.RightEdge
        if top:
            edges |= _Qt.TopEdge
        if bottom:
            edges |= _Qt.BottomEdge
        # NB: in newer PySide6 ``int(edges)`` raises on the flag enum, so test
        # the value via ``.value`` (with a plain-int fallback for older Qt).
        try:
            truthy = bool(edges.value)
        except AttributeError:
            truthy = bool(int(edges))
        return edges if truthy else None

    def _cursor_for_edges(self, edges):
        from PySide6.QtCore import Qt as _Qt
        L, R = _Qt.LeftEdge, _Qt.RightEdge
        T, B = _Qt.TopEdge, _Qt.BottomEdge
        if (edges & T and edges & L) or (edges & B and edges & R):
            return _Qt.SizeFDiagCursor
        if (edges & T and edges & R) or (edges & B and edges & L):
            return _Qt.SizeBDiagCursor
        if edges & L or edges & R:
            return _Qt.SizeHorCursor
        if edges & T or edges & B:
            return _Qt.SizeVerCursor
        return _Qt.ArrowCursor

    def mouseMoveEvent(self, event):
        # update the cursor to a resize arrow when hovering an edge (only when
        # the window is in its normal, resizable state — not maximized)
        from PySide6.QtCore import Qt as _Qt
        if not (self.isMaximized() or self.isFullScreen()):
            edges = self._edge_at(event.position().toPoint())
            self.setCursor(self._cursor_for_edges(edges) if edges
                           else _Qt.ArrowCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        from PySide6.QtCore import Qt as _Qt
        if (event.button() == _Qt.LeftButton
                and not (self.isMaximized() or self.isFullScreen())):
            edges = self._edge_at(event.position().toPoint())
            if edges is not None:
                win = self.windowHandle()
                if win is not None and hasattr(win, "startSystemResize"):
                    win.startSystemResize(edges)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def toggle_language(self):
        """Switch FA⇄EN and rebuild the window so every label retranslates (#6).

        A full in-place retranslate of hundreds of widgets is brittle; instead
        we persist the new language and recreate MainWindow (fast, < a few ms),
        carrying over the live theme + layout direction. The engine is stopped
        cleanly first so no socket/proxy state leaks across the rebuild.
        """
        from ui import i18n
        new_lang = "en" if i18n.language() == "fa" else "fa"
        i18n.set_language(new_lang)
        self.store.set("language", new_lang)
        self.store.save_config()
        # stop the engine cleanly before tearing the window down
        try:
            self.engine.stop()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            from PySide6.QtCore import Qt as _Qt
            app.setLayoutDirection(
                _Qt.RightToLeft if new_lang == "fa" else _Qt.LeftToRight)
        # build the replacement window, then close this one
        geo = self.geometry()
        new_win = MainWindow(theme=self._theme)
        new_win.setGeometry(geo)
        new_win.show()
        # keep a reference so it isn't garbage-collected during the swap
        if app is not None:
            existing = getattr(app, "_sni_windows", [])
            existing.append(new_win)
            app._sni_windows = existing
        self._is_rebuilding = True
        self.close()

    def resizeEvent(self, event):
        # keep the wave backdrop filling the whole window behind the content
        try:
            self.wave_bg.setGeometry(self.rect())
            self.wave_bg.lower()
        except Exception:
            pass
        # #1: pin the size grip to the trailing bottom corner (mirrors for RTL)
        self._position_size_grip()
        super().resizeEvent(event)

    def _position_size_grip(self):
        """Keep the resize grip in the bottom corner; hide it when maximized."""
        try:
            grip = self.size_grip
        except AttributeError:
            return
        maxed = self.isMaximized() or self.isFullScreen()
        grip.setVisible(not maxed)
        if maxed:
            return
        m = 3
        gw, gh = grip.width(), grip.height()
        if self.layoutDirection() == Qt.RightToLeft:
            x = m                                   # bottom-left for RTL
        else:
            x = self.width() - gw - m               # bottom-right for LTR
        grip.move(x, self.height() - gh - m)
        grip.raise_()

    def showEvent(self, event):
        # resume the animation when visible
        try:
            self.wave_bg.setGeometry(self.rect())
            self.wave_bg.set_enabled(True)
        except Exception:
            pass
        super().showEvent(event)

    def hideEvent(self, event):
        # park the animation while hidden/minimised so it spends zero CPU
        try:
            self.wave_bg.set_enabled(False)
        except Exception:
            pass
        super().hideEvent(event)

    def changeEvent(self, event):
        # park while minimised, resume when restored
        try:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.WindowStateChange:
                minimized = self.isMinimized()
                self.wave_bg.set_enabled(not minimized)
                if not minimized:
                    # #2: the window just left the minimised state (restored from
                    # the taskbar). Re-assert the size the user actually had:
                    # if they were *not* maximized before minimising, force the
                    # window back to its normal geometry — otherwise some
                    # platforms restore it stuck at the previous maximized size.
                    if not self._is_maximized and (
                            self.isMaximized() or self.isFullScreen()):
                        QTimer.singleShot(0, self._restore_normal)
                # #6: keep the maximize/restore button glyph in sync whether the
                # state changed via our button, double-click, or the OS itself.
                if not minimized:
                    self._is_maximized = self.isMaximized() or self.isFullScreen()
                self._sync_max_button()
        except Exception:
            pass
        super().changeEvent(event)

    def closeEvent(self, event):
        """Stop the engine cleanly so no subprocess / thread is orphaned."""
        try:
            self.wave_bg.set_enabled(False)
        except Exception:
            pass
        # wait for any in-flight inline-ping QThreads before teardown so none is
        # GC'd mid-run (would crash on exit)
        try:
            self.page_profiles.stop_inline_pings()
        except Exception:
            pass
        # stop any in-flight inline pool scan so its QThread isn't torn down mid-run
        try:
            self.page_pool.shutdown_scan()
        except Exception:
            pass
        try:
            self.engine.stop()
        except Exception:
            pass
        super().closeEvent(event)
