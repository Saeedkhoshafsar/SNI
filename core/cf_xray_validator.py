"""Phase 2 — real Xray end-to-end validation of clean-IP candidates.

This is the Python port of ``MatinSenPai/SenPaiScanner``'s
``internal/xraytest/runner.go``, adapted to drive the **bundled ``xray.exe``**
that this app already ships (instead of embedding xray-core as a Go library).

What it does, per candidate IP
------------------------------
1. Build the *real* Xray config for the reference :class:`core.profile.Profile`
   with the candidate IP swapped into the outbound address, exposing a SOCKS5
   inbound on a private localhost port (via :func:`core.xray_config.build_config`
   — the exact same builder the live tunnel uses, so the transport / TLS / SNI /
   ws-or-grpc settings are identical to what the user will actually run).
2. Launch ``xray.exe run -config <tmp>`` and wait for the SOCKS port to open.
3. **Connectivity check** — issue ``GET https://cp.cloudflare.com/cdn-cgi/trace``
   *through* the SOCKS5 proxy and require the body to contain ``colo=`` (proves
   genuine Cloudflare traffic flowed end-to-end through the user's config). The
   time-to-first-byte is the honest proxied latency.
4. **Speed sample** (best-effort, never fails the result) — download a small
   payload through the proxy and report throughput.

A failure is retried once (DPI is flaky), mirroring ``ValidateConfig``.

Everything here is **fail-soft and headless-testable**: the heavy bits (xray
process + the proxied HTTP calls) are injectable so the orchestration logic runs
in unit tests without spawning anything or touching the network.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# Speed-sample sizes (bytes), mirroring runner.go's constants.
SPEED_SAMPLE_BYTES = 512 * 1024
SPEED_SAMPLE_BYTES_FAST = 128 * 1024
SPEED_MIN_BYTES = 8 * 1024

# The trusted connectivity probe URL (same endpoint SenPaiScanner uses).
TRACE_PROBE_HOST = "cp.cloudflare.com"
TRACE_PROBE_PATH = "/cdn-cgi/trace"


@dataclass
class XrayValidation:
    """Outcome of validating one candidate IP through a real Xray instance."""

    ip: str
    port: int = 443
    success: bool = False
    latency_ms: float = 0.0       # time-to-first-byte through the proxy
    throughput_bps: float = 0.0   # download throughput through the proxy
    bytes_recv: int = 0
    error: str = ""
    transport: str = ""
    retries: int = 0


# ---------------------------------------------------------------------------
#  self-contained SOCKS5 HTTP client (no PySocks dependency)
# ---------------------------------------------------------------------------

def _socks5_connect(proxy_host: str, proxy_port: int, dst_host: str,
                    dst_port: int, timeout: float) -> socket.socket:
    """Open a TCP socket to *dst* through a no-auth SOCKS5 proxy.

    Raises ``OSError`` on any failure. Implements just enough of RFC 1928 to
    CONNECT to a remote host by name (so the proxy resolves it) — which is all
    the validator needs.
    """
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        s.settimeout(timeout)
        # greeting: VER=5, NMETHODS=1, METHOD=0 (no auth)
        s.sendall(b"\x05\x01\x00")
        resp = _recv_exact(s, 2)
        if resp[0] != 0x05 or resp[1] != 0x00:
            raise OSError("socks5: no acceptable auth method")
        # CONNECT (CMD=1) to a domain name (ATYP=3)
        host_bytes = dst_host.encode("idna") if _is_hostname(dst_host) \
            else dst_host.encode("ascii")
        if _is_hostname(dst_host):
            req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes
        else:
            # numeric IPv4 literal → ATYP=1
            req = b"\x05\x01\x00\x01" + socket.inet_aton(dst_host)
        req += struct.pack(">H", dst_port)
        s.sendall(req)
        reply = _recv_exact(s, 4)
        if reply[1] != 0x00:
            raise OSError(f"socks5: connect failed (rep={reply[1]})")
        atyp = reply[3]
        if atyp == 0x01:
            _recv_exact(s, 4)
        elif atyp == 0x04:
            _recv_exact(s, 16)
        elif atyp == 0x03:
            ln = _recv_exact(s, 1)[0]
            _recv_exact(s, ln)
        _recv_exact(s, 2)  # bound port
        return s
    except OSError:
        try:
            s.close()
        except OSError:
            pass
        raise


def _is_hostname(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return False
    except OSError:
        return True


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("socks5: short read")
        buf += chunk
    return buf


def _https_get_through_socks(proxy_port: int, host: str, path: str,
                             timeout: float, max_bytes: int,
                             read_full: bool = False
                             ) -> tuple[int, bytes, float]:
    """``GET https://host+path`` through the local SOCKS5 proxy.

    Returns ``(status, body, ttfb_ms)``. The TLS is terminated *here* with the
    real ``host`` as SNI, so the request looks exactly like a browser's. TTFB is
    measured from request send to the first response byte.
    """
    import ssl as _ssl

    raw = _socks5_connect("127.0.0.1", proxy_port, host, 443, timeout)
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        conn = ctx.wrap_socket(raw, server_hostname=host)
        conn.settimeout(timeout)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: senpaiscanner/1.0\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        start = time.monotonic()
        conn.sendall(req)
        # read headers first to capture TTFB + status
        data = b""
        ttfb = 0.0
        deadline = time.monotonic() + timeout
        header_end = -1
        while time.monotonic() < deadline:
            try:
                chunk = conn.recv(8192)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            if ttfb == 0.0:
                ttfb = (time.monotonic() - start) * 1000.0
            data += chunk
            header_end = data.find(b"\r\n\r\n")
            if header_end != -1 and not read_full:
                if len(data) - (header_end + 4) > 256 or not read_full:
                    break
            if read_full and len(data) >= max_bytes:
                break
        # continue reading body if a full read was requested
        if read_full:
            while len(data) < max_bytes and time.monotonic() < deadline:
                try:
                    chunk = conn.recv(8192)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                data += chunk
        try:
            conn.close()
        except OSError:
            pass
        status = 0
        body = b""
        if data.startswith(b"HTTP/"):
            line_end = data.find(b"\r\n")
            if line_end != -1:
                parts = data[:line_end].split(b" ")
                if len(parts) >= 2 and parts[1].isdigit():
                    status = int(parts[1])
            he = data.find(b"\r\n\r\n")
            if he != -1:
                body = data[he + 4:]
        return status, body, ttfb
    except OSError:
        try:
            raw.close()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
#  default real checks (network) — injectable for tests
# ---------------------------------------------------------------------------

def _default_connectivity_check(proxy_port: int,
                                timeout: float):  # pragma: no cover - net
    """GET cp.cloudflare.com/cdn-cgi/trace through the proxy; require ``colo=``.

    Returns ``(ok, ttfb_ms, error)``.
    """
    try:
        status, body, ttfb = _https_get_through_socks(
            proxy_port, TRACE_PROBE_HOST, TRACE_PROBE_PATH, timeout, 2048)
    except OSError as exc:
        return False, 0.0, str(exc)
    if status != 200:
        return False, ttfb, f"HTTP {status}"
    if b"colo=" not in body:
        return False, ttfb, "no colo in trace response"
    return True, ttfb, ""


def _default_speed_check(proxy_port: int, profile, timeout: float
                         ):  # pragma: no cover - net
    """Download a small sample through the proxy; return ``(bytes, bps)``.

    Mirrors ``measureProxySpeed``: try the config host, then
    speed.cloudflare.com, then www.cloudflare.com. Best-effort — a 0 result
    never fails the validation.
    """
    targets: List[tuple[str, str]] = []
    host = (getattr(profile, "host", "") or getattr(profile, "sni", "") or "")
    path = (getattr(profile, "path", "") or "/")
    if host:
        if not path.startswith("/"):
            path = "/" + path
        targets.append((host, path))
        targets.append((host, "/"))
    targets.append(("speed.cloudflare.com",
                    f"/__down?bytes={SPEED_SAMPLE_BYTES_FAST}"))
    targets.append(("www.cloudflare.com", "/"))

    seen = set()
    for h, p in targets:
        key = (h, p)
        if key in seen:
            continue
        seen.add(key)
        try:
            status, body, _ = _https_get_through_socks(
                proxy_port, h, p, timeout, SPEED_SAMPLE_BYTES,
                read_full=True)
        except OSError:
            continue
        if status >= 500:
            continue
        n = len(body)
        if n >= SPEED_MIN_BYTES:
            # rough throughput: we don't have a precise timer split here, so
            # re-measure with an explicit window for an honest bytes/sec.
            return n, _timed_download(proxy_port, h, p, timeout)
    return 0, 0.0


def _timed_download(proxy_port: int, host: str, path: str, timeout: float
                    ) -> float:  # pragma: no cover - net
    """Download once more, timing the transfer, to report bytes/sec."""
    try:
        start = time.monotonic()
        status, body, _ = _https_get_through_socks(
            proxy_port, host, path, timeout, SPEED_SAMPLE_BYTES,
            read_full=True)
        elapsed = time.monotonic() - start
        if status >= 500 or elapsed <= 0 or not body:
            return 0.0
        return len(body) / elapsed
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
#  xray process lifecycle (injectable)
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class _XrayProcess:
    """Start/stop a throwaway xray.exe instance for one validation.

    Used as a context manager. On ``__enter__`` it writes a temp config (real
    profile + candidate IP, SOCKS inbound only) and launches xray; on exit it
    terminates the process and removes the temp file.
    """

    def __init__(self, profile, ip: str, socks_port: int,
                 xray_exe: str, bin_dir: str):
        self.profile = profile
        self.ip = ip
        self.socks_port = socks_port
        self.xray_exe = xray_exe
        self.bin_dir = bin_dir
        self._proc: Optional[subprocess.Popen] = None
        self._cfg_path = ""

    def __enter__(self) -> "_XrayProcess":
        from core.cf_scanner import profile_with_ip
        from core.xray_config import build_config

        candidate = profile_with_ip(self.profile, self.ip, suffix="probe")
        cfg = build_config(
            candidate,
            socks_port=self.socks_port,
            http_port=_find_free_port(),
            dest_address=self.ip,
            dest_port=int(getattr(self.profile, "port", 443) or 443),
            loglevel="none",
        )
        fd, self._cfg_path = tempfile.mkstemp(prefix="cfscan-xray-",
                                              suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp)

        kwargs: dict = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        env = dict(os.environ)
        env.setdefault("XRAY_LOCATION_ASSET", self.bin_dir)
        kwargs["env"] = env
        kwargs["cwd"] = self.bin_dir
        self._proc = subprocess.Popen(
            [self.xray_exe, "run", "-config", self._cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        return self

    def started_ok(self) -> bool:
        if self._proc is None:
            return False
        time.sleep(0.3)
        if self._proc.poll() is not None:
            return False
        return _wait_for_port(self.socks_port, 3.0)

    def __exit__(self, *exc):
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=4)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            if sys.platform == "win32" and getattr(self._proc, "pid", None):
                try:
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = subprocess.SW_HIDE
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        capture_output=True, timeout=5, startupinfo=si,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    pass
            self._proc = None
        if self._cfg_path and os.path.isfile(self._cfg_path):
            try:
                os.remove(self._cfg_path)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------------------
#  validator
# ---------------------------------------------------------------------------

class XrayValidator:
    """Validate clean-IP candidates by routing real traffic through Xray.

    The expensive operations are injectable so the orchestration is unit-tested
    without xray or the network:

    * ``process_factory(profile, ip, socks_port) -> ctx`` — context manager
      whose ``started_ok()`` says whether xray came up on ``socks_port``.
    * ``connectivity_fn(socks_port, timeout) -> (ok, ttfb_ms, err)``.
    * ``speed_fn(socks_port, profile, timeout) -> (bytes, bps)``.
    """

    def __init__(
        self,
        profile,
        *,
        xray_exe: Optional[str] = None,
        bin_dir: Optional[str] = None,
        timeout: float = 20.0,
        on_log: Optional[Callable[[str], None]] = None,
        on_result: Optional[Callable[[XrayValidation], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        process_factory: Optional[Callable] = None,
        connectivity_fn: Optional[Callable] = None,
        speed_fn: Optional[Callable] = None,
    ) -> None:
        self.profile = profile
        self.timeout = timeout
        self._on_log = on_log
        self._on_result = on_result
        self._should_stop = should_stop
        self._stop_flag = threading.Event()

        from core.binary_utils import get_bin_dir
        self.bin_dir = bin_dir or get_bin_dir()
        self.xray_exe = xray_exe or os.path.join(self.bin_dir, "xray.exe")

        # When a caller injects a custom process factory (tests, or a future
        # alternative launcher) we trust it and skip the on-disk xray.exe check.
        self._custom_factory = process_factory is not None
        self._process_factory = process_factory or self._default_factory
        self._connectivity_fn = connectivity_fn or _default_connectivity_check
        self._speed_fn = speed_fn or _default_speed_check

    # ------------------------------------------------------------------
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

    @property
    def is_available(self) -> bool:
        if self._custom_factory:
            return True
        return bool(self.xray_exe) and os.path.isfile(self.xray_exe)

    def _default_factory(self, profile, ip: str, socks_port: int):
        return _XrayProcess(profile, ip, socks_port, self.xray_exe,
                            self.bin_dir)

    # ------------------------------------------------------------------
    def validate_ip(self, ip: str) -> XrayValidation:
        """Validate a single IP, retrying once on failure (DPI is flaky)."""
        res = self._validate_once(ip)
        if not res.success and not self._stopping():
            time.sleep(0.5)
            res2 = self._validate_once(ip)
            res2.retries = 1
            if res2.success:
                return res2
            res.retries = 1
        return res

    def _validate_once(self, ip: str) -> XrayValidation:
        port = int(getattr(self.profile, "port", 443) or 443)
        transport = (getattr(self.profile, "transport", "") or "tcp")
        res = XrayValidation(ip=ip, port=port, transport=transport)
        socks_port = _find_free_port()
        try:
            ctx = self._process_factory(self.profile, ip, socks_port)
        except Exception as exc:
            res.error = f"spawn: {exc}"
            return res
        try:
            with ctx as proc:
                if not proc.started_ok():
                    res.error = "xray did not start / socks port not ready"
                    return res
                ok, ttfb, err = self._connectivity_fn(socks_port, self.timeout)
                res.latency_ms = ttfb
                if not ok:
                    res.error = f"connectivity: {err}"
                    return res
                # best-effort speed (never fails the result)
                try:
                    n, bps = self._speed_fn(socks_port, self.profile,
                                            self.timeout)
                    res.bytes_recv = int(n)
                    res.throughput_bps = float(bps)
                except Exception:
                    pass
                res.success = True
                return res
        except Exception as exc:
            res.error = f"validate: {exc}"
            return res

    # ------------------------------------------------------------------
    def validate_all(self, ips: List[str],
                     concurrency: int = 1) -> List[XrayValidation]:
        """Validate a list of IPs.

        Xray validation is heavy (a full process per IP), so the default is
        sequential — that matches SenPaiScanner's final-validation pass and
        keeps port/process pressure low. A small concurrency (>1) is allowed for
        callers that want to parallelise on a fast machine.
        """
        if not self.is_available:
            self._log("هشدار: xray.exe در دسترس نیست — فاز ۲ نادیده گرفته شد")
            return []
        out: List[XrayValidation] = []
        self._log(f"فاز ۲ — اعتبارسنجی واقعی {len(ips)} IP با xray "
                  f"(کانفیگ مرجع روی هر IP اجرا و ترافیک واقعی تست می‌شود) …")

        if concurrency <= 1:
            for ip in ips:
                if self._stopping():
                    break
                res = self.validate_ip(ip)
                out.append(res)
                self._emit(res)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = max(1, min(int(concurrency), 8))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self.validate_ip, ip): ip for ip in ips}
                for fut in as_completed(futs):
                    if self._stopping():
                        break
                    try:
                        res = fut.result()
                    except Exception as exc:
                        res = XrayValidation(ip=futs[fut], error=repr(exc))
                    out.append(res)
                    self._emit(res)
        passed = sum(1 for r in out if r.success)
        self._log(f"فاز ۲ تمام شد — {passed} از {len(out)} IP "
                  f"اعتبارسنجی واقعی را گذراندند")
        # best-first by proxied latency among the successes
        out.sort(key=lambda r: (not r.success,
                                r.latency_ms if r.latency_ms > 0 else 1e9))
        return out

    def _emit(self, res: XrayValidation) -> None:
        if res.success:
            spd = _fmt_speed(res.throughput_bps) if res.throughput_bps else "—"
            self._log(f"✓✓ تأیید واقعی: {res.ip} "
                      f"({res.latency_ms:.0f}ms پروکسی · {spd})")
        else:
            self._log(f"✗ رد شد در فاز ۲: {res.ip} — {res.error}")
        if self._on_result:
            try:
                self._on_result(res)
            except Exception:
                pass


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    val = float(bps)
    i = 0
    while val >= 1024 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}"
