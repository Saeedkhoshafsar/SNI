"""Cloudflare clean-IP scanner — SenPaiScanner engine, ported into this app.

This module is a **faithful Python port of ``MatinSenPai/SenPaiScanner``** (Go),
brought in as the *core* of our "اسکن IP تمیز" feature at the user's request.
The proven two-phase design is preserved exactly:

**Phase 1 — connectivity probe** (:func:`cf_ip_probe`, ported from
``internal/prober/prober.go``):
    * Several tries per IP with **SNI rotation** + small **jitter** between
      tries (so the sweep doesn't look like a scanner and DPI black-holing of a
      single SNI can't sink an otherwise good IP).
    * Three modes — ``ModeTCP`` (bare connect), ``ModeTLS`` (handshake only),
      ``ModeHTTP`` (full ``GET /cdn-cgi/trace`` → must see a real Cloudflare
      ``colo=``/CF-Ray). HTTP mode optionally measures a **download sample** and
      runs a **WebSocket probe** (2 s idle hold to detect DPI RSTs + a real WS
      upgrade that must elicit an ``HTTP/`` response).
    * Per-try latencies feed honest stats — :class:`IPResult` exposes loss /
      avg / min / max / jitter and an :meth:`IPResult.is_healthy` mirroring
      ``internal/result/result.go``'s ``IsHealthy``.

**Phase 2 — Xray end-to-end validation** (:mod:`core.cf_xray_validator`, ported
from ``internal/xraytest/runner.go``):
    * For the IPs that pass Phase 1, build the **real** Xray config for the
      reference profile with the candidate IP swapped in, start the bundled
      ``xray.exe`` on a private SOCKS port, then send genuine traffic through
      the proxy (TTFB via ``cp.cloudflare.com/cdn-cgi/trace`` + a download
      throughput sample). Only IPs that actually carry the user's config
      end-to-end survive — this is what makes SenPaiScanner trustworthy and is
      now part of our scanner too.

Design (unchanged, consistent with ``core/ping.py`` / ``core/prober.py``)
-------------------------------------------------------------------------
* **UI-agnostic, no Qt.** Plain dataclasses + optional ``on_log`` /
  ``on_result`` / ``should_stop`` callbacks.
* **Network is injectable.** The per-IP probe is a callable with a real stdlib
  default, so the sweep / ranking logic runs deterministically headless in
  tests with a fake probe and no sockets.
* **Bounded & cancellable.** A thread pool with a hard cap, an overall result
  limit, and a cooperative ``should_stop`` so a long scan never runs away.

The IP pool comes from Cloudflare's published ranges (``cf_ip_pool``), sampled
round-robin and shuffled so the scan covers the whole anycast space.
"""
from __future__ import annotations

import base64 as _b64
import ipaddress
import math
import os as _os
import random
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence


# ---------------------------------------------------------------------------
#  Cloudflare published IPv4 ranges (https://www.cloudflare.com/ips-v4)
# ---------------------------------------------------------------------------
# Kept inline so the scanner works fully offline. These are the public,
# well-known Cloudflare anycast ranges the front IPs live in. (Mirrors
# ``internal/ipsrc/ranges_v4.txt`` from SenPaiScanner.)
CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)


# Well-known Cloudflare hostnames used as SNI values. Rotating the SNI between
# tries reduces the chance of DPI black-holing a specific name (ported verbatim
# from SenPaiScanner's ``sniHostnames``). ``speed.cloudflare.com`` leads because
# it also serves the ``/__down`` speed endpoint used by the download probe.
SNI_HOSTNAMES: tuple[str, ...] = (
    "speed.cloudflare.com",
    "www.cloudflare.com",
    "cloudflare.com",
    "1.1.1.1.cdn.cloudflare.net",
    "blog.cloudflare.com",
)


# Probe modes (mirror prober.Mode in Go).
MODE_TCP = "tcp"
MODE_TLS = "tls"
MODE_HTTP = "http"


# Candidate-IP sources (mirror SenPaiScanner's "Source" row).
SOURCE_RANDOM = "random"   # sample random Cloudflare ranges
SOURCE_FILE = "file"       # read candidates from a user-supplied list


# Cloudflare CDN ports that behave differently under DPI — SenPaiScanner offers
# these as a multi-select so Phase 1 can find the best ``IP:port`` pair before
# Phase 2 validation. ``0`` is a sentinel meaning "use the config's own port".
CF_PORTS: tuple[int, ...] = (443, 8443, 2053, 2083, 2087, 2096)

# SenPaiScanner's Count / Workers / Timeout / Top-N presets (the dialog rows).
COUNT_PRESETS: tuple[int, ...] = (1000, 5000, 20000, 100000)
WORKER_PRESETS: tuple[int, ...] = (50, 100, 200)
TIMEOUT_PRESETS: tuple[float, ...] = (2.0, 3.0, 5.0)
TOPN_PRESETS: tuple[int, ...] = (10, 25, 50, 100)

# Hard ceiling on how many candidate IPs a single run may sweep (matches the
# 100,000 the user asked for — keeps a runaway "Custom" entry from OOM-ing).
MAX_CANDIDATES_HARD = 100_000


# probe outcome (kept for backward compatibility with the old API + the UI).
OK = "ok"
RST = "rst"
TIMEOUT = "timeout"
ERROR = "error"


