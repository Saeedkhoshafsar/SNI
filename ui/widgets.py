"""Reusable building blocks for the SNI Spoofer UI.

Kept deliberately light: a soft-shadow card, a custom title bar (drag + window
controls), and a side-nav button. Dynamic/animated behaviour (fade, slide,
ripple) lives in ``animations.py`` and is layered on top of these in step 2.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QPointF, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ui.i18n import tr


# ---------------------------------------------------------------------------
#  Scroll-safe numeric / combo inputs
# ---------------------------------------------------------------------------
#
# Plain QSpinBox / QDoubleSpinBox / QComboBox change their value whenever the
# mouse wheel rolls over them — even when they don't have focus. Inside a
# scroll area that's infuriating: scrolling the page silently turns 40443 into
# 40444 (feedback #6). These subclasses simply *ignore* the wheel and let the
# event bubble up to the scroll area, so the page scrolls and the value is
# only ever changed via the arrows, typing, or arrow keys (when focused).

class _NoWheelMixin:
    def wheelEvent(self, event):  # noqa: D401 - Qt override
        # Don't consume the wheel — pass it to the parent (scroll area) so the
        # page scrolls instead of the value changing under the cursor.
        event.ignore()

    def focusInEvent(self, event):  # keep keyboard editing fully functional
        super().focusInEvent(event)
        # never grab the wheel even when focused; matches the user's request
        # that scrolling should never bump these fields.
        self.setFocusPolicy(Qt.StrongFocus)


class NoScrollSpinBox(_NoWheelMixin, QSpinBox):
    """A QSpinBox that never changes value on mouse-wheel scroll."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setFocusPolicy(Qt.StrongFocus)


class NoScrollDoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    """A QDoubleSpinBox that never changes value on mouse-wheel scroll."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setFocusPolicy(Qt.StrongFocus)


class NoScrollComboBox(_NoWheelMixin, QComboBox):
    """A QComboBox that never changes selection on mouse-wheel scroll."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setFocusPolicy(Qt.StrongFocus)


# ---------------------------------------------------------------------------
#  Card with soft drop shadow
# ---------------------------------------------------------------------------

