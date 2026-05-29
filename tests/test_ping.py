"""Tests for core.ping — latency/download ranking + strategy testing.

All network primitives are injected with deterministic fakes so the ranking /
selection / aggregation logic runs headless with no sockets.
"""
import unittest

from core.ping import (
    PingResult,
    PingTester,
    Target,
    StrategyPing,
    StrategyPingReport,
    target_from_profile,
    probe_strategies,
    default_strategy_keys,
)
from core.prober import Candidate, ProbeResult, OK, RST, TIMEOUT


# ---------------------------------------------------------------------------
#  fakes
# ---------------------------------------------------------------------------

def make_latency_fn(table):
    """table: {host: [ms, ms, ...] or ms-or-None}; cycles through a list."""
    state = {h: list(v) if isinstance(v, list) else [v] for h, v in table.items()}
    def fn(host, port, timeout):
        seq = state.get(host)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return fn


def make_throughput_fn(table):
    def fn(host, port, timeout):
        return table.get(host)
    return fn


class _Prof:
    """Duck-typed stand-in for core.profile.Profile."""
    def __init__(self, address, port, remark=""):
        self.address = address
        self.port = port
        self.remark = remark

    @property
    def display_name(self):
        return self.remark or f"vless://{self.address}:{self.port}"


# ---------------------------------------------------------------------------
#  PingResult aggregation
# ---------------------------------------------------------------------------

class TestPingResult(unittest.TestCase):
    def test_aggregates_best_avg_jitter(self):
        r = PingResult("s", "h", 443, samples_sent=3, latencies=[100.0, 120.0, 80.0])
        self.assertEqual(r.received, 3)
        self.assertAlmostEqual(r.best_ms, 80.0)
        self.assertAlmostEqual(r.avg_ms, 100.0)
        self.assertGreater(r.jitter_ms, 0.0)
        self.assertTrue(r.reachable)
        self.assertEqual(r.loss, 0.0)

    def test_loss_and_unreachable(self):
        r = PingResult("s", "h", 443, samples_sent=3, latencies=[])
        self.assertFalse(r.reachable)
        self.assertEqual(r.loss, 1.0)
        self.assertIsNone(r.best_ms)
        self.assertIsNone(r.avg_ms)
        self.assertEqual(r.sort_key, float("inf"))

    def test_partial_loss(self):
        r = PingResult("s", "h", 443, samples_sent=4, latencies=[50.0, 60.0])
        self.assertAlmostEqual(r.loss, 0.5)
        # sort_key penalises loss
        self.assertGreater(r.sort_key, r.avg_ms)


# ---------------------------------------------------------------------------
#  single + many server ping
# ---------------------------------------------------------------------------

class TestPingTester(unittest.TestCase):
    def test_ping_single_target(self):
        t = PingTester(latency_fn=make_latency_fn({"h": [10.0, 20.0, 30.0]}),
                       samples=3, timeout=1.0)
        res = t.ping_target(Target("s", "h", 443))
        self.assertEqual(res.samples_sent, 3)
        self.assertEqual(res.received, 3)
        self.assertAlmostEqual(res.best_ms, 10.0)

    def test_invalid_target_no_crash(self):
        t = PingTester(latency_fn=make_latency_fn({}), samples=2)
        res = t.ping_target(Target("bad", "", 0))
        self.assertFalse(res.reachable)
        self.assertIn("نامعتبر", res.error)
        self.assertEqual(res.samples_sent, 0)

    def test_ping_all_sorted_lowest_first(self):
        lat = make_latency_fn({"slow": 200.0, "fast": 10.0, "mid": 80.0})
        t = PingTester(latency_fn=lat, samples=1, timeout=1.0)
        targets = [Target("slow", "slow", 443),
                   Target("fast", "fast", 443),
                   Target("mid", "mid", 443)]
        results = t.ping_all(targets)
        self.assertEqual([r.host for r in results], ["fast", "mid", "slow"])

    def test_unreachable_sinks_to_bottom(self):
        lat = make_latency_fn({"ok": 50.0, "dead": None})
        t = PingTester(latency_fn=lat, samples=1)
        results = t.ping_all([Target("dead", "dead", 443),
                              Target("ok", "ok", 443)])
        self.assertEqual(results[0].host, "ok")
        self.assertFalse(results[-1].reachable)

    def test_best_picks_lowest_reachable(self):
        lat = make_latency_fn({"a": 90.0, "b": 30.0})
        t = PingTester(latency_fn=lat, samples=1)
        results = t.ping_all([Target("a", "a", 443), Target("b", "b", 443)])
        self.assertEqual(PingTester.best(results).host, "b")

    def test_best_none_when_all_dead(self):
        lat = make_latency_fn({"a": None, "b": None})
        t = PingTester(latency_fn=lat, samples=1)
        results = t.ping_all([Target("a", "a", 443), Target("b", "b", 443)])
        self.assertIsNone(PingTester.best(results))

    def test_download_measured_when_reachable(self):
        lat = make_latency_fn({"h": 20.0})
        thr = make_throughput_fn({"h": 512.0})
        t = PingTester(latency_fn=lat, throughput_fn=thr, samples=1)
        res = t.ping_target(Target("s", "h", 443), measure_download=True)
        self.assertEqual(res.download_kbps, 512.0)

    def test_download_skipped_when_unreachable(self):
        lat = make_latency_fn({"h": None})
        thr = make_throughput_fn({"h": 999.0})
        t = PingTester(latency_fn=lat, throughput_fn=thr, samples=1)
        res = t.ping_target(Target("s", "h", 443), measure_download=True)
        self.assertIsNone(res.download_kbps)

    def test_ping_profiles_uses_display_name(self):
        lat = make_latency_fn({"1.2.3.4": 15.0})
        t = PingTester(latency_fn=lat, samples=1)
        prof = _Prof("1.2.3.4", 443, remark="MyServer")
        results = t.ping_profiles([prof])
        self.assertEqual(results[0].label, "MyServer")
        self.assertTrue(results[0].reachable)

    def test_samples_must_be_positive(self):
        with self.assertRaises(ValueError):
            PingTester(samples=0)

    def test_log_callback_fires(self):
        logs = []
        lat = make_latency_fn({"h": 12.0})
        t = PingTester(latency_fn=lat, samples=1, on_log=logs.append)
        t.ping_target(Target("s", "h", 443))
        self.assertTrue(any("پینگ" in m for m in logs))


