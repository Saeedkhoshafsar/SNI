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

from core.pool import (
    PairStats,
    CombinationExplorer,
    ActivePool,
    ConnectionManager,
    build_connection_manager,
    _weighted_sample,
)


def _make_probe(good_ips):
    """Return a probe_fn where only IPs in *good_ips* succeed."""
    good = set(good_ips)

    def probe(ip, port, timeout):
        return ip in good

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


if __name__ == "__main__":
    unittest.main()