class Card(QFrame):
    """A rounded panel with a soft, *theme-aware* drop shadow.

    The shadow is intentionally subtle (#4): a heavy near-black shadow looked
    fine on dark surfaces but cheap/dirty on the light theme. Default values
    are tuned for the dark theme; :meth:`tune_shadow_for` (or the host's
    theme walk) softens and re-tints them for light.
    """

    # baseline shadow per theme: (rgba, blur, y-offset)
    _SHADOW_DARK = ("rgba(0,0,0,0.40)", 26, 6)
    _SHADOW_LIGHT = ("rgba(20,40,60,0.12)", 22, 4)

    def __init__(self, parent: QWidget | None = None, *, object_name: str = "Card",
                 shadow_color: str | None = None, blur: int | None = None,
                 y_offset: int | None = None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setProperty("class", "Card")
        c, b, y = self._SHADOW_DARK
        self._apply_shadow(shadow_color or c, blur if blur is not None else b,
                           y_offset if y_offset is not None else y)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        self._layout = lay

    def body(self) -> QVBoxLayout:
        return self._layout

    def _apply_shadow(self, color: str, blur: int, y: int):
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(blur)
        eff.setOffset(0, y)
        eff.setColor(_qcolor_from_rgba(color))
        self.setGraphicsEffect(eff)
        self._shadow = eff

    def tune_shadow_for(self, is_dark: bool) -> None:
        """Re-tint/soften the shadow for the active theme (#4)."""
        color, blur, y = self._SHADOW_DARK if is_dark else self._SHADOW_LIGHT
        self.set_shadow(blur=blur, y=y, color=color)

    def set_shadow(self, *, blur: int | None = None, y: int | None = None,
                   color: str | None = None) -> None:
        """Adjust the drop shadow at runtime (used for hover-lift, step 24)."""
        eff = getattr(self, "_shadow", None)
        if eff is None:
            return
        if blur is not None:
            eff.setBlurRadius(blur)
        if y is not None:
            eff.setOffset(0, y)
        if color is not None:
            eff.setColor(_qcolor_from_rgba(color))


def _qcolor_from_rgba(s: str) -> QColor:
    """Parse 'rgba(r,g,b,a)' (a in 0..1) or '#rrggbb' into QColor."""
    s = s.strip()
    if s.startswith("rgba"):
        nums = s[s.index("(") + 1:s.index(")")].split(",")
        r, g, b = (int(float(x)) for x in nums[:3])
        a = int(float(nums[3]) * 255) if len(nums) > 3 else 255
        return QColor(r, g, b, a)
    c = QColor(s)
    return c


# ---------------------------------------------------------------------------
#  Custom title bar (frameless window)
# ---------------------------------------------------------------------------

class TitleBar(QFrame):
    """Draggable title bar with min / theme-toggle / close controls."""

    minimize_clicked = Signal()
    close_clicked = Signal()
    theme_toggled = Signal()
    language_toggled = Signal()

    def __init__(self, parent: QWidget, title: str = "SNI Spoofer"):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(44)
        self._win = parent
        self._drag_pos: QPoint | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(6)

        self.icon = QLabel("\u25c9")
        self.icon.setStyleSheet("font-size:16px;")
        self.title = QLabel(title)
        self.title.setObjectName("H2")

        lay.addWidget(self.icon)
        lay.addWidget(self.title)
        lay.addStretch(1)

        # language toggle (FA/EN) — #6. Shows the language you'll switch TO.
        from ui.i18n import language
        self.btn_lang = self._win_btn(
            "EN" if language() == "fa" else "FA", "WinBtn")
        self.btn_lang.setFixedSize(38, 30)
        self.btn_lang.setToolTip("Language / زبان")
        self.btn_theme = self._win_btn("\u25d1", "WinBtn")   # half-moon
        self.btn_min = self._win_btn("\u2013", "WinBtn")     # en-dash
        self.btn_close = self._win_btn("\u2715", "WinClose") # multiply

        self.btn_lang.clicked.connect(self.language_toggled.emit)
        self.btn_theme.clicked.connect(self.theme_toggled.emit)
        self.btn_min.clicked.connect(self.minimize_clicked.emit)
        self.btn_close.clicked.connect(self.close_clicked.emit)

        for b in (self.btn_lang, self.btn_theme, self.btn_min, self.btn_close):
            lay.addWidget(b)

    def update_lang_label(self) -> None:
        """Refresh the FA/EN button to show the *other* language."""
        from ui.i18n import language
        self.btn_lang.setText("EN" if language() == "fa" else "FA")

    def _win_btn(self, glyph: str, obj: str) -> QPushButton:
        b = QPushButton(glyph)
        b.setObjectName(obj)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(34, 30)
        return b

    # --- window dragging --------------------------------------------------
    #
    # Prefer the OS-native window move (``QWindow.startSystemMove``): on
    # frameless windows it produces buttery-smooth dragging that respects the
    # compositor / DPI / snap-assist, instead of the jittery manual ``move()``
    # loop that lagged behind the cursor (the bug in feedback 6). We fall back
    # to the manual approach only when the native move is unavailable.
    def _begin_native_move(self) -> bool:
        win = self._win.windowHandle() if self._win else None
        if win is not None and hasattr(win, "startSystemMove"):
            try:
                return bool(win.startSystemMove())
            except Exception:
                return False
        return False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._begin_native_move():
                self._drag_pos = None       # OS now owns the drag
            else:
                self._drag_pos = (e.globalPosition().toPoint()
                                  - self._win.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e):
        # manual fallback only (native move handles its own motion)
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


# ---------------------------------------------------------------------------
#  Side-navigation item
# ---------------------------------------------------------------------------

class NavItem(QPushButton):
    def __init__(self, text: str, icon: str = "", parent: QWidget | None = None):
        label = f"{icon}  {text}" if icon else text
        super().__init__(label, parent)
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)


