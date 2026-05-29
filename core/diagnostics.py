"""Diagnostics — a UI-agnostic snapshot of what the engine is doing right now.

The Strategy/Diagnostics page (step 12) needs to *show* the inner state that the
auto-prober (step 9) and resilience layer (step 10) maintain: which strategy is
locked, every candidate's measured score / success-rate, the live throughput &
its throttle verdict, and the forged-RST tally. Rather than let the Qt page reach
into those objects directly (and couple the GUI to their internals), we expose a
single plain-data :class:`DiagnosticsSnapshot` built by :func:`snapshot`.

Everything here is pure data — no Qt, no network — so the snapshot logic is unit
tested in the sandbox, and the page becomes a thin renderer that polls
``engine.diagnostics()`` on a timer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class CandidateStat:
    """Per-candidate measured health, as shown in the diagnostics table."""

    key: str                    # e.g. "fake_ttl+ftcp"
    strategy: str               # bare strategy key, e.g. "fake_ttl"
    samples: int = 0            # probes recorded in the health window
    success_rate: float = 0.0   # fraction of OK outcomes (0..1)
    mean_score: float = 0.0     # latency-aware mean score (0..1)
    selected: bool = False      # is this the locked / active candidate?
    last_outcome: str = ""      # most recent outcome (ok/rst/timeout/error)


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    """One immutable picture of the engine's live bypass state."""

    status: str = "idle"
    active_strategy: Optional[str] = None
    spoof_port: Optional[int] = None

    candidates: List[CandidateStat] = field(default_factory=list)

    # resilience
    resilience_on: bool = False
    forged_rst_count: int = 0
    rst_budget: int = 0
    throttled: bool = False
    recent_bps: float = 0.0
    baseline_bps: float = 0.0
    strategy_chain: List[str] = field(default_factory=list)
    ip_chain: List[str] = field(default_factory=list)
    current_ip: Optional[str] = None

    @property
    def has_probe_data(self) -> bool:
        return any(c.samples > 0 for c in self.candidates)

    @property
    def throttle_ratio(self) -> float:
        """recent / baseline throughput (0..1+); 0 when no baseline yet."""
        if self.baseline_bps <= 0:
            return 0.0
        return self.recent_bps / self.baseline_bps


def _candidate_stats(prober: Any) -> List[CandidateStat]:
    """Build per-candidate stats from an AutoProber, ranked by mean_score desc."""
    stats: List[CandidateStat] = []
    selected_key = (prober.selected.key
                    if getattr(prober, "selected", None) is not None else None)
    for cand in getattr(prober, "candidates", []):
        win = prober.health.get(cand.key)
        if win is None:
            stats.append(CandidateStat(key=cand.key, strategy=cand.strategy))
            continue
        last = win._outcomes[-1] if getattr(win, "_outcomes", None) else ""
        stats.append(CandidateStat(
            key=cand.key,
            strategy=cand.strategy,
            samples=win.samples,
            success_rate=win.success_rate,
            mean_score=win.mean_score,
            selected=(cand.key == selected_key),
            last_outcome=last,
        ))
    stats.sort(key=lambda s: (s.selected, s.mean_score, s.samples), reverse=True)
    return stats


def snapshot(engine: Any) -> DiagnosticsSnapshot:
    """Capture the current diagnostics from an :class:`EngineController`.

    Tolerant by design: any missing attribute (engine idle, prober/resilience
    not built yet) simply yields empty / default fields rather than raising, so
    the page can poll unconditionally.
    """
    status = getattr(engine, "status", "idle")
    spoof_port = getattr(engine, "spoof_port", None)

    prober = getattr(engine, "_prober", None)
    candidates: List[CandidateStat] = []
    active_strategy: Optional[str] = None
    if prober is not None:
        try:
            candidates = _candidate_stats(prober)
            if getattr(prober, "selected", None) is not None:
                active_strategy = prober.selected.strategy
        except Exception:
            candidates = []

    res = getattr(engine, "resilience", None)
    if res is not None:
        try:
            tp = res.throughput
            chain = list(getattr(res, "_strategy_chain", []))
            if active_strategy is None and res.current_strategy is not None:
                active_strategy = res.current_strategy
            return DiagnosticsSnapshot(
                status=status,
                active_strategy=active_strategy,
                spoof_port=spoof_port,
                candidates=candidates,
                resilience_on=True,
                forged_rst_count=res.forged_rst_count,
                rst_budget=res.rst_budget,
                throttled=tp.is_throttled,
                recent_bps=tp.recent_bps,
                baseline_bps=tp.baseline_bps,
                strategy_chain=chain,
                ip_chain=list(getattr(res, "_ip_chain", [])),
                current_ip=res.current_ip,
            )
        except Exception:
            pass

    return DiagnosticsSnapshot(
        status=status,
        active_strategy=active_strategy,
        spoof_port=spoof_port,
        candidates=candidates,
        resilience_on=False,
    )
