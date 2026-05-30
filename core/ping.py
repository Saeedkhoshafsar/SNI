"""Ping / latency measurement — *before* connecting.

User need (feedback 9): "before connecting I need to know which server gives
what ping, which has lower ping and even better download. When we ping, there
should be an option to test our strategies to see which works / can connect, or
to be able to select a strategy to ping with."

This module answers three questions, all *before* a real session starts:

1. **Which server is fastest?** — TCP latency (multiple samples → min/avg/jitter)
   for each profile, ranked ascending so the lowest-ping server floats to top.
2. **Which has better download?** — a light throughput estimate per server
   (optional; skipped when no estimator is supplied).
3. **Which strategy works / is best?** — reuse the Auto-Prober (``core.prober``)
   to probe several bypass strategies against a server and report which one
   connects / scores best. You can also pin a *single* strategy to ping with.

Design mirrors the rest of ``core/`` (prober, fragment, resilience):

* **UI-agnostic, no Qt.** Plain dataclasses + optional ``on_log`` callback.
* **Network is injectable.** Every network primitive (latency probe, throughput
  estimator, strategy probe) is a callable with a real stdlib default, so the
  whole ranking / selection logic runs deterministically headless in tests.
"""
from __future__ import annotations

import socket
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .prober import (
    AutoProber,
    Candidate,
    ProbeResult,
    ProbeFn,
    build_candidates,
    tcp_probe,
)

try:  # Profile is plain data; import is safe everywhere
    from .profile import Profile
except Exception:  # pragma: no cover - defensive only
    Profile = object  # type: ignore


# ---------------------------------------------------------------------------
#  injectable network primitives
# ---------------------------------------------------------------------------

# one TCP latency sample: (host, port, timeout) -> latency_ms or None on failure
LatencyFn = Callable[[str, int, float], Optional[float]]

# rough download estimate: (host, port, timeout) -> kilobytes/sec or None
ThroughputFn = Callable[[str, int, float], Optional[float]]


