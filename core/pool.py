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
import os
import random
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("snispf.pool")


# A probe is ``(ip, port, timeout) -> bool`` (True == TCP handshake succeeded).
# Injectable so tests never touch the network.
ProbeFn = Callable[[str, int, float], bool]

# A spoof probe additionally takes the fake SNI: ``(ip, port, timeout, sni)``
# and returns True only when a spoofed ClientHello carrying ``sni`` is confirmed
# to survive the DPI (see :func:`spoof_handshake_probe`).
SpoofProbeFn = Callable[[str, int, float, str], bool]


# --- per-IP failover thresholds (ported 1:1 from the reference forwarder) ---
# How many failures within :data:`FAILOVER_WINDOW` seconds before an IP is
# considered "blocked" and the forwarder should fail over to another route.
FAILOVER_THRESHOLD: int = 3
# Rapid-failure window in seconds. Failures older than this are pruned, so a
# slow trickle of unrelated errors never trips the failover.
FAILOVER_WINDOW: float = 30.0


# --- background-optimiser promotion policy (redesign v2) --------------------
# CRITICAL LESSON FROM THE FIELD: a clean TCP probe does NOT mean a route can
# carry a spoofed ClientHello past DPI. Many CDN IPs accept the TCP handshake
# (probe loss = 0) yet the fake-SNI injection times out — so promoting purely on
# probe loss swapped a *working* route for probe-clean-but-DPI-dead ones and
# flooded the log with TimeoutError. Therefore the promoter is now governed by
# REAL TRAFFIC, not probes:
#
#   * We NEVER swap away from a route that is serving real traffic successfully
#     (a healthy incumbent is the strongest evidence we have — leave it alone).
#   * We only swap when the current route is genuinely BROKEN (rapid real
#     failures via the ConnectionTracker), and only TO a candidate that is not
#     itself failing — preferring one with proven real-traffic success.
#   * Probe data is used only as a weak tie-breaker / liveness gate, never as a
#     reason to disturb a working route.
PROMOTE_MARGIN: float = 0.15          # (legacy) probe-loss margin, tie-break only
PROMOTE_MIN_PROBES: int = 3           # min probes before a candidate is trusted
# A candidate is "real-proven" once it has carried at least this many successful
# real connections with an acceptable real-loss rate.
REAL_PROOF_MIN_PACKETS: int = 1
REAL_PROOF_MAX_LOSS: float = 0.50


# ---------------------------------------------------------------------------
# Default real probe (stdlib only; not exercised in sandbox tests)
# ---------------------------------------------------------------------------

