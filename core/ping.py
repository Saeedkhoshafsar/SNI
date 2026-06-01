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


def tls_latency(host: str, port: int, timeout: float, *,
                server_name: str = "", host_header: str = "",
                path: str = "/", is_ws: bool = False,
                is_tls: bool = True, retries: int = 2
                ) -> Optional[float]:  # pragma: no cover - net
    """Single-sample latency with **real, config-accurate** validation (stdlib).

    Why this exists (issues #1/#2 — *"ordinary configs: ping shows green but
    they don't actually connect"*): neither a bare :func:`tcp_latency` nor even
    a bare TLS handshake honestly tells you whether a config works behind a CDN.

    * A TCP three-way handshake to a Cloudflare front IP almost always succeeds.
    * A TLS handshake (with cert validation off) *also* almost always succeeds —
      every Cloudflare anycast IP answers TLS for **any** SNI. That is exactly
      why the old TLS ping went green for IPs that could never carry the config
      (the bug the user reported: clean-looking IPs that didn't work still
      pinged green).

    So we now validate what actually matters, mirroring ``core.cf_scanner`` /
    ``MatinSenPai/SenPaiScanner``: connect, (optionally) wrap TLS with the real
    SNI, then send a real **HTTP request to the Cloudflare edge**
    (``/cdn-cgi/trace``) carrying the config's Host header, and require a
    genuine edge response. For WebSocket configs we additionally require a WS
    **upgrade** on the config's path/Host. Only then do we report a latency;
    otherwise we return ``None`` so the ping is honestly red.

    Returns elapsed-ms on success, ``None`` on any failure — so a green ping
    now means "this endpoint really answers for this config", matching the
    behaviour the user expects from V2RayTun.
    """
    from .cf_scanner import cf_ip_probe, ProbeSpec, OK, RST, TIMEOUT, is_cloudflare_host

    spec = ProbeSpec(
        port=int(port),
        server_name=(server_name or host or "").strip().strip("[]"),
        host=(host_header or server_name or host or "").strip(),
        path=path or "/",
        is_ws=bool(is_ws),
        is_tls=bool(is_tls),
    )
    # The honest edge probe is the right test for BOTH TLS and plaintext (port
    # 80 / security=none) Cloudflare-fronted configs: it sends a real HTTP/1.1
    # request (and, for ws, a real Upgrade) carrying the config's Host header, so
    # a dead Worker route or wrong host fails honestly — unlike a bare TCP
    # connect to an anycast IP, which ALWAYS succeeds and produced the reported
    # "ping is green but the config never connects" for the plaintext SIN-04/06
    # servers.
    #
    # Robustness (the "بدون پاسخ" false-red on a config that DOES connect): a
    # single attempt is fragile under load — the edge can momentarily reset or
    # time out the trace / second WS handshake even though the route is fine. So
    # we retry a couple of times and only report a miss if EVERY attempt failed,
    # mirroring how the live-tunnel ping sweeps multiple endpoints.
    attempts = max(1, int(retries) + 1)
    saw_retryable = False
    for _ in range(attempts):
        res = cf_ip_probe(host, spec, timeout)
        if res.outcome == OK:
            return float(res.latency_ms)
        if res.outcome in (RST, TIMEOUT):
            saw_retryable = True  # transient — worth another shot
    # Fallback for NON-Cloudflare ordinary configs (plain VPS VLESS/Trojan/etc):
    # the /cdn-cgi/trace edge check only passes for Cloudflare-fronted hosts, so
    # a perfectly working direct server failed it → "پینگ پاسخ نمیده با اینکه کار
    # میکنه". For a direct config the host IS the real server (not a shared
    # anycast IP), so a genuine TLS handshake presenting the config's own SNI is
    # honest evidence the endpoint is alive and speaks TLS for this config.
    #
    # CRITICAL (电信-SIN-07 / AYYILDIZ false-green): this fallback is ONLY honest
    # for a *direct* (non-Cloudflare) server. For a Cloudflare-fronted host
    # (a CF anycast IP, or a *.pages.dev / *.workers.dev SNI) every anycast edge
    # completes a TLS handshake for ANY SNI — so the handshake "succeeds" even
    # when the IP is dirty / the Worker route is dead / the config can't connect.
    # That is precisely the reported "should be red but shows green" bug. So for
    # Cloudflare-fronted configs we DO NOT fall back: only the real edge probe
    # (trace + ws upgrade) above may turn the ping green. Honest red otherwise.
    cf_fronted = (is_cloudflare_host(host)
                  or is_cloudflare_host(server_name)
                  or is_cloudflare_host(host_header))
    if is_tls and not cf_fronted:
        sni = (server_name or host or "").strip().strip("[]")
        ms = _tls_handshake_latency(host, int(port), timeout, server_name=sni)
        if ms is not None:
            return ms
    # all edge attempts failed and the TLS fallback (if any) didn't help.
    _ = saw_retryable
    return None