# A genuine Cloudflare edge answers /cdn-cgi/trace with a body containing a
# ``colo=`` marker, and stamps a ``CF-Ray`` header. Either proves a live edge.
_CF_TRACE_PATH = "/cdn-cgi/trace"
_SPEED_DOWN_PATH = "/__down?bytes={n}"


# ---------------------------------------------------------------------------
#  result model
# ---------------------------------------------------------------------------

@dataclass
class IPResult:
    """The full measurement for one candidate IP (mirrors ``result.Result``).

    The legacy single-shot fields (``outcome`` / ``latency_ms`` / ``detail``)
    are kept so existing callers/tests keep working: ``outcome`` is the *summary*
    outcome (OK when the IP is healthy), and ``latency_ms`` is the average of the
    successful tries. The richer per-try data lives in :attr:`latencies` and the
    Cloudflare-edge facts (``tls_ok`` / ``colo`` / ``http_status`` / ``ws_ok`` /
    ``throughput``).
    """

    ip: str
    outcome: str = ERROR          # OK / RST / TIMEOUT / ERROR (summary)
    latency_ms: float = 0.0       # average successful latency (back-compat)
    detail: str = ""

    # --- rich, per-IP statistics (Phase 1) ---
    port: int = 443
    mode: str = MODE_HTTP
    latencies_ms: List[float] = field(default_factory=list)  # 0 == failed try
    tls_ok: bool = False
    ws_ok: bool = False
    require_ws: bool = False
    http_status: int = 0
    colo: str = ""
    throughput_bps: float = 0.0   # bytes/sec from the Phase-1 download sample
    speed_tested: bool = False

    # --- Phase 2 (real Xray end-to-end validation) ---
    xray_validated: bool = False  # an Xray validation was attempted
    xray_ok: bool = False         # the config carried real traffic end-to-end
    xray_latency_ms: float = 0.0  # TTFB through the proxy
    xray_throughput_bps: float = 0.0  # download throughput through the proxy

    # ------------------------------------------------------------------
    @property
    def ok(self) -> bool:
        return self.outcome == OK

    # -- statistics (ported from result.go) ----------------------------
    def loss(self) -> float:
        """Packet-loss percentage across the tries (0–100)."""
        if not self.latencies_ms:
            return 100.0
        failed = sum(1 for l in self.latencies_ms if l <= 0)
        return failed / len(self.latencies_ms) * 100.0

    def avg(self) -> float:
        """Mean of the *successful* latencies (ms); 0 when none succeeded."""
        good = [l for l in self.latencies_ms if l > 0]
        return sum(good) / len(good) if good else 0.0

    def best(self) -> float:
        good = [l for l in self.latencies_ms if l > 0]
        return min(good) if good else 0.0

    def worst(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    def jitter(self) -> float:
        """Standard deviation of the successful latencies (ms)."""
        good = [l for l in self.latencies_ms if l > 0]
        if len(good) < 2:
            return 0.0
        mean = sum(good) / len(good)
        var = sum((l - mean) ** 2 for l in good) / len(good)
        return math.sqrt(var)

    def is_healthy(self) -> bool:
        """True when the mode's success criteria are met (mirrors IsHealthy).

        A failed try records latency 0; a timeout must never count as success.
        For HTTP mode the IP must be a real Cloudflare edge (TLS where the port
        isn't 80, a 2xx/3xx status, a non-empty ``colo``), and — when a speed
        sample or WS probe was *required* — those must also have passed.
        """
        if self.loss() >= 50.0 or self.avg() <= 0.0:
            return False
        if self.mode == MODE_HTTP:
            if self.port != 80 and not self.tls_ok:
                return False
            if self.http_status < 200 or self.http_status >= 400:
                return False
            if not self.colo:
                return False
            if self.speed_tested and self.throughput_bps <= 0:
                return False
            if self.require_ws and not self.ws_ok:
                return False
            return True
        if self.mode == MODE_TLS:
            return self.tls_ok
        return True  # tcp


@dataclass(frozen=True)
class ProbeSpec:
    """What a clean IP must satisfy *for this specific config*.

    A bare TLS handshake is **not** a valid test: every Cloudflare anycast IP
    completes a TLS handshake with *any* SNI, so SenPaiScanner (and now us)
    require a **real HTTP response from the Cloudflare edge** (``/cdn-cgi/trace``
    carrying a ``colo``) and, for WebSocket configs, a successful WS upgrade on
    the config's Host + path.

    Fields
    ------
    port        : the port the config dials (clean IP must answer on it).
    server_name : TLS SNI to present (the config's ``sni``/``host``).
    host        : HTTP ``Host`` header (ws/h2 host → the Worker hostname).
    path        : ws / xhttp path the config uses (validated on WS upgrade).
    is_ws       : require a WebSocket upgrade (config ``type=ws``/httpupgrade).
    is_tls      : whether the transport is wrapped in TLS (CDN default True).
    tries       : connectivity attempts per IP (SNI rotates each try).
    mode        : ``tcp`` | ``tls`` | ``http`` (http = full edge validation).
    speed_bytes : optional HTTP download sample size; 0 disables it.
    require_ws  : require a successful WebSocket probe for HTTP health.
    """

    port: int = 443
    server_name: str = ""
    host: str = ""
    path: str = "/"
    is_ws: bool = False
    is_tls: bool = True
    tries: int = 4
    mode: str = MODE_HTTP
    speed_bytes: int = 0
    require_ws: bool = False


# (ip, spec, timeout) -> IPResult
ProbeFn = Callable[[str, "ProbeSpec", float], IPResult]


# ---------------------------------------------------------------------------
#  Phase 1 — connectivity probe (ported from prober.go)
# ---------------------------------------------------------------------------

def cf_ip_probe(ip: str, spec: "ProbeSpec",
                timeout: float) -> IPResult:  # pragma: no cover - needs net
    """Run a full Phase-1 measurement session against *ip* (port ``spec.port``).

    Faithful port of ``prober.Probe``: it runs ``spec.tries`` attempts, rotating
    the SNI and adding a little jitter between tries, then folds the per-try
    results into a single :class:`IPResult` whose summary ``outcome`` is ``OK``
    iff the result is healthy for the chosen mode. Honest latency (the average of
    the *successful* tries) is reported; failed tries record 0.
    """
    res = IPResult(ip=ip, port=spec.port, mode=spec.mode,
                   require_ws=spec.require_ws)
    if spec.mode == MODE_HTTP and spec.speed_bytes > 0:
        res.speed_tested = True

    tries = max(1, int(spec.tries))
    for i in range(tries):
        sni = spec.server_name
        if not sni and spec.mode == MODE_HTTP:
            sni = "speed.cloudflare.com"
        elif not sni:
            sni = random.choice(SNI_HOSTNAMES)

        lat = 0.0
        tls_ok = False
        http_status = 0
        colo = ""
        throughput = 0.0

        if spec.mode == MODE_TCP:
            lat = _probe_tcp(ip, spec.port, timeout)
        elif spec.mode == MODE_TLS:
            lat, tls_ok = _probe_tls(ip, spec.port, sni, timeout)
        else:  # MODE_HTTP
            lat, tls_ok, http_status, colo, throughput, ws_ok = _probe_http(
                ip, spec.port, sni, timeout, spec.speed_bytes,
                spec.host, spec.path, spec.is_ws or spec.require_ws,
                spec.is_tls)
            if ws_ok:
                res.ws_ok = True

        res.latencies_ms.append(lat)
        if tls_ok:
            res.tls_ok = True
        if http_status:
            res.http_status = http_status
        if colo:
            res.colo = colo
        if throughput > 0:
            res.throughput_bps = throughput

        # small jitter between tries (10–60ms) to avoid looking like a scanner
        if i < tries - 1:
            time.sleep((random.randint(10, 60)) / 1000.0)

    # summarise
    res.latency_ms = res.avg()
    healthy = res.is_healthy()
    if healthy:
        res.outcome = OK
        kind = "ws+edge" if (spec.is_ws or spec.require_ws) else "edge"
        bits = [f"{kind} ok"]
        if res.colo:
            bits.append(f"colo={res.colo}")
        if res.speed_tested and res.throughput_bps > 0:
            bits.append(f"{_fmt_speed(res.throughput_bps)}")
        res.detail = "  ".join(bits)
    else:
        # classify the failure for the UI / tests
        if all(l <= 0 for l in res.latencies_ms):
            res.outcome = TIMEOUT
            res.detail = "no successful try"
        elif spec.mode == MODE_HTTP and not res.colo:
            res.outcome = ERROR
            res.detail = "not a live cf edge (no colo)"
        elif spec.require_ws and not res.ws_ok:
            res.outcome = ERROR
            res.detail = "ws upgrade refused"
        elif res.speed_tested and res.throughput_bps <= 0:
            res.outcome = ERROR
            res.detail = "edge ok but download stalled"
        else:
            res.outcome = ERROR
            res.detail = res.detail or "unhealthy"
    return res


# Back-compat shim — older callers / tests may still import ``tls_ip_probe``.
def tls_ip_probe(ip: str, port: int, server_name: str,
                 timeout: float) -> IPResult:  # pragma: no cover - needs net
    return cf_ip_probe(
        ip, ProbeSpec(port=port, server_name=server_name,
                      host=server_name, is_tls=True), timeout)


def _probe_tcp(ip: str, port: int,
               timeout: float) -> float:  # pragma: no cover - needs net
    """Raw TCP connect time in ms; 0 on failure."""
    start = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.monotonic() - start) * 1000.0
    except OSError:
        return 0.0