# ---------------------------------------------------------------------------
#  Rich profile-list row (icon + name + server detail + badges + active mark)
# ---------------------------------------------------------------------------

# A glyph per protocol so each row is instantly recognisable.
_PROTO_ICON = {
    "vless": "\u2728",        # sparkles
    "vmess": "\u25c8",        # diamond
    "trojan": "\u2694",       # crossed swords
    "shadowsocks": "\U0001f512",  # lock
}


class ProfileRow(QFrame):
    """One server entry rendered as a card-like row.

    Shows: protocol glyph, display name, a muted server-detail line
    (``proto · host:port``), transport/security badges, and — when this is the
    currently selected profile — a green **● فعال** pill so the user always
    knows which server is in force (feedback #3). An ``edit`` signal lets the
    page open the editor straight from the row.
    """

    edit = Signal()
    ping = Signal()       # inline "ping this server" button clicked
    activate = Signal()   # inline "use this server" button clicked
    share = Signal()      # inline "copy this config as a share link" clicked
    scan = Signal()       # inline "scan clean Cloudflare IPs for this config"

    def __init__(self, profile, *, active: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ProfileRow")
        self.setProperty("active", "1" if active else "0")
        # a guaranteed height so the name + active pill + badges always fit on
        # two lines without clipping (the "active label clips the fields" bug)
        self.setMinimumHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)

        # protocol glyph (vertically centred so it lines up with the two-row text)
        glyph = QLabel(_PROTO_ICON.get(profile.protocol, "\u25c9"))
        glyph.setObjectName("RowGlyph")
        glyph.setFixedWidth(24)
        glyph.setAlignment(Qt.AlignCenter)
        lay.addWidget(glyph, 0, Qt.AlignVCenter)

        # name + detail column (gets the stretch so it owns the free width)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        name = QLabel(profile.display_name)
        name.setObjectName("RowName")
        # let the name shrink gracefully so the pill/badges never push it out
        # of the row and clip it (RTL elide is unreliable)
        name.setMinimumWidth(0)
        top.addWidget(name, 1)
        if active:
            pill = QLabel(tr("\u25cf فعال"))
            pill.setObjectName("ActivePill")
            top.addWidget(pill, 0, Qt.AlignVCenter)
        col.addLayout(top)

        # show a meaningful endpoint. For SNI-spoof configs (127.0.0.1:40443)
        # the literal address is just our local spoofer, so display the real
        # CDN host from the SNI/Host header instead — that's what the user
        # recognises. Otherwise show the real server address:port.
        if getattr(profile, "is_spoof_config", False):
            _addr = (profile.sni or profile.host or profile.address)
            _port = 443 if getattr(profile, "is_tls", False) else profile.port
        else:
            _addr = profile.address
            _port = profile.port
        # the address line ELIDES on overflow so a long ``*.workers.dev`` host
        # never pushes the inline ping result out of the visible box (#1). The
        # ping result gets its OWN label beside it so it is always visible
        # regardless of host length.
        detail_row = QHBoxLayout()
        detail_row.setContentsMargins(0, 0, 0, 0)
        detail_row.setSpacing(8)
        detail = QLabel()
        detail.setObjectName("RowDetail")
        detail.setMinimumWidth(0)
        self._detail = detail
        self._detail_base = f"{profile.protocol} · {_addr}:{_port}"
        self._detail_full = self._detail_base
        detail.setText(self._detail_base)
        detail.setToolTip(self._detail_base)
        detail_row.addWidget(detail, 1)
        # dedicated, never-clipped inline ping-result slot (#1/#3)
        self._ping_label = QLabel("")
        self._ping_label.setObjectName("RowPingResult")
        self._ping_label.setMinimumWidth(0)
        self._ping_label.setVisible(False)
        detail_row.addWidget(self._ping_label, 0, Qt.AlignVCenter)
        col.addLayout(detail_row)
        lay.addLayout(col, 1)

        # transport / security badges (vertically centred)
        for txt in self._badges(profile):
            b = QLabel(txt)
            b.setObjectName("RowBadge")
            lay.addWidget(b, 0, Qt.AlignVCenter)

        # inline ping button — measure THIS server's latency right here and
        # show the result inline, instead of a buried separate panel (#3).
        self.btn_ping = QPushButton("\U0001f4e1")
        self.btn_ping.setObjectName("RowPing")
        self.btn_ping.setCursor(Qt.PointingHandCursor)
        self.btn_ping.setFixedSize(28, 28)
        self.btn_ping.setToolTip(tr("پینگ این سرور"))
        self.btn_ping.clicked.connect(self.ping.emit)
        lay.addWidget(self.btn_ping, 0, Qt.AlignVCenter)

        # inline "use this server" button — one click activates the profile
        # without opening any dialog (#8). Hidden when already active.
        self.btn_use = QPushButton("\u2714")
        self.btn_use.setObjectName("RowUse")
        self.btn_use.setCursor(Qt.PointingHandCursor)
        self.btn_use.setFixedSize(28, 28)
        self.btn_use.setToolTip(tr("فعال‌سازی این سرور"))
        self.btn_use.clicked.connect(self.activate.emit)
        self.btn_use.setVisible(not active)
        lay.addWidget(self.btn_use, 0, Qt.AlignVCenter)

        # inline "scan clean Cloudflare IPs" button — runs the scanner using
        # THIS config as the reference test (issue #3).
        self.btn_scan = QPushButton("\U0001f50d")
        self.btn_scan.setObjectName("RowScan")
        self.btn_scan.setCursor(Qt.PointingHandCursor)
        self.btn_scan.setFixedSize(28, 28)
        self.btn_scan.setToolTip(tr("اسکن IP تمیز کلودفلر با این کانفیگ"))
        self.btn_scan.clicked.connect(self.scan.emit)
        lay.addWidget(self.btn_scan, 0, Qt.AlignVCenter)

        # inline "share / copy link" button — re-serialise this profile back to
        # a share link and copy it to the clipboard (issue #2).
        self.btn_share = QPushButton("\U0001f517")
        self.btn_share.setObjectName("RowShare")
        self.btn_share.setCursor(Qt.PointingHandCursor)
        self.btn_share.setFixedSize(28, 28)
        self.btn_share.setToolTip(tr("کپی لینک اشتراک‌گذاری این کانفیگ"))
        self.btn_share.clicked.connect(self.share.emit)
        lay.addWidget(self.btn_share, 0, Qt.AlignVCenter)

        # inline edit button
        self.btn_edit = QPushButton("\u270e")
        self.btn_edit.setObjectName("RowEdit")
        self.btn_edit.setCursor(Qt.PointingHandCursor)
        self.btn_edit.setFixedSize(28, 28)
        self.btn_edit.setToolTip(tr("ویرایش این پروفایل"))
        self.btn_edit.clicked.connect(self.edit.emit)
        lay.addWidget(self.btn_edit, 0, Qt.AlignVCenter)

    # -- inline ping result -------------------------------------------------
    def set_ping_state(self, text: str, kind: str = "info") -> None:
        """Show the inline ping status/result in its OWN label (#1).

        Previously the result was appended to the address line, so a long host
        pushed the latency value off-screen. It now lives in a dedicated
        ``_ping_label`` that is never clipped. ``kind`` ∈
        {"info","busy","ok","err"} tints the text via the ``pingkind`` property.
        """
        lbl = getattr(self, "_ping_label", None)
        if lbl is None:                       # pragma: no cover - defensive
            return
        lbl.setProperty("pingkind", kind)
        lbl.setText(text or "")
        lbl.setVisible(bool(text))
        lbl.setToolTip(text or "")
        # re-polish so the dynamic-property style refreshes
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)

    def resizeEvent(self, event):
        """Elide the address line so long hosts never overflow the box (#1)."""
        super().resizeEvent(event)
        self._elide_detail()

    def _elide_detail(self) -> None:
        d = getattr(self, "_detail", None)
        full = getattr(self, "_detail_full", None)
        if d is None or full is None:
            return
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(d.font())
        avail = max(40, d.width() - 2)
        elided = fm.elidedText(full, Qt.ElideMiddle, avail)
        if elided != d.text():
            d.setText(elided)

    def set_pinging(self) -> None:
        self.btn_ping.setEnabled(False)
        self.set_ping_state(tr("در حال پینگ…"), "busy")

    def set_ping_idle(self) -> None:
        self.btn_ping.setEnabled(True)

    @staticmethod
    def _badges(profile) -> list[str]:
        out = []
        tr = (profile.transport or "tcp").lower()
        if tr and tr != "tcp":
            out.append(tr.upper())
        sec = (profile.security or "none").lower()
        if sec in ("tls", "reality", "xtls"):
            out.append(sec.upper())
        return out


