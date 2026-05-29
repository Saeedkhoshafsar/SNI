"""Auto-Prober — the "final boss" that makes the tool dead-end-proof.

The prober turns the static arsenal (inject strategies × fragmentation options)
into a *self-adapting* system: on Start it probes several candidates against the
real server, scores each by real success (did a genuine ServerHello come back?
RST? timeout? latency?), picks and **locks** the best, then keeps a sliding
window of health and re-probes / switches automatically when the chosen
candidate degrades.

Design goals (mirrors strategies/ and core/fragment.py)
-------------------------------------------------------
* **UI-agnostic, no Qt.** Emits plain callbacks; the bridge adapts them.
* **Network is injectable.** :class:`AutoProber` takes a ``probe_fn`` that does
  the actual connect+measure. The default real implementation lives in
  :func:`tcp_probe`, but tests pass a fake so the whole ranking / selection /
  health logic runs deterministically off-Windows with no sockets.
* **Score is a *prior*, not the verdict.** ``BypassStrategy.score()`` only seeds
  the initial candidate order; the prober measures actual success.

A *candidate* is the pair (strategy_key, fragment_options) that fully describes
one bypass attempt.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
#  candidate + probe result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One fully-specified bypass attempt: an inject strategy + fragmentation."""

    strategy: str                       # a key in strategies.REGISTRY
    fragment_tcp: bool = False
    fragment_tls: bool = False
    tls_chunk: int = 64

    @property
    def key(self) -> str:
        flags = []
        if self.fragment_tcp:
            flags.append("ftcp")
        if self.fragment_tls:
            flags.append(f"ftls{self.tls_chunk}")
        suffix = ("+" + "+".join(flags)) if flags else ""
        return f"{self.strategy}{suffix}"

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return self.key


# probe outcomes
OK = "ok"               # real ServerHello came back
RST = "rst"             # connection reset (likely DPI injection / refusal)
TIMEOUT = "timeout"     # no response in time
ERROR = "error"         # other failure (dns, route, …)


@dataclass
class ProbeResult:
    candidate: Candidate
    outcome: str                        # one of OK/RST/TIMEOUT/ERROR
    latency_ms: float = 0.0
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.outcome == OK

    def score(self) -> float:
        """Convert a single probe into a 0..1 score (latency-aware on success)."""
        if self.outcome != OK:
            return 0.0
        # 1.0 at 0ms, decaying gently; 500ms → ~0.66, 2000ms → ~0.33
        return 1.0 / (1.0 + self.latency_ms / 1000.0)


# the network primitive the prober calls (injectable for tests)
ProbeFn = Callable[[Candidate, str, int, float], ProbeResult]


# ---------------------------------------------------------------------------
#  sliding-window health per candidate
# ---------------------------------------------------------------------------

@dataclass
class HealthWindow:
    """Bounded recent-history of probe scores for one candidate."""

    size: int = 10
    _scores: List[float] = field(default_factory=list)
    _outcomes: List[str] = field(default_factory=list)

    def record(self, result: ProbeResult) -> None:
        self._scores.append(result.score())
        self._outcomes.append(result.outcome)
        if len(self._scores) > self.size:
            self._scores.pop(0)
            self._outcomes.pop(0)

    @property
    def samples(self) -> int:
        return len(self._scores)

    @property
    def success_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(1 for o in self._outcomes if o == OK) / len(self._outcomes)

    @property
    def mean_score(self) -> float:
        if not self._scores:
            return 0.0
        return sum(self._scores) / len(self._scores)

    @property
    def healthy(self) -> bool:
        """Healthy == we have data and the recent success rate is decent."""
        return self.samples > 0 and self.success_rate >= 0.5


# ---------------------------------------------------------------------------
#  the prober
# ---------------------------------------------------------------------------

