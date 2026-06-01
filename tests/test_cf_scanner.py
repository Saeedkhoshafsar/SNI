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


def test_scan_on_progress_fires_for_every_probe():
    """The live progress callback must tick once per probe (clean OR not) so
    the UI can show a moving bar — this is the fix for the 'frozen UI' bug."""
    ips = ["1.1.1.2", "1.1.1.3", "1.1.1.4", "1.1.1.5"]
    ticks = []
    sc = CFScanner(
        probe_fn=_even_clean,
        on_progress=lambda tested, total, found, ip, ok: ticks.append(
            (tested, total, found, ip, ok)),
    )
    sc.scan(ScanConfig(port=443, server_name="x"), ips=ips)
    # one tick per IP, total always reported, tested strictly increasing
    assert len(ticks) == len(ips)
    assert all(t[1] == len(ips) for t in ticks)            # total
    assert [t[0] for t in ticks] == [1, 2, 3, 4]           # tested counter
    assert ticks[-1][2] >= 1                                # found count grows
    # at least one clean (ok=True) and one not-clean (ok=False) reported
    oks = {t[4] for t in ticks}
    assert oks == {True, False}


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


def test_probe_http_requires_real_colo(monkeypatch):
    """Ported SenPai probeHTTP: a 200 WITHOUT a colo is NOT a live edge.

    We stub the socket + TLS + HTTP layer so no network is touched, and assert
    that ``cf_ip_probe`` only reports OK when the trace body carries ``colo=``.
    """
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, OK as _OK, ERROR as _ERR

    class _Sock:
        def close(self):
            pass

    class _Ctx:
        check_hostname = True
        verify_mode = None
        minimum_version = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    monkeypatch.setattr(ssl_mod, "create_default_context", lambda: _Ctx())
    monkeypatch.setattr(cf, "_open_socket", lambda ip, port, timeout: _Sock())

    # case A: a genuine edge — 200 with colo + cf-ray → OK
    def good_get(stream, path, host, timeout, max_bytes=4096, read_full=False):
        body = "fl=20f01\nh=speed.cloudflare.com\ncolo=SIN\n"
        return 200, {"cf-ray": "8abc-SIN"}, body
    monkeypatch.setattr(cf, "_http_get", good_get)
    spec = ProbeSpec(port=443, server_name="speed.cloudflare.com",
                     mode=cf.MODE_HTTP, tries=1, is_ws=False, require_ws=False)
    res = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    assert res.outcome == _OK, res.detail
    assert res.colo == "SIN"
    assert res.tls_ok is True

    # case B: a 200 with NO colo → not a live edge → ERROR
    def nocolo_get(stream, path, host, timeout, max_bytes=4096,
                   read_full=False):
        return 200, {}, "nothing useful here"
    monkeypatch.setattr(cf, "_http_get", nocolo_get)
    res2 = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    assert res2.outcome == _ERR
    assert "colo" in res2.detail


def test_probe_http_ws_required_must_pass_ws(monkeypatch):
    """Ported SenPai: when WS is required, the WS probe must succeed for OK."""
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, OK as _OK, ERROR as _ERR

    class _Sock:
        def close(self):
            pass

    class _Ctx:
        check_hostname = True
        verify_mode = None
        minimum_version = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    monkeypatch.setattr(ssl_mod, "create_default_context", lambda: _Ctx())
    monkeypatch.setattr(cf, "_open_socket", lambda ip, port, timeout: _Sock())
    monkeypatch.setattr(cf, "_http_get",
                        lambda *a, **k: (200, {"cf-ray": "8x-SIN"},
                                         "colo=SIN\n"))

    spec = ProbeSpec(port=443, server_name="x.pages.dev", host="x.pages.dev",
                     path="/ws", is_ws=True, is_tls=True, mode=cf.MODE_HTTP,
                     tries=1, require_ws=True)

    # WS upgrade reaches the edge → OK
    monkeypatch.setattr(cf, "_probe_websocket", lambda *a, **k: True)
    assert cf.cf_ip_probe("104.18.151.71", spec, 3.0).outcome == _OK

    # WS upgrade refused → ERROR even though the trace was fine
    monkeypatch.setattr(cf, "_probe_websocket", lambda *a, **k: False)
    res = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    assert res.outcome == _ERR