def _tls_handshake_latency(host: str, port: int, timeout: float, *,
                           server_name: str = ""
                           ) -> Optional[float]:  # pragma: no cover - net
    """Validated TLS-handshake latency to a real server (stdlib only).

    Connects then completes a TLS handshake presenting ``server_name`` as SNI
    (cert validation off — proxies use self/edge certs). A completed handshake
    proves the endpoint actually answers TLS for this config, which for a
    *direct* (non-CDN) server is meaningful liveness — unlike a bare TCP connect.
    Returns elapsed-ms on success, ``None`` on any failure.
    """
    import ssl

    sni = (server_name or host or "").strip().strip("[]")
    start = time.monotonic()
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with ctx.wrap_socket(raw, server_hostname=sni or host) as tls:
                # handshake completed; touch the cipher to be sure it's up
                _ = tls.cipher()
            return (time.monotonic() - start) * 1000.0
        except (ssl.SSLError, OSError):
            return None
    finally:
        try:
            raw.close()
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
    """A single host:port to ping, with a human label.

    ``server_name`` is the SNI / TLS server name to present in the ClientHello
    when *validating* the handshake (TLS probe). For spoof configs this is the
    real CDN SNI the spoofer fronts; for direct configs it's the host itself.
    ``tls`` marks the target as TLS-bearing so the prober knows to validate a
    real ServerHello instead of trusting a bare TCP connect (#1).

    ``host_header`` / ``path`` / ``is_ws`` describe the transport so the honest
    edge ping (#2) can send the config's real HTTP Host + (for ws) drive a real
    WebSocket upgrade — the only way to tell a *working* edge from an anycast IP
    that merely answers TLS.
    """

    label: str
    host: str
    port: int
    server_name: str = ""
    tls: bool = False
    host_header: str = ""
    path: str = "/"
    is_ws: bool = False


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
    is_tls = bool(getattr(profile, "is_tls", False))
    # default SNI to validate: the explicit server name → host header → host.
    server_name = (getattr(profile, "sni", "") or getattr(profile, "host", "")
                   or host)
    # transport details for the honest edge ping (#2): real HTTP Host header +
    # ws path so a WebSocket config is validated with a real upgrade.
    host_header = (getattr(profile, "host", "") or server_name or host)
    path = (getattr(profile, "path", "") or "/")
    transport = (getattr(profile, "transport", "") or "tcp").lower()
    is_ws = transport in ("ws", "websocket", "httpupgrade")
    if getattr(profile, "is_spoof_config", False):
        # Spoof configs dial *our* loopback spoofer, which forwards to a fixed
        # CDN IP while injecting a **decoy** SNI to beat DPI.
        #
        # The OLD honest-test pinged that connect IP presenting the **decoy**
        # SNI (e.g. ``www.hcaptcha.com``) + the decoy Host header. That is wrong:
        # a `/cdn-cgi/trace` to ANY live Cloudflare anycast IP answers 200 for
        # ANY SNI, so EVERY spoof config — working or broken — went green. That
        # is exactly the user's "both broken and healthy configs ping positive"
        # bug: we only proved the CDN edge is alive, never that *this config's*
        # Worker actually routes.
        #
        # The honest test must validate the **real config endpoint**: connect to
        # the CDN edge but present the config's REAL SNI (the workers.dev /
        # pages.dev host) and send the REAL Host header + path. Cloudflare routes
        # by the inner SNI/Host, so a dead Worker, wrong host or unrouteable
        # path now fails the trace/Worker check → honest red — while a genuinely
        # working config still answers. The decoy SNI is purely a DPI-evasion
        # detail on the wire and is irrelevant to whether the config *works*.
        connect_ip = getattr(profile, "spoof_connect_ip", "") or host
        connect_port = getattr(profile, "spoof_connect_port", 0) or (
            443 if is_tls else port)
        real_sni = (getattr(profile, "sni", "") or getattr(profile, "host", "")
                    or server_name)
        real_host = (getattr(profile, "host", "") or real_sni)
        host = connect_ip
        port = int(connect_port)
        server_name = real_sni
        host_header = real_host
        # keep the real transport so a ws/xhttp config is validated against its
        # real path on the real edge route (already set above from the profile).
    return Target(label=str(label), host=host, port=port,
                  server_name=str(server_name or ""), tls=is_tls,
                  host_header=str(host_header or ""), path=str(path or "/"),
                  is_ws=bool(is_ws))


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
        # We DON'T trust a bare TCP connect for a CDN-fronted config — DPI lets
        # the TCP handshake through and only drops/resets the real session, and
        # worse, a bare connect to a Cloudflare anycast IP ALWAYS succeeds, so a
        # plaintext (security=none, port 80) config went green even when its
        # Worker route was dead (the reported SIN-04/06 "pings but never
        # connects" bug). So we use the honest edge probe — a real HTTP/1.1
        # request (and, for ws, a real Upgrade) carrying the config's Host —
        # whenever the target is TLS **or** a WebSocket transport (i.e. fronted
        # by Cloudflare/a CDN), regardless of whether TLS wraps the wire.
        #
        # A caller that injects a custom ``latency_fn`` (tests) always wins so
        # the suite stays deterministic and offline.
        is_tls = bool(getattr(target, "tls", False))
        is_ws = bool(getattr(target, "is_ws", False))
        use_edge = ((is_tls or is_ws) and self.latency_fn is tcp_latency)
        # The edge probe is heavier than a TCP connect and already retries
        # internally, so taking it once (instead of ``samples`` times) keeps the
        # ping snappy and — crucially — avoids hammering the same edge with many
        # back-to-back handshakes, which was itself triggering the transient
        # reset/timeout false-reds the user saw under a "ping all" burst.
        n = 1 if use_edge else self.samples
        for _ in range(n):
            res.samples_sent += 1
            try:
                if use_edge:
                    ms = tls_latency(
                        target.host, target.port, self.timeout,
                        server_name=getattr(target, "server_name", ""),
                        host_header=getattr(target, "host_header", ""),
                        path=getattr(target, "path", "/"),
                        is_ws=is_ws,
                        is_tls=is_tls)
                else:
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
    #
    # #1: for a TLS target we DON'T trust a bare TCP connect — DPI lets the TCP
    # handshake through and only resets the TLS ClientHello. So the default
    # probe validates a real TLS handshake (presenting the target's SNI). A
    # caller-supplied ``probe_fn`` (or a test's monkeypatched ``tcp_probe``)
    # always wins so the deterministic test suite stays in control.
    if probe_fn is None:
        from . import prober as _prober_mod
        sni = getattr(target, "server_name", "") or ""
        host_header = getattr(target, "host_header", "") or sni
        tpath = getattr(target, "path", "/") or "/"
        is_ws = bool(getattr(target, "is_ws", False))
        if getattr(target, "tls", False):
            # #C: a bare TLS handshake (``tls_probe``) is NOT enough for a
            # CDN-fronted config — every Cloudflare anycast IP answers TLS for
            # *any* SNI, so a broken Worker still "connected". Validate the REAL
            # edge route instead: TLS with the real SNI + a real
            # ``/cdn-cgi/trace`` carrying the real Host header (and, for ws, a
            # real Upgrade on the real path). Only a genuinely working config's
            # edge answers, so broken configs honestly fail every strategy.
            from .cf_scanner import cf_ip_probe, ProbeSpec, OK as _CF_OK
            from .prober import ProbeResult as _PR, OK, RST, TIMEOUT, ERROR

            def probe_fn(cand, host, port, timeout,  # noqa: E306
                         _sni=sni, _h=host_header, _p=tpath, _ws=is_ws):
                spec = ProbeSpec(
                    port=int(port),
                    server_name=(_sni or host or "").strip().strip("[]"),
                    host=(_h or _sni or host or "").strip(),
                    path=_p or "/", is_ws=bool(_ws), is_tls=True)
                res = cf_ip_probe(host, spec, timeout)
                outcome = {"ok": OK, "rst": RST, "timeout": TIMEOUT,
                           "error": ERROR}.get(res.outcome, ERROR)
                return _PR(cand, outcome,
                           latency_ms=float(getattr(res, "latency_ms", 0.0)),
                           detail=getattr(res, "detail", ""))
        else:
            # Non-TLS target → a bare TCP connect is the honest test. Resolve
            # the symbol lazily through the module so a monkeypatched
            # ``core.prober.tcp_probe`` (tests) is still honoured.
            def probe_fn(cand, host, port, timeout):  # noqa: E306
                return _prober_mod.tcp_probe(cand, host, port, timeout)
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
