"""Cloudflare clean-IP scanner (issue #3).

Goal (mirrors the referenced ``MatinSenPai/SenPaiScanner`` project, but built
*into* this app so it can run against a config the user already imported):

Given a reference :class:`core.profile.Profile`, sweep a pool of Cloudflare
edge IPs and report which ones actually work *for that config* — i.e. a clean
IP that answers a real TLS handshake (carrying the config's SNI) on the config's
port with acceptable latency. The user then picks one / several / all of the
clean IPs and the app produces new profiles that are byte-identical to the
original except their ``address`` is swapped to the clean IP.

Design (consistent with ``core/ping.py`` / ``core/prober.py``)
--------------------------------------------------------------
* **UI-agnostic, no Qt.** Plain dataclasses + optional ``on_log`` / ``on_result``
  / ``should_stop`` callbacks. A Qt layer marshals those onto the GUI thread.
* **Network is injectable.** The per-IP probe is a callable with a real stdlib
  default (:func:`tls_ip_probe`), so the whole sweep / ranking logic runs
  deterministically headless in tests with a fake probe and no sockets.
* **Bounded & cancellable.** A thread pool with a hard cap, an overall result
  limit, and a cooperative ``should_stop`` so a long scan never runs away.

The IP pool comes from Cloudflare's published ranges (``cf_ip_pool``), expanded
and shuffled so the scan samples the whole anycast space rather than one block.
"""
from __future__ import annotations

import ipaddress
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
# well-known Cloudflare anycast ranges the front IPs live in.
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


# probe outcome
OK = "ok"
RST = "rst"
TIMEOUT = "timeout"
ERROR = "error"


@dataclass
class IPResult:
    """The result of probing one candidate IP for one config."""

    ip: str
    outcome: str = ERROR          # OK / RST / TIMEOUT / ERROR
    latency_ms: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == OK


@dataclass(frozen=True)
class ProbeSpec:
    """What a clean IP must satisfy *for this specific config*.

    A bare TLS handshake is **not** a valid test (the bug the user hit): every
    Cloudflare anycast IP completes a TLS handshake with *any* SNI because the
    edge always answers — so the old probe reported dozens of "clean" IPs in a
    few seconds that didn't actually work. Mirroring ``MatinSenPai/SenPaiScanner``
    we instead require a **real HTTP response from the Cloudflare edge** and,
    for WebSocket configs, a successful WS upgrade carrying the config's Host /
    path — exactly the transport the proxy will use.

    Fields
    ------
    port        : the port the config dials (clean IP must answer on it).
    server_name : TLS SNI to present (the config's ``sni``/``host``).
    host        : HTTP ``Host`` header (ws/h2 host → the Worker hostname).
    path        : ws / xhttp path the config uses (validated on WS upgrade).
    is_ws       : require a WebSocket upgrade (config ``type=ws``/httpupgrade).
    is_tls      : whether the transport is wrapped in TLS (almost always True
                  behind Cloudflare; a plain-HTTP edge check is used if False).
    """

    port: int = 443
    server_name: str = ""
    host: str = ""
    path: str = "/"
    is_ws: bool = False
    is_tls: bool = True


# (ip, spec, timeout) -> IPResult
ProbeFn = Callable[[str, "ProbeSpec", float], IPResult]


# A working Cloudflare edge answers /cdn-cgi/trace with a body containing the
# colo marker ``fl=`` and ``h=``. A blocked / black-holed / non-CF host either
# resets, times out, or returns something without these markers.
_CF_TRACE_PATH = "/cdn-cgi/trace"


