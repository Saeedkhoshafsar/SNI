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
    # 3-D gradient stops (backdrop + cards). Sensible defaults derived from the
    # base/surface so older code keeps working; each theme overrides them.
    bg_grad_a: str = "#10141a"
    bg_grad_b: str = "#0b0e13"
    bg_grad_c: str = "#070a0e"
    card_grad_top: str = "rgba(34, 41, 51, 0.96)"
    card_grad_bottom: str = "rgba(20, 25, 32, 0.96)"
    card_highlight: str = "rgba(255, 255, 255, 0.06)"


# ── Gaming × Hacker dark ──────────────────────────────────────────────
# Deep matte near-black surfaces with a single bright neon-cyan accent and a
# violet "gaming" secondary. Minimal soft shadows, hairline borders, mono feel
# for technical zones. The accent is intentionally vivid against matte panels.
DARK = Palette(
    name="dark",
    base="#0a0c10",                       # deep matte near-black
    surface="#161b22",                    # matte panel (solid now)
    surface_alt="#1d242d",                # raised / hovered
    elevated="#12161c",
    border="rgba(120, 230, 255, 0.12)",   # faint cyan hairline
    text="#d7e3ec",
    text_muted="#8a99a8",
    text_faint="#5a6675",
    accent="#27e0c8",                      # neon cyan-teal (hacker)
    accent_hover="#46f0d8",
    accent_press="#13c6b0",
    on_accent="#04130f",
    success="#41e08a",
    warning="#ffcf5c",
    danger="#ff6b81",
    shadow="rgba(0, 0, 0, 0.65)",
    is_dark=True,
    # 3-D gradient identity
    bg_grad_a="#141b24",
    bg_grad_b="#0c1016",
    bg_grad_c="#07090d",
    card_grad_top="#1e252e",
    card_grad_bottom="#161b22",
    card_highlight="rgba(255, 255, 255, 0.07)",
)

# Secondary "gaming" accent (violet) — used by widgets/animations in step 2.
ACCENT2_DARK = "#9b7bff"

