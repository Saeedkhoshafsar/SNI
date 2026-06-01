"""Tests for Phase 2 — the real Xray end-to-end clean-IP validator.

The expensive bits (the xray process + the proxied HTTP calls) are injected, so
the whole orchestration — start xray → connectivity check → speed sample → retry
on failure → ranking — runs offline and deterministically with no subprocess and
no network.

Run:  python -m pytest tests/test_cf_xray_validator.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cf_xray_validator import XrayValidator, XrayValidation
from core.profile import Profile


def _profile():
    return Profile(protocol="vless", address="hammm.pages.dev", port=443,
                   uuid="11111111-1111-1111-1111-111111111111",
                   security="tls", sni="hammm.pages.dev", host="hammm.pages.dev",
                   transport="ws", path="/ws")


class _FakeProc:
    """A fake xray process context manager that pretends to come up."""

    def __init__(self, started=True):
        self._started = started

    def __enter__(self):
        return self

    def started_ok(self):
        return self._started

    def __exit__(self, *exc):
        return False


def _validator(profile, *, started=True, connectivity=None, speed=None,
               on_result=None, **kw):
    return XrayValidator(
        profile,
        xray_exe="/fake/xray.exe",
        bin_dir="/fake",
        timeout=5.0,
        on_result=on_result,
        process_factory=lambda p, ip, port: _FakeProc(started=started),
        connectivity_fn=connectivity or (lambda port, t: (True, 42.0, "")),
        speed_fn=speed or (lambda port, prof, t: (200000, 1_500_000.0)),
        **kw,
    )


# ---------------------------------------------------------------------------
#  single-IP validation
# ---------------------------------------------------------------------------

def test_validate_ip_success_records_latency_and_speed():
    v = _validator(_profile())
    # is_available is patched via os.path.isfile? No — assert validate_ip works
    res = v.validate_ip("104.18.0.10")
    assert isinstance(res, XrayValidation)
    assert res.success is True
    assert res.latency_ms == pytest.approx(42.0)
    assert res.throughput_bps == pytest.approx(1_500_000.0)
    assert res.bytes_recv == 200000
    assert res.transport == "ws"
    assert res.error == ""


def test_validate_ip_fails_when_xray_does_not_start():
    v = _validator(_profile(), started=False)
    res = v.validate_ip("104.18.0.10")
    assert res.success is False
    assert "start" in res.error or "socks" in res.error
    # a failed start is retried once
    assert res.retries == 1


def test_validate_ip_fails_when_connectivity_fails_then_retries():
    calls = {"n": 0}

    def flaky(port, timeout):
        calls["n"] += 1
        # first attempt fails, the retry succeeds (DPI is flaky)
        if calls["n"] == 1:
            return False, 0.0, "no colo in trace response"
        return True, 88.0, ""

    v = _validator(_profile(), connectivity=flaky)
    res = v.validate_ip("104.18.0.10")
    assert res.success is True
    assert res.retries == 1
    assert res.latency_ms == pytest.approx(88.0)
    assert calls["n"] == 2


def test_validate_ip_speed_failure_does_not_fail_result():
    def boom_speed(port, prof, timeout):
        raise RuntimeError("speed endpoint blocked")

    v = _validator(_profile(), speed=boom_speed)
    res = v.validate_ip("104.18.0.10")
    # connectivity passed, so the IP is still a success; speed is best-effort
    assert res.success is True
    assert res.throughput_bps == 0.0


# ---------------------------------------------------------------------------
#  batch validation + ranking
# ---------------------------------------------------------------------------

def test_validate_all_ranks_successes_by_latency():
    # validate sequentially with descending latencies, then assert the final
    # output is sorted best-first (ascending) among the successes.
    seq = iter([120.0, 40.0])

    def connectivity(port, timeout):
        return True, next(seq), ""

    v = _validator(_profile(), connectivity=connectivity)
    out = v.validate_all(["104.18.0.10", "104.18.0.20"], concurrency=1)
    assert [r.success for r in out] == [True, True]
    # sorted best-first: 40ms before 120ms
    assert out[0].latency_ms == pytest.approx(40.0)
    assert out[1].latency_ms == pytest.approx(120.0)


def test_validate_all_streams_results_via_on_result():
    seen = []
    v = _validator(_profile(), on_result=lambda r: seen.append(r.ip))
    v.validate_all(["104.18.0.10", "104.18.0.20"], concurrency=1)
    assert set(seen) == {"104.18.0.10", "104.18.0.20"}


def test_validate_all_skips_when_xray_unavailable(tmp_path):
    # a real validator whose xray_exe does not exist → returns [] and logs
    logs = []
    v = XrayValidator(_profile(), xray_exe=str(tmp_path / "nope.exe"),
                      bin_dir=str(tmp_path), on_log=logs.append)
    assert v.is_available is False
    out = v.validate_all(["104.18.0.10"])
    assert out == []
    assert any("xray" in m for m in logs)


def test_validate_all_honours_stop():
    v = _validator(_profile())
    v.stop()
    out = v.validate_all(["104.18.0.10", "104.18.0.20"], concurrency=1)
    assert out == []


# ---------------------------------------------------------------------------
#  SOCKS5 helper sanity
# ---------------------------------------------------------------------------

def test_is_hostname_distinguishes_ip_from_name():
    from core.cf_xray_validator import _is_hostname
    assert _is_hostname("speed.cloudflare.com") is True
    assert _is_hostname("104.18.0.10") is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