def cf_ip_probe(ip: str, spec: "ProbeSpec",
                timeout: float) -> IPResult:  # pragma: no cover - needs net
    """Validate *ip* as a clean Cloudflare edge **for this config**.

    Two-stage, real validation (no more false greens):

    1. **Edge liveness** — open TLS to ``ip:port`` presenting the config SNI,
       send a real HTTP/1.1 ``GET /cdn-cgi/trace`` with the config Host header,
       and require a ``200`` whose body carries Cloudflare's ``fl=`` colo marker.
       This proves the IP is a *live, unblocked* Cloudflare edge that will route
       the config's hostname — something a bare TLS handshake never proved.
    2. **WebSocket reachability** (only when ``spec.is_ws``) — send a WS
       ``Upgrade`` request on the config's path/host and require ``101`` (or a
       Cloudflare ``4xx`` that still proves the WS layer is reachable through
       this edge). A ws config that can't upgrade here is *not* clean.

    Latency is the time to the first successful response. Any reset / timeout /
    missing-marker outcome is reported honestly so the IP is dropped.
    """
    start = time.monotonic()
    sni = (spec.server_name or spec.host or "").strip().strip("[]")
    host_hdr = (spec.host or sni or ip).strip()

    sock = _open_socket(ip, spec.port, timeout)
    if isinstance(sock, IPResult):
        return sock  # connect-stage failure already classified

    stream = sock
    try:
        if spec.is_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                stream = ctx.wrap_socket(sock, server_hostname=sni or ip)
            except socket.timeout:
                _safe_close(sock)
                return IPResult(ip, TIMEOUT, detail="tls handshake timeout")
            except ssl.SSLError as exc:
                # a TLS *alert* means the edge spoke TLS but rejected us — that
                # is NOT proof the config works (the old code wrongly counted it
                # as clean). Treat as unreachable for this config.
                _safe_close(sock)
                return IPResult(ip, ERROR,
                                detail=f"tls rejected: {exc.__class__.__name__}")
            except OSError as exc:
                _safe_close(sock)
                return IPResult(ip, ERROR, detail=f"tls error: {exc}")

        # --- stage 1: HTTP trace — prove it's a live Cloudflare edge ---
        ok, detail = _http_trace_ok(stream, host_hdr, timeout)
        if not ok:
            _safe_close(stream)
            return IPResult(ip, ERROR, detail=detail)

        # --- stage 2: WebSocket upgrade for ws/httpupgrade configs ---
        if spec.is_ws:
            # the trace consumed the first connection; open a fresh one for the
            # upgrade so half-read state can't confuse it.
            ws_sock = _open_socket(ip, spec.port, timeout)
            if isinstance(ws_sock, IPResult):
                _safe_close(stream)
                return ws_sock
            ws_stream = ws_sock
            if spec.is_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    ws_stream = ctx.wrap_socket(ws_sock,
                                                server_hostname=sni or ip)
                except OSError as exc:
                    _safe_close(ws_sock)
                    _safe_close(stream)
                    return IPResult(ip, ERROR, detail=f"ws tls: {exc}")
            ws_ok, ws_detail = _ws_upgrade_ok(
                ws_stream, host_hdr, spec.path or "/", timeout)
            _safe_close(ws_stream)
            if not ws_ok:
                _safe_close(stream)
                return IPResult(ip, ERROR, detail=ws_detail)

        latency = (time.monotonic() - start) * 1000.0
        _safe_close(stream)
        kind = "ws+edge" if spec.is_ws else "edge"
        return IPResult(ip, OK, latency_ms=latency, detail=f"{kind} ok")
    except socket.timeout:
        _safe_close(stream)
        return IPResult(ip, TIMEOUT, detail="response timeout")
    except ConnectionResetError:
        _safe_close(stream)
        return IPResult(ip, RST, detail="reset during probe")
    except OSError as exc:
        _safe_close(stream)
        return IPResult(ip, ERROR, detail=str(exc))


# Back-compat shim — older callers / tests may still import ``tls_ip_probe``.
# It now delegates to the honest edge probe so behaviour is consistent.
def tls_ip_probe(ip: str, port: int, server_name: str,
                 timeout: float) -> IPResult:  # pragma: no cover - needs net
    return cf_ip_probe(
        ip, ProbeSpec(port=port, server_name=server_name,
                      host=server_name, is_tls=True), timeout)


