"""Unit tests for :class:`core.config_store.ConfigStore`."""
import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
