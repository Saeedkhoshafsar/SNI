"""Top-level entry point for the packaged SNI Spoofer application (step 13).

This is the script PyInstaller freezes into ``SNISpoofer.exe``. It is kept
deliberately thin:

1. Ensure Administrator rights (WinDivert/inject needs them). On Windows, if we
   are not elevated, :func:`core.admin.ensure_admin` relaunches the very same
   exe via ``runas`` and we exit so the elevated copy takes over.
2. Launch the Qt GUI (:func:`ui.app_qt.main`).

Running ``python app.py`` in development behaves the same way — off-Windows the
admin step is a no-op, so the GUI just starts.

A tiny ``--theme {light,dark}`` flag is accepted and forwarded to the UI; any
unknown flags are ignored so future args don't break the elevation relaunch.
"""
from __future__ import annotations

import sys


def _parse_theme(argv) -> str | None:
    """Best-effort extraction of ``--theme <value>`` / ``--theme=<value>``."""
    for i, arg in enumerate(argv):
        if arg == "--theme" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--theme="):
            return arg.split("=", 1)[1]
    return None


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv

    # Step 1: self-elevate if needed. When a relaunch is triggered we must NOT
    # continue running the (un-elevated) original process.
    from core.admin import ensure_admin
    if ensure_admin(argv):
        return 0

    # Step 2: hand off to the Qt UI.
    from ui.app_qt import main as qt_main
    return qt_main(theme=_parse_theme(argv))


if __name__ == "__main__":
    raise SystemExit(main())