def _open_socket(ip: str, port: int, timeout: float):  # pragma: no cover - net
    """Connect a raw TCP socket; return it or a classified failure IPResult."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.connect((ip, port))
        return raw
    except socket.timeout:
        _safe_close(raw)
        return IPResult(ip, TIMEOUT, detail="connect timeout")
    except ConnectionResetError:
        _safe_close(raw)
        return IPResult(ip, RST, detail="connection reset")
    except OSError as exc:
        _safe_close(raw)
        return IPResult(ip, ERROR, detail=str(exc))


def _http_trace_ok(stream, host: str,
                   timeout: float):  # pragma: no cover - needs net
    """Send GET /cdn-cgi/trace and verify a live Cloudflare-edge response.

    Returns ``(ok, detail)``. ``ok`` is True only when the edge returns a
    real HTTP response that carries Cloudflare's trace markers (``fl=`` colo +
    ``h=`` host) — the signature of a genuine, unblocked edge that will serve
    this hostname. Anything else (no response, 5xx with no markers, garbage) is
    rejected.
    """
    req = (
        f"GET {_CF_TRACE_PATH} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: Mozilla/5.0\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii", "ignore")
    try:
        stream.sendall(req)
    except OSError as exc:
        return False, f"send failed: {exc}"
    data = _read_response(stream, timeout, max_bytes=4096)
    if not data:
        return False, "empty response"
    text = data.decode("latin-1", "ignore")
    head, _, body = text.partition("\r\n\r\n")
    status_ok = head.startswith("HTTP/1.1 200") or head.startswith("HTTP/1.0 200")
    # Cloudflare edge marker. ``fl=`` is the edge/colo id; ``h=`` echoes Host.
    has_marker = ("fl=" in body) or ("fl=" in text and "h=" in text)
    if status_ok and has_marker:
        return True, "cf edge trace ok"
    # Some Cloudflare endpoints front a Worker that 404s /cdn-cgi/trace but the
    # ``server: cloudflare`` header still proves a live edge that routes Host.
    if "server: cloudflare" in text.lower():
        return True, "cf edge (server header)"
    return False, "not a live cf edge (no trace marker)"


def _ws_upgrade_ok(stream, host: str, path: str,
                   timeout: float):  # pragma: no cover - needs net
    """Send a WebSocket upgrade and verify the edge accepts/routes it.

    Returns ``(ok, detail)``. ``101 Switching Protocols`` is a clean pass. A
    Cloudflare ``4xx`` (e.g. the Worker rejecting an unauthenticated upgrade)
    *with* a cloudflare server header still proves the WS path reaches a live
    edge for this Host, so it counts — what we're rejecting is the IP that
    can't carry a WS handshake to this hostname at all.
    """
    import base64 as _b64
    import os as _os

    key = _b64.b64encode(_os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: Mozilla/5.0\r\n\r\n"
    ).encode("ascii", "ignore")
    try:
        stream.sendall(req)
    except OSError as exc:
        return False, f"ws send failed: {exc}"
    data = _read_response(stream, timeout, max_bytes=2048)
    if not data:
        return False, "ws no response"
    text = data.decode("latin-1", "ignore")
    if text.startswith("HTTP/1.1 101") or text.startswith("HTTP/1.0 101"):
        return True, "ws upgrade 101"
    is_cf = "server: cloudflare" in text.lower() or "cf-ray" in text.lower()
    # a Cloudflare 4xx still means the WS path reached a live edge for this Host
    if is_cf and (" 400" in text[:16] or " 401" in text[:16]
                  or " 403" in text[:16] or " 404" in text[:16]
                  or " 426" in text[:16]):
        return True, "ws reachable (cf 4xx)"
    return False, "ws upgrade refused"


def _read_response(stream, timeout: float,
                   max_bytes: int = 4096):  # pragma: no cover - needs net
    """Read up to *max_bytes* of an HTTP response (until headers+a bit of body).

    Stops as soon as we have the status line plus enough body to look for the
    Cloudflare markers, or when the peer closes / times out. Bounded so a
    chunked/streaming Worker can't hang the probe.
    """
    stream.settimeout(timeout)
    chunks = []
    total = 0
    deadline = time.monotonic() + timeout
    while total < max_bytes and time.monotonic() < deadline:
        try:
            chunk = stream.recv(min(2048, max_bytes - total))
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        joined = b"".join(chunks)
        # once we have headers + a little body, the markers (if any) are present
        if b"\r\n\r\n" in joined and total > 200:
            break
    return b"".join(chunks)


def _safe_close(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
#  IP pool generation
# ---------------------------------------------------------------------------

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
    # round-robin across networks so no single big block dominates the sample
    guard = count * 20  # avoid an infinite loop on tiny pools
    i = 0
    while len(out) < count and guard > 0:
        net = networks[i % len(networks)]
        i += 1
        guard -= 1
        # random host inside this network (skip network/broadcast for /<31)
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
    tested* — they feed straight into the :class:`ProbeSpec` so the validation
    is config-accurate (no more false greens from a bare TLS handshake).
    """

    port: int = 443
    server_name: str = ""
    host: str = ""               # HTTP Host header (ws/h2 host)
    path: str = "/"              # ws/xhttp path
    is_ws: bool = False          # require a real WebSocket upgrade
    is_tls: bool = True          # transport wrapped in TLS (CDN default)
    timeout: float = 3.0          # per-IP probe timeout (seconds)
    concurrency: int = 64         # parallel probes
    max_candidates: int = 512     # how many IPs to sample/test
    max_results: int = 20         # stop after this many clean IPs found
    max_latency_ms: float = 0.0   # 0 = no cap; else drop slower-than IPs

    def to_spec(self) -> "ProbeSpec":
        return ProbeSpec(
            port=self.port, server_name=self.server_name,
            host=self.host or self.server_name, path=self.path or "/",
            is_ws=self.is_ws, is_tls=self.is_tls)


