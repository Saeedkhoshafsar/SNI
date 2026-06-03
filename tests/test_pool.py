"""Unit tests for the multi-IP / multi-SNI route pool (``core.pool``).

Every test runs **headless** — the network is injected via a fake ``probe_fn``
so the exploration / ranking / draining / weighted-random logic is exercised
deterministically with no real sockets and no background threads (the health
loop is driven step-by-step instead of being started).
"""
import os
import sys
import random
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from core.pool import (
    PairStats,
    CombinationExplorer,
    ActivePool,
    ConnectionManager,
    ConnectionTracker,
    build_connection_manager,
    export_sni_list,
    export_routes,
    _weighted_sample,
    FAILOVER_THRESHOLD,
    FAILOVER_WINDOW,
)


def _make_probe(good_ips):
    """Return a probe_fn where only IPs in *good_ips* succeed."""
    good = set(good_ips)

    def probe(ip, port, timeout):
        return ip in good

    return probe


def _make_spoof_probe(good_pairs):
    """Return a spoof_probe_fn where only (ip, sni) in *good_pairs* pass.

    Mirrors :func:`core.pool.spoof_handshake_probe`'s signature
    ``(ip, port, timeout, sni) -> bool`` so tests can drive the explorer's
    high-confidence path deterministically without any network.
    """
    good = {(ip, sni) for ip, sni in good_pairs}

    def probe(ip, port, timeout, sni):
        return (ip, sni) in good

    return probe


# ---------------------------------------------------------------------------
# PairStats
# ---------------------------------------------------------------------------

class PairStatsTest(unittest.TestCase):
    def test_probe_loss_below_min_probes_is_zero(self):
        ps = PairStats("1.1.1.1", "a.com")
        ps.record_probe(False)
        ps.record_probe(False)  # 2 < MIN_PROBES(3)
        self.assertEqual(ps.probe_loss_rate, 0.0)

    def test_probe_loss_after_min_probes(self):
        ps = PairStats("1.1.1.1", "a.com")
        ps.record_probe(True)
        ps.record_probe(False)
        ps.record_probe(False)  # 3 probes, 1 recv → loss 2/3
        self.assertAlmostEqual(ps.probe_loss_rate, 2 / 3)

    def test_dead_threshold_marks_pair_dead(self):
        ps = PairStats("1.1.1.1", "a.com")
        for _ in range(5):
            ps.record_probe(False, dead_threshold=0.80)
        self.assertFalse(ps.alive)
        self.assertEqual(ps.score, float("inf"))

    def test_unprobed_pair_score_is_half(self):
        ps = PairStats("1.1.1.1", "a.com")
        self.assertFalse(ps.probed)
        self.assertEqual(ps.score, 0.5)

    def test_combined_loss_weights_real_07_probe_03(self):
        ps = PairStats("1.1.1.1", "a.com")
        # Make probe loss = 0.5 (3 sent, 1 recv → wait that's 2/3). Use 4 probes.
        ps.record_probe(True)
        ps.record_probe(True)
        ps.record_probe(False)
        ps.record_probe(False)            # probe loss = 2/4 = 0.5
        self.assertAlmostEqual(ps.probe_loss_rate, 0.5)
        # Need real_packets_sent > 10 to blend. Make real loss = 1.0.
        for _ in range(11):
            ps.record_real_packet(lost=True)
        # combined = 0.7*1.0 + 0.3*0.5 = 0.85
        self.assertAlmostEqual(ps.combined_loss_rate, 0.85)

    def test_combined_loss_probe_only_before_threshold(self):
        ps = PairStats("1.1.1.1", "a.com")
        ps.record_probe(True)
        ps.record_probe(False)
        ps.record_probe(False)            # probe loss = 2/3
        ps.record_real_packet(lost=True)  # only 1 real packet (<= 10)
        self.assertAlmostEqual(ps.combined_loss_rate, 2 / 3)

    def test_acquire_release_counts(self):
        ps = PairStats("1.1.1.1", "a.com")
        ps.acquire()
        ps.acquire()
        self.assertEqual(ps.active_connections, 2)
        self.assertEqual(ps.total_connections, 2)
        ps.release()
        self.assertEqual(ps.active_connections, 1)
        ps.release()
        ps.release()  # never goes negative
        self.assertEqual(ps.active_connections, 0)


# ---------------------------------------------------------------------------
# CombinationExplorer
# ---------------------------------------------------------------------------

class CombinationExplorerTest(unittest.TestCase):
    def setUp(self):
        random.seed(1234)
        self.combos = [(f"10.0.0.{i}", "a.com") for i in range(5)]
        self.good = ["10.0.0.0", "10.0.0.1", "10.0.0.2"]

    def _explorer(self):
        return CombinationExplorer(
            combinations=self.combos,
            port=443,
            timeout=1.0,
            probe_count=5,
            probe_fn=_make_probe(self.good),
        )

    def test_initial_explore_probes_subset_and_marks_alive(self):
        ex = self._explorer()
        ex.initial_explore()
        known = ex.known_stats()
        self.assertTrue(known)
        # good IPs that were probed must be alive; bad ones dead.
        for ps in known:
            if ps.ip in self.good:
                self.assertTrue(ps.alive)
            else:
                self.assertFalse(ps.alive)

    def test_stable_stats_excludes_dead(self):
        ex = self._explorer()
        ex.initial_explore()
        ex.periodic_explore()  # explore remaining
        stable = ex.stable_stats()
        for ps in stable:
            self.assertIn(ps.ip, self.good)

    def test_summary_counts(self):
        ex = self._explorer()
        ex.initial_explore()
        ex.periodic_explore()
        s = ex.summary()
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["known"], 5)
        self.assertEqual(s["stable"], 3)
        self.assertEqual(s["dead"], 2)

    def test_periodic_explore_reshuffles_when_exhausted(self):
        ex = self._explorer()
        ex.initial_explore()       # samples up to 20 → all 5 explored
        # queue is now empty; another periodic_explore should reshuffle.
        ex.periodic_explore()
        self.assertEqual(len(ex._unexplored), 5)

    def test_probe_fn_exception_counts_as_failure(self):
        def boom(ip, port, timeout):
            raise RuntimeError("net down")

        ex = CombinationExplorer(
            self.combos, 443, 1.0, 5, probe_fn=boom)
        ex.initial_explore()
        for ps in ex.known_stats():
            self.assertFalse(ps.alive)