# ---------------------------------------------------------------------------
#  Persistent active-config status bar (visible on every tab)
# ---------------------------------------------------------------------------

class ActiveConfigBar(QFrame):
    """A slim always-visible strip showing the active server + live status.

    Sits directly under the title bar so, no matter which tab the user is on,
    they can always see *which config is active* and whether the engine is
    connected (feedback #9). Three zones: a status dot + state text on one
    side, the active profile name/endpoint in the centre, and a compact live
    up/down readout on the other side.
    """

    _STATE_FA = {
        "idle": "متوقف",
        "connecting": "در حال اتصال…",
        "active": "متصل",
        "error": "خطا",
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ActiveBar")
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(10)

        self._dot = QLabel("\u25cf")
        self._dot.setObjectName("ActiveBarDot")
        self._dot.setProperty("state", "idle")
        self._state = QLabel(tr(self._STATE_FA["idle"]))
        self._state.setObjectName("ActiveBarState")
        lay.addWidget(self._dot)
        lay.addWidget(self._state)

        sep = QLabel("\u2502")
        sep.setObjectName("ActiveBarSep")
        lay.addWidget(sep)

        self._name = QLabel(tr("سروری انتخاب نشده"))
        self._name.setObjectName("ActiveBarName")
        lay.addWidget(self._name)

        lay.addStretch(1)

        self._rate = QLabel("")
        self._rate.setObjectName("ActiveBarRate")
        lay.addWidget(self._rate)

    # -- public API --------------------------------------------------------
    def set_profile(self, profile) -> None:
        """Show the active profile's name + endpoint (or an empty hint)."""
        if profile is None:
            self._name.setText(tr("سروری انتخاب نشده — حالت SNI Only"))
            return
        name = getattr(profile, "display_name", "") or tr("سرور")
        if getattr(profile, "is_spoof_config", False):
            addr = (getattr(profile, "sni", "") or getattr(profile, "host", "")
                    or getattr(profile, "address", ""))
        else:
            addr = getattr(profile, "address", "")
        proto = getattr(profile, "protocol", "")
        detail = f"{proto} · {addr}" if addr else proto
        self._name.setText(f"{name}   —   {detail}" if detail else name)

    def set_status(self, state: str) -> None:
        self._dot.setProperty("state", state)
        self._state.setText(tr(self._STATE_FA.get(state, state)))
        # re-polish so the dot colour (driven by the dynamic property) updates
        self._dot.style().unpolish(self._dot)
        self._dot.style().polish(self._dot)
        if state != "active":
            self._rate.setText("")

    def set_rate(self, down_bps: float, up_bps: float) -> None:
        def _h(n: float) -> str:
            n = max(0.0, float(n))
            for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
                if n < 1024 or unit == "GB/s":
                    return f"{n:.0f} {unit}" if unit == "B/s" else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} GB/s"
        self._rate.setText(f"\u2193 {_h(down_bps)}    \u2191 {_h(up_bps)}")


