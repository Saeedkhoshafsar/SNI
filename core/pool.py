"""Multi-IP / Multi-SNI connection pool with adaptive health tracking.

This module ports the self-healing **route pool** from the reference project
``hjfisher/SNISPF-HJ`` into our architecture. Where our existing tooling only
ever talked to a single fixed ``CONNECT_IP`` + ``FAKE_SNI``, this turns the
cartesian product of *several* IPs × *several* SNIs into a pool of
``(IP, SNI)`` pairs that is:

* **health-checked** continuously in the background (TCP-connect probes),
* **load-balanced** with weighted-random selection (lower loss → higher
  chance), and
* **self-repairing** via *graceful rotation* — a degraded pair is drained
  (no new connections) while its live connections finish, then replaced.

Building blocks (mirroring the reference 1:1 so behaviour matches):

  - :class:`PairStats`          — probe-loss + real-traffic-loss for one pair
  - :class:`CombinationExplorer`— gradually discovers / re-verifies pairs
  - :class:`ActivePool`         — keeps ``ACTIVE_SLOTS`` warm pairs; drains weak
  - :class:`ConnectionManager`  — ties it together + a daemon health loop

Design rules kept consistent with the rest of ``core/`` (see ``prober.py``):

* **Network is injectable.** :class:`CombinationExplorer` takes a ``probe_fn``
  so the whole exploration / ranking / draining logic runs deterministically in
  headless tests with **no real sockets**. The default real implementation is
  :func:`tcp_connect_probe` (stdlib ``socket`` only).
* **Single-target path is sacred.** :func:`build_connection_manager` returns
  ``None`` when only one pair exists, so callers fall back to the legacy direct
  mode with zero background threads / overhead.
* **Real traffic outweighs synthetic probes.** The blended score keeps the
  reference ``0.7 * real + 0.3 * probe`` weighting once enough real data exist.
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("snispf.pool")


# A probe is ``(ip, port, timeout) -> bool`` (True == TCP handshake succeeded).
# Injectable so tests never touch the network.
ProbeFn = Callable[[str, int, float], bool]


# ---------------------------------------------------------------------------
# Default real probe (stdlib only; not exercised in sandbox tests)
# ---------------------------------------------------------------------------

def tcp_connect_probe(ip: str, port: int, timeout: float) -> bool:  # pragma: no cover - needs net
    """Real probe: a single TCP connect attempt. ``True`` on a clean handshake.

    Deliberately tiny and dependency-free (mirrors :func:`core.prober.tcp_probe`)
    — a completed three-way handshake means the IP is reachable on *port*.
    """
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PairStats
# ---------------------------------------------------------------------------

class PairStats:
    """Per-(IP, SNI) statistics used to rank and health-check upstream pairs.

    Loss rates blend two sources:
      * *probe* loss — lightweight TCP connect probes from the explorer
      * *real* loss  — actual forwarded connections that failed mid-stream

    Once enough real-traffic data exist (> 10 packets) the score weights real
    loss at 70 % and probe loss at 30 %. Before that it is purely probe-based so
    the pool bootstraps quickly.
    """

    # Minimum probe count before we treat the loss rate as meaningful.
    MIN_PROBES: int = 3

    def __init__(self, ip: str, sni: str) -> None:
        self.ip: str = ip
        self.sni: str = sni

        self.probes_sent: int = 0
        self.probes_recv: int = 0
        self.real_packets_sent: int = 0
        self.real_packets_lost: int = 0

        self.active_connections: int = 0
        self.total_connections: int = 0
        self.alive: bool = True
        # Has this pair been probed at least once?
        self.probed: bool = False
        # Is this pair currently in the active pool?
        self.in_active_pool: bool = False

        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def probe_loss_rate(self) -> float:
        """Fraction of probes that received no response."""
        if self.probes_sent < self.MIN_PROBES:
            return 0.0
        return (self.probes_sent - self.probes_recv) / self.probes_sent

    @property
    def real_loss_rate(self) -> float:
        """Fraction of real forwarded packets that were lost mid-stream."""
        if self.real_packets_sent == 0:
            return 0.0
        return self.real_packets_lost / self.real_packets_sent

    @property
    def combined_loss_rate(self) -> float:
        """Blended loss rate: 70 % real + 30 % probe once real data exist."""
        if self.real_packets_sent > 10:
            return 0.7 * self.real_loss_rate + 0.3 * self.probe_loss_rate
        return self.probe_loss_rate

    @property
    def score(self) -> float:
        """Lower is better.

        * Dead pairs return ``+inf`` so they always rank last.
        * Unknown (not yet probed) pairs return ``0.5`` to give them a chance.
        * Otherwise the combined loss rate is returned directly.
        """
        if not self.alive:
            return float("inf")
        if not self.probed:
            return 0.5          # unknown — eligible for first probe
        return self.combined_loss_rate

    @property
    def is_stable(self) -> bool:
        """True when the pair is alive and has been probed at least once."""
        return self.alive and self.probed

    # ------------------------------------------------------------------
    # Mutation helpers (thread-safe)
    # ------------------------------------------------------------------

    def record_probe(self, success: bool, dead_threshold: float = 0.80) -> None:
        """Update probe counters and flip ``alive`` if needed."""
        with self.lock:
            self.probes_sent += 1
            self.probed = True
            if success:
                self.probes_recv += 1
            if self.probes_sent >= self.MIN_PROBES:
                loss = (self.probes_sent - self.probes_recv) / self.probes_sent
                if loss >= dead_threshold:
                    self.alive = False
                elif self.probes_recv > 0:
                    self.alive = True

    def record_real_packet(self, lost: bool) -> None:
        """Update real-traffic counters for a forwarded connection."""
        with self.lock:
            self.real_packets_sent += 1
            if lost:
                self.real_packets_lost += 1

    def acquire(self) -> None:
        """Mark a new live connection assigned to this pair."""
        with self.lock:
            self.active_connections += 1
            self.total_connections += 1

    def release(self) -> None:
        """Mark a live connection on this pair as finished."""
        with self.lock:
            if self.active_connections > 0:
                self.active_connections -= 1

    def __repr__(self) -> str:
        return (
            f"<PairStats {self.ip} sni={self.sni!r} "
            f"loss={self.combined_loss_rate * 100:.1f}% "
            f"alive={self.alive} active={self.active_connections}>"
        )


# ---------------------------------------------------------------------------
# CombinationExplorer
# ---------------------------------------------------------------------------

class CombinationExplorer:
    """Gradually discovers and health-checks (IP, SNI) combinations.

    Instead of probing all N×M combinations at startup (slow + noisy), the
    explorer works in stages:

    1. **Initial batch** — probes a random sample of ``INITIAL_SAMPLE`` pairs so
       the pool populates quickly.
    2. **Periodic cycles** — re-verifies the top ``VERIFY_TOP`` known pairs and
       explores ``EXPLORE_BATCH`` new ones.
    3. **Reshuffle** — once every combination has been explored, the queue is
       reshuffled and the cycle restarts.

    Probes default to plain TCP connect attempts (no TLS); the ``probe_fn`` is
    injectable so tests run with a deterministic fake.
    """

    INITIAL_SAMPLE: int = 20
    EXPLORE_BATCH: int = 10
    VERIFY_TOP: int = 15

    def __init__(
        self,
        combinations: List[Tuple[str, str]],
        port: int,
        timeout: float,
        probe_count: int,
        loss_threshold: float = 0.20,
        dead_threshold: float = 0.80,
        probe_fn: Optional[ProbeFn] = None,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.probe_count = probe_count
        self.loss_threshold = loss_threshold
        self.dead_threshold = dead_threshold
        # Injectable network primitive (default = real TCP connect).
        self._probe_fn: ProbeFn = probe_fn or tcp_connect_probe

        # Build a stats object for every (ip, sni) pair.
        self.stats: Dict[Tuple[str, str], PairStats] = {
            (ip, sni): PairStats(ip, sni)
            for ip, sni in combinations
        }

        # Queue of unexplored pairs, shuffled for randomness.
        self._unexplored: List[Tuple[str, str]] = list(combinations)
        random.shuffle(self._unexplored)
        self._lock = threading.Lock()

        logger.info(
            "CombinationExplorer initialised: %d IP(s) × SNI(s) = %d pairs",
            len({ip for ip, _ in combinations}),
            len(combinations),
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def all_stats(self) -> List[PairStats]:
        return list(self.stats.values())

    def known_stats(self) -> List[PairStats]:
        """Return pairs that have been probed at least once."""
        return [ps for ps in self.stats.values() if ps.probed]

    def stable_stats(self) -> List[PairStats]:
        """Return pairs that are alive, probed, and below the loss threshold."""
        return [
            ps for ps in self.known_stats()
            if ps.alive and ps.combined_loss_rate < self.loss_threshold
        ]

    # ------------------------------------------------------------------
    # Internal probing helpers
    # ------------------------------------------------------------------

    def _probe_one(self, ps: PairStats) -> None:
        """Run ``probe_count`` connect probes against one pair (via probe_fn)."""
        # Randomise the count slightly to avoid perfectly synchronised bursts.
        count = max(2, self.probe_count + random.randint(-1, 1))
        for _ in range(count):
            try:
                ok = bool(self._probe_fn(ps.ip, self.port, self.timeout))
            except Exception:
                ok = False
            ps.record_probe(success=ok, dead_threshold=self.dead_threshold)

    def _run_probes_parallel(self, pairs: List[PairStats]) -> None:
        """Probe a list of pairs in parallel daemon threads."""
        if not pairs:
            return
        random.shuffle(pairs)
        threads = [
            threading.Thread(target=self._probe_one, args=(ps,), daemon=True)
            for ps in pairs
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ------------------------------------------------------------------
    # Exploration lifecycle
    # ------------------------------------------------------------------

    def initial_explore(self) -> None:
        """Probe the initial random sample to bootstrap the pool."""
        with self._lock:
            batch_keys = self._unexplored[: self.INITIAL_SAMPLE]
            self._unexplored = self._unexplored[self.INITIAL_SAMPLE:]
        batch = [self.stats[k] for k in batch_keys]
        logger.info("Initial probe: %d combinations ...", len(batch))
        self._run_probes_parallel(batch)

    def periodic_explore(self) -> None:
        """Re-verify top known pairs and discover a new batch of unknowns."""
        # Re-verify the best known pairs to catch degraded upstreams early.
        known = sorted(self.known_stats(), key=lambda ps: ps.score)
        to_verify = known[: self.VERIFY_TOP]
        if to_verify:
            logger.debug("Verifying top %d known pairs ...", len(to_verify))
            self._run_probes_parallel(to_verify)

        # Discover a fresh batch from the unexplored queue.
        with self._lock:
            batch_keys = self._unexplored[: self.EXPLORE_BATCH]
            self._unexplored = self._unexplored[self.EXPLORE_BATCH:]
            remaining = len(self._unexplored)

        if batch_keys:
            batch = [self.stats[k] for k in batch_keys]
            logger.debug(
                "Exploring %d new combinations (%d remaining) ...",
                len(batch), remaining,
            )
            self._run_probes_parallel(batch)
        else:
            # All combinations explored — reshuffle for the next cycle.
            logger.info("All combinations explored — reshuffling for next cycle.")
            with self._lock:
                all_keys = list(self.stats.keys())
                random.shuffle(all_keys)
                self._unexplored = all_keys

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return a snapshot dict (counts + ranked rows) for UI / logging."""
        known = self.known_stats()
        stable = [ps for ps in known
                  if ps.alive and ps.combined_loss_rate < self.loss_threshold]
        weak = [ps for ps in known
                if ps.alive and ps.combined_loss_rate >= self.loss_threshold]
        dead = [ps for ps in known if not ps.alive]
        rows = [
            {
                "ip": ps.ip,
                "sni": ps.sni,
                "loss": ps.combined_loss_rate,
                "alive": ps.alive,
                "active": ps.active_connections,
                "in_pool": ps.in_active_pool,
            }
            for ps in sorted(known, key=lambda x: x.score)
        ]
        return {
            "total": len(self.stats),
            "known": len(known),
            "stable": len(stable),
            "weak": len(weak),
            "dead": len(dead),
            "unexplored": len(self.stats) - len(known),
            "rows": rows,
        }

    def print_summary(self) -> None:
        """Log a ranked summary of known (IP, SNI) pairs."""
        s = self.summary()
        logger.info(
            "Pool summary — known=%d  stable=%d  weak=%d  dead=%d  unexplored=%d",
            s["known"], s["stable"], s["weak"], s["dead"], s["unexplored"],
        )
        for row in s["rows"][:8]:
            marker = "*" if row["in_pool"] else " "
            logger.info(
                "  %s %-20s %-25s  loss=%.1f%%  active=%d",
                marker, row["ip"], row["sni"],
                row["loss"] * 100, row["active"],
            )


