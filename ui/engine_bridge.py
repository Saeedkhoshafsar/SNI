"""Thread-safe Qt bridge between :class:`core.engine.EngineController` and the UI.

The engine fires ``on_log`` / ``on_status`` / ``on_count`` from **worker
threads** (the spoofer's asyncio loop, the xray output pump, …). Touching Qt
widgets from a non-GUI thread is undefined behaviour, so this bridge converts
those plain callbacks into Qt **signals**, which Qt automatically marshals onto
the GUI thread (queued connection across threads).

Usage::

    bridge = EngineBridge(controller)
    bridge.log.connect(log_page.append)
    bridge.status.connect(dashboard.set_status)
    bridge.count.connect(dashboard.on_count)
    bridge.start()            # forwards to controller.start()
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.engine import EngineController


class EngineBridge(QObject):
    """Wrap an :class:`EngineController`, re-emitting its callbacks as signals."""

    log = Signal(str)
    status = Signal(str)          # "idle" | "connecting" | "active" | "error"
    count = Signal(int, int)      # (active, total)
    strategy = Signal(str)        # live bypass method in force
    traffic = Signal(int, int, float, float)  # up_bytes, down_bytes, up_bps, down_bps

    def __init__(self, controller: EngineController | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller or EngineController()
        # route the engine's thread-affine callbacks through the signals;
        # Qt::QueuedConnection (implicit cross-thread) hops to the GUI thread.
        self.controller.on_log = self.log.emit
        self.controller.on_status = self.status.emit
        self.controller.on_count = self.count.emit
        self.controller.on_strategy = self.strategy.emit
        self.controller.on_traffic = self.traffic.emit

    # -- pass-through API the UI can call directly ------------------------

    def set_profile(self, profile) -> None:
        self.controller.set_profile(profile)

    def update_config(self, config: dict) -> None:
        self.controller.update_config(config)

    def start(self) -> None:
        self.controller.start()

    def stop(self) -> None:
        self.controller.stop()

    def diagnostics(self):
        """Live diagnostics snapshot (see :meth:`EngineController.diagnostics`)."""
        return self.controller.diagnostics()

    @property
    def is_running(self) -> bool:
        return self.controller.is_running

    @property
    def status_value(self) -> str:
        return self.controller.status