# Light variant keeps the same neon family but on warm-white matte surfaces,
# so toggling preserves the product's identity rather than feeling like a
# different app.
LIGHT = Palette(
    name="light",
    base="#f3f5f8",                        # soft cool-white backdrop
    surface="#ffffff",                     # crisp white cards
    surface_alt="#eef1f5",                 # gently raised / inputs
    elevated="#ffffff",
    border="rgba(20, 40, 70, 0.10)",       # very subtle hairline (cleaner)
    text="#141a21",
    text_muted="#566472",
    text_faint="#94a2b0",
    accent="#0d9488",                      # balanced teal — readable on white
    accent_hover="#0b8378",
    accent_press="#0a6e64",
    on_accent="#ffffff",
    success="#12a05f",
    warning="#c2860f",
    danger="#dc3b58",
    shadow="rgba(20, 40, 60, 0.12)",       # soft, professional shadow
    is_dark=False,
    # gentle vertical wash instead of a heavy diagonal — looks calmer/cleaner
    bg_grad_a="#f7f9fb",
    bg_grad_b="#eff3f7",
    bg_grad_c="#e7ecf1",
    card_grad_top="#ffffff",
    card_grad_bottom="#fbfcfe",            # near-flat so cards read as clean white
    card_highlight="rgba(255, 255, 255, 0.0)",   # no harsh top highlight on light
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
        /* diagonal multi-stop gradient gives the window real depth (3-D feel,
           feedback 6) instead of a flat fill */
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:1,
            stop:0    {p.bg_grad_a},
            stop:0.55 {p.bg_grad_b},
            stop:1    {p.bg_grad_c});
        border: 1px solid {p.border};
        border-radius: 14px;
    }}

    /* ---- Cards / panels ---- */
    QFrame.Card, QFrame#Card {{
        background: qlineargradient(
            x1:0, y1:0, x2:0, y2:1,
            stop:0 {p.card_grad_top}, stop:1 {p.card_grad_bottom});
        border: 1px solid {p.border};
        border-top: 1px solid {p.card_highlight};   /* top edge catches light */
        border-radius: {p.radius}px;
    }}
    QFrame#CardAlt {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}
    /* ---- Clickable strategy cards (step 24) ---- */
    QFrame#StrategyCard {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}
    QFrame#StrategyCard:hover {{
        border: 1px solid {p.accent};
        background: {p.surface};
    }}
    QFrame#StrategyCard[selected="true"] {{
        border: 1.5px solid {p.accent};
        background: {p.surface};
    }}
    QLabel#StrategyCheck {{
        color: {p.accent};
        font-size: 12px;
        font-weight: 600;
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
    /* #3: WinClose must share WinBtn's transparent base style — without it the
       close button fell back to Qt's default opaque white button. */
    QPushButton#WinBtn, QPushButton#WinClose {{
        background: transparent; border: none; border-radius: 6px;
        color: {p.text_muted}; font-size: 15px; padding: 4px 10px;
    }}
    QPushButton#WinBtn:hover {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton#WinClose:hover {{ background: {p.danger}; color: #fff; }}
    QPushButton#WinBtn:pressed, QPushButton#WinClose:pressed {{ background: {p.border}; }}

    /* ---- Persistent active-config status bar (visible on every tab, #9) ---- */
    QFrame#ActiveBar {{
        background: {p.surface};
        border-top: 1px solid {p.border};
        border-bottom: 1px solid {p.border};
    }}
    QLabel#ActiveBarDot {{ font-size: 11px; color: {p.text_faint}; }}
    QLabel#ActiveBarDot[state="active"]     {{ color: {p.success}; }}
    QLabel#ActiveBarDot[state="connecting"] {{ color: {p.warning}; }}
    QLabel#ActiveBarDot[state="error"]      {{ color: {p.danger}; }}
    QLabel#ActiveBarDot[state="idle"]       {{ color: {p.text_faint}; }}
    QLabel#ActiveBarState {{ font-size: 12px; font-weight: 600; color: {p.text_muted}; }}
    QLabel#ActiveBarSep {{ color: {p.border}; }}
    QLabel#ActiveBarName {{ font-size: 12.5px; font-weight: 600; color: {p.text}; }}
    QLabel#ActiveBarRate {{
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 11.5px; color: {p.text_muted};
    }}

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
        padding: 10px 12px;
        min-height: 22px;          /* guarantees text is never clipped (Win DPI) */
        color: {p.text};
        font-size: 13.5px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
        border: 1px solid {p.text_faint};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {p.accent};
        background: {p.surface};
    }}
    QLineEdit::placeholder {{ color: {p.text_faint}; }}

    QComboBox::drop-down {{
        border: none; width: 26px;
        subcontrol-origin: padding; subcontrol-position: center right;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid {p.text_muted};
        width: 0; height: 0; margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {p.elevated};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
        padding: 4px;
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 4px 8px; }}

    /* spinbox up/down buttons — give them real width so they're clickable */
    QSpinBox::up-button, QSpinBox::down-button {{
        subcontrol-origin: border;
        width: 22px;
        background: {p.surface};
        border-left: 1px solid {p.border};
    }}
    QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: {p.radius_sm}px; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: {p.radius_sm}px; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {p.elevated}; }}
    QSpinBox::up-arrow {{
        border-left: 4px solid transparent; border-right: 4px solid transparent;
        border-bottom: 5px solid {p.text_muted}; width: 0; height: 0;
    }}
    QSpinBox::down-arrow {{
        border-left: 4px solid transparent; border-right: 4px solid transparent;
        border-top: 5px solid {p.text_muted}; width: 0; height: 0;
    }}

    /* ---- Checkboxes ---- */
    QCheckBox {{
        color: {p.text};
        spacing: 9px;
        padding: 4px 0;
        font-size: 13.5px;
    }}
    QCheckBox::indicator {{
        width: 19px; height: 19px;
        border: 1px solid {p.border};
        border-radius: 5px;
        background: {p.surface_alt};
    }}
    QCheckBox::indicator:hover {{ border: 1px solid {p.accent}; }}
    QCheckBox::indicator:checked {{
        background: {p.accent};
        border: 1px solid {p.accent};
        image: none;
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
        background: transparent;   /* selection shown by the ProfileRow itself */
    }}

    /* ---- Rich profile row (custom item widget) ---- */
    QFrame#ProfileRow {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}
    QFrame#ProfileRow:hover {{ background: {p.surface_alt}; }}
    QFrame#ProfileRow[active="1"] {{
        background: {p.surface_alt};
        border: 1px solid {p.accent};
    }}
    QLabel#RowGlyph {{ font-size: 17px; color: {p.accent}; }}
    QLabel#RowName  {{ font-size: 14px; font-weight: 600; color: {p.text}; }}
    QLabel#RowDetail {{
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 11.5px; color: {p.text_muted};
    }}
    /* dedicated, never-clipped inline ping-result label (#1) */
    QLabel#RowPingResult {{
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 11.5px; font-weight: 600; color: {p.text_muted};
    }}
    QLabel#RowPingResult[pingkind="busy"] {{ color: {p.warning}; }}
    QLabel#RowPingResult[pingkind="ok"]   {{ color: {p.success}; }}
    QLabel#RowPingResult[pingkind="err"]  {{ color: {p.danger}; }}
    QLabel#ActivePill {{
        background: {p.success}; color: {p.on_accent};
        border-radius: 8px; padding: 1px 8px;
        font-size: 11px; font-weight: 700;
    }}
    QLabel#RowBadge {{
        background: {p.elevated}; color: {p.text_muted};
        border: 1px solid {p.border}; border-radius: 7px;
        padding: 1px 7px; font-size: 10.5px; font-weight: 600;
        letter-spacing: 0.4px;
    }}
    QPushButton#RowEdit, QPushButton#RowPing, QPushButton#RowUse,
    QPushButton#RowShare, QPushButton#RowScan {{
        background: transparent; border: 1px solid {p.border};
        border-radius: 7px; color: {p.text_muted}; font-size: 13px;
    }}
    QPushButton#RowEdit:hover {{ background: {p.accent}; color: {p.on_accent}; border-color: {p.accent}; }}
    QPushButton#RowPing:hover {{ background: {p.warning}; color: {p.on_accent}; border-color: {p.warning}; }}
    QPushButton#RowPing:disabled {{ color: {p.text_faint}; }}
    QPushButton#RowUse:hover {{ background: {p.success}; color: {p.on_accent}; border-color: {p.success}; }}
    QPushButton#RowShare:hover {{ background: {p.accent}; color: {p.on_accent}; border-color: {p.accent}; }}
    QPushButton#RowScan:hover {{ background: {p.accent_hover}; color: {p.on_accent}; border-color: {p.accent_hover}; }}

    /* ---- Profile editor dialog ---- */
    QDialog#ProfileDialog {{
        background: {p.base};
        border: 1px solid {p.border};
        border-radius: {p.radius}px;
    }}
    QDialog#ProfileDialog QLabel {{ color: {p.text}; }}
    QScrollArea#DialogScroll {{ border: none; background: transparent; }}
    QScrollArea#DialogScroll > QWidget > QWidget {{ background: transparent; }}

    /* ---- Text views (log + diagnostics table) ---- */
    /* base rule: NO text edit is ever the default white Windows control */
    QTextEdit, QPlainTextEdit {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        color: {p.text};
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}
    QTextEdit#Log, QPlainTextEdit#Log {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        color: {p.text};
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 12.5px;
        padding: 8px;
    }}

    /* ---- Progress bar (throughput meter) ---- */
    QProgressBar {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        min-height: 16px;
        text-align: center;
        color: {p.text};
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {p.accent}, stop:1 {p.success});
        border-radius: {p.radius_sm}px;
        margin: 1px;
    }}
    QComboBox#LogFilter {{
        min-width: 88px;
        padding: 5px 8px;
    }}
    QLineEdit#LogSearch {{
        padding: 6px 10px;
    }}
    QLabel#LogCounters {{
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 11.5px;
    }}
    QPlainTextEdit#PingOutput {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
        color: {p.text};
        font-family: "Cascadia Code", "Consolas", monospace;
        font-size: 12.5px;
        padding: 8px;
    }}
    QLabel#ModeBadge {{
        padding: 4px 12px;
        border-radius: 11px;
        font-size: 12px;
        font-weight: 600;
        border: 1px solid {p.border};
        color: {p.text_muted};
        background: {p.surface_alt};
    }}
    QLabel#ModeBadge[kind="tunnel"] {{
        color: {p.on_accent};
        background: {p.accent};
        border: 1px solid {p.accent};
    }}
    QLabel#ModeBadge[kind="proxy"] {{
        color: {p.text_muted};
        background: {p.surface_alt};
    }}
    QLabel#RateDown {{ color: {p.accent}; font-weight: 600; }}
    QLabel#RateUp {{ color: {p.success}; font-weight: 600; }}
    QWidget#Sparkline {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {p.radius_sm}px;
    }}
    """