def _probe_tls(ip: str, port: int, sni: str,
               timeout: float) -> tuple[float, bool]:  # pragma: no cover - net
    """TLS handshake time in ms + whether it succeeded."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (ValueError, AttributeError):
        pass
    start = time.monotonic()
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return 0.0, False
    try:
        raw.settimeout(timeout)
        tls = ctx.wrap_socket(raw, server_hostname=sni or ip)
        lat = (time.monotonic() - start) * 1000.0
        _safe_close(tls)
        return lat, True
    except (ssl.SSLError, socket.timeout, OSError):
        _safe_close(raw)
        return 0.0, False


def _probe_http(ip: str, port: int, sni: str, timeout: float, speed_bytes: int,
                ws_host: str, ws_path: str, run_ws: bool,
                is_tls: bool):  # pragma: no cover - needs net
    """Full HTTP edge check (port-forced to *ip*) + optional speed/WS probes.

    Returns ``(latency_ms, tls_ok, http_status, colo, throughput_bps, ws_ok)``.
    Mirrors ``prober.probeHTTP``: a real ``GET /cdn-cgi/trace`` must come back
    2xx/3xx with a non-empty ``colo`` before we bother with the speed/WS probes.
    """
    sock = _open_socket(ip, port, timeout)
    if sock is None:
        return 0.0, False, 0, "", 0.0, False

    stream = sock
    tls_ok = False
    try:
        if is_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            except (ValueError, AttributeError):
                pass
            try:
                stream = ctx.wrap_socket(sock, server_hostname=sni or ip)
                tls_ok = True
            except (ssl.SSLError, socket.timeout, OSError):
                _safe_close(sock)
                return 0.0, False, 0, "", 0.0, False

        host_hdr = sni  # trace is fetched against the rotating SNI host
        start = time.monotonic()
        status, headers, body = _http_get(stream, _CF_TRACE_PATH, host_hdr,
                                           timeout, max_bytes=4096)
        lat = (time.monotonic() - start) * 1000.0
        _safe_close(stream)

        if status == 0:
            return 0.0, tls_ok, 0, "", 0.0, False

        colo = _parse_colo_trace(body) or _parse_colo_ray(
            headers.get("cf-ray", ""))

        throughput = 0.0
        ws_ok = False
        if 200 <= status < 400 and colo:
            if speed_bytes > 0:
                throughput = _probe_download(ip, port, timeout, speed_bytes,
                                             is_tls)
            if speed_bytes > 0 or run_ws:
                ws_ok = _probe_websocket(ip, port, sni, ws_host, ws_path,
                                         timeout, is_tls)
        return lat, tls_ok, status, colo, throughput, ws_ok
    except (socket.timeout, ConnectionResetError, OSError):
        _safe_close(stream)
        return 0.0, tls_ok, 0, "", 0.0, False


def _probe_websocket(ip: str, port: int, sni: str, host: str, path: str,
                     timeout: float,
                     is_tls: bool) -> bool:  # pragma: no cover - needs net
    """WebSocket reachability probe (faithful port of ``probeWebSocket``).

    Two checks:

      1. **Idle hold** — after the TLS handshake, hold the connection idle for
         up to 2 s *without* sending data. Some DPI boxes RST long-lived TLS
         tunnels that send no early data; if the socket dies during the hold the
         probe fails. A read *timeout* here is expected (the server only speaks
         after the upgrade), so only a real error (RST/EOF) fails the probe.
      2. **Upgrade** — send a WebSocket ``Upgrade`` request and require that *any*
         HTTP response arrives (even 400/404 — that still proves the WS bytes
         reached the Cloudflare edge). If DPI drops the upgrade, no ``HTTP/``
         line comes back and the probe fails.

    TLS verification is skipped here because ``_probe_http`` already validated
    the certificate for this IP on the same edge.
    """
    if timeout <= 0:
        timeout = 5.0
    deadline = time.monotonic() + timeout
    if not host:
        host = sni
    path = _normalize_ws_path(path)

    raw = _open_socket(ip, port, timeout)
    if raw is None:
        return False
    conn = raw
    try:
        if is_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                conn = ctx.wrap_socket(raw, server_hostname=sni or ip)
            except (ssl.SSLError, socket.timeout, OSError):
                _safe_close(raw)
                return False

        # Phase 1: idle hold (detect DPI that RSTs idle TLS tunnels).
        idle_hold = 2.0
        remaining = deadline - time.monotonic()
        if remaining < 2 * idle_hold:
            idle_hold = max(0.0, remaining / 2)
        conn.settimeout(max(0.05, idle_hold))
        try:
            chunk = conn.recv(1)
            # An EOF (empty) during the idle hold means the edge closed on us.
            if chunk == b"":
                _safe_close(conn)
                return False
        except socket.timeout:
            pass  # EXPECTED — the server speaks only after the upgrade
        except (ConnectionResetError, OSError):
            _safe_close(conn)
            return False

        # Phase 2: send the WebSocket upgrade and require an HTTP response.
        key = _b64.b64encode(_os.urandom(16)).decode("ascii")
        ws_req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: senpaiscanner/1.0\r\n"
            f"\r\n"
        ).encode("ascii", "ignore")

        write_to = max(0.05, min(timeout / 2, deadline - time.monotonic()))
        conn.settimeout(write_to)
        try:
            conn.sendall(ws_req)
        except (socket.timeout, ConnectionResetError, OSError):
            _safe_close(conn)
            return False

        read_to = max(0.05, min(timeout / 3, deadline - time.monotonic()))
        conn.settimeout(read_to)
        try:
            resp = conn.recv(1024)
        except (socket.timeout, ConnectionResetError, OSError):
            _safe_close(conn)
            return False
        _safe_close(conn)
        if not resp:
            return False
        return b"HTTP/" in resp
    except OSError:
        _safe_close(conn)
        return False


def _probe_download(ip: str, port: int, timeout: float, sample_bytes: int,
                    is_tls: bool) -> float:  # pragma: no cover - needs net
    """Fetch a small sample from speed.cloudflare.com (port-forced to *ip*).

    Returns bytes/sec, 0 on failure. Faithful port of ``probeDownload``: it
    catches IPs that handshake cleanly then stall on real data.
    """
    if sample_bytes <= 0:
        return 0.0
    sock = _open_socket(ip, port, timeout)
    if sock is None:
        return 0.0
    stream = sock
    try:
        if is_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                stream = ctx.wrap_socket(
                    sock, server_hostname="speed.cloudflare.com")
            except (ssl.SSLError, socket.timeout, OSError):
                _safe_close(sock)
                return 0.0
        path = _SPEED_DOWN_PATH.format(n=sample_bytes)
        start = time.monotonic()
        status, _, body = _http_get(stream, path, "speed.cloudflare.com",
                                    timeout, max_bytes=sample_bytes,
                                    read_full=True)
        _safe_close(stream)
        if status < 200 or status >= 400:
            return 0.0
        n = len(body)
        elapsed = time.monotonic() - start
        if n <= 0 or elapsed <= 0:
            return 0.0
        return n / elapsed
    except (socket.timeout, ConnectionResetError, OSError):
        _safe_close(stream)
        return 0.0


# ---------------------------------------------------------------------------
#  raw socket / HTTP helpers
# ---------------------------------------------------------------------------

def _open_socket(ip: str, port: int,
                 timeout: float):  # pragma: no cover - needs net
    """Connect a raw TCP socket; return it or ``None`` on failure."""
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
        raw.settimeout(timeout)
        return raw
    except OSError:
        return None


def _http_get(stream, path: str, host: str, timeout: float,
              max_bytes: int = 4096,
              read_full: bool = False):  # pragma: no cover - needs net
    """Send ``GET path`` and read the response.

    Returns ``(status, headers_lower, body)``. When *read_full* is True we read
    up to *max_bytes* of body (used by the download sampler); otherwise we stop
    as soon as we have the headers plus a little body (enough to spot the colo).
    """
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: senpaiscanner/1.0\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii", "ignore")
    try:
        stream.sendall(req)
    except OSError:
        return 0, {}, ""

    data = _read_response(stream, timeout, max_bytes=max_bytes,
                          read_full=read_full)
    if not data:
        return 0, {}, ""
    text = data.decode("latin-1", "ignore")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split(" ", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers = {}
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def _read_response(stream, timeout: float, max_bytes: int = 4096,
                   read_full: bool = False):  # pragma: no cover - needs net
    """Read up to *max_bytes* of an HTTP response, bounded by *timeout*."""
    try:
        stream.settimeout(timeout)
    except OSError:
        pass
    chunks = []
    total = 0
    deadline = time.monotonic() + timeout
    while total < max_bytes and time.monotonic() < deadline:
        try:
            chunk = stream.recv(min(8192, max_bytes - total))
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if not read_full:
            joined = b"".join(chunks)
            if b"\r\n\r\n" in joined and total > 200:
                break
    return b"".join(chunks)


def _parse_colo_trace(body: str) -> str:
    """Extract the ``colo=`` field from a /cdn-cgi/trace body."""
    for line in body.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("colo="):
            return line[len("colo="):].strip()
    return ""


def _parse_colo_ray(ray: str) -> str:
    """Extract a 3-letter colo code from a ``CF-Ray`` header value."""
    parts = (ray or "").split("-")
    if len(parts) < 2:
        return ""
    colo = parts[-1].strip()
    if len(colo) < 3:
        return ""
    return colo[:3].upper()


def _normalize_ws_path(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        return "/" + path
    return path


def _fmt_speed(bps: float) -> str:
    """Human-readable throughput (e.g. ``3.2 MB/s``)."""
    if bps <= 0:
        return "—"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    val = float(bps)
    i = 0
    while val >= 1024 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}"


def _safe_close(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
#  relay-path / Cloudflare-host helpers (kept for compatibility)
# ---------------------------------------------------------------------------

def _is_relay_path(path: str) -> bool:
    """True for Worker *relay* paths that embed a nested backend URL.

    Configs like AYYILDIZ use a path of the form
    ``/stars/http://user:pass@vps.webtun.xyz:2087`` — the Worker reads the
    embedded URL and tunnels to that backend. A bare, unauthenticated WS probe
    on such a path is often reset/refused by the relay even when the config
    works end-to-end (which is exactly why Phase 2's real Xray validation is the
    authoritative check for these configs).
    """
    p = (path or "").lower()
    return ("http://" in p) or ("https://" in p) or ("@" in p and ":" in p)


# Hostnames that are *always* fronted by Cloudflare — a bare TLS handshake to
# them proves nothing about whether the specific Worker/Pages route works.
_CLOUDFLARE_HOST_SUFFIXES: tuple[str, ...] = (
    ".pages.dev",
    ".workers.dev",
    ".trycloudflare.com",
    ".cloudflare.com",
    ".cdn.cloudflare.net",
)


def is_cloudflare_host(host: str) -> bool:
    """True when *host* is served by Cloudflare's anycast edge.

    Accepts an IP literal (checked against the published Cloudflare ranges) or a
    hostname (checked against well-known Cloudflare suffixes).
    """
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    try:
        addr = ipaddress.ip_address(h)
        for cidr in CLOUDFLARE_IPV4_CIDRS:
            try:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False
    except ValueError:
        pass
    return any(h == s.lstrip(".") or h.endswith(s)
               for s in _CLOUDFLARE_HOST_SUFFIXES)


# ---------------------------------------------------------------------------
#  IP pool generation (ported from ipsrc.go)
# ---------------------------------------------------------------------------

def parse_ip_list(text: str, *, limit: int = MAX_CANDIDATES_HARD) -> List[str]:
    """Extract candidate IPs from free-form *text* (paste box **or** file).

    Mirrors SenPaiScanner's "From File" loader, which tolerates messy input:

    * one entry per line **or** comma/space separated,
    * ``#`` / ``//`` comments and blank lines ignored,
    * CSV rows accepted (the IP is taken from the first column),
    * ``IP:port`` and ``IP/CIDR`` accepted — a CIDR is **expanded** (host
      addresses only) so a pasted ``104.16.0.0/24`` becomes 254 candidates,
    * duplicates removed while preserving first-seen order.

    Both bare IPs and the result of pasting another user's ``ips.txt`` (which
    may carry ``IP:port`` endpoints) work unchanged.
    """
    out: List[str] = []
    seen: set[str] = set()
    if not text:
        return out
    # split on newlines first, then on commas/whitespace within a line.
    raw_tokens: List[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # a CSV row → keep the first column only (SenPaiScanner behaviour)
        if "," in line:
            line = line.split(",", 1)[0].strip()
        raw_tokens.extend(t for t in line.replace("\t", " ").split(" ") if t)

    def _add(ip: str) -> None:
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)

    for tok in raw_tokens:
        if len(out) >= limit:
            break
        tok = tok.strip().strip(",;")
        if not tok:
            continue
        # strip an "IP:port" suffix (but leave IPv6 alone)
        if tok.count(":") == 1 and "." in tok:
            tok = tok.split(":", 1)[0]
        # CIDR → expand to host addresses (bounded by the remaining budget)
        if "/" in tok:
            try:
                net = ipaddress.ip_network(tok, strict=False)
            except ValueError:
                continue
            hosts = net.hosts() if net.num_addresses > 2 else iter(
                [net.network_address])
            for host in hosts:
                if len(out) >= limit:
                    break
                _add(str(host))
            continue
        try:
            ipaddress.ip_address(tok)
        except ValueError:
            continue
        _add(tok)
    return out


def cf_ip_pool(count: int = 512,
               cidrs: Sequence[str] = CLOUDFLARE_IPV4_CIDRS,
               *, rng: Optional[random.Random] = None) -> List[str]:
    """Return up to *count* random Cloudflare IPs sampled across all ranges.

    Sampling is spread evenly over the CIDR blocks (round-robin) and shuffled,
    so the scan covers the whole anycast space instead of hammering one /13.
    Deterministic when an explicit *rng* is supplied (used by tests).
    """
    r = rng or random.Random()
    networks = []
    for c in cidrs:
        try:
            networks.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue
    if not networks:
        return []
    out: List[str] = []
    seen: set[str] = set()
    guard = count * 20  # avoid an infinite loop on tiny pools
    i = 0
    while len(out) < count and guard > 0:
        net = networks[i % len(networks)]
        i += 1
        guard -= 1
        size = net.num_addresses
        if size <= 2:
            host_int = int(net.network_address)
        else:
            host_int = int(net.network_address) + r.randint(1, size - 2)
        ip = str(ipaddress.ip_address(host_int))
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    r.shuffle(out)
    return out


# ---------------------------------------------------------------------------
#  scanner
# ---------------------------------------------------------------------------

@dataclass
class ScanConfig:
    """Tunables for a scan run.

    The ``port`` / ``server_name`` / ``host`` / ``path`` / ``is_ws`` / ``is_tls``
    fields describe exactly what a clean IP must satisfy *for the config being
    tested* — they feed straight into the :class:`ProbeSpec`.

    SenPaiScanner extras
    --------------------
    tries       : Phase-1 connectivity attempts per IP (SNI rotates each try).
    mode        : ``tcp`` | ``tls`` | ``http`` (http = full edge validation).
    speed_bytes : Phase-1 download sample size (0 disables the speed probe).
    require_ws  : require a successful WebSocket probe for HTTP health.
    """

    port: int = 443
    server_name: str = ""
    host: str = ""               # HTTP Host header (ws/h2 host)
    path: str = "/"              # ws/xhttp path
    is_ws: bool = False          # require a real WebSocket upgrade
    is_tls: bool = True          # transport wrapped in TLS (CDN default)
    timeout: float = 5.0          # per-IP probe timeout (seconds)
    concurrency: int = 64         # parallel probes
    max_candidates: int = 512     # how many IPs to sample/test
    max_results: int = 20         # stop after this many clean IPs found
    max_latency_ms: float = 0.0   # 0 = no cap; else drop slower-than IPs
    # --- SenPaiScanner Phase-1 tunables ---
    tries: int = 4
    mode: str = MODE_HTTP
    speed_bytes: int = 0
    require_ws: bool = False
    # --- SenPaiScanner setup-row extras ---
    source: str = SOURCE_RANDOM   # SOURCE_RANDOM | SOURCE_FILE
    # Extra CDN ports to also probe each IP on (beyond ``port``). Empty = just
    # the config port. Each selected port multiplies Phase-1 work (IPs × ports).
    ports: tuple[int, ...] = ()
    # Phase-2 budget: how many of the best Phase-1 hits to validate with xray.
    # 0 = validate **all** clean hits.
    top_n: int = 0

    def all_ports(self) -> List[int]:
        """The full, de-duplicated port list to probe each IP on.

        Always includes the config ``port`` (the SenPaiScanner "Config" pill),
        plus any extra :attr:`ports`. Order is stable: config port first.
        """
        seen: set[int] = set()
        out: List[int] = []
        for p in (self.port, *self.ports):
            p = int(p)
            if 0 < p < 65536 and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def to_spec(self, port: Optional[int] = None) -> "ProbeSpec":
        return ProbeSpec(
            port=int(port if port is not None else self.port),
            server_name=self.server_name,
            host=self.host or self.server_name, path=self.path or "/",
            is_ws=self.is_ws, is_tls=self.is_tls, tries=self.tries,
            mode=self.mode, speed_bytes=self.speed_bytes,
            require_ws=self.require_ws or self.is_ws)


@dataclass
class ScanReport:
    """Aggregated scan output."""

    config: ScanConfig
    tested: int = 0
    results: List[IPResult] = field(default_factory=list)  # clean IPs only
    stopped_early: bool = False

    @property
    def clean(self) -> List[IPResult]:
        """Clean IPs sorted best-first (lowest avg latency)."""
        return sorted((r for r in self.results if r.ok),
                      key=lambda r: (r.latency_ms or r.avg() or 1e9))


class CFScanner:
    """Sweep Cloudflare IPs and report the clean ones for a given config.

    Parameters
    ----------
    probe_fn     : per-IP Phase-1 probe (injectable). Default :func:`cf_ip_probe`.
                   Signature ``(ip, spec, timeout) -> IPResult``. Tests inject a
                   deterministic fake.
    on_log       : optional ``str -> None`` progress callback.
    on_result    : optional ``IPResult -> None`` fired for each clean hit as it
                   is found (lets the UI stream results live).
    should_stop  : optional ``() -> bool`` polled to cancel the scan early.
    on_phase     : optional ``str -> None`` fired when the scan advances phases.
    on_progress  : optional ``(tested, total, found, last_ip, last_ok) -> None``
                   fired after **every** probe completes — even failed ones —
                   so the UI can show a live "X / Y" counter + progress bar and
                   the user never wonders whether the scan is stuck.
    """

    def __init__(
        self,
        *,
        probe_fn: ProbeFn = cf_ip_probe,
        on_log: Optional[Callable[[str], None]] = None,
        on_result: Optional[Callable[[IPResult], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
        on_progress: Optional[
            Callable[[int, int, int, str, bool], None]] = None,
    ) -> None:
        self.probe_fn = probe_fn
        self._on_log = on_log
        self._on_result = on_result
        self._should_stop = should_stop
        self._on_phase = on_phase
        self._on_progress = on_progress
        self._stop_flag = threading.Event()

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _phase(self, msg: str) -> None:
        if self._on_phase:
            try:
                self._on_phase(msg)
            except Exception:
                pass

    def _progress(self, tested: int, total: int, found: int,
                  last_ip: str, last_ok: bool) -> None:
        if self._on_progress:
            try:
                self._on_progress(tested, total, found, last_ip, last_ok)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_flag.set()

    def _stopping(self) -> bool:
        if self._stop_flag.is_set():
            return True
        if self._should_stop is not None:
            try:
                return bool(self._should_stop())
            except Exception:
                return False
        return False

    def scan(self, cfg: ScanConfig,
             ips: Optional[Sequence[str]] = None) -> ScanReport:
        """Run the Phase-1 sweep. Blocking — call on a worker thread.

        *ips* lets the caller supply an explicit candidate list (tests / custom
        pools); otherwise a fresh Cloudflare sample of ``cfg.max_candidates`` is
        generated. Fully fail-soft: a bad probe never aborts the whole run.
        """
        report = ScanReport(config=cfg)
        if ips is not None:
            base_ips = list(ips)
        else:
            base_ips = cf_ip_pool(min(cfg.max_candidates, MAX_CANDIDATES_HARD))

        ports = cfg.all_ports()
        if not base_ips or not ports:
            self._log("اسکن لغو شد — لیست IP یا پورت نامعتبر است")
            return report

        # SenPaiScanner multi-port: every IP is probed on every selected port,
        # so the candidate set is the cartesian product (ip × port). Phase 1
        # work = len(IPs) × len(ports) — surfaced in the log so a big port list
        # doesn't silently balloon the run.
        candidates = [(ip, port) for ip in base_ips for port in ports]

        ws_note = " · WS" if (cfg.is_ws or cfg.require_ws) else ""
        ports_note = ("پورت " + ", ".join(str(p) for p in ports)
                      if len(ports) == 1
                      else f"{len(ports)} پورت ({', '.join(str(p) for p in ports)})")
        self._phase("phase1")
        self._log(f"فاز ۱ — پروب اتصال {len(base_ips)} IP کلودفلر روی "
                  f"{ports_note} (SNI: {cfg.server_name or '—'}{ws_note}, "
                  f"تلاش: {cfg.tries}) — مجموعاً {len(candidates)} پروب …")

        total = len(candidates)
        workers = max(1, min(int(cfg.concurrency), 512))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._probe_one, ip, port, cfg): (ip, port)
                for ip, port in candidates
            }
            try:
                for fut in as_completed(futures):
                    if self._stopping():
                        report.stopped_early = True
                        break
                    res = fut.result()
                    report.tested += 1
                    found = len([r for r in report.results if r.ok])
                    if res is None:
                        # cancelled probe — still tick the progress bar
                        self._progress(report.tested, total, found, "", False)
                        continue
                    accepted = res.ok and self._accept(res, cfg)
                    if accepted:
                        report.results.append(res)
                        found += 1
                        extra = f" · {res.detail}" if res.detail else ""
                        self._log(f"✓ IP تمیز: {res.ip}:{res.port} "
                                  f"({res.latency_ms:.0f}ms{extra})")
                        if self._on_result:
                            try:
                                self._on_result(res)
                            except Exception:
                                pass
                    # always report progress so the UI shows live activity
                    self._progress(report.tested, total, found,
                                   res.ip, accepted)
                    if (accepted and cfg.max_results > 0
                            and found >= cfg.max_results):
                        report.stopped_early = True
                        break
            finally:
                for fut in futures:
                    fut.cancel()

        clean = report.clean
        self._log(f"فاز ۱ تمام شد — {len(clean)} IP تمیز از "
                  f"{report.tested} پروب آزمایش‌شده پیدا شد")
        return report

    def _accept(self, res: IPResult, cfg: ScanConfig) -> bool:
        if cfg.max_latency_ms and res.latency_ms > cfg.max_latency_ms:
            return False
        return True

    def _probe_one(self, ip: str, port: int,
                   cfg: ScanConfig) -> Optional[IPResult]:
        if self._stopping():
            return None
        try:
            res = self.probe_fn(ip, cfg.to_spec(port), cfg.timeout)
            # make sure the result records which port it was probed on, even if
            # an injected fake probe forgot to set it.
            if res is not None and not getattr(res, "port", 0):
                res.port = port
            return res
        except Exception as exc:  # never let one bad probe kill the sweep
            return IPResult(ip, ERROR, detail=repr(exc), port=port)


# ---------------------------------------------------------------------------
#  config-aware helpers
# ---------------------------------------------------------------------------

def scan_config_from_profile(profile, **overrides) -> ScanConfig:
    """Build a :class:`ScanConfig` from a profile (full config-accurate probe).

    The clean IP must answer on the *same port* the config dials, accept the
    *same SNI* the config presents, and — for a WebSocket config — carry a WS
    upgrade on the config's Host + path. ``overrides`` tweak any field
    (timeout / concurrency / limits / tries / speed_bytes …).
    """
    port = int(getattr(profile, "port", 0) or 443)
    sni = (getattr(profile, "sni", "") or getattr(profile, "host", "")
           or getattr(profile, "address", "") or "")
    host = (getattr(profile, "host", "") or sni or "")
    path = (getattr(profile, "path", "") or "/")
    transport = (getattr(profile, "transport", "") or "tcp").lower()
    is_ws = transport in ("ws", "websocket", "httpupgrade")
    is_tls = bool(getattr(profile, "is_tls", True))
    cfg = ScanConfig(port=port, server_name=str(sni), host=str(host),
                     path=str(path), is_ws=is_ws, is_tls=is_tls,
                     require_ws=is_ws)
    for k, v in overrides.items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, v)
    return cfg


def profile_with_ip(profile, ip: str, *, suffix: str = ""):
    """Return a *copy* of *profile* with its server address swapped to *ip*.

    Everything else (uuid/password, transport, TLS/SNI, host header, path …)
    is preserved exactly, so the new config is the original config delivered
    over a clean IP. The remark gets a short suffix so the user can tell the
    clean-IP variants apart in the list.
    """
    from core.profile import Profile  # local import to avoid a cycle

    data = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
    data["address"] = ip
    base_remark = data.get("remark", "") or "config"
    tag = suffix or f"CF {ip}"
    data["remark"] = f"{base_remark} · {tag}"
    data["raw"] = ""
    return Profile.from_dict(data)


# ---------------------------------------------------------------------------
#  result export (SenPaiScanner's live "ips.txt" / result file)
# ---------------------------------------------------------------------------

def format_endpoints(results: Sequence[IPResult]) -> List[str]:
    """Return ``IP:port`` strings for the given results (clean, paste-ready).

    Mirrors SenPaiScanner's export lines (e.g. ``104.16.72.162:443``) which
    drop straight into client configs / IP lists. Duplicates are removed while
    preserving order.
    """
    out: List[str] = []
    seen: set[str] = set()
    for r in results:
        ep = f"{r.ip}:{int(getattr(r, 'port', 0) or 443)}"
        if ep not in seen:
            seen.add(ep)
            out.append(ep)
    return out


def write_result_file(path: str, results: Sequence[IPResult]) -> str:
    """Write ``IP:port`` endpoints to *path* (one per line) and return *path*.

    Used for both the live, continuously-updated result file SenPaiScanner
    keeps next to the binary and the on-demand "export" the user triggers.
    Fail-soft: any I/O error is swallowed so a read-only CWD never crashes a
    scan.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(format_endpoints(results)))
            fh.write("\n")
    except Exception:
        pass
    return path


def default_result_filename() -> str:
    """``SenPaiScannerResult-YYYYMMDD-HHMMSS.txt`` (SenPaiScanner naming)."""
    return time.strftime("SenPaiScannerResult-%Y%m%d-%H%M%S.txt")
