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

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ui import win_effects
from ui.theme import get_palette, build_qss
from ui.widgets import (
    Card, NavItem, PowerButton, ProfileRow, Sparkline, TitleBar, Toast,
)
from ui.animations import CountUp, PulseDot, stagger_in

from core.config_store import ConfigStore
from core.engine import EngineController
from core.logbuffer import LEVELS, LogBuffer
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

MODES = [
    "Tunnel",          # default: VLESS/xray chained under the spoofer (needs a profile)
    "SNI Only",        # raw forwarder, no xray — for plain HTTPS DPI bypass
    "SNI + Warp",
    "SNI + Psiphon",
    "SNI + Warp-in-Warp",
    "Gaming Mode",
]

# human-readable, Persian hint shown under the mode selector
MODE_HINTS = {
    "Tunnel": "اتصال کامل از طریق کانفیگ انتخاب‌شده (VLESS/VMess/Trojan) با هسته‌ی xray + اسپوف SNI. برای استفاده از کانفیگ‌ها این حالت را انتخاب کنید.",
    "SNI Only": "فقط فورواردر اسپوف SNI بدون xray — برای دور زدن DPI روی ترافیک HTTPS عادی. کانفیگ VLESS در این حالت استفاده نمی‌شود.",
    "SNI + Warp": "کانفیگ + لایه‌ی Cloudflare Warp (نیازمند باینری warp).",
    "SNI + Psiphon": "کانفیگ + لایه‌ی Psiphon (نیازمند باینری psiphon).",
    "SNI + Warp-in-Warp": "کانفیگ + دو لایه‌ی Warp تو در تو.",
    "Gaming Mode": "بهینه‌سازی تأخیر پایین برای بازی (TCP no-delay/fast-open).",
}

STRATEGIES = [
    ("wrong_seq", "Wrong Sequence", "تزریق ClientHello جعلی با seq خارج از پنجره"),
    ("wrong_checksum", "Wrong Checksum", "چک‌سام نامعتبر تا سرور دور بریزد"),
    ("fake_ttl", "Fake TTL", "TTL کوتاه تا فقط به DPI برسد"),
    ("multi_fake", "Multi Fake", "چند بسته جعلی پشت‌سرهم"),
    ("fake_disorder", "Fake Disorder", "بی‌نظمی عمدی در ترتیب بسته‌ها"),
]


# ---------------------------------------------------------------------------
#  Ping worker — runs latency / strategy-test off the GUI thread (step 18)
# ---------------------------------------------------------------------------