class AutoProber:
    """Probe candidates, rank by real success, select & lock the best.

    Parameters
    ----------
    candidates : ordered sequence of :class:`Candidate` (best *prior* first).
    probe_fn   : callable doing the real connect+measure (injectable).
    window     : sliding-window size for per-candidate health.
    timeout    : per-probe timeout in seconds.
    on_log     : optional ``str -> None`` progress callback.
    rng        : optional ``random.Random`` for deterministic randomization.
    """

    def __init__(
        self,
        candidates: Sequence[Candidate],
        probe_fn: ProbeFn,
        *,
        window: int = 10,
        timeout: float = 5.0,
        on_log: Optional[Callable[[str], None]] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        if not candidates:
            raise ValueError("نیاز به حداقل یک کاندیدا برای probe")
        self.candidates: List[Candidate] = list(candidates)
        self.probe_fn = probe_fn
        self.timeout = float(timeout)
        self._on_log = on_log
        self._rng = rng or random.Random()
        self.health: Dict[str, HealthWindow] = {
            c.key: HealthWindow(size=window) for c in self.candidates
        }
        self.selected: Optional[Candidate] = None

    # -- helpers ----------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _probe(self, candidate: Candidate, host: str, port: int) -> ProbeResult:
        try:
            return self.probe_fn(candidate, host, port, self.timeout)
        except Exception as exc:  # never let a bad probe kill the run
            return ProbeResult(candidate, ERROR, detail=repr(exc))

    # -- ranking ----------------------------------------------------------
    @classmethod
    def candidate_order(
        cls, strategies_scores: Dict[str, float], candidates: Sequence[Candidate]
    ) -> List[Candidate]:
        """Order *candidates* by their strategy's static prior (desc)."""
        return sorted(
            candidates,
            key=lambda c: strategies_scores.get(c.strategy, 0.0),
            reverse=True,
        )

    # -- the main loop ----------------------------------------------------
    def probe_all(self, host: str, port: int) -> List[ProbeResult]:
        """Probe every candidate once; record health. Returns all results."""
        results: List[ProbeResult] = []
        self._log(f"شروع probe روی {host}:{port} ({len(self.candidates)} کاندیدا)")
        for cand in self.candidates:
            res = self._probe(cand, host, port)
            self.health[cand.key].record(res)
            results.append(res)
            self._log(
                f"  {cand.key:24} → {res.outcome} "
                f"({res.latency_ms:.0f}ms, score={res.score():.2f})")
        return results

    def select_best(self) -> Optional[Candidate]:
        """Pick the candidate with the highest mean health score and lock it.

        Ties are broken randomly (light randomization → no fixed signature).
        Candidates with no successful sample are skipped.
        """
        ranked = sorted(
            self.candidates,
            key=lambda c: self.health[c.key].mean_score,
            reverse=True,
        )
        best = [c for c in ranked if self.health[c.key].mean_score > 0.0]
        if not best:
            self.selected = None
            self._log("هیچ کاندیدای موفقی یافت نشد")
            return None
        top_score = self.health[best[0].key].mean_score
        # all candidates within a small epsilon of the top are equally good
        tied = [c for c in best
                if abs(self.health[c.key].mean_score - top_score) < 1e-9]
        self.selected = self._rng.choice(tied)
        self._log(f"انتخاب شد: {self.selected.key} (score={top_score:.2f})")
        return self.selected

    def run(self, host: str, port: int) -> Optional[Candidate]:
        """One full pass: probe all candidates then select & lock the best."""
        self.probe_all(host, port)
        return self.select_best()

    # -- runtime health monitoring ---------------------------------------
    def record_live(self, result: ProbeResult) -> None:
        """Feed a live (in-flight connection) result into the health window."""
        win = self.health.get(result.candidate.key)
        if win is not None:
            win.record(result)

    def needs_reprobe(self) -> bool:
        """True if the locked candidate has degraded and we should re-probe."""
        if self.selected is None:
            return True
        return not self.health[self.selected.key].healthy

    def fallback_order(self) -> List[Candidate]:
        """Remaining candidates (excluding the selected) by mean health desc.

        This is the chain to walk if the selected candidate fails mid-session.
        """
        others = [c for c in self.candidates if c is not self.selected]
        return sorted(
            others, key=lambda c: self.health[c.key].mean_score, reverse=True)


# ---------------------------------------------------------------------------
#  default real probe (Windows runtime; not exercised in sandbox tests)
# ---------------------------------------------------------------------------

def tcp_probe(candidate: Candidate, host: str, port: int,
              timeout: float) -> ProbeResult:  # pragma: no cover - needs net
    """Real probe: open a TCP connection and time the handshake.

    This is a *connectivity* prior used before the full injected handshake; the
    in-session ``record_live`` calls refine the picture with real ServerHello
    outcomes. Kept deliberately small and dependency-free (stdlib socket only);
    WinDivert-level injection happens in the live path, not here.
    """
    import socket

    start = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        latency = (time.monotonic() - start) * 1000.0
        return ProbeResult(candidate, OK, latency_ms=latency)
    except socket.timeout:
        return ProbeResult(candidate, TIMEOUT, detail="connect timeout")
    except ConnectionResetError:
        return ProbeResult(candidate, RST, detail="connection reset")
    except OSError as exc:
        return ProbeResult(candidate, ERROR, detail=str(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  candidate enumeration helper
# ---------------------------------------------------------------------------

def build_candidates(
    strategy_keys: Sequence[str],
    *,
    fragment_tcp: bool = False,
    fragment_tls: bool = False,
    tls_chunk: int = 64,
) -> List[Candidate]:
    """Build the candidate list from strategy keys + optional fragmentation.

    With both fragmentation flags off this is one candidate per strategy. When a
    flag is on, both the plain and the fragmented variant are emitted so the
    prober can discover whether fragmentation actually helps on this network.
    """
    out: List[Candidate] = []
    for key in strategy_keys:
        out.append(Candidate(key))
        if fragment_tcp or fragment_tls:
            out.append(Candidate(
                key, fragment_tcp=fragment_tcp, fragment_tls=fragment_tls,
                tls_chunk=tls_chunk))
    return out
