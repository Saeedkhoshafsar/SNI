"""Structured log buffer + level classification (step 23).

The engine emits plain strings on its ``log`` signal. To render a *professional*
log (timestamps, per-level colours, filtering, counters) the UI needs structure.
Rather than bake that into the Qt widget, the **parsing / classification /
filtering / counting logic lives here as pure, OS-agnostic code** (the same
pattern as :mod:`core.admin` / :mod:`core.system_proxy`) so it is fully unit-
tested without a running Qt app.

A :class:`LogEntry` is one line with a level (info/ok/warn/err) and a timestamp.
:func:`classify` infers the level from the message text (the engine already uses
``✓`` for success and Persian words like «خطا»/«ناموفق» for errors), so existing
call-sites keep working unchanged while the log gains colour.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

# the four levels, ordered by severity (used for the filter dropdown)
LEVELS = ("info", "ok", "warn", "err")

# log SOURCES (issue #4) — which subsystem produced a line. The engine tags
# each line with a leading ``[tag]`` (see core.engine), so a line about
# WinDivert / "run as Administrator" is attributed to the SNI spoofer and never
# confused with an ordinary xray-core config line.
SOURCES = ("engine", "spoof", "core")

# map the Persian leading tags produced by core.engine onto a source key.
_SOURCE_ALIASES = {
    "موتور": "engine",
    "engine": "engine",
    "اسپوف sni": "spoof",
    "اسپوف": "spoof",
    "spoof": "spoof",
    "sni spoof": "spoof",
    "هسته xray": "core",
    "هسته": "core",
    "xray": "core",
    "core": "core",
    "xray core": "core",
}


def classify_source(message: str) -> str:
    """Infer the log source from a leading ``[tag]`` prefix (pure, issue #4).

    Lines look like ``[اسپوف SNI] …`` / ``[هسته xray] …`` / ``[موتور] …``.
    Anything without a recognised tag defaults to ``"engine"``.
    """
    if not message:
        return "engine"
    msg = message.lstrip()
    if not msg.startswith("["):
        return "engine"
    end = msg.find("]")
    if end <= 1:
        return "engine"
    tag = msg[1:end].strip().lower()
    return _SOURCE_ALIASES.get(tag, "engine")

# Persian/English keyword hints used to colour a plain log line.
_ERR_HINTS = ("خطا", "ناموفق", "شکست", "نامعتبر", "error", "failed", "fail",
              "exception", "✗", "❌")
_WARN_HINTS = ("هشدار", "اخطار", "throttl", "محدود", "تلاش مجدد", "retry",
               "warn", "⚠", "نادیده")
_OK_HINTS = ("✓", "✅", "برقرار شد", "موفق", "روشن شد", "متصل", "success",
             "connected", "ok ", "آماده")


def classify(message: str) -> str:
    """Infer a log level (info/ok/warn/err) from the message text (pure)."""
    if not message:
        return "info"
    low = message.lower()
    # errors win over warnings win over ok (most severe match first)
    for hint in _ERR_HINTS:
        if hint in message or hint in low:
            return "err"
    for hint in _WARN_HINTS:
        if hint in message or hint in low:
            return "warn"
    for hint in _OK_HINTS:
        if hint in message or hint in low:
            return "ok"
    return "info"


@dataclass
class LogEntry:
    message: str
    level: str = "info"
    source: str = "engine"
    ts: float = field(default_factory=time.time)

    @property
    def stamp(self) -> str:
        """HH:MM:SS local time, e.g. ``14:03:09``."""
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    def format(self) -> str:
        """Single-line plain representation: ``[HH:MM:SS] LEVEL  message``."""
        return f"[{self.stamp}] {self.level.upper():<4} {self.message}"


def matches(entry: LogEntry, *, level: str = "all", query: str = "",
            source: str = "all") -> bool:
    """Return True if *entry* passes the level + source filters and query (pure).

    *level* — ``"all"`` or one of :data:`LEVELS`.
    *source* — ``"all"`` or one of :data:`SOURCES` (issue #4).
    *query* — case-insensitive substring; empty matches everything.
    """
    if level and level != "all" and entry.level != level:
        return False
    if source and source != "all" and entry.source != source:
        return False
    q = (query or "").strip().lower()
    if q and q not in entry.message.lower():
        return False
    return True


class LogBuffer:
    """A bounded, classified rolling log with per-level counters (pure).

    The Qt widget owns one of these; it stays headless-testable. ``add`` returns
    the created :class:`LogEntry` so the caller can render it incrementally
    without re-scanning the whole buffer.
    """

    def __init__(self, capacity: int = 2000):
        self.capacity = max(1, int(capacity))
        self._entries: list[LogEntry] = []
        self.counts: dict[str, int] = {lv: 0 for lv in LEVELS}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[LogEntry]:
        return list(self._entries)

    def add(self, message: str, level: Optional[str] = None) -> LogEntry:
        lv = level if level in LEVELS else classify(message)
        src = classify_source(message)
        entry = LogEntry(message=message, level=lv, source=src)
        self._entries.append(entry)
        self.counts[lv] = self.counts.get(lv, 0) + 1
        # evict oldest while over capacity, decrementing its counter
        while len(self._entries) > self.capacity:
            old = self._entries.pop(0)
            self.counts[old.level] = max(0, self.counts.get(old.level, 0) - 1)
        return entry

    def clear(self) -> None:
        self._entries.clear()
        self.counts = {lv: 0 for lv in LEVELS}

    def filtered(self, *, level: str = "all", query: str = "",
                 source: str = "all") -> list[LogEntry]:
        """Entries passing the level + source filters + text query (pure)."""
        return [e for e in self._entries
                if matches(e, level=level, query=query, source=source)]

    def counts_summary(self) -> str:
        """Compact counter string, e.g. ``info 12 · ok 3 · warn 1 · err 0``."""
        return " · ".join(f"{lv} {self.counts.get(lv, 0)}" for lv in LEVELS)