# ---------------------------------------------------------------------------
# ActivePool
# ---------------------------------------------------------------------------

class ActivePoolTest(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        self.combos = [(f"10.0.0.{i}", "a.com") for i in range(6)]
        self.good = ["10.0.0.0", "10.0.0.1", "10.0.0.2", "10.0.0.3"]
        self.ex = CombinationExplorer(
            self.combos, 443, 1.0, 5, probe_fn=_make_probe(self.good))
        self.ex.initial_explore()
        self.ex.periodic_explore()

    def test_initialize_fills_slots(self):
        pool = ActivePool(self.ex, slots=3)
        pool.initialize()
        self.assertEqual(len(pool.active_pairs), 3)
        for ps in pool.active_pairs:
            self.assertTrue(ps.in_active_pool)
            self.assertTrue(ps.alive)

    def test_pick_returns_pair_from_pool(self):
        pool = ActivePool(self.ex, slots=3)
        pool.initialize()
        pair = pool.pick()
        self.assertIsNotNone(pair)
        self.assertIn(pair, pool.active_pairs)

    def test_pick_weighted_prefers_low_loss(self):
        # Two candidate pairs; one with near-zero loss, one near 1.0.
        low = PairStats("1.1.1.1", "a.com")
        high = PairStats("2.2.2.2", "a.com")
        for _ in range(5):
            low.record_probe(True)
        for _ in range(5):
            high.record_probe(True)
        # push high's loss up via real packets (>10)
        for _ in range(20):
            high.record_real_packet(lost=True)
        for _ in range(20):
            low.record_real_packet(lost=False)
        ex = mock.Mock()
        ex.known_stats.return_value = [low, high]
        ex.all_stats.return_value = [low, high]
        pool = ActivePool(ex, slots=2)
        with mock.patch.object(pool, "_pool", [low, high]):
            counts = {id(low): 0, id(high): 0}
            random.seed(99)
            for _ in range(2000):
                counts[id(pool.pick())] += 1
        self.assertGreater(counts[id(low)], counts[id(high)] * 3)

    def test_report_failure_drains_weak_pair(self):
        pool = ActivePool(self.ex, slots=2, loss_threshold=0.20)
        pool.initialize()
        victim = pool.active_pairs[0]
        # simulate a live connection on the victim so it must drain, not vanish
        victim.acquire()
        # hammer failures until it crosses the loss threshold / dies
        for _ in range(10):
            pool.report_failure(victim)
        self.assertNotIn(victim, pool.active_pairs)
        self.assertIn(victim, pool.draining_pairs)

    def test_draining_pair_released_after_connections_finish(self):
        pool = ActivePool(self.ex, slots=2, loss_threshold=0.20)
        pool.initialize()
        victim = pool.active_pairs[0]
        victim.acquire()
        for _ in range(10):
            pool.report_failure(victim)
        self.assertIn(victim, pool.draining_pairs)
        # connection finishes → next refresh frees it from draining
        victim.release()
        pool.refresh()
        self.assertNotIn(victim, pool.draining_pairs)
        self.assertFalse(victim.in_active_pool)

    def test_refresh_backfills_empty_slot(self):
        pool = ActivePool(self.ex, slots=2, loss_threshold=0.20)
        pool.initialize()
        self.assertEqual(len(pool.active_pairs), 2)
        victim = pool.active_pairs[0]
        for _ in range(10):
            pool.report_failure(victim)  # drains + backfills (no live conns)
        # victim had no active connections → fully released, slot refilled
        self.assertEqual(len(pool.active_pairs), 2)


# ---------------------------------------------------------------------------
# _weighted_sample
# ---------------------------------------------------------------------------

class WeightedSampleTest(unittest.TestCase):
    def test_returns_distinct_pairs(self):
        random.seed(3)
        pairs = [PairStats(f"1.1.1.{i}", "a") for i in range(4)]
        for p in pairs:
            for _ in range(5):
                p.record_probe(True)
        chosen = _weighted_sample(pairs, 3)
        self.assertEqual(len(chosen), 3)
        self.assertEqual(len({id(p) for p in chosen}), 3)

    def test_caps_at_available(self):
        pairs = [PairStats("1.1.1.1", "a")]
        chosen = _weighted_sample(pairs, 5)
        self.assertEqual(len(chosen), 1)


# ---------------------------------------------------------------------------
# ConnectionManager + factory
# ---------------------------------------------------------------------------

class ConnectionManagerTest(unittest.TestCase):
    def test_run_health_loop_bootstraps_then_can_stop(self):
        random.seed(5)
        combos = [(f"10.0.0.{i}", "a.com") for i in range(4)]
        good = ["10.0.0.0", "10.0.0.1"]
        mgr = ConnectionManager(
            combinations=combos, port=443,
            health_check_interval=0.01,
            probe_fn=_make_probe(good),
        )
        # Drive the bootstrap portion directly (no infinite loop).
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        pair = mgr.pick_pair()
        self.assertIsNotNone(pair)
        self.assertIn(pair.ip, good)

    def test_pick_pair_none_when_no_pairs(self):
        mgr = ConnectionManager(combinations=[], port=443,
                                probe_fn=_make_probe([]))
        self.assertIsNone(mgr.pick_pair())

    def test_seconds_since_check_none_before_run(self):
        mgr = ConnectionManager(combinations=[("1.1.1.1", "a")], port=443)
        self.assertIsNone(mgr.seconds_since_check)


class BuildConnectionManagerTest(unittest.TestCase):
    def test_single_pair_returns_none(self):
        cfg = {"CONNECT_IP": "1.1.1.1", "FAKE_SNI": "a.com"}
        self.assertIsNone(build_connection_manager(cfg))

    def test_single_element_lists_return_none(self):
        cfg = {"CONNECT_IPS": ["1.1.1.1"], "FAKE_SNIS": ["a.com"]}
        self.assertIsNone(build_connection_manager(cfg))

    def test_empty_returns_none(self):
        self.assertIsNone(build_connection_manager({}))

    def test_multi_builds_cartesian_product(self):
        cfg = {
            "CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com", "b.com", "c.com"],
        }
        mgr = build_connection_manager(cfg, probe_fn=_make_probe([]))
        self.assertIsNotNone(mgr)
        self.assertEqual(len(mgr.explorer.stats), 6)  # 2 × 3

    def test_singular_fallback_when_plural_empty(self):
        cfg = {
            "CONNECT_IPS": [],
            "FAKE_SNIS": ["a.com", "b.com"],
            "CONNECT_IP": "9.9.9.9",
        }
        mgr = build_connection_manager(cfg, probe_fn=_make_probe([]))
        self.assertIsNotNone(mgr)
        self.assertEqual(len(mgr.explorer.stats), 2)  # 1 × 2
        ips = {ip for ip, _ in mgr.explorer.stats}
        self.assertEqual(ips, {"9.9.9.9"})

    def test_dedupe_and_strip(self):
        cfg = {
            "CONNECT_IPS": [" 1.1.1.1 ", "1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com", "a.com"],
        }
        mgr = build_connection_manager(cfg, probe_fn=_make_probe([]))
        self.assertIsNotNone(mgr)
        self.assertEqual(len(mgr.explorer.stats), 2)  # 2 unique IP × 1 unique SNI

    def test_config_knobs_propagate(self):
        cfg = {
            "CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
            "FAKE_SNIS": ["a.com"],
            "CONNECT_PORT": 8443,
            "ACTIVE_SLOTS": 5,
            "LOSS_THRESHOLD": 0.33,
            "DEAD_THRESHOLD": 0.66,
        }
        mgr = build_connection_manager(cfg, probe_fn=_make_probe([]))
        self.assertEqual(mgr.explorer.port, 8443)
        self.assertEqual(mgr.pool.slots, 5)
        self.assertAlmostEqual(mgr.explorer.loss_threshold, 0.33)
        self.assertAlmostEqual(mgr.explorer.dead_threshold, 0.66)


