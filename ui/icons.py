"""3-D vector icon system for the SNI Spoofer UI.

The previous UI used raw emoji / unicode glyphs as "icons" (📡, ✔, 🔍, ✎ …).
On many systems those render as flat, mismatched, sometimes-tofu boxes — the
user reported the icons as "bad / unrecognisable" and asked for proper 3-D
icons everywhere (especially in the saved-servers list).

This module renders crisp **vector** icons from inline SVG paths and gives
them a subtle embossed / 3-D look:

* a soft drop-shadow copy behind the glyph,
* a gradient-coloured main stroke,
* a thin lighter highlight along the top edge.

Icons are rendered at a high supersampled resolution and cached as
``QPixmap`` / ``QIcon`` keyed by (name, color, size, dpr, embossed) so the
hot path is cheap.

Public API
----------
    icon(name, color=None, size=22, embossed=True) -> QIcon
    pixmap(name, color=None, size=22, embossed=True) -> QPixmap
    set_default_color(hex)            # global default stroke colour
    apply_palette(palette)            # pull colours from the active theme
    clear_cache()
    PROTO_ICON_NAME                   # protocol -> icon-name map
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# --------------------------------------------------------------------------- #
#  Theme-driven colours (updated by apply_palette)
# --------------------------------------------------------------------------- #
_default_color = "#8a99a8"     # generic icon stroke (muted text)
_nav_active = "#27e0c8"        # nav item when selected (accent)
_nav_idle = "#8a99a8"          # nav item when idle (muted)

_cache: dict[tuple, QPixmap] = {}


# --------------------------------------------------------------------------- #
#  SVG glyph library — all drawn on a 24×24 viewBox, stroke-based so they
#  recolour cleanly. Paths use no fill (fill="none") and rely on the stroke
#  we inject in _build_svg.
# --------------------------------------------------------------------------- #
# Each entry is the *inner* markup (paths/shapes) of a 24×24 SVG. ``{stroke}``
# and ``{sw}`` placeholders are filled in by _build_svg.
_GLYPHS: dict[str, str] = {
    # --- navigation -------------------------------------------------------- #
    "dashboard": (
        '<rect x="3" y="3" width="7" height="9" rx="1.6"/>'
        '<rect x="14" y="3" width="7" height="5" rx="1.6"/>'
        '<rect x="14" y="11" width="7" height="10" rx="1.6"/>'
        '<rect x="3" y="15" width="7" height="6" rx="1.6"/>'
    ),
    "servers": (
        '<rect x="3" y="4" width="18" height="6" rx="1.8"/>'
        '<rect x="3" y="14" width="18" height="6" rx="1.8"/>'
        '<circle cx="7" cy="7" r="0.9"/>'
        '<circle cx="7" cy="17" r="0.9"/>'
    ),
    "settings": (
        '<circle cx="12" cy="12" r="3.2"/>'
        '<path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3'
        'M5.2 5.2l2.1 2.1M16.7 16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1"/>'
    ),
    "strategy": (
        '<path d="M4 18 L9 11 L13 14 L20 6"/>'
        '<path d="M20 6 h-4 M20 6 v4"/>'
    ),
    "logs": (
        '<rect x="4" y="3" width="16" height="18" rx="2"/>'
        '<path d="M8 8h8M8 12h8M8 16h5"/>'
    ),
    # --- actions ----------------------------------------------------------- #
    "ping": (
        '<path d="M2 12 a10 10 0 0 1 20 0"/>'
        '<path d="M6 12 a6 6 0 0 1 12 0"/>'
        '<circle cx="12" cy="12" r="1.8"/>'
    ),
    "check": '<path d="M4 12.5 L9.5 18 L20 6"/>',
    "search": (
        '<circle cx="10.5" cy="10.5" r="6.5"/>'
        '<path d="M15.5 15.5 L21 21"/>'
    ),
    "link": (
        '<path d="M9 15 L15 9"/>'
        '<path d="M11 7 L13.5 4.5 a3.5 3.5 0 0 1 5 5 L16 12"/>'
        '<path d="M13 17 L10.5 19.5 a3.5 3.5 0 0 1 -5 -5 L8 12"/>'
    ),
    "edit": (
        '<path d="M4 20 h4 L18.5 9.5 a2 2 0 0 0 -3 -3 L5 17 z"/>'
        '<path d="M14 7 l3 3"/>'
    ),
    "trash": (
        '<path d="M5 7 h14"/>'
        '<path d="M9 7 V5 a1 1 0 0 1 1 -1 h4 a1 1 0 0 1 1 1 V7"/>'
        '<path d="M7 7 l1 13 a1 1 0 0 0 1 1 h6 a1 1 0 0 0 1 -1 l1 -13"/>'
        '<path d="M10 11 v6 M14 11 v6"/>'
    ),
    "check_all": (
        '<path d="M2 12.5 L6 16.5 L13 8"/>'
        '<path d="M11 14 L13 16 L21 7"/>'
    ),
    "uncheck_all": (
        '<rect x="4" y="4" width="16" height="16" rx="3"/>'
        '<path d="M8.5 12 h7"/>'
    ),
    "broadcast": (
        '<circle cx="12" cy="12" r="2.2"/>'
        '<path d="M7.5 7.5 a6.4 6.4 0 0 0 0 9 M16.5 7.5 a6.4 6.4 0 0 1 0 9"/>'
        '<path d="M4.7 4.7 a10.4 10.4 0 0 0 0 14.6 M19.3 4.7 a10.4 10.4 0 0 1 0 14.6"/>'
    ),
    "plus": '<path d="M12 5 v14 M5 12 h14"/>',
    "globe": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M3 12 h18"/>'
        '<path d="M12 3 a14 14 0 0 1 0 18 a14 14 0 0 1 0 -18"/>'
    ),
    # --- protocol marks ---------------------------------------------------- #
    "proto_vless": (        # sparkle / star
        '<path d="M12 3 L13.6 9.4 L20 11 L13.6 12.6 L12 19 L10.4 12.6 L4 11 '
        'L10.4 9.4 Z"/>'
    ),
    "proto_vmess": (        # diamond
        '<path d="M12 3 L20 12 L12 21 L4 12 Z"/>'
        '<path d="M7.5 12 h9"/>'
    ),
    "proto_trojan": (       # shield
        '<path d="M12 3 L19 6 V11 C19 16 15.5 19.5 12 21 '
        'C8.5 19.5 5 16 5 11 V6 Z"/>'
        '<path d="M9.5 12 l1.8 1.8 L15 9.5"/>'
    ),
    "proto_ss": (           # lock (shadowsocks)
        '<rect x="5" y="10" width="14" height="10" rx="2"/>'
        '<path d="M8 10 V7 a4 4 0 0 1 8 0 v3"/>'
        '<circle cx="12" cy="15" r="1.4"/>'
    ),
    "proto_generic": (      # concentric ring
        '<circle cx="12" cy="12" r="8"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
}

# protocol name -> glyph name
PROTO_ICON_NAME: dict[str, str] = {
    "vless": "proto_vless",
    "vmess": "proto_vmess",
    "trojan": "proto_trojan",
    "shadowsocks": "proto_ss",
}


# --------------------------------------------------------------------------- #
#  Colour helpers
# --------------------------------------------------------------------------- #
def _lighten(hex_color: str, amount: float) -> str:
    c = QColor(hex_color)
    if not c.isValid():
        c = QColor(_default_color)
    h, s, l, a = c.getHslF()
    l = min(1.0, l + amount)
    c.setHslF(h, s, l, a)
    return c.name()


def _darken(hex_color: str, amount: float) -> str:
    return _lighten(hex_color, -amount)


# --------------------------------------------------------------------------- #
#  SVG assembly
# --------------------------------------------------------------------------- #
def _build_svg(name: str, color: str, embossed: bool) -> str:
    inner = _GLYPHS.get(name)
    if inner is None:
        inner = _GLYPHS["proto_generic"]

    grad_top = _lighten(color, 0.18)
    grad_bottom = _darken(color, 0.10)
    highlight = _lighten(color, 0.40)
    shadow = "rgba(0,0,0,0.45)"
    sw = 1.9  # main stroke width

    common_attrs = (
        'fill="none" stroke-linecap="round" stroke-linejoin="round"'
    )

    layers = []

    if embossed:
        # 1) drop-shadow copy, nudged down & right, slightly thicker + blurred
        layers.append(
            f'<g transform="translate(0,0.7)" stroke="{shadow}" '
            f'stroke-width="{sw + 0.6}" {common_attrs} '
            f'filter="url(#blur)" opacity="0.55">{inner}</g>'
        )

    # 2) main gradient-stroked glyph
    layers.append(
        f'<g stroke="url(#grad)" stroke-width="{sw}" {common_attrs}>{inner}</g>'
    )

    if embossed:
        # 3) thin top highlight, nudged up slightly for the bevel feel
        layers.append(
            f'<g transform="translate(0,-0.45)" stroke="{highlight}" '
            f'stroke-width="0.7" {common_attrs} opacity="0.65">{inner}</g>'
        )

    defs = (
        '<defs>'
        '<linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{grad_top}"/>'
        f'<stop offset="1" stop-color="{grad_bottom}"/>'
        '</linearGradient>'
        '<filter id="blur" x="-20%" y="-20%" width="140%" height="140%">'
        '<feGaussianBlur stdDeviation="0.55"/>'
        '</filter>'
        '</defs>'
    )

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'width="24" height="24">'
        f'{defs}{"".join(layers)}</svg>'
    )


# --------------------------------------------------------------------------- #
#  Public render API
# --------------------------------------------------------------------------- #
def pixmap(name: str, color: str | None = None, size: int = 22,
           embossed: bool = True, dpr: float = 2.0) -> QPixmap:
    """Render *name* to a crisp QPixmap at logical *size* px square."""
    col = color or _default_color
    key = (name, col, int(size), round(float(dpr), 2), bool(embossed))
    cached = _cache.get(key)
    if cached is not None:
        return cached

    # supersample: render to a high-res image, then downscale smoothly.
    px_size = max(1, int(round(size * dpr)))
    svg = _build_svg(name, col, embossed)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))

    img = QImage(px_size, px_size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(p, QRectF(0, 0, px_size, px_size))
    p.end()

    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    _cache[key] = pm
    return pm


def icon(name: str, color: str | None = None, size: int = 22,
         embossed: bool = True) -> QIcon:
    """Return a QIcon for *name* coloured with *color* (defaults to theme)."""
    return QIcon(pixmap(name, color=color, size=size, embossed=embossed))


# --------------------------------------------------------------------------- #
#  Theme integration
# --------------------------------------------------------------------------- #
def set_default_color(hex_color: str) -> None:
    global _default_color
    if hex_color and hex_color != _default_color:
        _default_color = hex_color
        clear_cache()


def apply_palette(palette) -> None:
    """Pull icon colours from the active theme palette and reset the cache."""
    global _default_color, _nav_active, _nav_idle
    _default_color = getattr(palette, "text_muted", _default_color)
    _nav_active = getattr(palette, "accent", _nav_active)
    _nav_idle = getattr(palette, "text_muted", _nav_idle)
    clear_cache()


def clear_cache() -> None:
    _cache.clear()
