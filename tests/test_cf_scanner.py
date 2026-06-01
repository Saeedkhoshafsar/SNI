"""Tests for the Cloudflare clean-IP scanner core (issue #3).

The network probe is injected with a deterministic fake, so the whole
sweep / ranking / limit / cancel / profile-rebuild logic runs offline.

Run:  python -m pytest tests/test_cf_scanner.py -q
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cf_scanner import (
    CFScanner, IPResult, ScanConfig, OK, RST, TIMEOUT,
    cf_ip_pool, scan_config_from_profile, profile_with_ip,
    CLOUDFLARE_IPV4_CIDRS,
)
from core.profile import Profile
from core.share_link import parse_link, profile_to_link


# ---------------------------------------------------------------------------
#  IP pool
# ---------------------------------------------------------------------------

def test_pool_is_deterministic_and_within_cloudflare_ranges():
    import ipaddress
    nets = [ipaddress.ip_network(c) for c in CLOUDFLARE_IPV4_CIDRS]
    pool = cf_ip_pool(50, rng=random.Random(7))
    pool2 = cf_ip_pool(50, rng=random.Random(7))
    assert pool == pool2                      # deterministic with a seeded rng
    assert len(pool) == 50
    assert len(set(pool)) == 50               # unique
    for ip in pool:
        addr = ipaddress.ip_address(ip)
        assert any(addr in n for n in nets), f"{ip} not in any CF range"


def test_pool_count_capped_to_available():
    # a tiny custom range can't yield more than its host count
    pool = cf_ip_pool(1000, cidrs=("192.0.2.0/29",), rng=random.Random(1))
    assert 0 < len(pool) <= 6


# ---------------------------------------------------------------------------
#  scanning
# ---------------------------------------------------------------------------

def _even_clean(ip, spec, timeout):
    """Fake probe (new ``(ip, spec, timeout)`` signature): even octet → clean.

    Mirrors the real :func:`cf_ip_probe` interface — it receives a
    :class:`ProbeSpec` rather than a bare ``(port, sni)`` pair — so the tests
    exercise exactly the contract the scanner now uses.
    """
    last = int(ip.split(".")[-1])
    if last % 2 == 0:
        return IPResult(ip, OK, latency_ms=float(last))
    return IPResult(ip, RST, detail="blocked")


def test_scan_returns_only_clean_sorted_fastest_first():
    ips = ["1.1.1.10", "1.1.1.11", "1.1.1.4", "1.1.1.7", "1.1.1.2"]
    sc = CFScanner(probe_fn=_even_clean)
    cfg = ScanConfig(port=443, server_name="x", concurrency=4, max_results=10)
    rep = sc.scan(cfg, ips=ips)
    clean = rep.clean
    assert [r.ip for r in clean] == ["1.1.1.2", "1.1.1.4", "1.1.1.10"]
    assert all(r.ok for r in clean)
    assert rep.tested == len(ips)


def test_scan_honours_max_results():
    ips = [f"1.1.1.{i}" for i in range(2, 40, 2)]  # all even → all clean
    sc = CFScanner(probe_fn=_even_clean)
    cfg = ScanConfig(port=443, server_name="x", concurrency=4, max_results=3)
    rep = sc.scan(cfg, ips=ips)
    assert len(rep.clean) == 3
    assert rep.stopped_early


def test_scan_max_latency_filter():
    ips = ["1.1.1.2", "1.1.1.100", "1.1.1.20"]  # latencies 2/100/20
    sc = CFScanner(probe_fn=_even_clean)
    cfg = ScanConfig(port=443, server_name="x", max_latency_ms=50)
    rep = sc.scan(cfg, ips=ips)
    assert {r.ip for r in rep.clean} == {"1.1.1.2", "1.1.1.20"}


def test_scan_on_result_streams_hits():
    ips = ["1.1.1.2", "1.1.1.3", "1.1.1.4"]
    seen = []
    sc = CFScanner(probe_fn=_even_clean, on_result=lambda r: seen.append(r.ip))
    sc.scan(ScanConfig(port=443, server_name="x"), ips=ips)
    assert set(seen) == {"1.1.1.2", "1.1.1.4"}


def test_scan_bad_probe_never_aborts():
    def boom(ip, spec, timeout):
        if ip.endswith(".3"):
            raise RuntimeError("kaboom")
        return _even_clean(ip, spec, timeout)
    sc = CFScanner(probe_fn=boom)
    rep = sc.scan(ScanConfig(port=443, server_name="x"),
                  ips=["1.1.1.2", "1.1.1.3", "1.1.1.4"])
    # the exception became an ERROR result, the clean ones still came through
    assert {r.ip for r in rep.clean} == {"1.1.1.2", "1.1.1.4"}


def test_scan_invalid_port_returns_empty():
    sc = CFScanner(probe_fn=_even_clean)
    rep = sc.scan(ScanConfig(port=0, server_name="x"), ips=["1.1.1.2"])
    assert rep.clean == []
    assert rep.tested == 0


def test_scan_cancel_via_should_stop():
    ips = [f"1.1.1.{i}" for i in range(2, 60, 2)]
    sc = CFScanner(probe_fn=_even_clean, should_stop=lambda: True)
    rep = sc.scan(ScanConfig(port=443, server_name="x"), ips=ips)
    assert rep.stopped_early


# ---------------------------------------------------------------------------
#  config-aware helpers
# ---------------------------------------------------------------------------

def test_scan_config_from_profile_uses_port_and_sni():
    p = Profile(protocol="vless", address="1.2.3.4", port=8443,
                security="tls", sni="my.sni.dev", host="h.dev")
    cfg = scan_config_from_profile(p, timeout=2.0, concurrency=10)
    assert cfg.port == 8443
    assert cfg.server_name == "my.sni.dev"
    assert cfg.timeout == 2.0
    assert cfg.concurrency == 10


def test_scan_config_from_profile_detects_ws_and_path():
    """A ws config must produce a WS-validating, path-carrying ScanConfig (#1)."""
    p = Profile(protocol="vless", address="1.2.3.4", port=8443,
                security="tls", sni="my.sni.dev", host="h.dev",
                transport="ws", path="/stars/abc")
    cfg = scan_config_from_profile(p)
    assert cfg.is_ws is True
    assert cfg.is_tls is True
    assert cfg.host == "h.dev"
    assert cfg.path == "/stars/abc"
    spec = cfg.to_spec()
    assert spec.is_ws is True
    assert spec.path == "/stars/abc"
    assert spec.host == "h.dev"


def test_scan_config_non_ws_transport_is_not_ws():
    p = Profile(protocol="trojan", address="1.2.3.4", port=443,
                security="tls", sni="x.dev", transport="grpc",
                service_name="gsvc")
    cfg = scan_config_from_profile(p)
    assert cfg.is_ws is False


def test_profile_with_ip_swaps_only_address_and_roundtrips():
    link = (
        "vless://e0f8189f-1ca1-429e-82d8-447d8b356846@104.18.151.71:8443"
        "?encryption=none&security=tls&sni=hammm2.pages.dev&fp=chrome"
        "&type=ws&host=hammm2.pages.dev"
        "&path=%2Fstars%2Fhttp%3A%2F%2FPQ3YjMsJql%3AfCfJXXbDcw%40vps.webtun.xyz%3A2087"
        "#AYYILDIZ")
    p = parse_link(link)
    p2 = profile_with_ip(p, "188.114.96.10")
    # only the address changed
    assert p2.address == "188.114.96.10"
    assert p2.port == p.port
    assert p2.uuid == p.uuid
    assert p2.sni == p.sni
    assert p2.host == p.host
    assert p2.path == p.path
    assert p2.transport == p.transport
    assert p2.security == p.security
    # remark is tagged so the user can tell the variant apart
    assert "188.114.96.10" in p2.remark
    # and it re-serialises to a valid link with the new IP
    out = profile_to_link(p2)
    p3 = parse_link(out)
    assert p3.address == "188.114.96.10"
    assert p3.path == p.path


# ---------------------------------------------------------------------------
#  relay-path detection (AYYILDIZ webtun config: long ws path with a nested URL)
# ---------------------------------------------------------------------------

def test_relay_path_detection_for_webtun_style_paths():
    from core.cf_scanner import _is_relay_path
    # the exact shape the user reported — a ws path that embeds a backend URL
    assert _is_relay_path(
        "/stars/http://PQ3YjMsJql:fCfJXXbDcw@vps.webtun.xyz:2087") is True
    assert _is_relay_path("/relay/https://backend.example.com:8443") is True
    assert _is_relay_path("/tunnel/user:pass@host:1234") is True
    # ordinary ws paths are NOT relay paths → strict WS validation still applies
    assert _is_relay_path("/") is False
    assert _is_relay_path("/ws") is False
    assert _is_relay_path("/stars/abc123") is False
    assert _is_relay_path("") is False


def test_ws_relay_path_revalidates_on_root_when_complex_path_refused():
    """Issue: AYYILDIZ ws config (``/stars/http://...vps.webtun.xyz:2087``)
    pinged RED even though it connects. A bare WS probe on the relay path is
    refused by the Worker (it tries to dial the nested backend), so the prober
    must fall back to validating the WS layer on the root path ``/``. We drive
    the real :func:`cf_ip_probe` with monkeypatched socket/ssl/IO so no network
    is touched, and assert it ends OK via the root-path retry.
    """
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, OK as _OK

    state = {"ws_calls": []}

    # --- stub out the socket / TLS layer (no real network) ---
    class _Sock:
        def close(self):
            pass

    def fake_open_socket(ip, port, timeout):
        return _Sock()

    class _Ctx:
        check_hostname = True
        verify_mode = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    real_ctx = ssl_mod.create_default_context
    real_open = cf._open_socket
    real_trace = cf._http_trace_ok
    real_ws = cf._ws_upgrade_ok
    ssl_mod.create_default_context = lambda: _Ctx()
    cf._open_socket = fake_open_socket
    # stage 1: the edge is live for this host
    cf._http_trace_ok = lambda stream, host, timeout: (True, "cf edge ok")

    # stage 2: the WS upgrade on the COMPLEX relay path is refused, but the
    # retry on "/" succeeds (proves the host route is alive).
    def fake_ws(stream, host, path, timeout, relaxed=False):
        state["ws_calls"].append(path)
        if path == "/":
            return (True, "ws upgrade 101")
        return (False, "ws upgrade refused")

    cf._ws_upgrade_ok = fake_ws
    try:
        spec = ProbeSpec(
            port=443, server_name="hammm2.pages.dev", host="hammm2.pages.dev",
            path="/stars/http://PQ3YjMsJql:fCfJXXbDcw@vps.webtun.xyz:2087",
            is_ws=True, is_tls=True)
        res = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    finally:
        ssl_mod.create_default_context = real_ctx
        cf._open_socket = real_open
        cf._http_trace_ok = real_trace
        cf._ws_upgrade_ok = real_ws

    assert res.outcome == _OK, res.detail
    # it tried the real relay path first, then fell back to "/"
    assert state["ws_calls"][0].startswith("/stars/http://")
    assert "/" in state["ws_calls"]


def test_ws_ordinary_path_does_not_get_root_fallback():
    """A plain ws path that is genuinely refused must stay RED — the root-path
    leniency is ONLY for relay paths, so a broken ordinary config is honest.
    """
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, ERROR as _ERR

    class _Sock:
        def close(self):
            pass

    class _Ctx:
        check_hostname = True
        verify_mode = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    real_ctx = ssl_mod.create_default_context
    real_open = cf._open_socket
    real_trace = cf._http_trace_ok
    real_ws = cf._ws_upgrade_ok
    ssl_mod.create_default_context = lambda: _Ctx()
    cf._open_socket = lambda ip, port, timeout: _Sock()
    cf._http_trace_ok = lambda stream, host, timeout: (True, "cf edge ok")
    cf._ws_upgrade_ok = lambda stream, host, path, timeout, relaxed=False: (False, "refused")
    try:
        spec = ProbeSpec(
            port=443, server_name="x.pages.dev", host="x.pages.dev",
            path="/ws", is_ws=True, is_tls=True)
        res = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    finally:
        ssl_mod.create_default_context = real_ctx
        cf._open_socket = real_open
        cf._http_trace_ok = real_trace
        cf._ws_upgrade_ok = real_ws

    assert res.outcome == _ERR


def test_is_cloudflare_host_classifies_ips_and_hostnames():
    from core.cf_scanner import is_cloudflare_host

    # IPs inside published Cloudflare ranges
    assert is_cloudflare_host("104.18.151.71") is True   # AYYILDIZ front IP
    assert is_cloudflare_host("104.19.229.21") is True   # spoof connect IP
    assert is_cloudflare_host("172.64.0.1") is True
    # IPs OUTSIDE Cloudflare → a direct VPS
    assert is_cloudflare_host("8.8.8.8") is False
    assert is_cloudflare_host("1.1.1.1") is False        # CF DNS, not in ranges
    assert is_cloudflare_host("203.0.113.7") is False
    # Cloudflare-fronted hostnames
    assert is_cloudflare_host("hammm2.pages.dev") is True
    assert is_cloudflare_host("myworker.workers.dev") is True
    assert is_cloudflare_host("abc.trycloudflare.com") is True
    # ordinary / direct hostnames
    assert is_cloudflare_host("vps.webtun.xyz") is False
    assert is_cloudflare_host("example.com") is False
    assert is_cloudflare_host("") is False


def test_ws_relay_relaxed_accepts_plain_cf_edge_response():
    """A relay Worker often answers the bare ``/`` upgrade with a plain 2xx (its
    landing page) rather than 101. With ``relaxed=True`` (relay revalidation)
    that still counts as the host route being live; without it, it's refused.
    """
    import core.cf_scanner as cf

    class _Stream:
        def __init__(self, payload):
            self._p = payload
            self._sent = False

        def settimeout(self, *_a):
            pass

        def sendall(self, *_a):
            pass

        def recv(self, _n):
            if self._sent:
                return b""
            self._sent = True
            return self._p

        def close(self):
            pass

    # a plain 200 from a Cloudflare edge (cf-ray header present), NOT a 101
    payload = (b"HTTP/1.1 200 OK\r\nserver: cloudflare\r\n"
               b"cf-ray: abc123\r\n\r\nhello")
    relaxed_ok, _ = cf._ws_upgrade_ok(_Stream(payload), "h", "/", 1.0,
                                      relaxed=True)
    strict_ok, _ = cf._ws_upgrade_ok(_Stream(payload), "h", "/", 1.0,
                                     relaxed=False)
    assert relaxed_ok is True       # relay revalidation accepts a live edge
    assert strict_ok is False       # strict mode still requires 101 / cf 4xx


def test_probe_latency_is_connect_rtt_not_cumulative_stages():
    """Bug 2: a ws / relay config reported 2000-5000ms because the latency was
    measured from the start of the probe through *every* validation stage
    (TCP connect + TLS + trace + WS upgrade + relay-root retry — up to three
    serial handshakes). The honest latency is the TCP connect RTT to the edge;
    the later stages are pass/fail checks, not part of the network round-trip.

    We drive the real :func:`cf_ip_probe` with a fake clock so the connect takes
    40ms but the validation stages burn another ~5000ms, and assert the reported
    ``latency_ms`` reflects the 40ms connect — not the 5000ms total.
    """
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, OK as _OK

    # fake monotonic clock: advances a lot during the validation stages
    ticks = iter([
        0.000,   # start
        0.040,   # right after TCP connect  -> connect_ms == 40ms
        5.040,   # success return (after trace + ws + relay retry burned 5s)
        5.041, 5.042, 5.043,  # spare reads, just in case
    ])
    last = {"v": 5.043}

    def fake_monotonic():
        try:
            last["v"] = next(ticks)
        except StopIteration:
            pass
        return last["v"]

    class _Sock:
        def close(self):
            pass

    class _Ctx:
        check_hostname = True
        verify_mode = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    real_ctx = ssl_mod.create_default_context
    real_open = cf._open_socket
    real_trace = cf._http_trace_ok
    real_ws = cf._ws_upgrade_ok
    real_mono = cf.time.monotonic
    ssl_mod.create_default_context = lambda: _Ctx()
    cf._open_socket = lambda ip, port, timeout: _Sock()
    cf._http_trace_ok = lambda stream, host, timeout: (True, "cf edge ok")
    cf._ws_upgrade_ok = lambda stream, host, path, timeout, relaxed=False: (
        True, "ws upgrade 101")
    cf.time.monotonic = fake_monotonic
    try:
        spec = ProbeSpec(
            port=8443, server_name="hammm2.pages.dev", host="hammm2.pages.dev",
            path="/stars/http://PQ3YjMsJql:fCfJXXbDcw@vps.webtun.xyz:2087",
            is_ws=True, is_tls=True)
        res = cf.cf_ip_probe("104.18.151.71", spec, 6.0)
    finally:
        ssl_mod.create_default_context = real_ctx
        cf._open_socket = real_open
        cf._http_trace_ok = real_trace
        cf._ws_upgrade_ok = real_ws
        cf.time.monotonic = real_mono

    assert res.outcome == _OK, res.detail
    # the reported latency is the ~40ms connect RTT, NOT the ~5040ms cumulative
    assert res.latency_ms == pytest.approx(40.0, abs=1.0), res.latency_ms
    assert res.latency_ms < 1000.0, (
        "latency must reflect the connect RTT, not the multi-stage total")


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
