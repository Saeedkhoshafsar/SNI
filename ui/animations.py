"""Reusable dynamic-motion helpers for the SNI Spoofer UI.

Everything here is built on ``QPropertyAnimation`` / ``QVariantAnimation`` so it
stays GPU-cheap and works regardless of the active theme. The helpers are
intentionally small and composable:

  * :func:`fade_in` / :func:`fade_out`   — opacity transitions on any widget
  * :func:`slide_in`                      — entrance slide (used for page cards)
  * :func:`stagger_in`                    — cascade a list of widgets in
  * :class:`PulseDot`                     — breathing status indicator
  * :class:`ColorTransition`              — animate one CSS colour → another
  * :class:`CountUp`                      — animate an integer label 0 → N

These are layered on top of the static widgets from ``widgets.py`` so the
product feels alive without coupling motion into the widgets themselves.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from PySide6.QtCore import (
    QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QTimer,
    QVariantAnimation, Qt, Signal,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget


# ---------------------------------------------------------------------------
#  Opacity transitions
# ---------------------------------------------------------------------------

def _opacity_effect(widget: QWidget) -> QGraphicsOpacityEffect | None:
    """Return (creating if needed) an opacity effect for *widget*.

    A QWidget can host only ONE QGraphicsEffect. If the widget already has a
    *different* effect (e.g. a drop shadow on a Card), we must NOT replace it —
    doing so both loses the shadow and triggers painter conflicts. In that case
    we return ``None`` and the caller skips the opacity part of the animation.
    """
    eff = widget.graphicsEffect()
    if isinstance(eff, QGraphicsOpacityEffect):
        return eff
    if eff is not None:
        return None  # widget already owns a non-opacity effect — leave it be
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    return eff


def fade_in(widget: QWidget, duration: int = 260, *,
            start: float = 0.0, on_done: Callable | None = None
            ) -> QPropertyAnimation | None:
    eff = _opacity_effect(widget)
    if eff is None:
        # widget has another effect (e.g. shadow); just ensure it's visible
        widget.show()
        if on_done:
            on_done()
        return None
    eff.setOpacity(start)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    if on_done:
        anim.finished.connect(on_done)
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    widget._fade_anim = anim  # keep a ref so GC doesn't kill it mid-flight
    return anim


def fade_out(widget: QWidget, duration: int = 220, *,
             on_done: Callable | None = None) -> QPropertyAnimation | None:
    eff = _opacity_effect(widget)
    if eff is None:
        if on_done:
            on_done()
        return None
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(eff.opacity())
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.InCubic)
    if on_done:
        anim.finished.connect(on_done)
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    widget._fade_anim = anim
    return anim


# ---------------------------------------------------------------------------
#  Slide / entrance
# ---------------------------------------------------------------------------

def slide_in(widget: QWidget, duration: int = 320, *, dy: int = 18,
             delay: int = 0) -> None:
    """Slide a widget up into place while fading in. Position-safe: captures
    the widget's laid-out pos lazily so it works even before first show."""

    def _run():
        end = widget.pos()
        start = QPoint(end.x(), end.y() + dy)
        widget.move(start)
        pos_anim = QPropertyAnimation(widget, b"pos", widget)
        pos_anim.setDuration(duration)
        pos_anim.setStartValue(start)
        pos_anim.setEndValue(end)
        pos_anim.setEasingCurve(QEasingCurve.OutCubic)
        pos_anim.start(QPropertyAnimation.DeleteWhenStopped)
        widget._slide_anim = pos_anim
        fade_in(widget, duration)

    if delay > 0:
        QTimer.singleShot(delay, _run)
    else:
        _run()


def stagger_in(widgets: Iterable[QWidget], *, step: int = 70,
               duration: int = 320, dy: int = 18) -> None:
    """Cascade-reveal a sequence of widgets (entrance for a page)."""
    for i, w in enumerate(widgets):
        slide_in(w, duration, dy=dy, delay=i * step)


# ---------------------------------------------------------------------------
#  Pulsing status dot (breathing indicator)
# ---------------------------------------------------------------------------

class PulseDot(QLabel):
    """A small circular indicator that breathes (opacity in/out) while 'active'.

    Colour is driven by :meth:`set_state` so it can reflect idle / connecting /
    active / error without restyling from the outside.
    """

    COLORS = {
        "idle": "#7d8b99",
        "connecting": "#ffcf5c",
        "active": "#41e08a",
        "error": "#ff6b81",
    }

    def __init__(self, parent: QWidget | None = None, *, diameter: int = 12):
        super().__init__(parent)
        self._d = diameter
        self.setFixedSize(diameter, diameter)
        self._state = "idle"
        self._pulse: QPropertyAnimation | None = None
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._eff.setOpacity(1.0)
        self._restyle()

    def _restyle(self):
        c = self.COLORS.get(self._state, "#7d8b99")
        r = self._d // 2
        self.setStyleSheet(
            f"background:{c}; border-radius:{r}px;")

    def set_state(self, state: str):
        self._state = state
        self._restyle()
        if state in ("connecting", "active"):
            self._start_pulse(fast=(state == "connecting"))
        else:
            self._stop_pulse()

    def _start_pulse(self, *, fast: bool):
        self._stop_pulse()
        anim = QPropertyAnimation(self._eff, b"opacity", self)
        anim.setDuration(700 if fast else 1300)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.5, 0.35)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.InOutSine)
        anim.setLoopCount(-1)
        anim.start()
        self._pulse = anim

    def _stop_pulse(self):
        if self._pulse:
            self._pulse.stop()
            self._pulse = None
        self._eff.setOpacity(1.0)


# ---------------------------------------------------------------------------
#  Colour transition (animate a widget's background/text colour via QSS)
# ---------------------------------------------------------------------------