class PingWorker(QThread):
    """Run a blocking ping / strategy-test via the engine on a worker thread.

    Emits ``line(str)`` for each result row and ``done(str)`` with a final
    summary. Three kinds:
      * ``"ping_all"``  — ping every profile, ranked lowest-latency first.
      * ``"ping_one"``  — ping a single profile.
      * ``"strategy"``  — test bypass strategies against one profile (which
                          connects / wins); ``strategy`` pins a single one.
    """

    line = Signal(str)
    done = Signal(str)

    def __init__(self, engine, kind: str, *, profile=None, profiles=None,
                 strategy: str = "", parent=None):
        super().__init__(parent)
        self._engine = engine
        self._kind = kind
        self._profile = profile
        self._profiles = list(profiles) if profiles else []
        self._strategy = strategy

    def run(self):  # pragma: no cover - exercised via Qt smoke, not unit
        try:
            if self._kind == "ping_all":
                self._do_ping_all()
            elif self._kind == "ping_one":
                self._do_ping_one()
            elif self._kind == "strategy":
                self._do_strategy()
            else:
                self.done.emit("نوع سنجش ناشناخته")
        except Exception as exc:
            self.line.emit(f"خطا: {exc}")
            self.done.emit("سنجش با خطا متوقف شد")

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def fmt_ping(res, *, rank=None) -> str:
        prefix = f"#{rank} " if rank is not None else ""
        if not res.reachable:
            return f"{prefix}✖ {res.label} — بدون پاسخ"
        parts = [f"{res.best_ms:.0f}ms", f"avg {res.avg_ms:.0f}",
                 f"jitter {res.jitter_ms:.0f}"]
        if res.loss > 0:
            parts.append(f"loss {res.loss*100:.0f}%")
        if res.download_kbps is not None:
            parts.append(f"dl≈{res.download_kbps:.0f}KB/s")
        return f"{prefix}✔ {res.label} — " + " · ".join(parts)

    def _do_ping_all(self):
        from core.ping import PingTester
        results = self._engine.ping_profiles(self._profiles)
        if not results:
            self.done.emit("هیچ نتیجه‌ای — پروفایلی نیست یا خطا رخ داد")
            return
        for i, res in enumerate(results, 1):
            self.line.emit(self.fmt_ping(res, rank=i))
        best = PingTester.best(results)
        if best is None:
            self.done.emit("هیچ سروری پاسخ نداد")
        else:
            self.done.emit(f"بهترین سرور: {best.label} ({best.best_ms:.0f}ms)")

    def _do_ping_one(self):
        res = self._engine.ping_profile(self._profile)
        if res is None:
            self.done.emit("نتیجه‌ای دریافت نشد")
            return
        self.line.emit(self.fmt_ping(res))
        if res.reachable:
            self.done.emit(f"{res.label}: {res.best_ms:.0f}ms")
        else:
            self.done.emit(f"{res.label}: بدون پاسخ")

    def _do_strategy(self):
        report = self._engine.probe_strategies_for(
            self._profile, strategy=(self._strategy or None))
        if not report.results:
            self.done.emit("استراتژی‌ای تست نشد (آدرس/کاندیدا نامعتبر)")
            return
        for r in report.results:
            mark = "✔" if r.ok else "✖"
            extra = (f"{r.latency_ms:.0f}ms · score={r.score:.2f}"
                     if r.ok else r.outcome)
            self.line.emit(f"{mark} {r.strategy:14} — {extra}")
        best = report.best
        if best is None:
            self.done.emit("هیچ استراتژی‌ای وصل نشد")
        else:
            self.done.emit(
                f"بهترین استراتژی: {best.strategy} ({best.latency_ms:.0f}ms)")


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
    """Return 'tunnel' for warp/psiphon chained modes, else 'proxy'."""
    m = (mode or "").lower()
    if "warp" in m or "psiphon" in m or "gaming" in m:
        return "tunnel"
    return "proxy"


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
            "کنترل‌مرکز", "وضعیت زنده‌ی تونل، مصرف و کنترل سریع روشن/خاموش")
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
        # tunnel / proxy badge — answers feedback 7 ("is this a tunnel or proxy?")
        self.mode_badge = QLabel("پروکسی محلی")
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
        tlabel = QLabel("مصرف زنده")
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
        self.stat_conns = _stat_card("0", "اتصالات فعال",
                                     accent_color=palette.accent)
        self.stat_total = _stat_card("0 B", "مصرف کل (↓/↑)")
        self.stat_mode = _stat_card("SNI Only", "حالت")
        self.stat_strategy = _stat_card("wrong_seq", "استراتژی فعال")
        self.stat_cards = [self.stat_conns, self.stat_total,
                           self.stat_mode, self.stat_strategy]
        for c in self.stat_cards:
            stats.addWidget(c)
        root.addLayout(stats)

        # --- resilience strip (live fallback state) ---
        self.lbl_resilience = QLabel("تاب‌آوری: —")
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
        self.status_label.setText({
            "idle": "آماده — متوقف",
            "connecting": "در حال اتصال…",
            "active": "متصل — تونل فعال",
            "error": "خطا — تلاش دوباره",
        }.get(state, "آماده — متوقف"))
        if state == "idle":
            # reset the live picture when the session ends
            self.spark.clear()
            self.rate_down.setText("↓ 0 B/s")
            self.rate_up.setText("↑ 0 B/s")
            self.lbl_resilience.setText("تاب‌آوری: —")

    def on_count(self, active: int, total: int):
        """Slot for the engine's connection-count signal."""
        self._count.to(active)

    def on_traffic(self, up_bytes: int, down_bytes: int,
                   up_bps: float, down_bps: float):
        """Slot for the engine's live traffic signal (step 20)."""
        self.spark.push(down_bps, up_bps)
        self.rate_down.setText(f"↓ {fmt_rate(down_bps)}")
        self.rate_up.setText(f"↑ {fmt_rate(up_bps)}")
        self.stat_total.value_label.setText(
            f"{fmt_bytes(down_bytes)} / {fmt_bytes(up_bytes)}")

    def set_resilience(self, text: str):
        """Slot for the live resilience/fallback summary line."""
        self.lbl_resilience.setText(f"تاب‌آوری: {text}")

    def set_active_strategy(self, key: str):
        self.stat_strategy.value_label.setText(key)

    def set_mode(self, mode: str):
        self.stat_mode.value_label.setText(mode)
        kind = mode_kind(mode)
        self.mode_badge.setProperty("kind", kind)
        self.mode_badge.setText(
            "تونل کامل" if kind == "tunnel" else "پروکسی محلی")
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
        self.mode = QComboBox()
        self.mode.addItems(MODES)
        form.addWidget(self.mode)
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Faint")
        self.mode_hint.setWordWrap(True)
        form.addWidget(self.mode_hint)
        self.mode.currentTextChanged.connect(self._update_mode_hint)
        self._update_mode_hint(self.mode.currentText())

        form.addWidget(self._field_label("SNI جعلی"))
        self.sni = QComboBox()
        self.sni.setEditable(True)
        self.sni.addItems(DEFAULT_SNIS)
        form.addWidget(self.sni)

        ports_wrap = QWidget()
        ports = QHBoxLayout(ports_wrap)
        ports.setContentsMargins(0, 0, 0, 0)
        ports.setSpacing(14)
        ports.addWidget(self._labelled_spin("پورت گوش‌دادن", 40443, out="listen"))
        ports.addWidget(self._labelled_spin("پورت SOCKS", 10808, out="socks"))
        ports_wrap.setMinimumHeight(78)
        form.addWidget(ports_wrap)
        form.addSpacing(10)

        form.addWidget(self._field_label("IP اتصال"))
        self.connect_ip = QLineEdit("www.speedtest.net")
        form.addWidget(self.connect_ip)

        # --- LAN sharing (use the proxy from a phone on the same Wi-Fi) ---
        form.addSpacing(8)
        self.chk_lan = QCheckBox(
            "اشتراک LAN — پروکسی روی شبکه‌ی محلی باز شود (برای گوشی)")
        form.addWidget(self.chk_lan)
        self.lan_hint = QLabel("")
        self.lan_hint.setObjectName("Muted")
        self.lan_hint.setWordWrap(True)
        form.addWidget(self.lan_hint)
        self.chk_lan.toggled.connect(self._update_lan_hint)

        # --- system proxy vs. tunnel (feedback 7) ---
        form.addSpacing(8)
        self.chk_system_proxy = QCheckBox(
            "پروکسی سیستم — همه‌ی برنامه‌های ویندوز خودکار از تونل رد شوند")
        form.addWidget(self.chk_system_proxy)
        self.proxy_hint = QLabel("")
        self.proxy_hint.setObjectName("Muted")
        self.proxy_hint.setWordWrap(True)
        form.addWidget(self.proxy_hint)
        self.chk_system_proxy.toggled.connect(self._update_proxy_hint)

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
        mode = cfg.get("connection_mode", "Tunnel")
        i = self.mode.findText(mode)
        if i >= 0:
            self.mode.setCurrentIndex(i)
        self.sni.setCurrentText(cfg.get("FAKE_SNI", "www.speedtest.net"))
        self.spin_listen.setValue(int(cfg.get("LISTEN_PORT", 40443)))
        self.spin_socks.setValue(int(cfg.get("socks_port", 10808)))
        self.connect_ip.setText(str(cfg.get("CONNECT_IP", "")))
        self.chk_lan.setChecked(bool(cfg.get("allow_lan", False)))
        self._update_lan_hint(self.chk_lan.isChecked())
        self.chk_system_proxy.setChecked(bool(cfg.get("system_proxy", False)))
        self._update_proxy_hint(self.chk_system_proxy.isChecked())

    def collect(self) -> dict:
        """Read the widgets back into a config dict fragment."""
        return {
            "connection_mode": self.mode.currentText(),
            "FAKE_SNI": self.sni.currentText().strip(),
            "LISTEN_PORT": self.spin_listen.value(),
            "socks_port": self.spin_socks.value(),
            "CONNECT_IP": self.connect_ip.text().strip(),
            "allow_lan": self.chk_lan.isChecked(),
            "system_proxy": self.chk_system_proxy.isChecked(),
        }

    def set_mode(self, mode: str) -> None:
        """Programmatically select a connection mode (keeps hint in sync)."""
        i = self.mode.findText(mode)
        if i >= 0:
            self.mode.setCurrentIndex(i)

    def _update_mode_hint(self, mode: str) -> None:
        self.mode_hint.setText(MODE_HINTS.get(mode, ""))

    def _update_lan_hint(self, on: bool) -> None:
        """Show the LAN address the phone should use when sharing is on."""
        if not on:
            self.lan_hint.setText(
                "خاموش — پروکسی فقط روی همین کامپیوتر (127.0.0.1) در دسترس است")
            return
        try:
            from core.xray_manager import lan_ip_address
            ip = lan_ip_address()
        except Exception:
            ip = "<IP این کامپیوتر>"
        port = self.spin_socks.value()
        self.lan_hint.setText(
            f"روشن — در گوشی، پروکسی SOCKS5 را روی {ip}:{port} تنظیم کنید "
            f"(هر دو دستگاه باید روی یک شبکه/Wi-Fi باشند)")

    def _update_proxy_hint(self, on: bool) -> None:
        """Explain the tunnel-vs-system-proxy choice (feedback 7)."""
        if on:
            self.proxy_hint.setText(
                "حالت «پروکسی سیستم»: هنگام اتصال، پروکسی ویندوز روی پورت HTTP "
                "محلی تنظیم می‌شود و با قطع اتصال خودکار برمی‌گردد. فقط در "
                "حالت‌های دارای xray (نه SNI Only) و روی ویندوز کار می‌کند.")
        else:
            self.proxy_hint.setText(
                "حالت «تونل»: فقط برنامه‌هایی که دستی روی پروکسی محلی تنظیم "
                "شده‌اند رد می‌شوند؛ تنظیمات ویندوز دست‌نخورده می‌ماند.")

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(t)
        lbl.setObjectName("Muted")
        return lbl

    def _labelled_spin(self, t: str, val: int, out: str) -> QWidget:
        from PySide6.QtWidgets import QSizePolicy
        w = QWidget()
        # the container must claim enough vertical room for label + spinbox,
        # otherwise the HBox squeezes it and the spinbox text gets clipped
        # (the "tiny squished port fields" bug).
        w.setMinimumHeight(74)
        w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._field_label(t))
        sp = QSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(val)
        sp.setMinimumHeight(42)          # never clipped (the spinbox bug)
        sp.setButtonSymbols(QSpinBox.UpDownArrows)
        lay.addWidget(sp)
        lay.addStretch(1)
        setattr(self, f"spin_{out}", sp)
        return w