# ---------------------------------------------------------------------------
# ActivePool
# ---------------------------------------------------------------------------

class ActivePool:
    """Maintains ``slots`` stable (IP, SNI) pairs for serving connections.

    Rules:
    * Always tries to keep ``slots`` pairs in the active set.
    * Pairs whose ``combined_loss_rate`` exceeds ``loss_threshold`` are moved to
      a *draining* list: existing connections finish normally, but no new ones
      are assigned to them.
    * Replacement pairs are chosen with weighted-random sampling (lower loss →
      higher weight) so the best pairs are preferred without being
      deterministically sticky (natural load-balancing, no fixed DPI signature).
    * **No live connection is ever forcefully terminated.**
    """

    def __init__(
        self,
        explorer: CombinationExplorer,
        slots: int,
        loss_threshold: float = 0.20,
    ) -> None:
        self.explorer = explorer
        self.slots = slots
        self.loss_threshold = loss_threshold
        self._pool: List[PairStats] = []
        self._draining: List[PairStats] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Introspection (for UI / tests)
    # ------------------------------------------------------------------

    @property
    def active_pairs(self) -> List[PairStats]:
        with self._lock:
            return list(self._pool)

    @property
    def draining_pairs(self) -> List[PairStats]:
        with self._lock:
            return list(self._draining)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Populate the initial active set from whatever the explorer knows."""
        with self._lock:
            candidates = self.explorer.stable_stats()
            if not candidates:
                candidates = [ps for ps in self.explorer.known_stats() if ps.alive]
            if not candidates:
                candidates = self.explorer.known_stats()
            random.shuffle(candidates)
            self._pool = candidates[: self.slots]
            for ps in self._pool:
                ps.in_active_pool = True
        self._log_pool("INIT")

    def refresh(self) -> None:
        """Rotate weak pairs out and fill empty slots with the best available."""
        with self._lock:
            # Free drained pairs that have no more active connections.
            still_draining: List[PairStats] = []
            for ps in self._draining:
                if ps.active_connections > 0:
                    still_draining.append(ps)
                else:
                    ps.in_active_pool = False
            self._draining = still_draining

            # Move pairs that are now above the loss threshold to draining.
            weak = [
                ps for ps in self._pool
                if not ps.alive or ps.combined_loss_rate >= self.loss_threshold
            ]
            for ps in weak:
                self._pool.remove(ps)
                self._draining.append(ps)

            # Fill empty slots with the best stable alternatives.
            in_use_ids = {id(ps) for ps in self._pool + self._draining}
            candidates = [
                ps for ps in self.explorer.stable_stats()
                if id(ps) not in in_use_ids
            ]
            if not candidates:
                # Fall back to any alive pair we haven't already assigned.
                candidates = [
                    ps for ps in self.explorer.known_stats()
                    if ps.alive and id(ps) not in in_use_ids
                ]

            needed = self.slots - len(self._pool)
            if needed > 0 and candidates:
                chosen = _weighted_sample(candidates, min(needed, len(candidates)))
                for ps in chosen:
                    ps.in_active_pool = True
                    self._pool.append(ps)

        self._log_pool("REFRESH")

    # ------------------------------------------------------------------
    # Per-connection interface
    # ------------------------------------------------------------------

    def pick(self) -> Optional[PairStats]:
        """Return the best pair for the next connection (weighted-random).

        Returns ``None`` only when the explorer knows of no pairs at all.
        """
        with self._lock:
            pool = self._pool if self._pool else self.explorer.known_stats()
            if not pool:
                pool = self.explorer.all_stats()
            if not pool:
                return None
            weights = [1.0 / (ps.combined_loss_rate + 0.01) for ps in pool]
            return random.choices(pool, weights=weights, k=1)[0]

    def report_failure(self, ps: PairStats) -> None:
        """Signal that a real connection on this pair failed mid-stream.

        Records a probe failure so the loss rate rises, then refreshes the pool
        if the pair is now above the threshold.
        """
        ps.record_probe(
            success=False,
            dead_threshold=self.explorer.dead_threshold,
        )
        if not ps.alive or ps.combined_loss_rate >= self.loss_threshold:
            self.refresh()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _log_pool(self, reason: str) -> None:
        logger.info(
            "[Pool/%s] active=%d  draining=%d",
            reason, len(self._pool), len(self._draining),
        )
        for ps in self._pool:
            logger.info(
                "  * %-18s %-25s  loss=%.1f%%  conns=%d",
                ps.ip, ps.sni,
                ps.combined_loss_rate * 100,
                ps.active_connections,
            )
        for ps in self._draining:
            logger.info(
                "  ~ %-18s  draining ...  conns=%d",
                ps.ip, ps.active_connections,
            )


def _weighted_sample(candidates: List[PairStats], k: int) -> List[PairStats]:
    """Pick *k* distinct pairs, weighted by ``1 / (loss + 0.01)`` (low loss wins)."""
    chosen: List[PairStats] = []
    pool = list(candidates)
    weights = [1.0 / (ps.combined_loss_rate + 0.01) for ps in pool]
    for _ in range(min(k, len(pool))):
        pick = random.choices(pool, weights=weights, k=1)[0]
        idx = pool.index(pick)
        chosen.append(pick)
        pool.pop(idx)
        weights.pop(idx)
    return chosen


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Facade that wires :class:`CombinationExplorer` and :class:`ActivePool`.

    Usage in a forwarder::

        pair = manager.pick_pair()
        if pair is not None:
            pair.acquire()
            try:
                ...  # relay data via pair.ip / pair.sni
                pair.record_real_packet(lost=False)   # on first S→C byte
            finally:
                pair.release()
                if connection_failed:
                    manager.report_failure(pair)

    The health loop runs in a daemon thread; start it before the first
    ``pick_pair()`` for the pool to be warm (though ``pick_pair`` is safe to
    call before — it just returns less-vetted pairs).
    """

    def __init__(
        self,
        combinations: List[Tuple[str, str]],
        port: int,
        health_check_interval: float = 30.0,
        health_check_timeout: float = 3.0,
        probe_count: int = 5,
        active_slots: int = 3,
        loss_threshold: float = 0.20,
        dead_threshold: float = 0.80,
        probe_fn: Optional[ProbeFn] = None,
    ) -> None:
        self.interval = health_check_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_check_ts: float = 0.0

        self.explorer = CombinationExplorer(
            combinations=combinations,
            port=port,
            timeout=health_check_timeout,
            probe_count=probe_count,
            loss_threshold=loss_threshold,
            dead_threshold=dead_threshold,
            probe_fn=probe_fn,
        )
        self.pool = ActivePool(
            explorer=self.explorer,
            slots=active_slots,
            loss_threshold=loss_threshold,
        )

    # ------------------------------------------------------------------
    # Health loop (run in a daemon thread)
    # ------------------------------------------------------------------

    def run_health_loop(self) -> None:
        """Blocking health loop — call from a daemon thread."""
        # Bootstrap: probe an initial sample, then populate the pool.
        self.explorer.initial_explore()
        self.pool.initialize()
        self.last_check_ts = time.time()
        self.explorer.print_summary()

        while not self._stop.is_set():
            jitter = random.uniform(-5, 5)
            # Interruptible sleep so stop() returns promptly.
            if self._stop.wait(max(10.0, self.interval + jitter)):
                break
            self.explorer.periodic_explore()
            self.pool.refresh()
            self.last_check_ts = time.time()
            self.explorer.print_summary()

    def start_health_loop(self) -> threading.Thread:
        """Start the health loop in a background daemon thread and return it."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_health_loop,
            name="snispf-health-loop",
            daemon=True,
        )
        self._thread.start()
        logger.info("Connection manager health loop started.")
        return self._thread

    def stop(self) -> None:
        """Signal the health loop to exit (best-effort, non-blocking)."""
        self._stop.set()

    @property
    def seconds_since_check(self) -> Optional[float]:
        """Seconds since the last completed health-check cycle, or None."""
        if not self.last_check_ts:
            return None
        return max(0.0, time.time() - self.last_check_ts)

    # ------------------------------------------------------------------
    # Per-connection interface
    # ------------------------------------------------------------------

    def pick_pair(self) -> Optional[PairStats]:
        """Pick the best (IP, SNI) pair for the next outbound connection."""
        return self.pool.pick()

    def report_failure(self, ps: PairStats) -> None:
        """Notify the pool that a connection on ``ps`` failed."""
        self.pool.report_failure(ps)


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def build_connection_manager(
    config: dict,
    *,
    probe_fn: Optional[ProbeFn] = None,
) -> Optional[ConnectionManager]:
    """Build a :class:`ConnectionManager` from a config dict, or ``None``.

    Returns ``None`` when the config resolves to a single IP / SNI (i.e. the
    legacy ``CONNECT_IP`` / ``FAKE_SNI`` keys, or single-element lists) so the
    caller falls back to the original single-target code path with **no
    background thread**.

    Config may contain the new plural keys ``CONNECT_IPS`` / ``FAKE_SNIS``
    (lists), the legacy singular ``CONNECT_IP`` / ``FAKE_SNI``, or a mix — the
    plural keys take precedence and the singular ones are used as fallback.
    When both lists hold entries the full cartesian product is used.
    """
    ips: List[str] = list(config.get("CONNECT_IPS") or [])
    snis: List[str] = list(config.get("FAKE_SNIS") or [])

    # Accept both plural (new) and singular (legacy) keys.
    if not ips and config.get("CONNECT_IP"):
        ips = [config["CONNECT_IP"]]
    if not snis and config.get("FAKE_SNI"):
        snis = [config["FAKE_SNI"]]

    # Clean + dedupe while preserving order.
    ips = _dedupe([str(x).strip() for x in ips if str(x).strip()])
    snis = _dedupe([str(x).strip() for x in snis if str(x).strip()])

    if not ips or not snis:
        logger.warning("No IPs or SNIs found in config — pool disabled.")
        return None

    if len(ips) == 1 and len(snis) == 1:
        # Single-pair: pool adds overhead with no benefit.
        logger.info("Single IP+SNI detected — pool disabled (using direct mode).")
        return None

    combinations: List[Tuple[str, str]] = [
        (ip, sni) for ip in ips for sni in snis
    ]
    logger.info(
        "Building connection pool: %d IP(s) × %d SNI(s) = %d pairs",
        len(ips), len(snis), len(combinations),
    )

    return ConnectionManager(
        combinations=combinations,
        port=int(config.get("CONNECT_PORT", 443)),
        health_check_interval=float(config.get("HEALTH_CHECK_INTERVAL", 30)),
        health_check_timeout=float(config.get("HEALTH_CHECK_TIMEOUT", 3)),
        probe_count=int(config.get("PROBE_COUNT", 5)),
        active_slots=int(config.get("ACTIVE_SLOTS", 3)),
        loss_threshold=float(config.get("LOSS_THRESHOLD", 0.20)),
        dead_threshold=float(config.get("DEAD_THRESHOLD", 0.80)),
        probe_fn=probe_fn,
    )


def _dedupe(items: List[str]) -> List[str]:
    """Return *items* with duplicates removed, preserving first-seen order."""
    seen: set = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
