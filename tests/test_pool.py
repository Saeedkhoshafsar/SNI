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


if __name__ == "__main__":
    unittest.main()
