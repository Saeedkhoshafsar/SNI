"""Resilience layer — survive *active* censorship, not just passive blocking.

Advanced DPI does more than drop packets: it injects forged RSTs to tear down
flows, and it throttles (slows instead of cutting) to make a tool look broken.
This module turns those attacks into signals the engine can react to, and pairs
them with the prober's ``fallback_order`` to rotate strategy / upstream IP.

Three pure, UI-agnostic, network-injectable pieces (testable in the sandbox):

* :class:`RstClassifier` — tell a *forged* (DPI-injected) RST apart from a
  legitimate one, using timing / position heuristics. A forged RST that arrives
  right after our ClientHello, before any application data, is the classic
  SNI-based reset; we mark it FORGED so the live path can **ignore** it (à la
  zapret) instead of giving up.
* :class:`ThroughputMonitor` — a sliding window of byte-rate samples that flags
  sustained **throttling** relative to an observed baseline.
* :class:`ResilienceController` — combines both with a rotation policy: ignore
  forged RSTs up to a budget, then rotate the bypass strategy, then rotate the
  upstream IP, walking the prober's fallback chain.

Nothing here imports pydivert or Qt; the engine feeds it events and acts on the
returned :class:`Action`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Sequence


# ---------------------------------------------------------------------------
#  RST classification
# ---------------------------------------------------------------------------

class RstVerdict(str, Enum):
    LEGIT = "legit"        # a normal reset (server closed, real refusal)
    FORGED = "forged"      # almost certainly DPI-injected — ignore it
    UNKNOWN = "unknown"    # not enough signal to decide


@dataclass
class RstEvent:
    """A TCP RST observed on a flow, with the context needed to judge it."""

    # milliseconds since our ClientHello was sent on this flow
    ms_since_client_hello: float
    # bytes of application data received before the RST (0 == none yet)
    app_bytes_received: int = 0
    # did we receive a real ServerHello before the RST?
    server_hello_seen: bool = False
    # optional IP TTL of the RST packet (forged RSTs often have an odd TTL
    # that differs from the genuine server path); None when unknown
    ttl: Optional[int] = None
    # the TTL we observed on genuine packets from the server, if known
    expected_ttl: Optional[int] = None


class RstClassifier:
    """Heuristic forged-vs-legit RST detector.

    The dominant signal is *position*: a reset that lands before any
    ServerHello / application data, very soon after our ClientHello, is the
    canonical SNI-based injected RST. A mismatching TTL reinforces the verdict.
    """

    def __init__(self, *, early_window_ms: float = 200.0,
                 ttl_tolerance: int = 4) -> None:
        self.early_window_ms = float(early_window_ms)
        self.ttl_tolerance = int(ttl_tolerance)

    def classify(self, ev: RstEvent) -> RstVerdict:
        # If we already had a real handshake / data, a reset is most likely real.
        if ev.server_hello_seen or ev.app_bytes_received > 0:
            return RstVerdict.LEGIT

        ttl_mismatch = (
            ev.ttl is not None and ev.expected_ttl is not None
            and abs(ev.ttl - ev.expected_ttl) > self.ttl_tolerance
        )

        early = ev.ms_since_client_hello <= self.early_window_ms

        # No handshake yet + arrives in the early window → injected reset.
        if early:
            return RstVerdict.FORGED
        # Late, no handshake, but TTL clearly off → still smells injected.
        if ttl_mismatch:
            return RstVerdict.FORGED
        return RstVerdict.UNKNOWN

    def is_forged(self, ev: RstEvent) -> bool:
        return self.classify(ev) is RstVerdict.FORGED


# ---------------------------------------------------------------------------
#  throughput / throttle detection
# ---------------------------------------------------------------------------

@dataclass
class ThroughputMonitor:
    """Sliding window of byte-rate samples to detect sustained throttling.

    Feed it ``(bytes, duration_s)`` samples via :meth:`record`. It tracks a
    rolling *baseline* (the best rate seen) and reports throttling when the
    recent mean rate stays below ``throttle_ratio`` × baseline for at least
    ``min_samples`` samples.
    """

    window: int = 8
    throttle_ratio: float = 0.4
    min_samples: int = 4
    _rates: List[float] = field(default_factory=list)
    baseline_bps: float = 0.0

    def record(self, num_bytes: int, duration_s: float) -> None:
        if duration_s <= 0:
            return
        rate = num_bytes / duration_s
        self._rates.append(rate)
        if len(self._rates) > self.window:
            self._rates.pop(0)
        if rate > self.baseline_bps:
            self.baseline_bps = rate

    @property
    def samples(self) -> int:
        return len(self._rates)

    @property
    def recent_bps(self) -> float:
        """Mean rate over the most recent ``min_samples`` samples.

        Throttling is a *recent* condition: averaging the whole window would let
        stale high samples mask a fresh slowdown. We therefore look only at the
        tail of length ``min_samples`` (or the whole history if shorter).
        """
        if not self._rates:
            return 0.0
        n = min(len(self._rates), max(1, self.min_samples))
        tail = self._rates[-n:]
        return sum(tail) / len(tail)

    @property
    def is_throttled(self) -> bool:
        if self.samples < self.min_samples or self.baseline_bps <= 0:
            return False
        return self.recent_bps < self.throttle_ratio * self.baseline_bps

    def reset(self) -> None:
        """Forget history (e.g. after a rotation) but keep the baseline."""
        self._rates.clear()


# ---------------------------------------------------------------------------
#  resilience controller
# ---------------------------------------------------------------------------

class Action(str, Enum):
    NONE = "none"                      # carry on
    IGNORE_RST = "ignore_rst"          # drop a forged RST, keep the flow
    ROTATE_STRATEGY = "rotate_strategy"  # switch to the next bypass strategy
    ROTATE_IP = "rotate_ip"            # switch upstream IP (strategies exhausted)
    GIVE_UP = "give_up"                # nothing left to try


@dataclass
class ResilienceController:
    """Decide how to react to forged RSTs and throttling.

    Policy:
    * a forged RST → :pyattr:`Action.IGNORE_RST`, until ``rst_budget`` forged
      RSTs accumulate within one flow, after which we rotate the strategy
      (the DPI clearly has our current technique pinned);
    * detected throttling → rotate the strategy immediately;
    * when strategies are exhausted → rotate the IP;
    * when both are exhausted → give up.

    The controller is told the remaining strategy/IP options and exposes the
    *next* one to use, so the engine can drive it together with the prober's
    ``fallback_order``.
    """

    rst_budget: int = 3
    classifier: RstClassifier = field(default_factory=RstClassifier)
    throughput: ThroughputMonitor = field(default_factory=ThroughputMonitor)
    on_log: Optional[Callable[[str], None]] = None

    # rotation state
    _strategy_chain: List[str] = field(default_factory=list)
    _ip_chain: List[str] = field(default_factory=list)
    _strategy_idx: int = 0
    _ip_idx: int = 0
    _forged_rst_count: int = 0

    # -- setup ------------------------------------------------------------
    def set_chains(self, strategies: Sequence[str], ips: Sequence[str]) -> None:
        """Provide the fallback order for strategies and upstream IPs."""
        self._strategy_chain = list(strategies)
        self._ip_chain = list(ips)
        self._strategy_idx = 0
        self._ip_idx = 0

    def _log(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    # -- current selections ----------------------------------------------
    @property
    def current_strategy(self) -> Optional[str]:
        if 0 <= self._strategy_idx < len(self._strategy_chain):
            return self._strategy_chain[self._strategy_idx]
        return None

    @property
    def current_ip(self) -> Optional[str]:
        if 0 <= self._ip_idx < len(self._ip_chain):
            return self._ip_chain[self._ip_idx]
        return None

    @property
    def forged_rst_count(self) -> int:
        return self._forged_rst_count

    # -- rotation primitives ---------------------------------------------
    def _rotate_strategy(self) -> Action:
        if self._strategy_idx + 1 < len(self._strategy_chain):
            self._strategy_idx += 1
            self._forged_rst_count = 0
            self.throughput.reset()
            self._log(f"چرخش استراتژی → {self.current_strategy}")
            return Action.ROTATE_STRATEGY
        # strategies exhausted → try the next IP and restart strategy chain
        return self._rotate_ip()

    def _rotate_ip(self) -> Action:
        if self._ip_idx + 1 < len(self._ip_chain):
            self._ip_idx += 1
            self._strategy_idx = 0
            self._forged_rst_count = 0
            self.throughput.reset()
            self._log(f"چرخش IP → {self.current_ip}")
            return Action.ROTATE_IP
        self._log("همه‌ی استراتژی‌ها و IPها امتحان شدند")
        return Action.GIVE_UP

    # -- event handlers ---------------------------------------------------
    def on_rst(self, ev: RstEvent) -> Action:
        """React to an observed RST. Forged ones are ignored up to the budget."""
        if not self.classifier.is_forged(ev):
            return Action.NONE
        self._forged_rst_count += 1
        if self._forged_rst_count >= self.rst_budget:
            self._log(
                f"بیش از {self.rst_budget} RST جعلی — استراتژی فعلی سوخته است")
            return self._rotate_strategy()
        self._log(f"RST جعلی نادیده گرفته شد ({self._forged_rst_count})")
        return Action.IGNORE_RST

    def on_throughput(self, num_bytes: int, duration_s: float) -> Action:
        """Feed a throughput sample; rotate strategy if throttling is detected."""
        self.throughput.record(num_bytes, duration_s)
        if self.throughput.is_throttled:
            self._log("throttle تشخیص داده شد — چرخش استراتژی")
            return self._rotate_strategy()
        return Action.NONE

    def reset_flow(self) -> None:
        """Call at the start of each new flow to clear per-flow counters."""
        self._forged_rst_count = 0