def tcp_connect_probe(ip: str, port: int, timeout: float) -> bool:  # pragma: no cover - needs net
    """Real probe: a single TCP connect attempt. ``True`` on a clean handshake.

    Deliberately tiny and dependency-free (mirrors :func:`core.prober.tcp_probe`)
    — a completed three-way handshake means the IP is reachable on *port*.

    NOTE: a clean three-way handshake proves only *reachability*, **not** that a
    spoofed ClientHello survives the DPI. That is exactly the trap that produced
    the "everything probes clean but nothing actually works" churn: a CDN edge
    accepts the TCP connection (loss=0) yet times out the moment a fake SNI is
    injected. Prefer :func:`spoof_handshake_probe` for promotion decisions.
    """
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def spoof_handshake_probe(ip: str, port: int, timeout: float,
                          fake_sni: str) -> bool:  # pragma: no cover - needs net
    """High-confidence probe: does a *spoofed* ClientHello survive the DPI?

    This is the proof the promoter needs before it dares swap the live route
    (the user's golden rule: *"never switch without full confidence"*). A plain
    TCP connect lies — it says "reachable" for routes that the DPI silently
    kills as soon as a fake SNI appears on the wire. So instead we replay the
    exact decoy the spoofer sends and watch what the network does with it:

      1. TCP-connect to ``ip:port`` (must complete the three-way handshake).
      2. Send a TLS ClientHello carrying the **fake** ``fake_sni`` — byte-for-byte
         the same decoy :class:`ClientHelloMaker` builds for live traffic.
      3. Watch the socket for a short window:

         * server sends **any TLS bytes back** (ServerHello / Alert / data) →
           the decoy reached a real TLS endpoint untouched → **route is good**.
         * connection is **reset / closed** right after the decoy, or we get a
           **timeout with zero bytes** → the DPI swallowed the fake SNI →
           **route is dead for spoofing** even though TCP connected fine.

    Crucially this needs **no WinDivert and no Admin** — we own this socket and
    speak raw bytes on it directly. It never touches the live forwarder path
    (xray ↔ spoofer), so normal pinging and ordinary configs are completely
    unaffected: this only runs inside the background explorer's probe threads.

    Returns ``True`` only when the spoofed handshake is *confirmed* to pass.
    Any error, reset, or empty timeout is a conservative ``False`` so an
    unproven route can never masquerade as a promotion candidate.
    """
    sock = None
    try:
        # Local import: utils is always present at runtime, but keeping the
        # import here means headless pool tests (which inject their own
        # probe_fn and never call this) don't depend on packet templates.
        from utils.packet_templates import ClientHelloMaker

        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.settimeout(timeout)
        # Disable Nagle so the decoy goes out as its own segment, exactly like
        # the live injector emits it.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        # Encode exactly like the live spoofer (main.py uses a plain ``.encode()``
        # i.e. UTF-8) so the decoy we replay is byte-for-byte what real traffic
        # sends — anything else would validate a different ClientHello than the
        # one the forwarder actually emits.
        fake_sni_bytes = str(fake_sni).strip().encode() if fake_sni else b""
        if not fake_sni_bytes:
            # No fake SNI to validate — fall back to reachability only.
            return True

        client_hello = ClientHelloMaker.get_client_hello_with(
            os.urandom(32), os.urandom(32), fake_sni_bytes, os.urandom(32))
        sock.sendall(client_hello)

        # A real TLS server answers a ClientHello within a round-trip. If the
        # DPI is going to kill the connection for the fake SNI it does so now:
        # either an RST (recv raises / returns b"") or dead silence (timeout).
        try:
            data = sock.recv(16)
        except socket.timeout:
            return False
        except OSError:
            return False
        # b"" == orderly close (RST/FIN) right after our decoy → blocked.
        # Any TLS record back (0x16 handshake, 0x15 alert, 0x17 appdata) → passed.
        return len(data) > 0
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


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
        # Spoofed-ClientHello validation (see :func:`spoof_handshake_probe`).
        # These count the high-confidence probes that actually replay a fake
        # SNI and confirm it survives the DPI — the *only* synthetic signal
        # trustworthy enough to promote a route on. ``record_probe`` (plain TCP)
        # never touches these.
        self.spoof_probes_sent: int = 0
        self.spoof_probes_ok: int = 0

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

    @property
    def spoof_proven(self) -> bool:
        """True when a *spoofed* ClientHello has been confirmed to survive DPI.

        Filled by :meth:`record_spoof_probe` from the background explorer's
        :func:`spoof_handshake_probe`. Unlike a plain TCP probe, this replays
        the exact fake-SNI decoy the live spoofer sends, so a pass is strong
        evidence the route works for spoofing — *before* any real traffic has
        ever touched it. Requires at least one confirmed pass and no observed
        block among the recent spoof probes.
        """
        if self.spoof_probes_sent < REAL_PROOF_MIN_PACKETS:
            return False
        # Demand a clean record: at least one confirmed pass, and the pass rate
        # must clear the same bar real traffic must (1 - MAX_LOSS).
        if self.spoof_probes_ok <= 0:
            return False
        pass_rate = self.spoof_probes_ok / self.spoof_probes_sent
        return pass_rate >= (1.0 - REAL_PROOF_MAX_LOSS)

    @property
    def real_proven(self) -> bool:
        """True only when this route is proven by **real forwarded traffic**.

        Hard-won lesson (the 21:33–21:37 churn log): a spoof-handshake probe is
        NOT enough. ``spoof_handshake_probe`` opens a *direct* socket to the CDN
        IP and sends a fake-SNI ClientHello — but a Cloudflare edge answers TLS
        bytes to *almost any* ClientHello on :443, so that probe passes for
        nearly every reachable CF IP. It therefore says nothing about whether
        the **live, WinDivert-injected** path (xray → spoofer → CDN) actually
        works. Trusting it made dozens of dead routes look "proven", and the
        promoter churned through them (xbox→nodejs→apple→deepseek→gmail) while
        every one still failed at the real injection layer.

        So promotion now demands the *only* trustworthy signal: this exact route
        carried **real forwarded traffic** with an acceptable loss rate. The
        spoof probe is kept purely as a liveness / tie-break hint (see
        :attr:`spoof_proven` and :meth:`_candidate_is_better`) and can never, on
        its own, authorise a swap of the live route.

        This is the gate the promoter trusts — the user's golden rule: never
        switch without full confidence.
        """
        if self.real_packets_sent < REAL_PROOF_MIN_PACKETS:
            return False
        return self.real_loss_rate <= REAL_PROOF_MAX_LOSS

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

    def record_spoof_probe(self, success: bool,
                           dead_threshold: float = 0.80) -> None:
        """Record the outcome of a spoofed-ClientHello probe (high confidence).

        This is the signal that lets a route become :attr:`real_proven` before
        it has ever carried live traffic. It also counts as a regular probe for
        liveness/scoring (so a route that connects-but-blocks correctly ranks
        worse than one whose decoy passes), but a *failed* spoof probe on an
        otherwise TCP-reachable IP still records as a probe miss so the route
        is not mistaken for healthy.
        """
        with self.lock:
            self.spoof_probes_sent += 1
            if success:
                self.spoof_probes_ok += 1
            # Mirror into the plain-probe counters so liveness + loss scoring
            # reflect the *spoof* outcome, not a misleading bare TCP connect.
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
# ConnectionTracker — per-IP rapid-failure detection (failover trigger)
# ---------------------------------------------------------------------------

