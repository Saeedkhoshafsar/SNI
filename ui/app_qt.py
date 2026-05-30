"""Qt application entry point for the professional SNI Spoofer UI.

Run with::

    python -m ui.app_qt

Sets sane high-DPI / font defaults, creates the :class:`MainWindow` and starts
the event loop. Real core wiring (ProxyServer / TransparentSpoofServer / admin
elevation) is layered on in step 3 — this entry point keeps the UI launchable
and visually complete on its own.
"""
from __future__ import annotations

import sys


def main(theme: str | None = None) -> int:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    # High-DPI friendliness (no-op on Qt6 where it's default, kept for clarity)
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SNI Spoofer")
    app.setApplicationDisplayName("SNI Spoofer")

    # Language (#6): restore the persisted choice and set the matching layout
    # direction (RTL for Persian, LTR for English) before the window is built.
    try:
        from core.config_store import ConfigStore
        from ui import i18n
        lang = str(ConfigStore().get("language", "fa"))
        if lang not in ("fa", "en"):
            lang = "fa"
        i18n._lang = lang
    except Exception:
        lang = "fa"
    app.setLayoutDirection(Qt.RightToLeft if lang == "fa" else Qt.LeftToRight)

    from ui.window import MainWindow
    win = MainWindow(theme=theme)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