# ---------------------------------------------------------------------------
#  Power button — large Start/Stop control with smooth colour transitions
# ---------------------------------------------------------------------------

class PowerButton(QPushButton):
    """The hero Start/Stop control.

    Drives four visual states (``idle`` / ``connecting`` / ``active`` /
    ``error``) with an animated background-colour transition between them.
    Emits :pyattr:`toggled_state` with the requested *next* action when the
    user clicks (``"start"`` from idle, ``"stop"`` from active/connecting).
    """

    request = Signal(str)  # "start" | "stop"

    _LABELS = {
        "idle": "شروع",
        "connecting": "در حال اتصال…",
        "active": "قطع اتصال",
        "error": "تلاش دوباره",
    }

    def __init__(self, palette, parent: QWidget | None = None):
        super().__init__(tr(self._LABELS["idle"]), parent)
        self.setObjectName("Power")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(150, 44)
        self._palette = palette
        self._state = "idle"
        # lazy import to avoid a hard cycle at module import time
        from ui.animations import ColorTransition
        self._ct = ColorTransition(self)
        self._color = self._target_color("idle")
        self._apply_color(self._color)
        self.clicked.connect(self._on_click)

    # -- public API --------------------------------------------------------
    def set_palette(self, palette):
        self._palette = palette
        self._color = self._target_color(self._state)
        self._apply_color(self._color)

    def set_state(self, state: str):
        if state == self._state:
            return
        prev = self._color
        self._state = state
        self.setText(tr(self._LABELS.get(state, "شروع")))
        self._ct.run(prev, self._target_color(state), self._on_frame)

    def state(self) -> str:
        return self._state

    # -- internals ---------------------------------------------------------
    def _target_color(self, state: str) -> str:
        p = self._palette
        return {
            "idle": p.accent,
            "connecting": p.warning,
            "active": p.danger,
            "error": p.danger,
        }.get(state, p.accent)

    def _on_frame(self, qcolor: QColor):
        self._color = qcolor.name()
        self._apply_color(self._color)

    def _apply_color(self, color: str):
        on = self._palette.on_accent
        # derive a slightly brighter hover/pressed from the base colour
        c = QColor(color)
        hover = c.lighter(115).name()
        press = c.darker(112).name()
        self.setStyleSheet(
            f"""
            QPushButton#Power {{
                background: {color}; color: {on}; border: none;
                border-radius: 11px; font-weight: 700; font-size: 15px;
                padding: 10px 22px;
            }}
            QPushButton#Power:hover  {{ background: {hover}; }}
            QPushButton#Power:pressed {{ background: {press}; }}
            """)

    def _on_click(self):
        if self._state in ("idle", "error"):
            self.request.emit("start")
        else:
            self.request.emit("stop")