def tcp_latency(host: str, port: int, timeout: float) -> Optional[float]:
    """Real single-sample TCP connect latency in ms (stdlib only).

    Returns ``None`` on any failure (timeout / refused / dns / route) so the
    caller can record a miss without exception handling. Used as the default;
    tests inject a deterministic fake.
    """
    start = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return (time.monotonic() - start) * 1000.0
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def tcp_throughput(host: str, port: int,
                   timeout: float) -> Optional[float]:  # pragma: no cover - net
    """Very rough download-quality estimate (KB/s) via a short read burst.

    This is intentionally lightweight — it opens a connection, sends a tiny TLS
    ClientHello-ish nudge, then measures how fast bytes flow back for a brief
    window. It is a *relative* indicator to compare servers, not a real speed
    test. Returns ``None`` if nothing came back. Windows/runtime only.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        # nudge the server to talk back (a real handshake would, this is a hint)
        try:
            sock.sendall(b"\x16\x03\x01\x00\x01\x00")
        except OSError:
            pass
        start = time.monotonic()
        total = 0
        deadline = start + min(timeout, 1.5)
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            total += len(chunk)
        elapsed = max(time.monotonic() - start, 1e-3)
        if total == 0:
            return None
        return (total / 1024.0) / elapsed
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  per-server latency result
# ---------------------------------------------------------------------------

@dataclass
class PingResult:
    """Aggregated latency (and optional download quality) for one server."""

    label: str
    host: str
    port: int
    samples_sent: int = 0
    latencies: List[float] = field(default_factory=list)  # successful samples (ms)
    download_kbps: Optional[float] = None
    error: str = ""

    # -- aggregates -------------------------------------------------------
    @property
    def received(self) -> int:
        return len(self.latencies)

    @property
    def loss(self) -> float:
        """Fraction of lost samples in 0..1 (1.0 == fully unreachable)."""
        if self.samples_sent <= 0:
            return 1.0
        return 1.0 - (self.received / self.samples_sent)

    @property
    def reachable(self) -> bool:
        return self.received > 0

    @property
    def best_ms(self) -> Optional[float]:
        return min(self.latencies) if self.latencies else None

    @property
    def avg_ms(self) -> Optional[float]:
        return (sum(self.latencies) / len(self.latencies)) if self.latencies else None

    @property
    def jitter_ms(self) -> Optional[float]:
        """Standard deviation of samples (0 for a single sample)."""
        if len(self.latencies) < 2:
            return 0.0 if self.latencies else None
        return statistics.pstdev(self.latencies)

    @property
    def sort_key(self) -> float:
        """Ordering key: reachable & low-latency first, misses sink to bottom."""
        if not self.reachable:
            return float("inf")
        # penalise loss a little so a flaky-but-fast server isn't ranked #1
        return (self.avg_ms or 0.0) + self.loss * 1000.0

    def summary(self) -> str:
        if not self.reachable:
            return f"{self.label}: نامحدود (بدون پاسخ)"
        parts = [f"{self.label}: {self.best_ms:.0f}ms (avg {self.avg_ms:.0f})"]
        if self.loss > 0:
            parts.append(f"loss {self.loss*100:.0f}%")
        if self.download_kbps is not None:
            parts.append(f"dl≈{self.download_kbps:.0f}KB/s")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
#  helpers to turn profiles into ping targets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """A single host:port to ping, with a human label."""

    label: str
    host: str
    port: int


def target_from_profile(profile) -> Target:
    """Build a :class:`Target` from a :class:`core.profile.Profile`.

    For **SNI-spoof** configs the stored ``address`` is the *local* spoofer
    (e.g. ``127.0.0.1:40443``), which only answers while the engine is running
    — so pinging it offline always failed (#3). In that case we instead ping
    the *real* CDN endpoint the spoofer fronts (its SNI / Host header on the
    TLS port), so latency is measurable whether or not the tunnel is up.
    """
    label = getattr(profile, "display_name", None) or "profile"
    if callable(label):  # display_name is a property, not a method, but be safe
        label = label()
    host = getattr(profile, "address", "") or ""
    port = int(getattr(profile, "port", 0) or 0)
    if getattr(profile, "is_spoof_config", False):
        real_host = (getattr(profile, "sni", "") or getattr(profile, "host", "")
                     or host)
        if real_host:
            host = real_host
            port = 443 if getattr(profile, "is_tls", False) else port
    return Target(label=str(label), host=host, port=port)


# ---------------------------------------------------------------------------
#  the ping engine
# ---------------------------------------------------------------------------

class PingTester:
    """Measure latency (and optionally download) of one or many servers.

    Parameters
    ----------
    latency_fn   : single-sample latency callable (injectable). Default real.
    throughput_fn: optional download-estimate callable; when ``None`` the
                   download column is simply skipped (fast path).
    samples      : how many latency samples to take per server.
    timeout      : per-sample timeout in seconds.
    on_log       : optional ``str -> None`` progress callback.
    """

    def __init__(
        self,
        *,
        latency_fn: LatencyFn = tcp_latency,
        throughput_fn: Optional[ThroughputFn] = None,
        samples: int = 3,
        timeout: float = 3.0,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if samples < 1:
            raise ValueError("samples باید حداقل ۱ باشد")
        self.latency_fn = latency_fn
        self.throughput_fn = throughput_fn
        self.samples = int(samples)
        self.timeout = float(timeout)
        self._on_log = on_log

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    # -- single server ----------------------------------------------------
    def ping_target(self, target: Target, *,
                    measure_download: bool = False) -> PingResult:
        """Ping one target ``samples`` times; aggregate into a PingResult."""
        res = PingResult(label=target.label, host=target.host, port=target.port)
        if not target.host or not (0 < target.port < 65536):
            res.error = "آدرس/پورت نامعتبر"
            return res
        for _ in range(self.samples):
            res.samples_sent += 1
            try:
                ms = self.latency_fn(target.host, target.port, self.timeout)
            except Exception as exc:  # never let one bad sample kill the run
                res.error = repr(exc)
                ms = None
            if ms is not None:
                res.latencies.append(float(ms))
        if measure_download and self.throughput_fn is not None and res.reachable:
            try:
                res.download_kbps = self.throughput_fn(
                    target.host, target.port, self.timeout)
            except Exception as exc:
                res.error = res.error or repr(exc)
        self._log(f"پینگ {res.summary()}")
        return res

    def ping_profile(self, profile, *, measure_download: bool = False) -> PingResult:
        return self.ping_target(target_from_profile(profile),
                                measure_download=measure_download)

    # -- many servers, ranked --------------------------------------------
    def ping_all(self, targets: Sequence[Target], *,
                 measure_download: bool = False) -> List[PingResult]:
        """Ping every target and return results sorted lowest-latency first."""
        self._log(f"شروع پینگ {len(targets)} سرور …")
        results = [self.ping_target(t, measure_download=measure_download)
                   for t in targets]
        results.sort(key=lambda r: r.sort_key)
        return results

    def ping_profiles(self, profiles: Sequence, *,
                      measure_download: bool = False) -> List[PingResult]:
        targets = [target_from_profile(p) for p in profiles]
        return self.ping_all(targets, measure_download=measure_download)

    @staticmethod
    def best(results: Sequence[PingResult]) -> Optional[PingResult]:
        """The single best (lowest sort_key, reachable) result, or None."""
        reachable = [r for r in results if r.reachable]
        if not reachable:
            return None
        return min(reachable, key=lambda r: r.sort_key)


# ---------------------------------------------------------------------------
#  strategy testing during ping  (feedback 9: "which strategy can connect?")
# ---------------------------------------------------------------------------

@dataclass
class StrategyPing:
    """Result of pinging one server *through* one bypass strategy."""

    strategy: str
    candidate_key: str
    outcome: str            # OK / RST / TIMEOUT / ERROR (from prober)
    latency_ms: float = 0.0
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@dataclass
class StrategyPingReport:
    """Per-strategy results for one server + the winner."""

    label: str
    host: str
    port: int
    results: List[StrategyPing] = field(default_factory=list)

    @property
    def best(self) -> Optional[StrategyPing]:
        ok = [r for r in self.results if r.ok]
        if not ok:
            return None
        return max(ok, key=lambda r: r.score)

    @property
    def any_connected(self) -> bool:
        return any(r.ok for r in self.results)

    def summary(self) -> str:
        b = self.best
        if b is None:
            return f"{self.label}: هیچ استراتژی‌ای وصل نشد"
        return (f"{self.label}: بهترین = {b.strategy} "
                f"({b.latency_ms:.0f}ms, score={b.score:.2f})")


def default_strategy_keys(implemented_only: bool = True) -> List[str]:
    """All registered (implemented) strategy keys — the test set by default."""
    try:
        from strategies import all_strategies  # late import (optional dep)
        return [s.meta.key for s in all_strategies(implemented_only=implemented_only)]
    except Exception:
        return []


def probe_strategies(
    target: Target,
    *,
    strategies: Optional[Sequence[str]] = None,
    probe_fn: Optional[ProbeFn] = None,
    timeout: float = 5.0,
    on_log: Optional[Callable[[str], None]] = None,
) -> StrategyPingReport:
    """Probe several strategies against a server; report which connect / win.

    Parameters
    ----------
    target     : the server to test against.
    strategies : strategy keys to test. ``None`` → all implemented strategies.
                 Pass a single-element list to "ping with one chosen strategy".
    probe_fn   : injectable probe (default real :func:`core.prober.tcp_probe`).
    timeout    : per-probe timeout (seconds).
    on_log     : optional progress callback.

    Returns a :class:`StrategyPingReport`. Fully fail-soft: an empty strategy
    set or a bad probe never raises — the report just shows no connection.
    """
    # Resolve the probe lazily so a monkeypatched core.prober.tcp_probe (tests /
    # alternate runtimes) is honoured rather than a default bound at import.
    if probe_fn is None:
        from . import prober as _prober_mod
        probe_fn = _prober_mod.tcp_probe
    keys = list(strategies) if strategies else default_strategy_keys()
    report = StrategyPingReport(label=target.label, host=target.host,
                                port=target.port)
    if not keys or not target.host or not (0 < target.port < 65536):
        if on_log:
            on_log(f"تست استراتژی برای {target.label} رد شد (کاندیدا/آدرس نامعتبر)")
        return report
    candidates = build_candidates(keys)
    prober = AutoProber(candidates, probe_fn, timeout=timeout, on_log=on_log)
    results = prober.probe_all(target.host, target.port)
    for r in results:
        report.results.append(StrategyPing(
            strategy=r.candidate.strategy,
            candidate_key=r.candidate.key,
            outcome=r.outcome,
            latency_ms=r.latency_ms,
            score=r.score(),
        ))
    if on_log:
        on_log(report.summary())
    return report


def probe_strategies_for_profile(profile, **kwargs) -> StrategyPingReport:
    return probe_strategies(target_from_profile(profile), **kwargs)