class ConnectionTracker:
    """Tracks per-IP connection failures within a rolling time window.

    Ported from the reference ``forwarder.ConnectionTracker`` so the
    forwarder can detect an upstream IP that has *suddenly* started blocking
    (e.g. a CDN edge that got poisoned) independently of the slower,
    probe-based pool scoring.

    The rule is simple and matches the reference exactly:

      * :meth:`record_failure` appends ``time.monotonic()`` for the IP and
        prunes anything older than :data:`FAILOVER_WINDOW` seconds, returning
        the live failure count.
      * :meth:`record_success` clears the IP's failure history (a working
        connection proves the route is healthy again) and bumps a success
        counter for diagnostics.
      * :meth:`should_failover` is ``True`` once an IP has accumulated
        :data:`FAILOVER_THRESHOLD` failures inside the window.

    Thread-safe: every mutation takes an internal lock so the forwarder's
    async tasks (which may run on different threads via ``run_in_executor``)
    and the UI poller can share one tracker.
    """

    def __init__(
        self,
        threshold: int = FAILOVER_THRESHOLD,
        window: float = FAILOVER_WINDOW,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.threshold = int(threshold)
        self.window = float(window)
        # Injectable clock so tests can advance time deterministically.
        self._clock: Callable[[], float] = clock or time.monotonic
        self._failures: Dict[str, List[float]] = {}
        self._successes: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - self.window
        kept = [t for t in self._failures.get(ip, []) if t > cutoff]
        if kept:
            self._failures[ip] = kept
        else:
            self._failures.pop(ip, None)

    def record_failure(self, ip: str) -> int:
        """Record a failure for *ip*; return live failures inside the window."""
        if not ip:
            return 0
        with self._lock:
            now = self._clock()
            self._failures.setdefault(ip, []).append(now)
            self._prune(ip, now)
            return len(self._failures.get(ip, []))

    def record_success(self, ip: str) -> None:
        """Record a success: clears failure history + bumps success count."""
        if not ip:
            return
        with self._lock:
            self._failures.pop(ip, None)
            self._successes[ip] = self._successes.get(ip, 0) + 1

    def should_failover(self, ip: str) -> bool:
        """``True`` when *ip* has hit the failover threshold in the window."""
        if not ip:
            return False
        with self._lock:
            now = self._clock()
            self._prune(ip, now)
            return len(self._failures.get(ip, [])) >= self.threshold

    def failure_count(self, ip: str) -> int:
        """Live failure count for *ip* inside the window (0 if none)."""
        if not ip:
            return 0
        with self._lock:
            now = self._clock()
            self._prune(ip, now)
            return len(self._failures.get(ip, []))

    def success_count(self, ip: str) -> int:
        """Cumulative successful connections recorded for *ip*."""
        with self._lock:
            return int(self._successes.get(ip, 0))

    def clear(self, ip: str) -> None:
        """Forget all failure history for *ip* (e.g. after manual rotation)."""
        with self._lock:
            self._failures.pop(ip, None)

    def reset(self) -> None:
        """Forget everything (used when a new session starts)."""
        with self._lock:
            self._failures.clear()
            self._successes.clear()

    def snapshot(self) -> dict:
        """Diagnostic snapshot of per-IP failure/success state for the UI."""
        with self._lock:
            now = self._clock()
            ips = set(self._failures) | set(self._successes)
            rows = []
            for ip in sorted(ips):
                live = len([t for t in self._failures.get(ip, [])
                            if t > now - self.window])
                rows.append({
                    "ip": ip,
                    "failures": live,
                    "successes": int(self._successes.get(ip, 0)),
                    "blocked": live >= self.threshold,
                })
            return {
                "threshold": self.threshold,
                "window": self.window,
                "ips": rows,
            }


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
        spoof_probe_fn: Optional["SpoofProbeFn"] = None,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.probe_count = probe_count
        self.loss_threshold = loss_threshold
        self.dead_threshold = dead_threshold
        # Injectable network primitive (default = real TCP connect).
        self._probe_fn: ProbeFn = probe_fn or tcp_connect_probe
        # High-confidence probe: replays a fake-SNI ClientHello to confirm the
        # decoy survives the DPI. When present (the real runtime default), the
        # explorer uses THIS for promotion-grade evidence; bare TCP is only a
        # fallback. Tests inject their own deterministic ``probe_fn`` and leave
        # this ``None`` so they never touch the network.
        self._spoof_probe_fn: Optional["SpoofProbeFn"] = spoof_probe_fn

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
        """Run ``probe_count`` probes against one pair.

        When a spoof probe is configured (the real runtime), it replays a fake
        ``ps.sni`` ClientHello and records the outcome as *high-confidence*
        evidence (:meth:`PairStats.record_spoof_probe`) — the only synthetic
        signal the promoter will swap a live route for. Otherwise it falls back
        to the plain TCP-connect ``probe_fn`` (used by headless tests, and as a
        safety net if the spoof probe can't run).
        """
        # Randomise the count slightly to avoid perfectly synchronised bursts.
        count = max(2, self.probe_count + random.randint(-1, 1))
        for _ in range(count):
            if self._spoof_probe_fn is not None:
                try:
                    ok = bool(self._spoof_probe_fn(
                        ps.ip, self.port, self.timeout, ps.sni))
                except Exception:
                    ok = False
                ps.record_spoof_probe(success=ok,
                                      dead_threshold=self.dead_threshold)
            else:
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
        spoof_probe_fn: Optional[SpoofProbeFn] = None,
    ) -> None:
        self.interval = health_check_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_check_ts: float = 0.0

        # Default to the real spoof-handshake probe so the live runtime gets
        # promotion-grade evidence out of the box. Tests pass an explicit
        # ``probe_fn`` and leave ``spoof_probe_fn=None`` *only* if they also opt
        # out — but since most tests inject ``probe_fn`` and expect plain-TCP
        # semantics, we only enable the spoof default when no ``probe_fn`` was
        # given (i.e. the real runtime) OR a spoof fn is explicitly supplied.
        if spoof_probe_fn is None and probe_fn is None:
            spoof_probe_fn = spoof_handshake_probe

        self.explorer = CombinationExplorer(
            combinations=combinations,
            port=port,
            timeout=health_check_timeout,
            probe_count=probe_count,
            loss_threshold=loss_threshold,
            dead_threshold=dead_threshold,
            probe_fn=probe_fn,
            spoof_probe_fn=spoof_probe_fn,
        )
        self.pool = ActivePool(
            explorer=self.explorer,
            slots=active_slots,
            loss_threshold=loss_threshold,
        )
        # Per-IP rapid-failure tracker (7.8). The forwarder records each
        # connect outcome here; ``should_failover`` lets it skip a route that
        # is failing in bursts without waiting for the slower probe cycle.
        self.tracker = ConnectionTracker()

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
        """Pick the best (IP, SNI) pair, skipping IPs in rapid-failover.

        Tries up to a few weighted picks and returns the first whose IP is not
        currently tripped in the :class:`ConnectionTracker`. Falls back to the
        last pick if *every* candidate is in failover (better a degraded route
        than none — the pool/probe loop will keep draining the worst).
        """
        last: Optional[PairStats] = None
        # Try several weighted picks; the more routes exist the more attempts we
        # allow so a single bad IP is reliably skipped when alternatives remain.
        attempts = max(8, len(self.pool.active_pairs) * 4)
        for _ in range(attempts):
            ps = self.pool.pick()
            if ps is None:
                return last
            last = ps
            if not self.tracker.should_failover(ps.ip):
                return ps
        return last

    def report_failure(self, ps: PairStats) -> None:
        """Notify the pool + tracker that a connection on ``ps`` failed."""
        self.pool.report_failure(ps)
        self.tracker.record_failure(ps.ip)

    def report_success(self, ps: PairStats) -> None:
        """Notify the tracker that a connection on ``ps`` succeeded."""
        self.tracker.record_success(ps.ip)

    # ------------------------------------------------------------------
    # Background-optimiser interface (redesign)
    # ------------------------------------------------------------------

    def best_candidate(self) -> Optional[PairStats]:
        """Return the single best candidate the optimiser has found, or None.

        Ranking is governed by REAL TRAFFIC first, probes only as a tie-break:

          1. A candidate that has *proven* itself with successful real traffic
             (:attr:`PairStats.real_proven`) always beats one that has only been
             TCP-probed — a clean probe does NOT mean a spoofed ClientHello
             survives DPI, so real-traffic proof is the only trustworthy signal.
          2. Among candidates of the same proof tier, the lower combined-loss
             wins.

        We still skip dead/unprobed pairs and any IP currently tripped in the
        rapid-failover tracker, so we never hand the promoter a route that is
        already known to be failing.
        """
        best: Optional[PairStats] = None
        for ps in self.explorer.stable_stats():
            if ps.probes_sent < PROMOTE_MIN_PROBES:
                continue
            if self.tracker.should_failover(ps.ip):
                continue
            if best is None or self._candidate_is_better(ps, best):
                best = ps
        return best

    @staticmethod
    def _candidate_is_better(cand: PairStats, incumbent: PairStats) -> bool:
        """True if ``cand`` should outrank ``incumbent`` as the best candidate.

        Proof tiers, strongest first:
          1. **real_proven** — proven by real forwarded traffic. Dominates all.
          2. **spoof_proven** — the fake-SNI decoy reached a TLS endpoint on a
             direct socket. A weak hint (a CDN answers almost any ClientHello)
             so it only ranks *within* the unproven tier; it never promotes a
             route by itself (see :meth:`find_better_route`).
          3. combined loss — final tie-break.
        """
        if cand.real_proven != incumbent.real_proven:
            return cand.real_proven  # real-traffic proof beats everything
        if cand.spoof_proven != incumbent.spoof_proven:
            return cand.spoof_proven  # within the same proof tier, decoy-pass wins
        return cand.combined_loss_rate < incumbent.combined_loss_rate

    def lookup_pair(self, ip: str, sni: str) -> Optional[PairStats]:
        """Return the :class:`PairStats` for an (ip, sni) if it lives in the pool."""
        return self.explorer.stats.get((ip, sni))

    def ensure_pair(self, ip: str, sni: str) -> PairStats:
        """Return the :class:`PairStats` for ``(ip, sni)``, creating it if absent.

        The user's confirmed primary route is frequently NOT one of the
        cartesian (IPs × SNIs) pool combinations, so :meth:`lookup_pair` returns
        ``None`` for it. Without a stats object the spoofer can't attribute the
        primary route's real-traffic successes/failures to anything — meaning the
        tracker never learns the primary actually works and the promoter has no
        real-traffic proof to weigh. ``ensure_pair`` guarantees the primary (or
        any route we adopt) is tracked, so real outcomes are always recorded.
        """
        key = (ip, sni)
        existing = self.explorer.stats.get(key)
        if existing is not None:
            return existing
        ps = PairStats(ip, sni)
        # register so probes/scoring/lookup all see it from now on
        self.explorer.stats[key] = ps
        return ps

    def find_better_route(
        self,
        current_ip: str,
        current_sni: str,
        *,
        current_healthy: bool,
    ) -> Optional[PairStats]:
        """Real-traffic-governed promotion decision for the background promoter.

        THE GOLDEN RULE (learned the hard way from the TimeoutError flood):
        **a working route is the strongest evidence we have — never disturb it.**

        * **Healthy incumbent** (``current_healthy=True``): the live route is
          carrying real traffic without rapid failures. We return ``None`` — no
          swap, full stop. A clean TCP probe on some other IP does NOT prove it
          can carry a spoofed ClientHello past DPI, so we refuse to gamble a
          working route for a probe-only "upgrade". This kills the route-churn /
          TimeoutError-flood regression.

        * **Broken incumbent** (``current_healthy=False``): the current route is
          genuinely failing (rapid real failures via the tracker). Only now do we
          consider a swap — and **only to a route we are confident in**. Per the
          user's rule ("never switch without full confidence"), the candidate
          MUST be :attr:`PairStats.real_proven` — i.e. proven either by real
          forwarded traffic OR by a spoofed-handshake probe that confirmed the
          fake SNI survives the DPI. A merely TCP-reachable candidate is NOT
          good enough: that is exactly what produced the endless churn through
          probe-clean-but-DPI-dead CDN edges. If no proven candidate exists we
          return ``None`` and stay put rather than gamble on an unproven route.

        Returns ``None`` when no swap is warranted.
        """
        # Healthy route → leave it alone. This is the whole fix.
        if current_healthy:
            return None

        cand = self.best_candidate()
        if cand is None:
            return None
        # CONFIDENCE GATE: only ever swap to a route proven to carry a spoofed
        # handshake (real traffic or spoof probe). Never churn onto a route that
        # merely completed a bare TCP connect.
        if not cand.real_proven:
            return None
        # never "swap" to the route we're already on
        if cand.ip == current_ip and cand.sni == current_sni:
            return None
        return cand


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def build_connection_manager(
    config: dict,
    *,
    probe_fn: Optional[ProbeFn] = None,
    spoof_probe_fn: Optional[SpoofProbeFn] = None,
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
        spoof_probe_fn=spoof_probe_fn,
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


# ---------------------------------------------------------------------------
# Export helpers (7.10 — Domain/SNI list export)
# ---------------------------------------------------------------------------

def export_sni_list(
    snis: List[str],
    filepath: str,
    *,
    header: str = "Verified fake-SNI domains",
) -> int:
    """Write a clean, de-duplicated SNI list to *filepath*; return the count.

    Ported (and adapted) from the reference ``DomainChecker.export_sni_list``.
    Where the reference filtered ``DomainResult`` objects, our pool already
    works with plain SNI strings, so this trims/dedupes them, writes a small
    provenance header, and returns how many were written. Blank/whitespace
    entries are dropped. The file is UTF-8 and newline-terminated so it can be
    pasted straight back into the multi-SNI box in Settings.
    """
    clean = _dedupe([str(s).strip() for s in (snis or []) if str(s).strip()])
    lines = [
        f"# {header}",
        "# Generated by the SNI route-pool exporter",
        f"# Total: {len(clean)} entries",
        "",
    ]
    lines.extend(clean)
    text = "\n".join(lines) + ("\n" if clean else "")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(clean)


def export_sni_pairs(
    pairs: List[Tuple[str, str, str]],
    filepath: str,
    *,
    header: str = "Verified SNI/IP pairs",
) -> int:
    """Write an ``IP <TAB> SNI <TAB> status`` list to *filepath*; return count.

    Each entry is a ``(ip, sni, status)`` triple — exactly what the live
    SNI/IP scanner produces. This is the IP-paired replacement for the old
    bare-SNI export (issue: "خروجیِ فهرستِ SNI، IP مناسب ندارد"). Blank rows are
    dropped and ``(ip, sni)`` duplicates are removed (first-seen wins). The file
    is UTF-8, TAB-separated, so it pastes straight back into the pool lists or a
    spreadsheet.
    """
    seen: set = set()
    clean: List[Tuple[str, str, str]] = []
    for row in (pairs or []):
        ip = str(row[0]).strip() if len(row) > 0 else ""
        sni = str(row[1]).strip() if len(row) > 1 else ""
        status = str(row[2]).strip() if len(row) > 2 else ""
        if not ip or not sni:
            continue
        key = (ip.lower(), sni.lower())
        if key in seen:
            continue
        seen.add(key)
        clean.append((ip, sni, status))
    lines = [
        f"# {header}",
        "# IP <TAB> SNI <TAB> status — generated by the SNI route-pool scanner",
        f"# Total: {len(clean)} entries",
        "",
    ]
    for ip, sni, status in clean:
        if status:
            lines.append(f"{ip}\t{sni}\t{status}")
        else:
            lines.append(f"{ip}\t{sni}")
    text = "\n".join(lines) + ("\n" if clean else "")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(clean)


def parse_sni_pairs_text(text: str) -> List[Tuple[str, str]]:
    """Parse an exported SNI/IP list back into ``(ip, sni)`` pairs.

    This is the inverse of :func:`export_sni_pairs` so a user can re-import a
    list they (or someone else) generated, instead of copy-pasting. It is
    deliberately forgiving about the exact layout so hand-edited or third-party
    files still load:

      * ``#`` comment lines and blank lines are ignored.
      * Columns may be separated by **TAB, comma, or whitespace**.
      * The first two non-status columns are read as ``IP`` and ``SNI``. We
        detect which one is the IP (``a.b.c.d`` / contains ``:`` for IPv6) so a
        ``SNI<TAB>IP`` file loads just as well as ``IP<TAB>SNI``; if neither
        column looks like an IP we assume the export order ``IP, SNI``.
      * A trailing ``status`` column (e.g. ``ok`` / ``سالم``) is ignored.

    Returns de-duplicated ``(ip, sni)`` tuples (case-insensitive, first wins)
    with blanks dropped — ready to merge straight into ``sni_ip_pairs``.
    """
    import re as _re

    def _looks_like_ip(token: str) -> bool:
        t = token.strip()
        if not t:
            return False
        # IPv4 dotted quad
        if _re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", t):
            return all(0 <= int(p) <= 255 for p in t.split("."))
        # IPv6 (very loose) — has a colon and only hex/colon chars
        if ":" in t and _re.fullmatch(r"[0-9A-Fa-f:]+", t):
            return True
        return False

    seen: set = set()
    out: List[Tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # split on TAB / comma first; fall back to any whitespace run
        if "\t" in line:
            cols = [c.strip() for c in line.split("\t")]
        elif "," in line:
            cols = [c.strip() for c in line.split(",")]
        else:
            cols = [c.strip() for c in line.split()]
        cols = [c for c in cols if c]
        if len(cols) < 2:
            continue
        a, b = cols[0], cols[1]
        # decide which column is the IP
        if _looks_like_ip(a) and not _looks_like_ip(b):
            ip, sni = a, b
        elif _looks_like_ip(b) and not _looks_like_ip(a):
            ip, sni = b, a
        else:
            ip, sni = a, b  # export order: IP, SNI
        if not ip or not sni:
            continue
        key = (ip.lower(), sni.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((ip, sni))
    return out


def import_sni_pairs(
    filepath: str,
    existing: Optional[List[dict]] = None,
) -> Tuple[List[dict], int]:
    """Read *filepath* and merge its ``(ip, sni)`` pairs into *existing*.

    Reads a file produced by :func:`export_sni_pairs` (or a compatible list) via
    :func:`parse_sni_pairs_text`, then appends any pairs not already present in
    *existing* (the user's ``sni_ip_pairs`` list of ``{"sni", "ip"}`` dicts),
    de-duplicated case-insensitively.

    Returns ``(merged_list, added_count)`` — ``merged_list`` is a new list ready
    to store back, and ``added_count`` is how many NEW pairs were imported (0
    means everything was already in the list).
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pairs = parse_sni_pairs_text(text)

    merged: List[dict] = list(existing or [])
    keys: set = set()
    for p in merged:
        ip = str(p.get("ip", "")).strip().lower()
        sni = str(p.get("sni", "")).strip().lower()
        keys.add((ip, sni))

    added = 0
    for ip, sni in pairs:
        key = (ip.lower(), sni.lower())
        if key in keys:
            continue
        keys.add(key)
        merged.append({"sni": sni, "ip": ip})
        added += 1
    return merged, added


def export_routes(
    manager_or_config,
    filepath: str,
    *,
    stable_only: bool = False,
) -> int:
    """Export the pool's ``(IP, SNI)`` routes to *filepath*; return the count.

    Accepts either a live :class:`ConnectionManager` (exports its current
    explorer stats, optionally only the *stable* pairs) or a plain config dict
    (exports the configured cartesian product). Each line is ``IP\\tSNI`` with a
    trailing ``loss%`` column when live stats are available, so the user can
    eyeball which routes are healthy. Returns the number of routes written.
    """
    rows: List[Tuple[str, str, Optional[float]]] = []

    explorer = None
    if isinstance(manager_or_config, ConnectionManager):
        explorer = manager_or_config.explorer
    elif hasattr(manager_or_config, "explorer"):
        explorer = getattr(manager_or_config, "explorer")

    if explorer is not None:
        stats = explorer.stable_stats() if stable_only else explorer.all_stats()
        for ps in sorted(stats, key=lambda p: p.score):
            loss = ps.combined_loss_rate if ps.probed else None
            rows.append((ps.ip, ps.sni, loss))
    elif isinstance(manager_or_config, dict):
        cfg = manager_or_config
        ips = _dedupe([str(x).strip() for x in (cfg.get("CONNECT_IPS") or [])
                       if str(x).strip()]) or (
            [str(cfg["CONNECT_IP"]).strip()] if cfg.get("CONNECT_IP") else [])
        snis = _dedupe([str(x).strip() for x in (cfg.get("FAKE_SNIS") or [])
                        if str(x).strip()]) or (
            [str(cfg["FAKE_SNI"]).strip()] if cfg.get("FAKE_SNI") else [])
        for ip in ips:
            for sni in snis:
                rows.append((ip, sni, None))
    else:
        raise TypeError(
            "export_routes expects a ConnectionManager or config dict")

    lines = [
        "# SNI route-pool export (IP <TAB> SNI [<TAB> loss%])",
        f"# Total: {len(rows)} routes",
        "",
    ]
    for ip, sni, loss in rows:
        if loss is None:
            lines.append(f"{ip}\t{sni}")
        else:
            lines.append(f"{ip}\t{sni}\t{loss * 100:.1f}%")
    text = "\n".join(lines) + ("\n" if rows else "")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(rows)


# ---------------------------------------------------------------------------
# Live SNI/IP scanner (issue: manual "شروع تست" button)
# ---------------------------------------------------------------------------
#
# Unlike the background :class:`CombinationExplorer` (which feeds the automatic
# promoter), this is a *one-shot, user-driven* sweep. The user clicks the
# "Start Test" button next to the pool list, picks ONE spoof config as the
# reference, and we replay the spoofer's decoy ClientHello (see
# :func:`spoof_handshake_probe`) against every candidate (IP, SNI) pair —
# streaming each verdict live so the GUI can show ✓/✗ next to it and offer an
# "add to my sni/ip list" action.
#
# Crucially this does **not** swap the live route. Per the user's golden rule
# the scan only *discovers* good pairs; the user decides which ones to add to
# their reusable ``sni_ip_pairs`` list (see ConfigStore). One scan is shared
# across every config — a route that survives DPI for one spoof config works
# for ~90% of the others, so we never re-scan from scratch per config.


class ScanCandidate:
    """One (IP, SNI) pair queued for the live scan, with its verdict."""

    PENDING = "pending"
    TESTING = "testing"
    OK = "ok"
    FAIL = "fail"

    __slots__ = ("ip", "sni", "status", "latency_ms")

    def __init__(self, ip: str, sni: str) -> None:
        self.ip = str(ip).strip()
        self.sni = str(sni).strip()
        self.status = self.PENDING
        self.latency_ms: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "ip": self.ip,
            "sni": self.sni,
            "status": self.status,
            "latency_ms": self.latency_ms,
        }


class SniIpScanner:
    """One-shot, stoppable sweep of (IP, SNI) pairs via the spoof handshake.

    Network is injectable (``spoof_probe_fn``) so the whole queue / ranking /
    callback logic runs deterministically in headless tests with no sockets —
    same contract as :class:`CombinationExplorer`.

    Callbacks (all optional, always called from the worker thread):
      * ``on_result(candidate_dict)`` — fired once per pair as its verdict lands
        (and once with status TESTING just before probing it).
      * ``on_progress(done, total)`` — fired after each pair completes.
      * ``on_done(ok_count, total)`` — fired once when the sweep finishes/stops.
      * ``on_log(text)``             — human-readable progress lines.
    """

    def __init__(
        self,
        candidates: List[Tuple[str, str]],
        *,
        port: int = 443,
        timeout: float = 4.0,
        workers: int = 8,
        spoof_probe_fn: Optional[SpoofProbeFn] = None,
        on_result: Optional[Callable[[dict], None]] = None,
        on_results_batch: Optional[Callable[[list], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_done: Optional[Callable[[int, int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        batch_interval: float = 0.10,
        batch_size: int = 40,
    ) -> None:
        # de-dupe while preserving order; drop blanks
        seen: set = set()
        self._cands: List[ScanCandidate] = []
        for ip, sni in candidates:
            ip = str(ip).strip()
            sni = str(sni).strip()
            if not ip or not sni:
                continue
            key = (ip.lower(), sni.lower())
            if key in seen:
                continue
            seen.add(key)
            self._cands.append(ScanCandidate(ip, sni))

        self.port = int(port)
        self.timeout = float(timeout)
        self.workers = max(1, int(workers))
        self._probe = spoof_probe_fn or spoof_handshake_probe
        self.on_result = on_result
        # on_results_batch(list[dict]) — preferred over on_result: verdicts are
        # COALESCED and delivered in batches so the GUI repaints a handful of
        # times per second instead of once per probe. Spraying one Qt signal per
        # probe (×2 with the old "testing" event) flooded the event loop and
        # froze the window the moment the user moved the mouse. Batching fixes
        # that freeze. on_result is kept for back-compat / headless tests.
        self.on_results_batch = on_results_batch
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_log = on_log
        self._batch_interval = max(0.02, float(batch_interval))
        self._batch_size = max(1, int(batch_size))

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._done = 0
        self._ok = 0

    # -- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def total(self) -> int:
        return len(self._cands)

    def candidates(self) -> List[dict]:
        return [c.as_dict() for c in self._cands]

    def _emit(self, cb, *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            pass

    def run(self) -> None:
        """Probe every candidate, streaming verdicts.

        Threading contract (this is what fixes the GUI hang/crash): the probe
        sockets run on a pool of **daemon** worker threads, but **every
        callback is emitted from the thread that called ``run()``** — i.e. the
        host's QThread — never from a worker. Emitting a Qt signal from a raw
        (non-Qt) thread is undefined behaviour in PySide6 and was what froze /
        crashed the window. Workers only push plain dicts onto a queue; this
        loop drains the queue and emits.

        Stop is *immediate*: when :meth:`stop` is set we break out of the drain
        loop at once and return without waiting for blocked sockets. The worker
        threads are daemons and unwind on their own short connect timeout, so
        the window can close instantly instead of hanging on ``wait()``.
        """
        total = len(self._cands)
        if total == 0:
            self._emit(self.on_log, "هیچ جفت IP/SNI برای آزمایش وجود ندارد.")
            self._emit(self.on_done, 0, 0)
            return

        self._emit(self.on_log,
                   "شروع آزمایش %d جفت IP/SNI…" % total)

        import queue as _queue
        # index queue feeding the workers
        jobs: "_queue.Queue[int]" = _queue.Queue()
        for i in range(total):
            jobs.put(i)
        # results queue drained by THIS (Qt) thread; each item is a tuple
        # ("testing"|"done", idx, ok, latency_ms)
        events: "_queue.Queue[tuple]" = _queue.Queue()

        def _worker() -> None:
            # network only — NEVER touch the Qt callbacks here. We no longer
            # push a "testing" event per pair: that doubled the signal volume
            # for no real benefit (the row flips straight to ✓/✗ fast enough).
            while not self._stop.is_set():
                try:
                    idx = jobs.get_nowait()
                except _queue.Empty:
                    return
                cand = self._cands[idx]
                t0 = time.monotonic()
                ok = False
                try:
                    ok = bool(self._probe(
                        cand.ip, self.port, self.timeout, cand.sni))
                except Exception:
                    ok = False
                latency = (time.monotonic() - t0) * 1000.0
                events.put((idx, ok, latency))

        n_workers = min(self.workers, total)
        threads = [
            threading.Thread(target=_worker, name="sni-scan-%d" % i,
                             daemon=True)
            for i in range(n_workers)
        ]
        for t in threads:
            t.start()

        # Drain verdicts on the caller's (Qt) thread until every candidate has a
        # final verdict or the user stops. We count verdicts rather than joining
        # the workers so a single blocked socket can never wedge the UI.
        #
        # Freeze fix: verdicts are COALESCED into a batch and flushed at most
        # every ``batch_interval`` seconds (or when ``batch_size`` piles up), so
        # the GUI does a handful of bulk repaints per second instead of one per
        # probe. Each flush emits ONE on_results_batch signal + ONE progress
        # signal — not 2N signals — which is what stops the window freezing when
        # the mouse moves mid-scan.
        done_count = 0
        pending: list = []
        last_flush = time.monotonic()

        def _flush() -> None:
            nonlocal pending, last_flush
            if pending:
                if self.on_results_batch is not None:
                    self._emit(self.on_results_batch, pending)
                else:
                    for d in pending:
                        self._emit(self.on_result, d)
                pending = []
            self._emit(self.on_progress, done_count, total)
            last_flush = time.monotonic()

        while done_count < total and not self._stop.is_set():
            try:
                idx, ok, latency = events.get(timeout=self._batch_interval)
            except _queue.Empty:
                # idle tick — flush whatever has accumulated so the table still
                # updates smoothly even when probes are slow.
                if pending or (time.monotonic() - last_flush) >= self._batch_interval:
                    _flush()
                continue
            cand = self._cands[idx]
            cand.latency_ms = latency
            cand.status = ScanCandidate.OK if ok else ScanCandidate.FAIL
            done_count += 1
            if ok:
                self._ok += 1
            self._done = done_count
            pending.append(cand.as_dict())
            # log lines are cheap (the LogBuffer is bounded) but still useful
            self._emit(
                self.on_log,
                "%s  %s  ←  %s  (%.0fms)" % (
                    "✓" if ok else "✗", cand.ip, cand.sni, latency or 0.0))
            now = time.monotonic()
            if (len(pending) >= self._batch_size
                    or (now - last_flush) >= self._batch_interval):
                _flush()

        # final flush so the last partial batch is never dropped
        _flush()

        if self._stop.is_set():
            self._emit(self.on_log, "آزمایش متوقف شد.")
        else:
            self._emit(
                self.on_log,
                "پایان آزمایش — %d از %d جفت سالم بود." % (self._ok, total))
        self._emit(self.on_done, self._ok, total)


def build_scan_candidates(
    ips: List[str],
    snis: List[str],
    *,
    extra_pairs: Optional[List[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    """Build the (IP, SNI) candidate list for :class:`SniIpScanner`.

    Combines the cartesian product of *ips* × *snis* with any explicit
    ``extra_pairs`` (e.g. the user's saved ``sni_ip_pairs``), de-duplicated
    while preserving first-seen order. ``extra_pairs`` come first so the user's
    known-good pairs are tested earliest.
    """
    seen: set = set()
    out: List[Tuple[str, str]] = []

    def _add(ip: str, sni: str) -> None:
        ip = str(ip).strip()
        sni = str(sni).strip()
        if not ip or not sni:
            return
        key = (ip.lower(), sni.lower())
        if key in seen:
            return
        seen.add(key)
        out.append((ip, sni))

    for ip, sni in (extra_pairs or []):
        _add(ip, sni)
    clean_ips = _dedupe([str(x).strip() for x in (ips or []) if str(x).strip()])
    clean_snis = _dedupe([str(x).strip() for x in (snis or []) if str(x).strip()])
    for ip in clean_ips:
        for sni in clean_snis:
            _add(ip, sni)
    return out