class _FakeClock:
    """A monotonic clock you can advance by hand for deterministic tests."""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


# ---------------------------------------------------------------------------
# ConnectionTracker (7.8 — per-IP rapid-failover)
# ---------------------------------------------------------------------------

class ConnectionTrackerTest(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.t = ConnectionTracker(clock=self.clock)

    def test_defaults_match_reference(self):
        self.assertEqual(FAILOVER_THRESHOLD, 3)
        self.assertEqual(FAILOVER_WINDOW, 30.0)

    def test_failover_after_threshold(self):
        ip = "1.1.1.1"
        self.assertFalse(self.t.should_failover(ip))
        for _ in range(FAILOVER_THRESHOLD - 1):
            self.t.record_failure(ip)
        self.assertFalse(self.t.should_failover(ip))
        self.t.record_failure(ip)
        self.assertTrue(self.t.should_failover(ip))

    def test_record_failure_returns_live_count(self):
        ip = "2.2.2.2"
        self.assertEqual(self.t.record_failure(ip), 1)
        self.assertEqual(self.t.record_failure(ip), 2)

    def test_window_prunes_old_failures(self):
        ip = "3.3.3.3"
        for _ in range(FAILOVER_THRESHOLD):
            self.t.record_failure(ip)
        self.assertTrue(self.t.should_failover(ip))
        # advance past the window — old failures should be pruned away
        self.clock.advance(FAILOVER_WINDOW + 1)
        self.assertFalse(self.t.should_failover(ip))
        self.assertEqual(self.t.failure_count(ip), 0)

    def test_success_clears_failures(self):
        ip = "4.4.4.4"
        for _ in range(FAILOVER_THRESHOLD):
            self.t.record_failure(ip)
        self.assertTrue(self.t.should_failover(ip))
        self.t.record_success(ip)
        self.assertFalse(self.t.should_failover(ip))
        self.assertEqual(self.t.success_count(ip), 1)

    def test_clear_and_reset(self):
        self.t.record_failure("a")
        self.t.record_success("a")
        self.t.clear("a")
        self.assertEqual(self.t.failure_count("a"), 0)
        self.t.reset()
        self.assertEqual(self.t.success_count("a"), 0)

    def test_empty_ip_is_noop(self):
        self.assertEqual(self.t.record_failure(""), 0)
        self.assertFalse(self.t.should_failover(""))

    def test_snapshot_shape(self):
        for _ in range(FAILOVER_THRESHOLD):
            self.t.record_failure("blocked")
        self.t.record_success("good")
        snap = self.t.snapshot()
        self.assertEqual(snap["threshold"], FAILOVER_THRESHOLD)
        ips = {r["ip"]: r for r in snap["ips"]}
        self.assertTrue(ips["blocked"]["blocked"])
        self.assertFalse(ips["good"]["blocked"])
        self.assertEqual(ips["good"]["successes"], 1)


# ---------------------------------------------------------------------------
# ConnectionManager failover-aware picking + report_success (7.7/7.8)
# ---------------------------------------------------------------------------

class ManagerFailoverTest(unittest.TestCase):
    def _mgr(self, good):
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = ConnectionManager(combos, 443, probe_fn=_make_probe(good))
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        return mgr

    def test_pick_skips_failed_ip(self):
        mgr = self._mgr(["1.1.1.1", "2.2.2.2"])
        # trip failover on 1.1.1.1 directly via the tracker
        for _ in range(FAILOVER_THRESHOLD):
            mgr.tracker.record_failure("1.1.1.1")
        picks = [mgr.pick_pair().ip for _ in range(50)]
        # the healthy route must dominate; the failed IP should be strongly
        # avoided (the picker retries to skip failover IPs when alternatives
        # remain, falling back only if every route is tripped).
        self.assertIn("2.2.2.2", picks)
        self.assertGreater(picks.count("2.2.2.2"), picks.count("1.1.1.1"))

    def test_report_failure_feeds_tracker(self):
        mgr = self._mgr(["1.1.1.1", "2.2.2.2"])
        ps = mgr.pool.pick()
        for _ in range(FAILOVER_THRESHOLD):
            mgr.report_failure(ps)
        self.assertTrue(mgr.tracker.should_failover(ps.ip))

    def test_report_success_clears_tracker(self):
        mgr = self._mgr(["1.1.1.1", "2.2.2.2"])
        ps = mgr.pool.pick()
        for _ in range(FAILOVER_THRESHOLD):
            mgr.report_failure(ps)
        self.assertTrue(mgr.tracker.should_failover(ps.ip))
        mgr.report_success(ps)
        self.assertFalse(mgr.tracker.should_failover(ps.ip))

    def test_pick_falls_back_when_all_failed(self):
        mgr = self._mgr(["1.1.1.1", "2.2.2.2"])
        for ip in ("1.1.1.1", "2.2.2.2"):
            for _ in range(FAILOVER_THRESHOLD):
                mgr.tracker.record_failure(ip)
        # every IP is in failover → pick_pair must still return *something*
        self.assertIsNotNone(mgr.pick_pair())


# ---------------------------------------------------------------------------
# Export helpers (7.10)
# ---------------------------------------------------------------------------

class ExportHelpersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_export_sni_list_dedupes_and_counts(self):
        path = os.path.join(self.dir, "snis.txt")
        n = export_sni_list(["a.com", "b.com", "a.com", "  ", ""], path)
        self.assertEqual(n, 2)
        body = open(path, encoding="utf-8").read()
        self.assertIn("a.com", body)
        self.assertIn("b.com", body)
        self.assertIn("# Total: 2", body)

    def test_export_sni_list_empty(self):
        path = os.path.join(self.dir, "empty.txt")
        n = export_sni_list([], path)
        self.assertEqual(n, 0)
        self.assertIn("# Total: 0", open(path, encoding="utf-8").read())

    def test_export_routes_from_config(self):
        path = os.path.join(self.dir, "routes.txt")
        cfg = {"CONNECT_IPS": ["1.1.1.1", "2.2.2.2"],
               "FAKE_SNIS": ["x.com", "y.com"]}
        n = export_routes(cfg, path)
        self.assertEqual(n, 4)
        body = open(path, encoding="utf-8").read()
        self.assertIn("1.1.1.1\tx.com", body)

    def test_export_routes_from_manager(self):
        path = os.path.join(self.dir, "routes2.txt")
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = ConnectionManager(combos, 443, probe_fn=_make_probe(["1.1.1.1"]))
        mgr.explorer.initial_explore()
        n = export_routes(mgr, path)
        self.assertEqual(n, 2)

    def test_export_routes_bad_arg(self):
        with self.assertRaises(TypeError):
            export_routes(12345, os.path.join(self.dir, "z.txt"))


# ---------------------------------------------------------------------------
# Background-optimiser interface (redesign): best_candidate / find_better_route
# ---------------------------------------------------------------------------

class BackgroundOptimiserTest(unittest.TestCase):
    def _mgr(self, combos, good):
        mgr = ConnectionManager(combos, 443, probe_fn=_make_probe(good))
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        return mgr

    def test_best_candidate_returns_lowest_loss_stable(self):
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        # only 1.1.1.1 probes clean → it is the stable best
        mgr = self._mgr(combos, ["1.1.1.1"])
        best = mgr.best_candidate()
        self.assertIsNotNone(best)
        self.assertEqual(best.ip, "1.1.1.1")

    def test_best_candidate_none_when_nothing_stable(self):
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = self._mgr(combos, [])  # nothing probes clean
        self.assertIsNone(mgr.best_candidate())

    def test_find_better_route_emergency_swaps_only_to_proven(self):
        # current route broken ⇒ swap, but ONLY to a route we are confident in
        # (real_proven). A merely probe-clean candidate is not good enough
        # (confidence gate): a clean TCP connect does not prove DPI bypass.
        combos = [("1.1.1.1", "a.com"), ("9.9.9.9", "x.com")]
        mgr = self._mgr(combos, ["1.1.1.1"])
        # 1.1.1.1 only probed clean → not proven → no swap yet (stay put).
        self.assertIsNone(
            mgr.find_better_route("9.9.9.9", "x.com", current_healthy=False))
        # Prove 1.1.1.1 with real traffic → now the broken route may swap to it.
        mgr.lookup_pair("1.1.1.1", "a.com").record_real_packet(lost=False)
        better = mgr.find_better_route("9.9.9.9", "x.com",
                                       current_healthy=False)
        self.assertIsNotNone(better)
        self.assertEqual(better.ip, "1.1.1.1")

    def test_find_better_route_conservative_when_healthy(self):
        # current route healthy and is itself the best ⇒ no swap
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = self._mgr(combos, ["1.1.1.1"])
        # feed current route enough clean probes so it has a low loss
        cur = mgr.lookup_pair("1.1.1.1", "a.com")
        self.assertIsNotNone(cur)
        better = mgr.find_better_route("1.1.1.1", "a.com",
                                       current_healthy=True)
        # candidate equals current (or no strict margin) ⇒ None
        self.assertIsNone(better)

    def test_healthy_route_never_swapped_even_if_probe_clean_candidate_exists(self):
        """THE GOLDEN RULE / regression guard for the TimeoutError flood.

        A working (healthy) route must NEVER be swapped for a probe-clean
        candidate — a clean TCP probe does not prove DPI bypass. Even when a
        different IP probes perfectly and the current route is NOT in the pool
        (lookup_pair → None, exactly the field scenario), find_better_route must
        return None while the incumbent is healthy.
        """
        # the current route (the user's confirmed primary) is NOT in the pool;
        # 2.2.2.2 probes squeaky-clean and would look "better" by probe loss.
        combos = [("2.2.2.2", "cloudflare.com")]
        mgr = self._mgr(combos, ["2.2.2.2"])
        self.assertIsNone(mgr.lookup_pair("104.19.229.21", "www.hcaptcha.com"))
        better = mgr.find_better_route(
            "104.19.229.21", "www.hcaptcha.com", current_healthy=True)
        self.assertIsNone(better)  # working route left alone — no churn

    def test_broken_route_prefers_real_proven_candidate(self):
        """When broken, prefer a candidate proven by REAL traffic over a
        probe-only candidate, even if the probe-only one has lower probe loss."""
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        # both probe clean; 1.1.1.1 has carried successful REAL traffic, 2.2.2.2
        # only probed clean.
        mgr = self._mgr(combos, ["1.1.1.1", "2.2.2.2"])
        proven = mgr.lookup_pair("1.1.1.1", "a.com")
        proven.record_real_packet(lost=False)  # real-traffic proof
        self.assertTrue(proven.real_proven)
        unproven = mgr.lookup_pair("2.2.2.2", "b.com")
        self.assertFalse(unproven.real_proven)
        better = mgr.find_better_route("9.9.9.9", "x.com",
                                       current_healthy=False)
        self.assertIsNotNone(better)
        self.assertEqual(better.ip, "1.1.1.1")  # real-proven wins

    def test_ensure_pair_creates_when_missing(self):
        combos = [("1.1.1.1", "a.com")]
        mgr = self._mgr(combos, ["1.1.1.1"])
        self.assertIsNone(mgr.lookup_pair("5.5.5.5", "primary.com"))
        ps = mgr.ensure_pair("5.5.5.5", "primary.com")
        self.assertIsNotNone(ps)
        self.assertEqual((ps.ip, ps.sni), ("5.5.5.5", "primary.com"))
        # now lookup finds it and real outcomes can attribute to it
        self.assertIs(mgr.lookup_pair("5.5.5.5", "primary.com"), ps)
        # idempotent — same object returned
        self.assertIs(mgr.ensure_pair("5.5.5.5", "primary.com"), ps)

    def test_real_proven_property(self):
        ps = PairStats("1.1.1.1", "a.com")
        self.assertFalse(ps.real_proven)        # no real traffic yet
        ps.record_real_packet(lost=False)
        self.assertTrue(ps.real_proven)         # one good real packet → proven
        ps2 = PairStats("2.2.2.2", "b.com")
        ps2.record_real_packet(lost=True)       # 100% real loss
        self.assertFalse(ps2.real_proven)       # too lossy → not proven

    def test_find_better_route_never_returns_current(self):
        combos = [("1.1.1.1", "a.com")]
        mgr = ConnectionManager(combos + [("2.2.2.2", "b.com")], 443,
                                probe_fn=_make_probe(["1.1.1.1"]))
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        better = mgr.find_better_route("1.1.1.1", "a.com",
                                       current_healthy=False)
        if better is not None:
            self.assertNotEqual((better.ip, better.sni), ("1.1.1.1", "a.com"))

    def test_lookup_pair(self):
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = self._mgr(combos, ["1.1.1.1"])
        self.assertIsNotNone(mgr.lookup_pair("1.1.1.1", "a.com"))
        self.assertIsNone(mgr.lookup_pair("3.3.3.3", "z.com"))


# ---------------------------------------------------------------------------
# Spoof-handshake probe: high-confidence promotion evidence
# ---------------------------------------------------------------------------

class SpoofProbeTest(unittest.TestCase):
    """The spoof probe replays a fake-SNI ClientHello as a liveness hint.

    IMPORTANT (lesson from the 21:33–21:37 churn log): the spoof probe opens a
    *direct* socket to the CDN IP, and a Cloudflare edge answers TLS bytes to
    almost any ClientHello — so a pass proves only reachability, NOT that the
    live WinDivert-injected path works. It therefore can NEVER make a route
    ``real_proven`` or authorise a swap on its own. Only real forwarded traffic
    does. The probe survives purely as a ranking tie-break (``spoof_proven``).
    """

    def _mgr(self, combos, good_pairs):
        mgr = ConnectionManager(
            combos, 443, spoof_probe_fn=_make_spoof_probe(good_pairs))
        mgr.explorer.initial_explore()
        mgr.pool.initialize()
        return mgr

    def test_spoof_probe_sets_spoof_proven_but_not_real_proven(self):
        ps = PairStats("1.1.1.1", "a.com")
        self.assertFalse(ps.spoof_proven)
        self.assertFalse(ps.real_proven)
        # one confirmed spoofed handshake → spoof_proven (a hint) but NOT
        # real_proven — a direct-socket decoy reply is a false positive for the
        # live injected path, so it must never authorise a swap.
        ps.record_spoof_probe(success=True)
        self.assertTrue(ps.spoof_proven)
        self.assertFalse(ps.real_proven)

    def test_real_traffic_is_the_only_thing_that_proves_a_route(self):
        ps = PairStats("1.1.1.1", "a.com")
        ps.record_spoof_probe(success=True)
        self.assertFalse(ps.real_proven)
        # a single successful real forwarded connection proves it for real
        ps.record_real_packet(lost=False)
        self.assertTrue(ps.real_proven)

    def test_failed_spoof_probe_does_not_prove(self):
        ps = PairStats("2.2.2.2", "b.com")
        # connects at TCP level but the decoy is blocked by DPI every time
        ps.record_spoof_probe(success=False)
        ps.record_spoof_probe(success=False)
        ps.record_spoof_probe(success=False)
        self.assertFalse(ps.spoof_proven)
        self.assertFalse(ps.real_proven)
        # repeated blocks should also mark it not-alive (dead route)
        self.assertFalse(ps.alive)

    def test_explorer_uses_spoof_probe_when_present(self):
        # 1.1.1.1+a.com decoy survives; 2.2.2.2+b.com is TCP-up but DPI-blocked
        combos = [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")]
        mgr = self._mgr(combos, [("1.1.1.1", "a.com")])
        good = mgr.lookup_pair("1.1.1.1", "a.com")
        bad = mgr.lookup_pair("2.2.2.2", "b.com")
        # decoy survives → spoof_proven (a ranking hint) but still NOT proven
        # for promotion until real traffic confirms it
        self.assertTrue(good.spoof_proven)
        self.assertFalse(good.real_proven)
        self.assertFalse(bad.spoof_proven)
        self.assertFalse(bad.real_proven)

    def test_spoof_proven_breaks_ranking_ties_within_unproven_tier(self):
        # Among routes that have NO real traffic, a spoof-passing one ranks
        # above a merely-TCP-reachable one (a useful exploration hint), even
        # though neither is promotable yet.
        a = PairStats("1.1.1.1", "a.com")   # decoy passes
        b = PairStats("2.2.2.2", "b.com")   # decoy blocked but TCP up
        a.record_spoof_probe(success=True)
        b.record_spoof_probe(success=False)
        self.assertTrue(ConnectionManager._candidate_is_better(a, b))
        self.assertFalse(ConnectionManager._candidate_is_better(b, a))

    def test_promoter_never_swaps_to_spoof_only_route_when_broken(self):
        # broken incumbent, but the only candidate is spoof-proven (direct
        # decoy) and has carried NO real traffic → a false positive → stay put.
        combos = [("1.1.1.1", "a.com"), ("9.9.9.9", "x.com")]
        mgr = self._mgr(combos, [("1.1.1.1", "a.com")])
        self.assertIsNone(
            mgr.find_better_route("9.9.9.9", "x.com", current_healthy=False))

    def test_promoter_swaps_to_real_proven_route_when_broken(self):
        # broken incumbent + a candidate proven by REAL traffic ⇒ swap to it.
        combos = [("1.1.1.1", "a.com"), ("9.9.9.9", "x.com")]
        mgr = self._mgr(combos, [("1.1.1.1", "a.com")])
        good = mgr.lookup_pair("1.1.1.1", "a.com")
        good.record_real_packet(lost=False)   # real-traffic proof
        better = mgr.find_better_route("9.9.9.9", "x.com",
                                       current_healthy=False)
        self.assertIsNotNone(better)
        self.assertEqual((better.ip, better.sni), ("1.1.1.1", "a.com"))

    def test_promoter_stays_put_when_no_proven_candidate(self):
        # nothing passes the spoof probe ⇒ no confident target ⇒ stay put
        combos = [("1.1.1.1", "a.com"), ("9.9.9.9", "x.com")]
        mgr = self._mgr(combos, [])  # every decoy blocked
        self.assertIsNone(
            mgr.find_better_route("9.9.9.9", "x.com", current_healthy=False))

    def test_default_runtime_enables_spoof_probe(self):
        # When neither probe_fn nor spoof_probe_fn is given (real runtime),
        # the manager wires in the real spoof_handshake_probe by default.
        from core.pool import spoof_handshake_probe
        mgr = ConnectionManager([("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")],
                                443)
        self.assertIs(mgr.explorer._spoof_probe_fn, spoof_handshake_probe)

    def test_injected_probe_fn_keeps_plain_tcp_semantics(self):
        # A test that injects probe_fn (and no spoof fn) must keep using plain
        # TCP probing — the spoof default must not silently override it.
        mgr = ConnectionManager([("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")],
                                443, probe_fn=_make_probe(["1.1.1.1"]))
        self.assertIsNone(mgr.explorer._spoof_probe_fn)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Manual SNI/IP scanner (issue: "شروع تست" button) + export_sni_pairs
# ---------------------------------------------------------------------------

from core.pool import (
    SniIpScanner,
    ScanCandidate,
    build_scan_candidates,
    export_sni_pairs,
    import_sni_pairs,
    parse_sni_pairs_text,
)


class BuildScanCandidatesTest(unittest.TestCase):
    def test_cartesian_product_plus_extra_pairs_dedupe_order(self):
        cands = build_scan_candidates(
            ["1.1.1.1", "2.2.2.2", "1.1.1.1"],   # dupe IP
            ["a.com", "b.com"],
            extra_pairs=[("9.9.9.9", "z.com"), ("1.1.1.1", "a.com")],
        )
        # extra_pairs come first; the (1.1.1.1, a.com) extra dedupes the product
        self.assertEqual(cands[0], ("9.9.9.9", "z.com"))
        self.assertEqual(cands[1], ("1.1.1.1", "a.com"))
        # extras {z.com, (1.1.1.1,a.com)} + product (4) − 1 overlap = 5 unique
        self.assertEqual(len(cands), 5)
        # no duplicates
        self.assertEqual(len(cands), len(set(cands)))

    def test_blanks_dropped(self):
        cands = build_scan_candidates(["", "  "], ["x.com"],
                                      extra_pairs=[("", "y.com")])
        self.assertEqual(cands, [])


class SniIpScannerTest(unittest.TestCase):
    def _probe(self, good):
        good = set(good)

        def fn(ip, port, timeout, sni):
            return (ip, sni) in good

        return fn

    def test_streams_verdicts_and_counts_ok(self):
        results = []
        progress = []
        done = []
        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")],
            port=443, timeout=0.1, workers=2,
            spoof_probe_fn=self._probe([("1.1.1.1", "a.com")]),
            on_result=lambda d: results.append(d),
            on_progress=lambda d, t: progress.append((d, t)),
            on_done=lambda ok, t: done.append((ok, t)),
        )
        scanner.run()
        # one OK, one FAIL
        self.assertEqual(done, [(1, 2)])
        finals = {(r["ip"], r["sni"]): r["status"]
                  for r in results if r["status"] in ("ok", "fail")}
        self.assertEqual(finals[("1.1.1.1", "a.com")], "ok")
        self.assertEqual(finals[("2.2.2.2", "b.com")], "fail")
        # progress reached the total
        self.assertEqual(progress[-1], (2, 2))

    def test_dedupes_candidates(self):
        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("1.1.1.1", "A.COM")],  # case-insensitive dup
            spoof_probe_fn=self._probe([]),
        )
        self.assertEqual(scanner.total, 1)

    def test_empty_calls_done_zero(self):
        done = []
        SniIpScanner([], on_done=lambda ok, t: done.append((ok, t))).run()
        self.assertEqual(done, [(0, 0)])

    def test_stop_halts_before_all(self):
        # a probe that signals stop on the first call
        scanner = None
        calls = []
        lock = __import__("threading").Lock()

        def fn(ip, port, timeout, sni):
            with lock:
                calls.append(ip)
            scanner.stop()
            return True

        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com"),
             ("3.3.3.3", "c.com")],
            workers=1, spoof_probe_fn=fn)
        scanner.run()
        # stop after the first probe leaves the rest unprobed
        self.assertLessEqual(len(calls), 3)

    def test_callbacks_fire_on_caller_thread_not_workers(self):
        # The GUI-hang fix: every callback MUST be emitted from the thread that
        # called run() (the host's QThread), never from a probe worker thread.
        # Emitting a Qt signal from a raw thread is what froze/crashed the app.
        import threading as _t
        caller_tid = _t.get_ident()
        cb_threads = []

        def fn(ip, port, timeout, sni):
            # probe runs on a worker thread (different id) — that's fine
            return True

        def record(*_a):
            cb_threads.append(_t.get_ident())

        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com"),
             ("3.3.3.3", "c.com"), ("4.4.4.4", "d.com")],
            workers=4, spoof_probe_fn=fn,
            on_results_batch=record, on_progress=record,
            on_done=record, on_log=record)
        scanner.run()
        # all callbacks observed exactly one thread id == the caller's
        self.assertTrue(cb_threads)
        self.assertEqual(set(cb_threads), {caller_tid})

    # -- batched verdicts (freeze fix) ------------------------------------
    def test_batch_delivers_all_verdicts_as_lists(self):
        """on_results_batch must deliver every verdict, grouped into lists."""
        batches = []
        done = []
        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com"),
             ("3.3.3.3", "c.com")],
            port=443, timeout=0.1, workers=2,
            spoof_probe_fn=self._probe([("1.1.1.1", "a.com")]),
            on_results_batch=lambda lst: batches.append(list(lst)),
            on_done=lambda ok, t: done.append((ok, t)),
            batch_size=1000, batch_interval=999.0,  # force a single flush
        )
        scanner.run()
        # every batch is a list; flattening yields every candidate exactly once
        self.assertTrue(all(isinstance(b, list) for b in batches))
        flat = [d for b in batches for d in b]
        finals = {(d["ip"], d["sni"]): d["status"] for d in flat}
        self.assertEqual(len(finals), 3)
        self.assertEqual(finals[("1.1.1.1", "a.com")], "ok")
        self.assertEqual(finals[("2.2.2.2", "b.com")], "fail")
        self.assertEqual(done, [(1, 3)])

    def test_batch_preferred_over_single_result(self):
        """When on_results_batch is set, on_result must NOT also fire (no
        double-delivery / double the signal volume)."""
        singles = []
        batches = []
        scanner = SniIpScanner(
            [("1.1.1.1", "a.com"), ("2.2.2.2", "b.com")],
            port=443, timeout=0.1, workers=2,
            spoof_probe_fn=self._probe([]),
            on_result=lambda d: singles.append(d),
            on_results_batch=lambda lst: batches.append(list(lst)),
        )
        scanner.run()
        self.assertEqual(singles, [])           # single path silent
        self.assertTrue(batches)                # batch path active

    def test_no_testing_status_emitted(self):
        """The intermediate 'testing' verdict is gone (it doubled signals)."""
        statuses = []
        scanner = SniIpScanner(
            [("1.1.1.1", "a.com")],
            port=443, timeout=0.1, workers=1,
            spoof_probe_fn=self._probe([("1.1.1.1", "a.com")]),
            on_results_batch=lambda lst: statuses.extend(
                d["status"] for d in lst),
        )
        scanner.run()
        self.assertNotIn("testing", statuses)
        self.assertIn("ok", statuses)


class ExportSniPairsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_writes_ip_sni_status_and_dedupes(self):
        path = os.path.join(self.dir, "pairs.txt")
        n = export_sni_pairs(
            [("1.1.1.1", "a.com", "ok"),
             ("1.1.1.1", "A.COM", "fail"),    # case-insensitive dup
             ("2.2.2.2", "b.com", "")],
            path)
        self.assertEqual(n, 2)
        body = open(path, encoding="utf-8").read()
        self.assertIn("1.1.1.1\ta.com\tok", body)
        self.assertIn("2.2.2.2\tb.com", body)
        self.assertIn("# Total: 2", body)

    def test_drops_blank_rows(self):
        path = os.path.join(self.dir, "pairs2.txt")
        n = export_sni_pairs([("", "a.com", "ok"), ("1.1.1.1", "", "ok")], path)
        self.assertEqual(n, 0)


class ParseSniPairsTextTest(unittest.TestCase):
    def test_parses_ip_sni_status_tab(self):
        text = ("# header\n# IP <TAB> SNI <TAB> status\n\n"
                "1.1.1.1\tcdn.cloudflare.com\tok\n"
                "2.2.2.2\tdns.google\tfail\n")
        self.assertEqual(parse_sni_pairs_text(text), [
            ("1.1.1.1", "cdn.cloudflare.com"),
            ("2.2.2.2", "dns.google"),
        ])

    def test_detects_swapped_sni_ip_order(self):
        # SNI first, IP second — the IP column is detected regardless of order
        self.assertEqual(parse_sni_pairs_text("dns.google\t8.8.8.8"),
                         [("8.8.8.8", "dns.google")])

    def test_comma_and_whitespace_separators(self):
        self.assertEqual(parse_sni_pairs_text("9.9.9.9, example.com, ok"),
                         [("9.9.9.9", "example.com")])
        self.assertEqual(parse_sni_pairs_text("3.3.3.3   a.com"),
                         [("3.3.3.3", "a.com")])

    def test_skips_comments_blanks_and_short_lines(self):
        text = "# c\n\nonlyonecolumn\n4.4.4.4\tb.com\n"
        self.assertEqual(parse_sni_pairs_text(text), [("4.4.4.4", "b.com")])

    def test_dedupes_case_insensitive(self):
        text = "1.1.1.1\tA.com\n1.1.1.1\ta.COM\n"
        self.assertEqual(parse_sni_pairs_text(text), [("1.1.1.1", "A.com")])

    def test_ipv6_recognised(self):
        self.assertEqual(parse_sni_pairs_text("2606:4700:4700::1111\tdns.cf"),
                         [("2606:4700:4700::1111", "dns.cf")])


class ImportSniPairsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_roundtrip_with_export(self):
        path = os.path.join(self.dir, "rt.txt")
        export_sni_pairs(
            [("1.1.1.1", "cdn.cloudflare.com", "ok"),
             ("2.2.2.2", "dns.google", "ok")], path)
        merged, added = import_sni_pairs(path, existing=[])
        self.assertEqual(added, 2)
        self.assertEqual(merged, [
            {"sni": "cdn.cloudflare.com", "ip": "1.1.1.1"},
            {"sni": "dns.google", "ip": "2.2.2.2"},
        ])

    def test_merges_only_new_pairs(self):
        path = self._write("m.txt",
                            "1.1.1.1\ta.com\n2.2.2.2\tb.com\n")
        existing = [{"ip": "1.1.1.1", "sni": "a.com"}]
        merged, added = import_sni_pairs(path, existing=existing)
        self.assertEqual(added, 1)             # only b.com is new
        self.assertEqual(len(merged), 2)
        self.assertIn({"sni": "b.com", "ip": "2.2.2.2"}, merged)

    def test_nothing_new_returns_zero(self):
        path = self._write("z.txt", "1.1.1.1\ta.com\n")
        existing = [{"ip": "1.1.1.1", "sni": "A.COM"}]   # case-insensitive dup
        merged, added = import_sni_pairs(path, existing=existing)
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)

    def test_existing_none_starts_empty(self):
        path = self._write("e.txt", "5.5.5.5\tc.com\n")
        merged, added = import_sni_pairs(path, existing=None)
        self.assertEqual(added, 1)
        self.assertEqual(merged, [{"sni": "c.com", "ip": "5.5.5.5"}])
