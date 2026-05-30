"""Windows system-proxy helper (step 22).

Lets the app flip the *OS-wide* HTTP/HTTPS proxy on and off so every browser /
app routes through our local proxy — the "system proxy" mode users expect from
v2rayN / Clash, as opposed to a "tunnel" that only the app itself uses.

On Windows this writes the WinINET keys under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings``:

* ``ProxyEnable``   (DWORD) 1/0
* ``ProxyServer``   (str)   ``host:port`` (applies to all protocols)
* ``ProxyOverride`` (str)   bypass list, e.g. ``localhost;127.*;<local>``

…then notifies running processes via ``InternetSetOption`` so the change takes
effect immediately (no logout needed).

As with :mod:`core.admin`, the **decision / string logic is pure and unit-
tested on any OS** — only the actual registry writes + WinINET refresh are
Windows-only and are injected (so tests never touch the real registry).
"""
from __future__ import annotations

import sys
from typing import Callable, Optional


# default LAN/loopback bypass so local + intranet traffic skips the proxy
DEFAULT_BYPASS = "localhost;127.*;10.*;172.16.*;192.168.*;<local>"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def format_proxy_server(host: str, port: int) -> str:
    """Build the ``ProxyServer`` value (``host:port``) with light validation."""
    host = (host or "").strip() or "127.0.0.1"
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"پورت پروکسی نامعتبر: {port!r}")
    if not (0 < port < 65536):
        raise ValueError(f"پورت پروکسی نامعتبر: {port}")
    return f"{host}:{port}"


def normalise_bypass(bypass: Optional[str]) -> str:
    """Return a clean ``;``-joined bypass list (falls back to the default)."""
    if not bypass or not bypass.strip():
        return DEFAULT_BYPASS
    parts = [p.strip() for p in bypass.replace(",", ";").split(";") if p.strip()]
    return ";".join(parts) if parts else DEFAULT_BYPASS


def desired_state(enable: bool, host: str = "", port: int = 0,
                  bypass: Optional[str] = None) -> dict:
    """Compute the registry values for the requested proxy state (pure).

    Returns a dict mirroring the WinINET keys. When *enable* is False the proxy
    is turned off but the server/bypass strings are still returned (so a caller
    could persist them) — only ``ProxyEnable`` flips to 0.
    """
    state = {
        "ProxyEnable": 1 if enable else 0,
        "ProxyOverride": normalise_bypass(bypass),
    }
    if enable:
        state["ProxyServer"] = format_proxy_server(host, port)
    else:
        state["ProxyServer"] = ""
    return state


class SystemProxy:
    """Apply / clear the Windows system proxy.

    *writer* — ``fn(values: dict) -> None`` persists the WinINET values.
    *refresher* — ``fn() -> None`` notifies WinINET so changes apply at once.
    *reader* — ``fn() -> dict`` reads back the current values (for status).
    Off-Windows (or in tests) these default to safe no-ops / injected fakes.
    """

    def __init__(self, *, writer: Optional[Callable] = None,
                 refresher: Optional[Callable] = None,
                 reader: Optional[Callable] = None,
                 on_log: Optional[Callable] = None):
        self._writer = writer
        self._refresher = refresher
        self._reader = reader
        self.on_log = on_log

    # -- logging ----------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    # -- public API -------------------------------------------------------
    def enable(self, host: str, port: int, bypass: Optional[str] = None) -> dict:
        """Turn the system proxy ON pointing at ``host:port``."""
        values = desired_state(True, host, port, bypass)
        self._apply(values)
        self._log(f"پروکسی سیستم روشن شد → {values['ProxyServer']}")
        return values

    def disable(self) -> dict:
        """Turn the system proxy OFF (leave server string blank)."""
        values = desired_state(False)
        self._apply(values)
        self._log("پروکسی سیستم خاموش شد")
        return values

    def is_enabled(self) -> bool:
        """Best-effort read of the current ProxyEnable flag."""
        try:
            cur = self._read()
        except Exception:
            return False
        return bool(cur.get("ProxyEnable", 0))

    # -- backend (Windows registry + WinINET) ----------------------------
    def _apply(self, values: dict) -> None:
        writer = self._writer or self._default_writer
        writer(values)
        refresher = self._refresher or self._default_refresher
        refresher()

    def _read(self) -> dict:
        reader = self._reader or self._default_reader
        return reader()

    # The three methods below are the only Windows-specific parts. They are
    # exercised only on Windows (guarded), so they're excluded from coverage.
    def _default_writer(self, values: dict) -> None:  # pragma: no cover
        if not is_windows():
            self._log("پروکسی سیستم فقط روی ویندوز پشتیبانی می‌شود")
            return
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD,
                              int(values["ProxyEnable"]))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ,
                              str(values.get("ProxyServer", "")))
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              str(values.get("ProxyOverride", "")))
        finally:
            winreg.CloseKey(key)

    def _default_refresher(self) -> None:  # pragma: no cover
        if not is_windows():
            return
        import ctypes
        internet_set_option = ctypes.windll.wininet.InternetSetOptionW
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        internet_set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        internet_set_option(0, INTERNET_OPTION_REFRESH, 0, 0)

    def _default_reader(self) -> dict:  # pragma: no cover
        if not is_windows():
            return {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_QUERY_VALUE)
        out: dict = {}
        try:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    out[name] = 0 if name == "ProxyEnable" else ""
        finally:
            winreg.CloseKey(key)
        return out
