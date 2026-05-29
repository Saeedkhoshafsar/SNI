"""Unit tests for core.resilience (forged-RST detection, throttle, rotation).

Pure data, no sockets — runs anywhere including the sandbox.

Run:  python tests/test_resilience.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.resilience import (
    Action, ResilienceController, RstClassifier, RstEvent, RstVerdict,
    ThroughputMonitor,
)


# ---------------------------------------------------------------------------
#  RstClassifier
# ---------------------------------------------------------------------------

def test_early_rst_before_handshake_is_forged():
    c = RstClassifier(early_window_ms=200)
    ev = RstEvent(ms_since_client_hello=30)
    assert c.classify(ev) is RstVerdict.FORGED
    assert c.is_forged(ev) is True


def test_rst_after_server_hello_is_legit():
    c = RstClassifier()
    ev = RstEvent(ms_since_client_hello=20, server_hello_seen=True)
    assert c.classify(ev) is RstVerdict.LEGIT


def test_rst_after_app_data_is_legit():
    c = RstClassifier()
    ev = RstEvent(ms_since_client_hello=10, app_bytes_received=512)
    assert c.classify(ev) is RstVerdict.LEGIT


def test_late_rst_no_handshake_is_unknown():
    c = RstClassifier(early_window_ms=200)
    ev = RstEvent(ms_since_client_hello=5000)
    assert c.classify(ev) is RstVerdict.UNKNOWN


def test_ttl_mismatch_marks_late_rst_forged():
    c = RstClassifier(early_window_ms=200, ttl_tolerance=4)
    ev = RstEvent(ms_since_client_hello=5000, ttl=40, expected_ttl=64)
    assert c.classify(ev) is RstVerdict.FORGED


def test_ttl_within_tolerance_stays_unknown():
    c = RstClassifier(early_window_ms=200, ttl_tolerance=4)
    ev = RstEvent(ms_since_client_hello=5000, ttl=62, expected_ttl=64)
    assert c.classify(ev) is RstVerdict.UNKNOWN


# ---------------------------------------------------------------------------
#  ThroughputMonitor
# ---------------------------------------------------------------------------

def test_throughput_not_throttled_without_enough_samples():
    m = ThroughputMonitor(min_samples=4)
    m.record(1000, 1.0)
    assert m.is_throttled is False


def test_throughput_detects_throttle():
    m = ThroughputMonitor(window=8, throttle_ratio=0.4, min_samples=4)
    # establish a high baseline
    for _ in range(4):
        m.record(1_000_000, 1.0)          # 1 MB/s
    assert m.baseline_bps >= 1_000_000
    # now sustained low rate
    for _ in range(4):
        m.record(100_000, 1.0)            # 100 KB/s == 0.1 baseline
    assert m.is_throttled is True


def test_throughput_recovers_above_ratio():
    m = ThroughputMonitor(window=4, throttle_ratio=0.4, min_samples=2)
    m.record(1_000_000, 1.0)
    m.record(1_000_000, 1.0)
    m.record(900_000, 1.0)
    m.record(900_000, 1.0)
    assert m.is_throttled is False        # 0.9 of baseline


def test_throughput_ignores_zero_duration():
    m = ThroughputMonitor()
    m.record(1000, 0)
    assert m.samples == 0


def test_throughput_reset_keeps_baseline():
    m = ThroughputMonitor()
    m.record(1_000_000, 1.0)
    base = m.baseline_bps
    m.reset()
    assert m.samples == 0
    assert m.baseline_bps == base


# ---------------------------------------------------------------------------
#  ResilienceController — RST handling + rotation
# ---------------------------------------------------------------------------

def _controller(strategies, ips, **kw):
    c = ResilienceController(**kw)
    c.set_chains(strategies, ips)
    return c


def test_legit_rst_yields_no_action():
    c = _controller(["a", "b"], ["1.1.1.1"])
    ev = RstEvent(ms_since_client_hello=10, server_hello_seen=True)
    assert c.on_rst(ev) is Action.NONE


def test_forged_rst_ignored_up_to_budget():
    c = _controller(["a", "b"], ["1.1.1.1"], rst_budget=3)
    ev = RstEvent(ms_since_client_hello=10)        # forged
    assert c.on_rst(ev) is Action.IGNORE_RST       # 1
    assert c.on_rst(ev) is Action.IGNORE_RST       # 2
    assert c.forged_rst_count == 2


def test_forged_rst_over_budget_rotates_strategy():
    c = _controller(["a", "b"], ["1.1.1.1"], rst_budget=3)
    ev = RstEvent(ms_since_client_hello=10)
    c.on_rst(ev)
    c.on_rst(ev)
    assert c.current_strategy == "a"
    act = c.on_rst(ev)                              # 3rd → over budget
    assert act is Action.ROTATE_STRATEGY
    assert c.current_strategy == "b"
    assert c.forged_rst_count == 0                 # reset after rotation


def test_strategy_exhaustion_rotates_ip():
    c = _controller(["only"], ["1.1.1.1", "2.2.2.2"], rst_budget=1)
    ev = RstEvent(ms_since_client_hello=10)
    # budget 1 → first forged RST rotates; strategy chain has 1 → go to IP
    act = c.on_rst(ev)
    assert act is Action.ROTATE_IP
    assert c.current_ip == "2.2.2.2"
    assert c.current_strategy == "only"            # strategy chain restarted


def test_full_exhaustion_gives_up():
    c = _controller(["only"], ["1.1.1.1"], rst_budget=1)
    ev = RstEvent(ms_since_client_hello=10)
    act = c.on_rst(ev)
    assert act is Action.GIVE_UP
    assert c.current_strategy == "only"


# ---------------------------------------------------------------------------
#  ResilienceController — throttling
# ---------------------------------------------------------------------------

def test_throttle_triggers_strategy_rotation():
    tp = ThroughputMonitor(window=8, throttle_ratio=0.4, min_samples=4)
    c = ResilienceController(throughput=tp)
    c.set_chains(["a", "b"], ["1.1.1.1"])
    for _ in range(4):
        assert c.on_throughput(1_000_000, 1.0) is Action.NONE
    # drop to throttled territory; rotation should fire once the recent tail
    # is dominated by the low samples (a sample or two before the window fills)
    actions = [c.on_throughput(50_000, 1.0) for _ in range(4)]
    assert Action.ROTATE_STRATEGY in actions
    assert c.current_strategy == "b"


def test_healthy_throughput_no_action():
    c = ResilienceController()
    c.set_chains(["a", "b"], ["1.1.1.1"])
    for _ in range(8):
        assert c.on_throughput(1_000_000, 1.0) is Action.NONE


# ---------------------------------------------------------------------------
#  flow reset
# ---------------------------------------------------------------------------

def test_reset_flow_clears_forged_count():
    c = _controller(["a", "b"], ["1.1.1.1"], rst_budget=5)
    ev = RstEvent(ms_since_client_hello=10)
    c.on_rst(ev)
    c.on_rst(ev)
    assert c.forged_rst_count == 2
    c.reset_flow()
    assert c.forged_rst_count == 0


# ---------------------------------------------------------------------------
#  integration with prober.fallback_order
# ---------------------------------------------------------------------------

def test_chains_from_prober_fallback_order():
    from core.prober import AutoProber, Candidate, ProbeResult, OK

    cands = [Candidate("a"), Candidate("b"), Candidate("c")]
    script = {"a": (OK, 10), "b": (OK, 500), "c": (OK, 100)}

    def probe(candidate, host, port, timeout):
        out, ms = script[candidate.key]
        return ProbeResult(candidate, out, latency_ms=ms)

    p = AutoProber(cands, probe)
    p.run("h", 443)
    assert p.selected.strategy == "a"
    # build the resilience strategy chain from selected + fallback order
    chain = [p.selected.strategy] + [c.strategy for c in p.fallback_order()]
    assert chain == ["a", "c", "b"]
    c = ResilienceController(rst_budget=1)
    c.set_chains(chain, ["1.1.1.1"])
    assert c.current_strategy == "a"
    c.on_rst(RstEvent(ms_since_client_hello=10))   # rotate
    assert c.current_strategy == "c"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL  {fn.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
