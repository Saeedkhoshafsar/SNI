"""Reusable building blocks for the SNI Spoofer UI.

Kept deliberately light: a soft-shadow card, a custom title bar (drag + window
controls), and a side-nav button. Dynamic/animated behaviour (fade, slide,
ripple) lives in ``animations.py`` and is layered on top of these in step 2.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
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
