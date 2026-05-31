"""Tests for the Cloudflare clean-IP scanner core (issue #3).

The network probe is injected with a deterministic fake, so the whole
sweep / ranking / limit / cancel / profile-rebuild logic runs offline.

Run:  python -m pytest tests/test_cf_scanner.py -q
"""
import os
import random
import sys

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


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
