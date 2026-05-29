"""Windows-only translucency effects (Mica / Acrylic / blur-behind).

Uses undocumented but widely-used DWM APIs:
  * DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)  -> Mica/Acrylic (Win11 22H2+)
  * DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE) -> dark title region
  * SetWindowCompositionAttribute(ACCENT_ENABLE_ACRYLICBLURBEHIND) -> Win10 fallback

All calls are wrapped in try/except and become no-ops on non-Windows or if the
OS build doesn't support a given effect, so the UI still renders (opaque) on
older systems and in the Linux sandbox used for development.
"""
from __future__ import annotations

import sys
import ctypes
from ctypes import wintypes


def _is_windows() -> bool:
    return sys.platform == "win32"


# DWM attribute constants
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWA_MICA_EFFECT = 1029  # legacy Win11 21H2

# Backdrop types for DWMWA_SYSTEMBACKDROP_TYPE
BACKDROP_AUTO = 0
BACKDROP_NONE = 1
BACKDROP_MICA = 2
BACKDROP_ACRYLIC = 3
BACKDROP_TABBED = 4


def _dwm_set_attr(hwnd: int, attr: int, value: int) -> bool:
    try:
        val = ctypes.c_int(value)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attr),
            ctypes.byref(val), ctypes.sizeof(val))
        return res == 0
    except Exception:
        return False


def set_dark_titlebar(hwnd: int, dark: bool) -> bool:
    """Toggle the immersive dark title region (affects DWM-drawn chrome)."""
    if not _is_windows():
        return False
    return _dwm_set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def apply_backdrop(hwnd: int, backdrop: int = BACKDROP_MICA) -> bool:
    """Request a system backdrop (Mica/Acrylic). Win11 22H2+.

    Returns True if the modern API accepted the request. Falls back to the
    legacy Mica flag, then to the Win10 acrylic composition API.
    """
    if not _is_windows():
        return False
    if _dwm_set_attr(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, backdrop):
        return True
    # Legacy Win11 21H2 Mica flag
    if backdrop == BACKDROP_MICA and _dwm_set_attr(hwnd, _DWMWA_MICA_EFFECT, 1):
        return True
    # Win10 acrylic fallback
    return _enable_acrylic_win10(hwnd)


# --- Win10 SetWindowCompositionAttribute acrylic fallback -------------------

class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(_ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]


_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
_WCA_ACCENT_POLICY = 19


def _enable_acrylic_win10(hwnd: int, tint: int = 0x99000000) -> bool:
    try:
        accent = _ACCENT_POLICY()
        accent.AccentState = _ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.GradientColor = tint  # AABBGGRR
        data = _WINCOMPATTRDATA()
        data.Attribute = _WCA_ACCENT_POLICY
        data.Data = ctypes.pointer(accent)
        data.SizeOfData = ctypes.sizeof(accent)
        res = ctypes.windll.user32.SetWindowCompositionAttribute(
            wintypes.HWND(hwnd), ctypes.byref(data))
        return bool(res)
    except Exception:
        return False