# ---------------------------------------------------------------------------
#  Toast — transient, auto-dismissing notification overlay
# ---------------------------------------------------------------------------

class Toast(QFrame):
    """A small floating message that fades in, waits, then fades out.

    Anchored to the bottom-centre of its parent. Use :func:`Toast.show_message`
    to fire-and-forget; the toast cleans itself up.
    """

    _KIND_ICON = {"info": "\u25cf", "ok": "\u2714", "warn": "\u26a0", "err": "\u2715"}

    def __init__(self, parent: QWidget, text: str, kind: str = "info"):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setProperty("kind", kind)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 16, 9)
        lay.setSpacing(8)
        icon = QLabel(self._KIND_ICON.get(kind, "\u25cf"))
        icon.setObjectName("ToastIcon")
        msg = QLabel(text)
        msg.setObjectName("ToastText")
        lay.addWidget(icon)
        lay.addWidget(msg)
        self.adjustSize()
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._eff.setOpacity(0.0)

    def _reposition(self):
        if not self.parent():
            return
        pw = self.parent().width()
        ph = self.parent().height()
        self.adjustSize()
        x = (pw - self.width()) // 2
        y = ph - self.height() - 28
        self.move(x, y)

    @classmethod
    def show_message(cls, parent: QWidget, text: str, kind: str = "info",
                     duration: int = 2200) -> "Toast":
        from ui.animations import fade_in, fade_out
        t = cls(parent, text, kind)
        t._reposition()
        t.show()
        t.raise_()
        fade_in(t, 200)
        QTimer.singleShot(
            duration,
            lambda: fade_out(t, 260, on_done=t.deleteLater))
        return t


