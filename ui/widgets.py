"""Reusable building blocks for the SNI Spoofer UI.

Kept deliberately light: a soft-shadow card, a custom title bar (drag + window
controls), and a side-nav button. Dynamic/animated behaviour (fade, slide,
ripple) lives in ``animations.py`` and is layered on top of these in step 2.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)


# ---------------------------------------------------------------------------
#  Card with soft drop shadow
# ---------------------------------------------------------------------------

class Card(QFrame):
    """A rounded translucent panel with a soft drop shadow."""

    def __init__(self, parent: QWidget | None = None, *, object_name: str = "Card",
                 shadow_color: str = "rgba(0,0,0,0.55)", blur: int = 34,
                 y_offset: int = 10):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setProperty("class", "Card")
        self._apply_shadow(shadow_color, blur, y_offset)
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

        self.btn_theme = self._win_btn("\u25d1", "WinBtn")   # half-moon
        self.btn_min = self._win_btn("\u2013", "WinBtn")     # en-dash
        self.btn_close = self._win_btn("\u2715", "WinClose") # multiply

        self.btn_theme.clicked.connect(self.theme_toggled.emit)
        self.btn_min.clicked.connect(self.minimize_clicked.emit)
        self.btn_close.clicked.connect(self.close_clicked.emit)

        for b in (self.btn_theme, self.btn_min, self.btn_close):
            lay.addWidget(b)

    def _win_btn(self, glyph: str, obj: str) -> QPushButton:
        b = QPushButton(glyph)
        b.setObjectName(obj)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(34, 30)
        return b

    # --- window dragging --------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
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
        super().__init__(self._LABELS["idle"], parent)
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
        self.setText(self._LABELS.get(state, "شروع"))
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