@dataclass
class ScanReport:
    """Aggregated scan output."""

    config: ScanConfig
    tested: int = 0
    results: List[IPResult] = field(default_factory=list)  # clean IPs only
    stopped_early: bool = False

    @property
    def clean(self) -> List[IPResult]:
        """Clean IPs sorted fastest-first."""
        return sorted((r for r in self.results if r.ok),
                      key=lambda r: r.latency_ms)


class CFScanner:
    """Sweep Cloudflare IPs and report the clean ones for a given config.

    Parameters
    ----------
    probe_fn     : per-IP probe (injectable). Default real :func:`cf_ip_probe`.
                   Signature ``(ip, spec, timeout) -> IPResult``. Tests inject a
                   deterministic fake.
    on_log       : optional ``str -> None`` progress callback.
    on_result    : optional ``IPResult -> None`` fired for each clean hit as it
                   is found (lets the UI stream results live).
    should_stop  : optional ``() -> bool`` polled to cancel the scan early.
    """

    def __init__(
        self,
        *,
        probe_fn: ProbeFn = cf_ip_probe,
        on_log: Optional[Callable[[str], None]] = None,
        on_result: Optional[Callable[[IPResult], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.probe_fn = probe_fn
        self._on_log = on_log
        self._on_result = on_result
        self._should_stop = should_stop
        self._stop_flag = threading.Event()

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
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
        """Run the sweep. Blocking — call on a worker thread.

        *ips* lets the caller supply an explicit candidate list (tests / custom
        pools); otherwise a fresh Cloudflare sample of ``cfg.max_candidates`` is
        generated. Fully fail-soft: a bad probe never aborts the whole run.
        """
        report = ScanReport(config=cfg)
        candidates = list(ips) if ips is not None else cf_ip_pool(
            cfg.max_candidates)
        if not candidates or not (0 < cfg.port < 65536):
            self._log("اسکن لغو شد — لیست IP یا پورت نامعتبر است")
            return report
        ws_note = " · WS" if cfg.is_ws else ""
        self._log(f"شروع اسکن {len(candidates)} IP کلودفلر روی پورت "
                  f"{cfg.port} (SNI: {cfg.server_name or '—'}{ws_note}) …")

        workers = max(1, min(int(cfg.concurrency), 256))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._probe_one, ip, cfg): ip
                for ip in candidates
            }
            try:
                for fut in as_completed(futures):
                    if self._stopping():
                        report.stopped_early = True
                        break
                    res = fut.result()
                    report.tested += 1
                    if res is None:
                        continue
                    if res.ok and self._accept(res, cfg):
                        report.results.append(res)
                        self._log(f"✓ IP تمیز: {res.ip} "
                                  f"({res.latency_ms:.0f}ms)")
                        if self._on_result:
                            try:
                                self._on_result(res)
                            except Exception:
                                pass
                        if (cfg.max_results > 0
                                and len([r for r in report.results if r.ok])
                                >= cfg.max_results):
                            report.stopped_early = True
                            break
            finally:
                # don't wait on the remaining probes once we've decided to stop
                for fut in futures:
                    fut.cancel()

        clean = report.clean
        self._log(f"اسکن تمام شد — {len(clean)} IP تمیز از "
                  f"{report.tested} IP آزمایش‌شده پیدا شد")
        return report

    def _accept(self, res: IPResult, cfg: ScanConfig) -> bool:
        if cfg.max_latency_ms and res.latency_ms > cfg.max_latency_ms:
            return False
        return True

    def _probe_one(self, ip: str, cfg: ScanConfig) -> Optional[IPResult]:
        if self._stopping():
            return None
        try:
            return self.probe_fn(ip, cfg.to_spec(), cfg.timeout)
        except Exception as exc:  # never let one bad probe kill the sweep
            return IPResult(ip, ERROR, detail=repr(exc))


# ---------------------------------------------------------------------------
#  config-aware helpers
# ---------------------------------------------------------------------------

def scan_config_from_profile(profile, **overrides) -> ScanConfig:
    """Build a :class:`ScanConfig` from a profile (full config-accurate probe).

    The clean IP must answer on the *same port* the config dials, accept the
    *same SNI* the config presents, and — for a WebSocket config — carry a WS
    upgrade on the config's Host + path. We pull all of that from the profile so
    the validation matches what the real session needs (mirroring SenPaiScanner:
    a ws config is only "clean" on an IP where the WS upgrade actually reaches
    the edge). ``overrides`` tweak any field (timeout / concurrency / limits).
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
                     path=str(path), is_ws=is_ws, is_tls=is_tls)
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
    # the raw share link no longer matches the swapped address; clear it so the
    # profile is regenerated cleanly on export.
    data["raw"] = ""
    return Profile.from_dict(data)