# ---------------------------------------------------------------------------
#  target_from_profile
# ---------------------------------------------------------------------------

class TestTargetFromProfile(unittest.TestCase):
    def test_builds_target(self):
        prof = _Prof("example.com", 8443, remark="Edge")
        tgt = target_from_profile(prof)
        self.assertEqual((tgt.label, tgt.host, tgt.port), ("Edge", "example.com", 8443))

    def test_fallback_label(self):
        prof = _Prof("h", 443)
        tgt = target_from_profile(prof)
        self.assertIn("vless", tgt.label)


# ---------------------------------------------------------------------------
#  strategy testing during ping
# ---------------------------------------------------------------------------

def make_strategy_probe(by_strategy):
    """by_strategy: {strategy_key: (outcome, latency_ms)}"""
    def fn(candidate, host, port, timeout):
        outcome, lat = by_strategy.get(candidate.strategy, (TIMEOUT, 0.0))
        return ProbeResult(candidate, outcome, latency_ms=lat)
    return fn


class TestStrategyPing(unittest.TestCase):
    def test_picks_best_connecting_strategy(self):
        probe = make_strategy_probe({
            "wrong_seq": (OK, 120.0),
            "fake_ttl": (OK, 40.0),    # lowest latency among OK → best
            "multi_fake": (RST, 0.0),
        })
        report = probe_strategies(
            Target("s", "h", 443),
            strategies=["wrong_seq", "fake_ttl", "multi_fake"],
            probe_fn=probe, timeout=1.0)
        self.assertTrue(report.any_connected)
        self.assertEqual(report.best.strategy, "fake_ttl")
        self.assertEqual(len(report.results), 3)

    def test_no_strategy_connects(self):
        probe = make_strategy_probe({"wrong_seq": (RST, 0.0),
                                     "fake_ttl": (TIMEOUT, 0.0)})
        report = probe_strategies(Target("s", "h", 443),
                                 strategies=["wrong_seq", "fake_ttl"],
                                 probe_fn=probe)
        self.assertFalse(report.any_connected)
        self.assertIsNone(report.best)
        self.assertIn("وصل نشد", report.summary())

    def test_pin_single_strategy(self):
        """Feedback 9: be able to select one strategy to ping with."""
        probe = make_strategy_probe({"multi_fake": (OK, 33.0)})
        report = probe_strategies(Target("s", "h", 443),
                                 strategies=["multi_fake"], probe_fn=probe)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.best.strategy, "multi_fake")

    def test_invalid_target_returns_empty_report(self):
        report = probe_strategies(Target("bad", "", 0),
                                 strategies=["wrong_seq"],
                                 probe_fn=make_strategy_probe({}))
        self.assertEqual(report.results, [])
        self.assertFalse(report.any_connected)

    def test_empty_strategy_list_no_crash(self):
        # default keys may be empty if strategies not importable; either way safe
        report = probe_strategies(Target("s", "h", 443), strategies=[],
                                 probe_fn=make_strategy_probe({}))
        self.assertIsInstance(report, StrategyPingReport)

    def test_default_strategy_keys_returns_implemented(self):
        keys = default_strategy_keys()
        # strategies package is importable in this repo; expect the 5 known keys
        for expected in ("wrong_seq", "fake_ttl", "multi_fake"):
            self.assertIn(expected, keys)


if __name__ == "__main__":
    unittest.main()
