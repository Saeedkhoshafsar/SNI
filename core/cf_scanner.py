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


# (ip, port, server_name, timeout) -> IPResult
ProbeFn = Callable[[str, int, str, float], IPResult]


def tls_ip_probe(ip: str, port: int, server_name: str,
                 timeout: float) -> IPResult:  # pragma: no cover - needs net
    """Real probe: TCP-connect to *ip:port* and drive a TLS handshake with SNI.

    A clean Cloudflare IP completes the TLS handshake when presented the
    config's SNI; a blocked / dead IP times out or is reset. Certificate
    validation is disabled on purpose — we only care that the edge spoke TLS
    back (so a self-signed / mismatched cert still counts as reachable), which
    is exactly what makes the config usable on that IP.
    """
    start = time.monotonic()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.connect((ip, port))
    except socket.timeout:
        _safe_close(raw)
        return IPResult(ip, TIMEOUT, detail="connect timeout")
    except ConnectionResetError:
        _safe_close(raw)
        return IPResult(ip, RST, detail="connection reset")
    except OSError as exc:
        _safe_close(raw)
        return IPResult(ip, ERROR, detail=str(exc))

    sni = (server_name or "").strip().strip("[]")
    if not sni:
        latency = (time.monotonic() - start) * 1000.0
        _safe_close(raw)
        return IPResult(ip, OK, latency_ms=latency, detail="tcp-only (no sni)")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        tls = ctx.wrap_socket(raw, server_hostname=sni)
        latency = (time.monotonic() - start) * 1000.0
        _safe_close(tls)
        return IPResult(ip, OK, latency_ms=latency, detail="tls ok")
    except socket.timeout:
        return IPResult(ip, TIMEOUT, detail="tls handshake timeout")
    except ConnectionResetError:
        return IPResult(ip, RST, detail="tls reset")
    except ssl.SSLError as exc:
        # peer answered in TLS (even an alert) → reachable for our purposes.
        latency = (time.monotonic() - start) * 1000.0
        _safe_close(raw)
        return IPResult(ip, OK, latency_ms=latency,
                        detail=f"tls alert: {exc.__class__.__name__}")
    except OSError as exc:
        return IPResult(ip, ERROR, detail=str(exc))
    finally:
        _safe_close(raw)


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
    """Tunables for a scan run."""

    port: int = 443
    server_name: str = ""
    timeout: float = 3.0          # per-IP probe timeout (seconds)
    concurrency: int = 64         # parallel probes
    max_candidates: int = 512     # how many IPs to sample/test
    max_results: int = 20         # stop after this many clean IPs found
    max_latency_ms: float = 0.0   # 0 = no cap; else drop slower-than IPs


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
    probe_fn     : per-IP probe (injectable). Default real :func:`tls_ip_probe`.
    on_log       : optional ``str -> None`` progress callback.
    on_result    : optional ``IPResult -> None`` fired for each clean hit as it
                   is found (lets the UI stream results live).
    should_stop  : optional ``() -> bool`` polled to cancel the scan early.
    """

    def __init__(
        self,
        *,
        probe_fn: ProbeFn = tls_ip_probe,
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
        self._log(f"شروع اسکن {len(candidates)} IP کلودفلر روی پورت "
                  f"{cfg.port} (SNI: {cfg.server_name or '—'}) …")

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
            return self.probe_fn(ip, cfg.port, cfg.server_name, cfg.timeout)
        except Exception as exc:  # never let one bad probe kill the sweep
            return IPResult(ip, ERROR, detail=repr(exc))


# ---------------------------------------------------------------------------
#  config-aware helpers
# ---------------------------------------------------------------------------

def scan_config_from_profile(profile, **overrides) -> ScanConfig:
    """Build a :class:`ScanConfig` from a profile (port + SNI to validate).

    The clean IP must answer on the *same port* the config dials and accept the
    *same SNI* the config presents — so we test exactly what the real session
    needs. ``overrides`` tweak any field (timeout / concurrency / limits).
    """
    port = int(getattr(profile, "port", 0) or 443)
    sni = (getattr(profile, "sni", "") or getattr(profile, "host", "")
           or getattr(profile, "address", "") or "")
    cfg = ScanConfig(port=port, server_name=str(sni))
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