class ColorTransition(QObject):
    """Animate a CSS colour from one value to another, emitting the QSS each
    frame via a callback. Used for the Start/Stop button's smooth recolour."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._on_frame: Callable[[QColor], None] | None = None
        # connect once; dispatch through a stable slot to avoid repeated
        # connect/disconnect churn (and its noisy warnings)
        self._anim.valueChanged.connect(self._dispatch)

    def _dispatch(self, v):
        if self._on_frame is not None:
            self._on_frame(v if isinstance(v, QColor) else QColor(v))

    def run(self, c_from: str, c_to: str,
            on_frame: Callable[[QColor], None]) -> None:
        self._anim.stop()
        self._on_frame = on_frame
        self._anim.setStartValue(QColor(c_from))
        self._anim.setEndValue(QColor(c_to))
        self._anim.start()


# ---------------------------------------------------------------------------
#  Count-up integer animation (e.g. active-connection counter)
# ---------------------------------------------------------------------------

class CountUp(QObject):
    """Animate a QLabel's integer text from its current value to a target."""

    def __init__(self, label: QLabel, *, duration: int = 600):
        super().__init__(label)
        self._label = label
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(
            lambda v: self._label.setText(str(int(v))))

    def to(self, target: int):
        try:
            current = int(self._label.text())
        except (ValueError, TypeError):
            current = 0
        if current == target:
            self._label.setText(str(target))
            return
        self._anim.stop()
        self._anim.setStartValue(current)
        self._anim.setEndValue(target)
        self._anim.start()


# ---------------------------------------------------------------------------
#  Dynamic mathematical wave backdrop (#10)
# ---------------------------------------------------------------------------

class WaveBackdrop(QWidget):
    """A living backdrop that paints superposed sine waves drifting over time.

    The motion is pure maths — each layer is ``A·sin(k·x + φ + ω·t)`` with its
    own amplitude/frequency/phase/speed, so the crests interfere and slowly
    breathe rather than scroll mechanically. It paints below all content
    (``WA_TransparentForMouseEvents`` so clicks pass straight through) and is
    palette-aware: :meth:`set_palette` recolours the layers for dark/light.

    Cheap by design: ~20 FPS timer, antialiased filled paths, no per-pixel
    work. Honours :meth:`set_enabled` so it can be parked when the window is
    hidden/minimised to spend zero CPU.
    """

    # phase offsets + relative speeds give each layer its own personality.
    _LAYERS = (
        # (amp_frac, wavelength_frac, phase, speed, y_frac, alpha)
        (0.060, 0.85, 0.0,            0.55, 0.78, 46),
        (0.045, 0.55, math.pi * 0.6,  0.80, 0.86, 34),
        (0.075, 1.25, math.pi * 1.3, -0.40, 0.70, 26),
    )

    def __init__(self, parent: QWidget | None = None, *, fps: int = 20):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._t = 0.0
        self._c_from = QColor("#27e0c8")
        self._c_to = QColor("#9b7bff")
        self._enabled = True
        self._timer = QTimer(self)
        self._timer.setInterval(max(16, int(1000 / max(1, fps))))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- public API ---------------------------------------------------------
    def set_palette(self, c_from: str, c_to: str) -> None:
        """Recolour the two ends of the wave gradient (accent → accent2)."""
        self._c_from = QColor(c_from)
        self._c_to = QColor(c_to)
        self.update()

    def set_enabled(self, on: bool) -> None:
        """Park (stop ticking) or resume the animation to save CPU."""
        self._enabled = on
        if on and not self._timer.isActive():
            self._timer.start()
        elif not on and self._timer.isActive():
            self._timer.stop()

    # -- internals ----------------------------------------------------------
    def _tick(self) -> None:
        if not self._enabled or not self.isVisible():
            return
        self._t += 0.045
        self.update()

    def _mix(self, f: float) -> QColor:
        """Linear blend c_from → c_to at fraction *f* (0..1)."""
        f = max(0.0, min(1.0, f))
        a, b = self._c_from, self._c_to
        return QColor(
            int(a.red()   + (b.red()   - a.red())   * f),
            int(a.green() + (b.green() - a.green()) * f),
            int(a.blue()  + (b.blue()  - a.blue())  * f),
        )

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt signature)
        w = self.width()
        h = self.height()
        if w <= 2 or h <= 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        step = max(6, w // 90)                  # sample density (perf vs smooth)
        for i, (amp_f, wl_f, phase, speed, y_f, alpha) in enumerate(self._LAYERS):
            amp = amp_f * h
            wavelength = max(40.0, wl_f * w)
            k = 2.0 * math.pi / wavelength
            omega = speed
            base_y = y_f * h

            path = QPainterPath()
            path.moveTo(0.0, h)
            x = 0.0
            first = True
            while x <= w:
                # superpose a slow second harmonic for organic, non-repeating crests
                y = (base_y
                     + amp * math.sin(k * x + phase + omega * self._t)
                     + 0.35 * amp * math.sin(0.5 * k * x - 0.7 * omega * self._t))
                if first:
                    path.lineTo(QPointF(x, y))
                    first = False
                else:
                    path.lineTo(QPointF(x, y))
                x += step
            path.lineTo(QPointF(float(w), h))
            path.closeSubpath()

            col = self._mix(i / max(1, len(self._LAYERS) - 1))
            grad = QLinearGradient(0.0, base_y - amp, 0.0, float(h))
            top = QColor(col); top.setAlpha(alpha)
            bot = QColor(col); bot.setAlpha(0)
            grad.setColorAt(0.0, top)
            grad.setColorAt(1.0, bot)
            painter.fillPath(path, grad)
        painter.end()
