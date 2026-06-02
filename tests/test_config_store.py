"""Unit tests for :class:`core.config_store.ConfigStore`."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore, DEFAULT_CONFIG
from core.profile import Profile


def _profile(remark="srv", addr="example.com"):
    return Profile(protocol="vless", address=addr, port=443,
                   uuid="11111111-1111-1111-1111-111111111111", remark=remark)


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ConfigStore(runtime_dir=self.tmp)

    # -- config ---------------------------------------------------------

    def test_defaults_when_no_file(self):
        self.assertEqual(self.store.get("connection_mode"),
                         DEFAULT_CONFIG["connection_mode"])
        self.assertEqual(self.store.get("LISTEN_PORT"), 40443)

    def test_config_roundtrip(self):
        self.store.set("CONNECT_IP", "9.9.9.9")
        self.store.update(FAKE_SNI="x.com", socks_port=1080)
        self.store.save_config()

        fresh = ConfigStore(runtime_dir=self.tmp)
        self.assertEqual(fresh.get("CONNECT_IP"), "9.9.9.9")
        self.assertEqual(fresh.get("FAKE_SNI"), "x.com")
        self.assertEqual(fresh.get("socks_port"), 1080)

    def test_corrupt_config_falls_back(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as fp:
            fp.write("{ not valid json ")
        fresh = ConfigStore(runtime_dir=self.tmp)
        self.assertEqual(fresh.get("connection_mode"),
                         DEFAULT_CONFIG["connection_mode"])

    def test_missing_keys_merged_over_defaults(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as fp:
            json.dump({"CONNECT_IP": "1.1.1.1"}, fp)
        fresh = ConfigStore(runtime_dir=self.tmp)
        self.assertEqual(fresh.get("CONNECT_IP"), "1.1.1.1")
        # a key absent from the file still comes from DEFAULT_CONFIG
        self.assertEqual(fresh.get("socks_port"), DEFAULT_CONFIG["socks_port"])

    # -- multi-IP / multi-SNI pool helpers ------------------------------

    def test_connect_ips_falls_back_to_singular(self):
        # default config has empty CONNECT_IPS → uses CONNECT_IP
        self.assertEqual(self.store.connect_ips(),
                         [DEFAULT_CONFIG["CONNECT_IP"]])

    def test_fake_snis_falls_back_to_singular(self):
        self.assertEqual(self.store.fake_snis(),
                         [DEFAULT_CONFIG["FAKE_SNI"]])

    def test_connect_ips_prefers_plural_list(self):
        self.store.set("CONNECT_IPS", ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(self.store.connect_ips(), ["1.1.1.1", "2.2.2.2"])

    def test_ips_dedupe_and_strip(self):
        self.store.set("CONNECT_IPS", [" 1.1.1.1 ", "1.1.1.1", "", "2.2.2.2"])
        self.assertEqual(self.store.connect_ips(), ["1.1.1.1", "2.2.2.2"])

    def test_pool_disabled_for_single_pair(self):
        self.assertFalse(self.store.pool_enabled())  # 1 IP × 1 SNI

    def test_pool_enabled_for_multi(self):
        self.store.set("CONNECT_IPS", ["1.1.1.1", "2.2.2.2"])
        self.store.set("FAKE_SNIS", ["a.com"])
        self.assertTrue(self.store.pool_enabled())   # 2 × 1 > 1

    def test_pool_keys_roundtrip(self):
        self.store.set("CONNECT_IPS", ["1.1.1.1"])
        self.store.set("FAKE_SNIS", ["a.com", "b.com"])
        self.store.set("ACTIVE_SLOTS", 4)
        self.store.save_config()
        fresh = ConfigStore(runtime_dir=self.tmp)
        self.assertEqual(fresh.get("CONNECT_IPS"), ["1.1.1.1"])
        self.assertEqual(fresh.get("FAKE_SNIS"), ["a.com", "b.com"])
        self.assertEqual(fresh.get("ACTIVE_SLOTS"), 4)

    def test_pool_defaults_present(self):
        for key in ("CONNECT_IPS", "FAKE_SNIS", "HEALTH_CHECK_INTERVAL",
                    "ACTIVE_SLOTS", "LOSS_THRESHOLD", "DEAD_THRESHOLD"):
            self.assertIn(key, DEFAULT_CONFIG)

    # -- profiles -------------------------------------------------------

    def test_add_and_select(self):
        i0 = self.store.add_profile(_profile("a"))
        i1 = self.store.add_profile(_profile("b"), select=False)
        self.assertEqual(i0, 0)
        self.assertEqual(i1, 1)
        # first add auto-selects; second (select=False) keeps selection at 0
        self.assertEqual(self.store.selected_index, 0)
        self.store.select(1)
        self.assertEqual(self.store.selected_profile.remark, "b")

    def test_profiles_persist(self):
        self.store.add_profile(_profile("keep"))
        fresh = ConfigStore(runtime_dir=self.tmp)
        self.assertEqual(len(fresh.profiles), 1)
        self.assertEqual(fresh.selected_profile.remark, "keep")

    def test_remove_adjusts_selection(self):
        self.store.add_profiles([_profile("a"), _profile("b"), _profile("c")])
        self.store.select(2)            # select "c"
        self.store.remove_profile(0)    # drop "a" → selection shifts to 1
        self.assertEqual(self.store.selected_profile.remark, "c")
        self.assertEqual(self.store.selected_index, 1)

    def test_remove_all(self):
        self.store.add_profile(_profile("only"))
        self.store.remove_profile(0)
        self.assertEqual(self.store.selected_index, -1)
        self.assertIsNone(self.store.selected_profile)

    def test_add_profiles_count(self):
        n = self.store.add_profiles([_profile("a"), _profile("b")])
        self.assertEqual(n, 2)
        self.assertEqual(self.store.add_profiles([]), 0)

    # -- bulk delete (#7) ----------------------------------------------

    def test_remove_profiles_bulk(self):
        self.store.add_profiles(
            [_profile("a"), _profile("b"), _profile("c"), _profile("d")])
        removed = self.store.remove_profiles([0, 2])  # drop "a" and "c"
        self.assertEqual(removed, 2)
        self.assertEqual([p.remark for p in self.store.profiles], ["b", "d"])

    def test_remove_profiles_keeps_active_profile(self):
        self.store.add_profiles(
            [_profile("a"), _profile("b"), _profile("c"), _profile("d")])
        self.store.select(3)            # "d" is active
        self.store.remove_profiles([0, 1])  # drop "a","b" → "d" shifts to 1
        self.assertEqual(self.store.selected_profile.remark, "d")
        self.assertEqual(self.store.selected_index, 1)

    def test_remove_profiles_active_removed_clamps(self):
        self.store.add_profiles([_profile("a"), _profile("b"), _profile("c")])
        self.store.select(2)            # "c" is active
        self.store.remove_profiles([2])  # delete the active one
        self.assertEqual(self.store.selected_index, 1)
        self.assertEqual(self.store.selected_profile.remark, "b")

    def test_remove_profiles_ignores_bad_indexes(self):
        self.store.add_profiles([_profile("a"), _profile("b")])
        # out-of-range and duplicate indexes are ignored
        removed = self.store.remove_profiles([5, 5, -1, 0, 0])
        self.assertEqual(removed, 1)
        self.assertEqual([p.remark for p in self.store.profiles], ["b"])

    def test_remove_profiles_empty(self):
        self.store.add_profile(_profile("a"))
        self.assertEqual(self.store.remove_profiles([]), 0)
        self.assertEqual(len(self.store.profiles), 1)

    def test_remove_profiles_all(self):
        self.store.add_profiles([_profile("a"), _profile("b")])
        self.store.remove_profiles([0, 1])
        self.assertEqual(self.store.selected_index, -1)
        self.assertIsNone(self.store.selected_profile)


if __name__ == "__main__":
    unittest.main()
