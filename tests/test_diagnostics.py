"""Tests for the UI-agnostic diagnostics snapshot (step 12).

Builds a real AutoProber + ResilienceController, drives them with a fake probe /
throughput samples, and asserts that :func:`core.diagnostics.snapshot` reflects
the locked strategy, per-candidate health, throttle verdict and RST tally. Also
checks tolerance to an idle / empty engine.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.diagnostics import DiagnosticsSnapshot, CandidateStat, snapshot
from core.prober import AutoProber, Candidate, ProbeResult, OK, RST
from core.resilience import ResilienceController, ThroughputMonitor


class _FakeEngine:
    """Minimal duck-typed stand-in for EngineController."""

    def __init__(self, status="idle", spoof_port=None,
                 prober=None, resilience=None):
        self.status = status
        self.spoof_port = spoof_port
        self._prober = prober
        self.resilience = resilience

    def diagnostics(self):
        return snapshot(self)


def _build_prober(winner="fake_ttl"):
    cands = [Candidate("wrong_seq"), Candidate("fake_ttl"),
             Candidate("multi_fake")]

    def probe(c, host, port, timeout):
        if c.strategy == winner:
            return ProbeResult(c, OK, latency_ms=5.0)
        return ProbeResult(c, RST)

    p = AutoProber(cands, probe, rng=__import__("random").Random(0))
    p.run("1.2.3.4", 443)
    return p


# --------------------------------------------------------------------------
#  snapshot field mapping
# --------------------------------------------------------------------------

class DiagnosticsTest(unittest.TestCase):
    def test_idle_engine_returns_defaults(self):
        snap = _FakeEngine().diagnostics()
        self.assertIsInstance(snap, DiagnosticsSnapshot)
        self.assertEqual(snap.status, "idle")
        self.assertEqual(snap.candidates, [])
        self.assertFalse(snap.resilience_on)
        self.assertFalse(snap.has_probe_data)
        self.assertIsNone(snap.active_strategy)

    def test_live_throughput_surfaced_without_resilience(self):
        # #4: even with resilience OFF, the engine's live rate must show up as
        # recent_bps so the diagnostics throughput card isn't dead.
        eng = _FakeEngine(status="active")
        eng._live_down_bps = 1500.0
        eng._live_up_bps = 500.0
        snap = eng.diagnostics()
        self.assertFalse(snap.resilience_on)
        self.assertEqual(snap.recent_bps, 2000.0)   # down + up
        # no baseline ⇒ throttle ratio stays 0 (bar empty, but live speed shown)
        self.assertEqual(snap.baseline_bps, 0.0)
        self.assertEqual(snap.throttle_ratio, 0.0)

    def test_no_live_attrs_is_tolerated(self):
        # an engine that never reported traffic → recent_bps falls back to 0
        snap = _FakeEngine(status="active").diagnostics()
        self.assertEqual(snap.recent_bps, 0.0)

    def test_reports_active_strategy_and_candidate_stats(self):
        p = _build_prober(winner="fake_ttl")
        snap = _FakeEngine(status="active", spoof_port=40443,
                           prober=p).diagnostics()
        self.assertEqual(snap.status, "active")
        self.assertEqual(snap.spoof_port, 40443)
        self.assertEqual(snap.active_strategy, "fake_ttl")
        self.assertTrue(snap.has_probe_data)
        self.assertEqual(len(snap.candidates), 3)
        # the selected candidate is first and flagged
        self.assertTrue(snap.candidates[0].selected)
        self.assertEqual(snap.candidates[0].strategy, "fake_ttl")
        # the winner has a positive score; the losers RSTed (score 0)
        self.assertGreater(snap.candidates[0].mean_score, 0.0)
        winner = next(c for c in snap.candidates if c.strategy == "fake_ttl")
        self.assertEqual(winner.success_rate, 1.0)
        self.assertEqual(winner.last_outcome, OK)
        loser = next(c for c in snap.candidates if c.strategy == "wrong_seq")
        self.assertEqual(loser.success_rate, 0.0)
        self.assertEqual(loser.last_outcome, RST)

    def test_candidates_ranked_selected_first_then_by_score(self):
        p = _build_prober(winner="multi_fake")
        snap = _FakeEngine(prober=p).diagnostics()
        self.assertTrue(snap.candidates[0].selected)
        self.assertEqual(snap.candidates[0].strategy, "multi_fake")

    def test_resilience_fields_reflected(self):
        res = ResilienceController(
            rst_budget=3, throughput=ThroughputMonitor(
                window=8, throttle_ratio=0.4, min_samples=4))
        res.set_chains(["fake_ttl", "wrong_seq"], ["1.1.1.1", "8.8.8.8"])
        snap = _FakeEngine(status="active", resilience=res).diagnostics()
        self.assertTrue(snap.resilience_on)
        self.assertEqual(snap.rst_budget, 3)
        self.assertEqual(snap.strategy_chain, ["fake_ttl", "wrong_seq"])
        self.assertEqual(snap.ip_chain, ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(snap.current_ip, "1.1.1.1")
        # no prober but resilience supplies the active strategy
        self.assertEqual(snap.active_strategy, "fake_ttl")
        self.assertFalse(snap.throttled)

    def test_throttle_and_rst_tally_reflected(self):
        res = ResilienceController(
            rst_budget=5, throughput=ThroughputMonitor(
                window=8, throttle_ratio=0.4, min_samples=4))
        res.set_chains(["fake_ttl", "wrong_seq", "multi_fake"], ["1.1.1.1"])
        # establish baseline then throttle
        for _ in range(4):
            res.on_throughput(1_000_000, 1.0)
        for _ in range(4):
            res.on_throughput(50_000, 1.0)
        # a couple of forged RSTs (early, no handshake)
        from core.resilience import RstEvent
        res.on_rst(RstEvent(ms_since_client_hello=10.0))
        snap = _FakeEngine(resilience=res).diagnostics()
        self.assertGreater(snap.baseline_bps, 0.0)
        self.assertGreaterEqual(snap.forged_rst_count, 1)
        self.assertGreater(snap.throttle_ratio, 0.0)
        self.assertLess(snap.throttle_ratio, 1.0)

    def test_prober_and_resilience_together(self):
        p = _build_prober(winner="fake_ttl")
        res = ResilienceController()
        res.set_chains(["fake_ttl"], ["1.1.1.1"])
        snap = _FakeEngine(status="active", spoof_port=40443,
                           prober=p, resilience=res).diagnostics()
        # prober wins for active_strategy; resilience still on
        self.assertEqual(snap.active_strategy, "fake_ttl")
        self.assertTrue(snap.resilience_on)
        self.assertEqual(len(snap.candidates), 3)

    def test_throttle_ratio_zero_without_baseline(self):
        snap = DiagnosticsSnapshot()
        self.assertEqual(snap.throttle_ratio, 0.0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(DiagnosticsTest)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