class ProfilesPage(QWidget):
    """Import + manage server profiles (share links / subscriptions).

    v2rayN-style: the user pastes a ``vless://`` / ``vmess://`` / ``trojan://``
    / ``ss://`` link or a subscription URL; the page parses it via
    :mod:`core.share_link` and stores the resulting :class:`Profile`(s). The
    selected profile is what the engine chains xray + spoofing under.
    """

    def __init__(self, store: ConfigStore, engine=None, parent=None):
        super().__init__(parent)
        self._store = store
        self._engine = engine          # EngineBridge — used for ping (optional)
        self._ping_worker = None       # live PingWorker (kept to avoid GC)
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
        self.btn_edit = QPushButton("\u270e  ویرایش")
        self.btn_edit.setObjectName("Ghost")
        self.btn_delete = QPushButton("\U0001f5d1  حذف انتخاب‌شده")
        self.btn_delete.setObjectName("Ghost")
        del_row.addWidget(self.btn_edit)
        del_row.addWidget(self.btn_delete)
        lb.addLayout(del_row)
        root.addWidget(listc, 1)

        # --- ping / strategy-test card (feedback 9) ---
        pingc = Card()
        pb = pingc.body()
        pb.addWidget(_section_title(
            "سنجش پیش از اتصال",
            "ببین کدوم سرور پینگ پایین‌تر/دانلود بهتری دارد و کدوم استراتژی وصل می‌شود"))

        ping_btns = QHBoxLayout()
        ping_btns.setSpacing(10)
        self.btn_ping_all = QPushButton("\U0001f4e1  پینگ همه")
        self.btn_ping_all.setObjectName("Primary")
        self.btn_ping_one = QPushButton("\U0001f4e1  پینگ این سرور")
        self.btn_ping_one.setObjectName("Ghost")
        ping_btns.addWidget(self.btn_ping_all)
        ping_btns.addWidget(self.btn_ping_one)
        ping_btns.addStretch(1)
        pb.addLayout(ping_btns)

        # strategy-ping row: choose a strategy (or "all") to test connectivity with
        strat_row = QHBoxLayout()
        strat_row.setSpacing(10)
        strat_lbl = QLabel("استراتژی برای تست:")
        strat_lbl.setObjectName("Muted")
        self.cmb_ping_strategy = QComboBox()
        self.cmb_ping_strategy.addItem("همه‌ی استراتژی‌ها", "")
        for key, title, _desc in STRATEGIES:
            self.cmb_ping_strategy.addItem(title, key)
        self.btn_test_strategies = QPushButton("\U0001f9ea  تست استراتژی‌ها")
        self.btn_test_strategies.setObjectName("Ghost")
        strat_row.addWidget(strat_lbl)
        strat_row.addWidget(self.cmb_ping_strategy, 1)
        strat_row.addWidget(self.btn_test_strategies)
        pb.addLayout(strat_row)

        self.ping_status = QLabel("")
        self.ping_status.setObjectName("Muted")
        pb.addWidget(self.ping_status)
        self.ping_output = QPlainTextEdit()
        self.ping_output.setObjectName("PingOutput")
        self.ping_output.setReadOnly(True)
        self.ping_output.setMaximumHeight(150)
        self.ping_output.setPlaceholderText("نتیجه‌ی پینگ/تست استراتژی اینجا نمایش داده می‌شود …")
        pb.addWidget(self.ping_output)
        root.addWidget(pingc)

        # wiring
        self.btn_import.clicked.connect(self._import_link)
        self.input.returnPressed.connect(self._import_link)
        self.btn_paste.clicked.connect(self._paste)
        self.btn_sub.clicked.connect(self._import_subscription)
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemDoubleClicked.connect(lambda *_: self._edit_selected())
        self.btn_ping_all.clicked.connect(self._ping_all)
        self.btn_ping_one.clicked.connect(self._ping_one)
        self.btn_test_strategies.clicked.connect(self._test_strategies)

        self.refresh()

    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(t)
        lbl.setObjectName("Muted")
        return lbl

    # -- list rendering ----------------------------------------------------
    def refresh(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        sel = self._store.selected_index
        for i, p in enumerate(self._store.profiles):
            item = QListWidgetItem(self.list)
            row = ProfileRow(p, active=(i == sel))
            row.edit.connect(lambda _=False, idx=i: self._edit_index(idx))
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        if 0 <= sel < self.list.count():
            self.list.setCurrentRow(sel)
        # empty-state hint
        if self.list.count() == 0:
            ph = QListWidgetItem("هنوز پروفایلی اضافه نشده — یک لینک بچسبانید")
            ph.setFlags(Qt.NoItemFlags)
            self.list.addItem(ph)
        self.list.blockSignals(False)

    # -- actions -----------------------------------------------------------
    def _toast(self, text: str, kind: str = "info"):
        Toast.show_message(self.window(), text, kind)

    def _import_link(self):
        """Paste a link → parse → open the editable dialog pre-filled.

        The user no longer types host/port by hand: every field is decoded
        from the share link and shown for review/edit before it is stored.
        """
        text = self.input.text().strip()
        if not text:
            return
        try:
            profile = parse_link(text)
        except ShareLinkError as exc:
            self._toast(f"لینک نامعتبر: {exc}", "err")
            return
        dlg = ProfileDialog(profile, self.window(), title="افزودن پروفایل جدید")
        if dlg.exec() != ProfileDialog.Accepted:
            self._toast("افزودن لغو شد", "info")
            return
        edited = dlg.result_profile
        self._store.add_profile(edited, select=True)
        self.input.clear()
        self.refresh()
        self._toast(f"پروفایل افزوده شد: {edited.display_name}", "ok")
        self._emit_selection()

    def _edit_selected(self):
        """Open the editor on the currently selected profile and save edits."""
        row = self.list.currentRow()
        if not (0 <= row < len(self._store.profiles)):
            self._toast("ابتدا یک پروفایل را انتخاب کنید", "warn")
            return
        self._edit_index(row)

    def _edit_index(self, row: int):
        """Open the editor on a specific profile row and save edits."""
        if not (0 <= row < len(self._store.profiles)):
            return
        current = self._store.profiles[row]
        dlg = ProfileDialog(current, self.window(), title="ویرایش پروفایل")
        if dlg.exec() != ProfileDialog.Accepted:
            return
        self._store.profiles[row] = dlg.result_profile
        self._store.save_profiles()
        self.refresh()
        # re-emit so the engine picks up edits to the active profile
        if row == self._store.selected_index:
            self._emit_selection()
        self._toast("پروفایل به‌روزرسانی شد", "ok")

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

    # -- ping / strategy-test (feedback 9) ---------------------------------
    def _ping_busy(self, busy: bool):
        for b in (self.btn_ping_all, self.btn_ping_one, self.btn_test_strategies):
            b.setEnabled(not busy)

    def _start_ping_job(self, kind: str, *, profile=None, strategy: str = ""):
        """Run a ping/strategy-test job on a background thread (GUI stays live)."""
        if self._engine is None:
            self._toast("موتور در دسترس نیست", "err")
            return
        if self._ping_worker is not None and self._ping_worker.isRunning():
            self._toast("یک سنجش در حال اجراست …", "warn")
            return
        # push freshest ping config into the engine before measuring
        self._engine.update_config(self._store.config)
        self.ping_output.clear()
        self.ping_status.setText("در حال سنجش …")
        self._ping_busy(True)
        worker = PingWorker(self._engine, kind, profile=profile,
                            profiles=list(self._store.profiles),
                            strategy=strategy)
        worker.line.connect(self._ping_line)
        worker.done.connect(self._ping_done)
        self._ping_worker = worker
        worker.start()

    def _ping_line(self, text: str):
        self.ping_output.appendPlainText(text)

    def _ping_done(self, summary: str):
        self.ping_status.setText(summary)
        self._ping_busy(False)

    def _ping_all(self):
        if not self._store.profiles:
            self._toast("هیچ پروفایلی برای پینگ نیست", "warn")
            return
        self._start_ping_job("ping_all")

    def _ping_one(self):
        prof = self._store.selected_profile
        if prof is None:
            self._toast("ابتدا یک سرور را انتخاب کنید", "warn")
            return
        self._start_ping_job("ping_one", profile=prof)

    def _test_strategies(self):
        prof = self._store.selected_profile
        if prof is None:
            self._toast("ابتدا یک سرور را انتخاب کنید", "warn")
            return
        strategy = self.cmb_ping_strategy.currentData() or ""
        self._start_ping_job("strategy", profile=prof, strategy=strategy)


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
        self.btn_autoprobe.setText("فعال ✓" if enabled else "فعال‌سازی")

    def _sync_pick_hint(self, auto_enabled: bool) -> None:
        if auto_enabled:
            self.pick_hint.setText(
                "پراب خودکار روشن است؛ انتخاب دستی نادیده گرفته می‌شود. "
                "برای انتخاب دستی، ابتدا پراب خودکار را خاموش کنید.")
        else:
            self.pick_hint.setText(
                "روی هر استراتژی کلیک کنید تا به‌صورت دستی انتخاب/قفل شود.")

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
        nm = QLabel(name)
        nm.setObjectName("H2")
        ds = QLabel(desc)
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
                card._check_label.setText("✓ انتخاب‌شده" if is_sel else "")
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffer = LogBuffer(capacity=2000)

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
        self.cmb_level = QComboBox()
        self.cmb_level.setObjectName("LogFilter")
        for lv in ("all",) + LEVELS:
            self.cmb_level.addItem(self._LEVEL_FA.get(lv, lv), lv)
        self.cmb_level.currentIndexChanged.connect(self._rerender)
        bar.addWidget(self.cmb_level)

        self.search = QLineEdit()
        self.search.setObjectName("LogSearch")
        self.search.setPlaceholderText("جستجو در لاگ…")
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
        self.btn_clear = QPushButton("پاک‌سازی")
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.clicked.connect(self.clear)
        clr.addWidget(self.btn_clear)
        b.addLayout(clr)

        root.addWidget(card, 1)

        # seed lines so the page never looks empty
        self.append("SNI Spoofer UI بارگذاری شد")
        self.append("منتظر شروع تونل…")

    # -- helpers ----------------------------------------------------------
    def _field_label(self, t: str) -> QLabel:
        lbl = QLabel(t)
        lbl.setObjectName("Muted")
        return lbl

    def _current_filter(self) -> str:
        data = self.cmb_level.currentData()
        return data if data else "all"

    def _row_html(self, entry) -> str:
        color = self._COLORS.get(entry.level, self._COLORS["info"])
        # escape minimal HTML so messages can't break the markup
        msg = (entry.message.replace("&", "&amp;")
                            .replace("<", "&lt;").replace(">", "&gt;"))
        return (f'<span style="color:#5b6b7b">[{entry.stamp}]</span> '
                f'<span style="color:{color};font-weight:600">'
                f'{entry.level.upper():<4}</span> '
                f'<span style="color:#d8e2ec">{msg}</span>')

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
                   query=self.search.text()):
            self.log.append(self._row_html(entry))
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _rerender(self, *args) -> None:
        """Rebuild the visible console from the buffer under current filters."""
        rows = self._buffer.filtered(level=self._current_filter(),
                                     query=self.search.text())
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
        self.engine = EngineBridge(EngineController(self.store.config))
        self.engine.set_profile(self.store.selected_profile)

        self._palette = get_palette(self._theme)
        self.setObjectName("RootBackdrop")
        self.setWindowTitle("SNI Spoofer")
        self.resize(940, 620)
        self.setMinimumSize(820, 540)

        # Frameless, but keep the window a *real* top-level window so the OS
        # still gives us minimise + taskbar entry + native system-move. We do
        # NOT use WA_TranslucentBackground: on Windows it broke showMinimized()
        # and startSystemMove() and made the UI look "scattered" (feedback 2/3).
        # Instead the RootBackdrop paints a solid 3-D gradient (feedback 6).
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowSystemMenuHint
        )

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
        self.page_profiles = ProfilesPage(self.store, engine=self.engine)
        self.page_settings = SettingsPage()
        self.page_strategy = StrategyPage(self.store)
        self.page_strategy.auto_prober_changed.connect(self._on_auto_prober_changed)
        self.page_strategy.strategy_selected.connect(self._on_strategy_selected)
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
        self.engine.traffic.connect(self.page_dashboard.on_traffic)
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
            self._resilience_timer.start()
            self._pump_resilience()
        elif status == "idle":
            Toast.show_message(self, "اتصال قطع شد", "warn")
            self._resilience_timer.stop()
        elif status == "error":
            Toast.show_message(self, "خطا در اتصال — لاگ را ببینید", "err")
            self._resilience_timer.stop()

    def _pump_resilience(self):
        """Push a concise live resilience summary into the dashboard strip."""
        try:
            snap = self.engine.diagnostics()
        except Exception:
            return
        if not getattr(snap, "resilience_on", False):
            self.page_dashboard.set_resilience("غیرفعال")
            return
        chain = " → ".join(snap.strategy_chain) or (snap.active_strategy or "—")
        throttle = " · throttle!" if snap.throttled else ""
        self.page_dashboard.set_resilience(
            f"RST {snap.forged_rst_count}/{snap.rst_budget} · "
            f"زنجیره {chain}{throttle}")

    def _on_strategy_changed(self, method: str):
        self.page_log.append(f"[strategy] استراتژی فعال: {method}")

    def _on_profile_selected(self, profile):
        self.engine.set_profile(profile)
        if profile:
            self.page_log.append(f"[profile] انتخاب شد: {profile.display_name}")
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
                    "[mode] حالت به «Tunnel» تغییر کرد تا کانفیگ انتخاب‌شده "
                    "واقعاً استفاده شود")
                Toast.show_message(
                    self, "حالت به «Tunnel» تغییر کرد (برای استفاده از کانفیگ)",
                    "ok")

    def _on_auto_prober_changed(self, enabled: bool):
        # the StrategyPage already persisted the flag; push it to the live engine
        self.store.save_config()
        self.engine.update_config(self.store.config)
        self.page_log.append(
            f"[auto-prober] {'فعال شد' if enabled else 'غیرفعال شد'}")
        Toast.show_message(
            self, "پراب خودکار فعال شد" if enabled else "پراب خودکار غیرفعال شد",
            "ok")

    def _on_strategy_selected(self, key: str):
        # StrategyPage already persisted bypass_method (and cleared auto_prober);
        # push to the live engine so the next connection uses it.
        self.store.save_config()
        self.engine.update_config(self.store.config)
        # find the human-readable name for the toast/log
        name = next((n for k, n, _ in STRATEGIES if k == key), key)
        self.page_log.append(f"[strategy] انتخاب دستی: {name} ({key})")
        Toast.show_message(self, f"استراتژی انتخاب شد: {name}", "ok")

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

    def closeEvent(self, event):
        """Stop the engine cleanly so no subprocess / thread is orphaned."""
        try:
            self.engine.stop()
        except Exception:
            pass
        super().closeEvent(event)