# ---------------------------------------------------------------------------
#  Live throughput sparkline (download + upload)
# ---------------------------------------------------------------------------

class Sparkline(QWidget):
    """A lightweight dual-series area sparkline for live up/down throughput.

    Holds a rolling window of samples for two series (download + upload) and
    redraws on every :meth:`push`. Pure-``QPainter`` (no external charting dep)
    so it stays cheap and dependency-free. The y-scale auto-fits to the largest
    value currently on screen so spikes stay readable.
    """

    def __init__(self, capacity: int = 60, parent=None):
        super().__init__(parent)
        self.setObjectName("Sparkline")
        self._cap = max(8, int(capacity))
        self._down: list[float] = []
        self._up: list[float] = []
        # colours are refreshed from the palette via :meth:`set_colors`
        self._c_down = QColor("#4f8cff")
        self._c_up = QColor("#36d399")
        self._c_grid = QColor(255, 255, 255, 22)
        self.setMinimumHeight(96)

    def set_colors(self, down: str, up: str, grid: str | None = None) -> None:
        self._c_down = QColor(down)
        self._c_up = QColor(up)
        if grid:
            self._c_grid = QColor(grid)
        self.update()

    def push(self, down_bps: float, up_bps: float) -> None:
        """Append one sample (bytes/sec) for each series and repaint."""
        self._down.append(max(0.0, float(down_bps)))
        self._up.append(max(0.0, float(up_bps)))
        if len(self._down) > self._cap:
            self._down = self._down[-self._cap:]
            self._up = self._up[-self._cap:]
        self.update()

    def clear(self) -> None:
        self._down.clear()
        self._up.clear()
        self.update()

    # -- painting ---------------------------------------------------------
    def _peak(self) -> float:
        peak = 0.0
        for s in (self._down, self._up):
            if s:
                peak = max(peak, max(s))
        return peak

    def paintEvent(self, _ev):  # pragma: no cover - visual; logic tested apart
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        pad = 6
        # baseline grid
        p.setPen(QPen(self._c_grid, 1))
        for i in range(1, 4):
            y = pad + (h - 2 * pad) * i / 4
            p.drawLine(pad, int(y), w - pad, int(y))

        peak = self._peak()
        if peak <= 0 or len(self._down) < 2:
            return
        # draw download under upload so upload stays visible on top
        self._draw_series(p, self._down, peak, self._c_down, pad, w, h)
        self._draw_series(p, self._up, peak, self._c_up, pad, w, h)

    def _draw_series(self, p, data, peak, color, pad, w, h):
        n = len(data)
        plot_w = w - 2 * pad
        plot_h = h - 2 * pad
        step = plot_w / (self._cap - 1)
        # right-align the newest sample
        x0 = w - pad - (n - 1) * step

        def pt(i: int) -> QPointF:
            x = x0 + i * step
            y = (h - pad) - (data[i] / peak) * plot_h
            return QPointF(x, y)

        line = QPainterPath()
        line.moveTo(pt(0))
        for i in range(1, n):
            line.lineTo(pt(i))

        area = QPainterPath(line)
        area.lineTo(QPointF(x0 + (n - 1) * step, h - pad))
        area.lineTo(QPointF(x0, h - pad))
        area.closeSubpath()

        grad = QLinearGradient(0, pad, 0, h - pad)
        fill = QColor(color)
        fill.setAlpha(70)
        grad.setColorAt(0.0, fill)
        tail = QColor(color)
        tail.setAlpha(10)
        grad.setColorAt(1.0, tail)
        p.fillPath(area, grad)

        p.setPen(QPen(color, 2))
        p.drawPath(line)
