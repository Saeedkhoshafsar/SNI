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


# ── Gaming × Hacker dark ──────────────────────────────────────────────
# Deep matte near-black surfaces with a single bright neon-cyan accent and a
# violet "gaming" secondary. Minimal soft shadows, hairline borders, mono feel
# for technical zones. The accent is intentionally vivid against matte panels.
DARK = Palette(
    name="dark",
    base="#0a0c10",                       # deep matte near-black
    surface="rgba(18, 22, 28, 0.82)",     # matte panel
    surface_alt="rgba(28, 34, 42, 0.88)", # raised / hovered
    elevated="#12161c",
    border="rgba(120, 230, 255, 0.10)",   # faint cyan hairline
    text="#d7e3ec",
    text_muted="#7d8b99",
    text_faint="#4a5663",
    accent="#27e0c8",                      # neon cyan-teal (hacker)
    accent_hover="#46f0d8",
    accent_press="#13c6b0",
    on_accent="#04130f",
    success="#41e08a",
    warning="#ffcf5c",
    danger="#ff6b81",
    shadow="rgba(0, 0, 0, 0.65)",
    is_dark=True,
)

# Secondary "gaming" accent (violet) — used by widgets/animations in step 2.
ACCENT2_DARK = "#9b7bff"

# Light variant keeps the same neon family but on warm-white matte surfaces,
# so toggling preserves the product's identity rather than feeling like a
# different app.
LIGHT = Palette(
    name="light",
    base="#eef2f4",
    surface="rgba(255, 255, 255, 0.86)",
    surface_alt="rgba(226, 234, 236, 0.92)",
    elevated="#ffffff",
    border="rgba(10, 60, 70, 0.12)",
    text="#10171c",
    text_muted="#48565f",
    text_faint="#8a98a0",
    accent="#0aa896",                      # deeper teal for contrast on light
    accent_hover="#089483",
    accent_press="#077a6c",
    on_accent="#ffffff",
    success="#0f9d5b",
    warning="#bf8410",
    danger="#d8415c",
    shadow="rgba(20, 40, 50, 0.18)",
    is_dark=False,
)

ACCENT2_LIGHT = "#7a52e8"

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
    QLabel#Mono {{
        font-family: "Cascadia Code", "JetBrains Mono", "Consolas", monospace;
        color: {p.accent}; font-size: 12px; letter-spacing: 0.3px;
    }}
    QLabel[class="AccentText"] {{ color: {p.accent}; }}

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

    /* ---- Side navigation (hacker rail w/ accent edge on active) ---- */
    QPushButton#NavItem {{
        background: transparent;
        border: none;
        border-left: 2px solid transparent;
        text-align: left;
        padding: 11px 16px; border-radius: {p.radius_sm}px;
        color: {p.text_muted}; font-weight: 500;
    }}
    QPushButton#NavItem:hover    {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton#NavItem:checked  {{
        background: {p.surface_alt};
        color: {p.accent};
        border-left: 2px solid {p.accent};
        font-weight: 600;
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

    /* ---- Toast (transient notifications) ---- */
    QFrame#Toast {{
        background: {p.elevated};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}
    QLabel#ToastText {{ color: {p.text}; font-weight: 500; }}
    QLabel#ToastIcon {{ color: {p.accent}; font-size: 13px; }}
    QFrame#Toast[kind="ok"]   QLabel#ToastIcon {{ color: {p.success}; }}
    QFrame#Toast[kind="warn"] QLabel#ToastIcon {{ color: {p.warning}; }}
    QFrame#Toast[kind="err"]  QLabel#ToastIcon {{ color: {p.danger}; }}

    /* ---- Profile list ---- */
    QListWidget#ProfileList {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        padding: 6px;
    }}
    QListWidget#ProfileList::item {{
        padding: 9px 10px;
        border-radius: {p.radius_sm}px;
        color: {p.text};
    }}
    QListWidget#ProfileList::item:hover {{
        background: {p.surface};
    }}
    QListWidget#ProfileList::item:selected {{
        background: {p.accent};
        color: {p.on_accent};
    }}

    /* ---- Profile editor dialog ---- */
    QDialog#ProfileDialog {{
        background: {p.base};
        border: 1px solid {p.border};
        border-radius: {p.radius}px;
    }}
    QDialog#ProfileDialog QLabel {{ color: {p.text}; }}
    QScrollArea#DialogScroll {{ border: none; background: transparent; }}
    QScrollArea#DialogScroll > QWidget > QWidget {{ background: transparent; }}

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
