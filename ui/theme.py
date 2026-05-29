"""Design tokens + QSS builder for the professional SNI Spoofer UI.

A single source of truth for colors, radii, spacing and typography. The
palette is intentionally *muted/matte* (no harsh saturation) so the
translucent Mica/acrylic backdrop can show through and cast soft shadows.

Two themes are provided — ``dark`` and ``light`` — sharing the same accent
family so switching feels seamless. ``build_qss(theme)`` returns a complete
Qt style sheet string applied to the whole app.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
#  Palette dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    name: str
    # surfaces (semi-transparent so the Mica backdrop shows through)
    base: str            # window backdrop tint
    surface: str         # cards / panels
    surface_alt: str     # nested / hovered surfaces
    elevated: str        # popups / menus
    border: str          # hairline borders
    # text
    text: str
    text_muted: str
    text_faint: str
    # accent family (muted blue-violet)
    accent: str
    accent_hover: str
    accent_press: str
    on_accent: str
    # semantic
    success: str
    warning: str
    danger: str
    # shadow color (rgba)
    shadow: str
    # is this a dark theme?
    is_dark: bool = True
    # design scalars
    radius: int = 14
    radius_sm: int = 9
    pad: int = 14


DARK = Palette(
    name="dark",
    base="#0f1115",
    surface="rgba(30, 33, 43, 0.72)",
    surface_alt="rgba(42, 46, 60, 0.78)",
    elevated="#1b1e27",
    border="rgba(255, 255, 255, 0.07)",
    text="#e6e9f0",
    text_muted="#9aa0b0",
    text_faint="#5b6172",
    accent="#7c9cff",
    accent_hover="#93acff",
    accent_press="#6485f0",
    on_accent="#0c0f17",
    success="#65d6a0",
    warning="#f2c879",
    danger="#f08c9e",
    shadow="rgba(0, 0, 0, 0.55)",
    is_dark=True,
)

LIGHT = Palette(
    name="light",
    base="#eef1f6",
    surface="rgba(255, 255, 255, 0.78)",
    surface_alt="rgba(238, 241, 248, 0.9)",
    elevated="#ffffff",
    border="rgba(15, 20, 35, 0.08)",
    text="#1b1f2a",
    text_muted="#5c6478",
    text_faint="#9aa1b3",
    accent="#4d6bf0",
    accent_hover="#3f5ee6",
    accent_press="#3552d6",
    on_accent="#ffffff",
    success="#1f9d6b",
    warning="#c98a1e",
    danger="#d8536c",
    shadow="rgba(40, 50, 80, 0.20)",
    is_dark=False,
)

THEMES = {"dark": DARK, "light": LIGHT}


def get_palette(name: str) -> Palette:
    return THEMES.get(name, DARK)


# ---------------------------------------------------------------------------
#  QSS builder
# ---------------------------------------------------------------------------

def build_qss(p: Palette) -> str:
    """Return a full Qt style sheet for the given palette."""
    return f"""
    * {{
        font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif;
        font-size: 14px;
        color: {p.text};
        outline: none;
    }}

    QWidget#RootBackdrop {{
        background: {p.base};
    }}

    /* ---- Cards / panels ---- */
    QFrame.Card, QFrame#Card {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: {p.radius}px;
    }}
    QFrame#CardAlt {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}

    /* ---- Headings ---- */
    QLabel#H1 {{ font-size: 22px; font-weight: 700; color: {p.text}; }}
    QLabel#H2 {{ font-size: 16px; font-weight: 600; color: {p.text}; }}
    QLabel#Muted {{ color: {p.text_muted}; }}
    QLabel#Faint {{ color: {p.text_faint}; font-size: 12px; }}

    /* ---- Primary button ---- */
    QPushButton#Primary {{
        background: {p.accent};
        color: {p.on_accent};
        border: none;
        border-radius: {p.radius_sm}px;
        padding: 9px 18px;
        font-weight: 600;
    }}
    QPushButton#Primary:hover  {{ background: {p.accent_hover}; }}
    QPushButton#Primary:pressed {{ background: {p.accent_press}; }}
    QPushButton#Primary:disabled {{
        background: {p.surface_alt}; color: {p.text_faint};
    }}

    /* ---- Ghost / secondary button ---- */
    QPushButton#Ghost {{
        background: transparent;
        color: {p.text_muted};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        padding: 8px 14px;
    }}
    QPushButton#Ghost:hover {{
        background: {p.surface_alt}; color: {p.text};
    }}

    /* ---- Window control buttons (title bar) ---- */
    QPushButton#WinBtn {{
        background: transparent; border: none; border-radius: 6px;
        color: {p.text_muted}; font-size: 15px; padding: 4px 10px;
    }}
    QPushButton#WinBtn:hover {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton#WinClose:hover {{ background: {p.danger}; color: #fff; }}

    /* ---- Side navigation ---- */
    QPushButton#NavItem {{
        background: transparent; border: none; text-align: left;
        padding: 11px 16px; border-radius: {p.radius_sm}px;
        color: {p.text_muted}; font-weight: 500;
    }}
    QPushButton#NavItem:hover    {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton#NavItem:checked  {{
        background: {p.surface_alt}; color: {p.accent}; font-weight: 600;
    }}

    /* ---- Inputs ---- */
    QLineEdit, QComboBox, QSpinBox {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        padding: 8px 10px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {p.accent};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p.elevated};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
        padding: 4px;
    }}

    /* ---- Scrollbars ---- */
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {p.text_faint}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

    /* ---- Log view ---- */
    QPlainTextEdit#Log {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 12.5px;
        padding: 8px;
    }}
    """
