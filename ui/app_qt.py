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


def main(theme: str = "dark") -> int:
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
    # Persian-first product: right-to-left layout direction.
    app.setLayoutDirection(Qt.RightToLeft)

    from ui.window import MainWindow
    win = MainWindow(theme=theme)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