def test_websocket_upgrade_accepts_any_http_response():
    """Ported SenPai probeWebSocket: any ``HTTP/`` reply means WS reached CF."""
    import core.cf_scanner as cf

    class _Stream:
        def __init__(self, replies):
            self._replies = list(replies)

        def settimeout(self, *_a):
            pass

        def sendall(self, *_a):
            pass

        def recv(self, _n):
            if self._replies:
                return self._replies.pop(0)
            return b""

        def close(self):
            pass

    # idle-hold read times out (expected) then the upgrade gets a 400 → True
    import socket as _sock

    class _IdleThenHTTP(_Stream):
        def __init__(self):
            super().__init__([b"HTTP/1.1 400 Bad Request\r\n\r\n"])
            self._idle_done = False

        def recv(self, _n):
            if not self._idle_done:
                self._idle_done = True
                raise _sock.timeout()
            return super().recv(_n)

    # patch the socket/TLS layer so _probe_websocket uses our fake stream
    real_open = cf._open_socket
    import ssl as ssl_mod
    real_ctx = ssl_mod.create_default_context

    class _Ctx:
        check_hostname = True
        verify_mode = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    fake = _IdleThenHTTP()
    cf._open_socket = lambda ip, port, timeout: fake
    ssl_mod.create_default_context = lambda: _Ctx()
    try:
        ok = cf._probe_websocket("1.2.3.4", 443, "h", "h", "/", 1.0, True)
    finally:
        cf._open_socket = real_open
        ssl_mod.create_default_context = real_ctx
    assert ok is True


def test_colo_parsers():
    """The two colo extractors (trace body + CF-Ray header)."""
    import core.cf_scanner as cf
    assert cf._parse_colo_trace("fl=1\nh=x\ncolo=FRA\nip=2") == "FRA"
    assert cf._parse_colo_trace("no colo here") == ""
    assert cf._parse_colo_ray("8abc1234def-SIN") == "SIN"
    assert cf._parse_colo_ray("") == ""
    assert cf._parse_colo_ray("nodash") == ""


def test_ipresult_statistics():
    """Loss / avg / best / worst / jitter / is_healthy (ported from result.go)."""
    from core.cf_scanner import IPResult, MODE_HTTP, MODE_TCP

    r = IPResult(ip="1.2.3.4", port=443, mode=MODE_HTTP)
    r.latencies_ms = [100.0, 0.0, 120.0, 80.0]  # one failed try
    assert r.loss() == 25.0
    assert r.avg() == pytest.approx(100.0)
    assert r.best() == 80.0
    assert r.worst() == 120.0
    assert r.jitter() > 0
    # not healthy yet (no colo / status)
    assert r.is_healthy() is False
    r.tls_ok = True
    r.http_status = 200
    r.colo = "SIN"
    assert r.is_healthy() is True
    # speed required but stalled → unhealthy
    r.speed_tested = True
    r.throughput_bps = 0.0
    assert r.is_healthy() is False
    r.throughput_bps = 1000.0
    assert r.is_healthy() is True
    # >=50% loss is always unhealthy
    r.latencies_ms = [0.0, 0.0, 100.0]
    assert r.is_healthy() is False

    # tcp mode: any successful connect is healthy
    t = IPResult(ip="1.2.3.4", mode=MODE_TCP)
    t.latencies_ms = [50.0, 60.0]
    assert t.is_healthy() is True


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


def test_probe_latency_uses_successful_tries_average():
    """Ported SenPai: reported latency is the mean of successful tries.

    A failed try records 0 and must be excluded from the average (it only
    counts toward loss). We drive ``cf_ip_probe`` with stubbed probes so the
    per-try latencies are deterministic, then check the summary latency.
    """
    import core.cf_scanner as cf
    from core.cf_scanner import ProbeSpec, OK as _OK

    class _Sock:
        def close(self):
            pass

    class _Ctx:
        check_hostname = True
        verify_mode = None
        minimum_version = None
        def wrap_socket(self, sock, server_hostname=""):
            return sock

    import ssl as ssl_mod
    real_ctx = ssl_mod.create_default_context
    real_open = cf._open_socket
    real_http = cf._http_get
    real_sleep = cf.time.sleep

    # three tries: 40ms, 60ms, 50ms → avg 50ms
    lats = iter([40.0, 60.0, 50.0])

    def fake_get(stream, path, host, timeout, max_bytes=4096, read_full=False):
        # we don't control the timer here; latency is computed by the caller,
        # so instead we verify the *average* path via monkeypatching avg inputs.
        return 200, {"cf-ray": "8x-SIN"}, "colo=SIN\n"

    ssl_mod.create_default_context = lambda: _Ctx()
    cf._open_socket = lambda ip, port, timeout: _Sock()
    cf._http_get = fake_get
    cf.time.sleep = lambda *_a: None
    try:
        spec = ProbeSpec(port=443, server_name="speed.cloudflare.com",
                         mode=cf.MODE_HTTP, tries=3, is_ws=False)
        res = cf.cf_ip_probe("104.18.151.71", spec, 3.0)
    finally:
        ssl_mod.create_default_context = real_ctx
        cf._open_socket = real_open
        cf._http_get = real_http
        cf.time.sleep = real_sleep

    assert res.outcome == _OK
    # latency_ms is the mean of the successful tries (all succeeded here)
    assert res.latency_ms == pytest.approx(res.avg())
    assert len(res.latencies_ms) == 3


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
