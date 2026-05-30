"""Tests for the remote signed-strategies channel (step 11).

Covers the pure-Python Ed25519 verifier (RFC 8032 vector + round-trip with a
local test-only signer), manifest parsing/validation, canonicalisation, and the
mirror-walking / version / trust policy of :class:`StrategiesUpdater` — all with
an injected in-memory fetcher (no real HTTP).
"""
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ed25519
from core.strategies_remote import (
    Manifest, ManifestError, Recipe, StrategiesUpdater,
    canonical_bytes, verify_manifest)


# --------------------------------------------------------------------------
#  test-only Ed25519 *signer* (RFC 8032) — production code only verifies
# --------------------------------------------------------------------------

def _sign(seed: bytes, msg: bytes) -> tuple[bytes, bytes]:
    """Return (public_key, signature) for *msg* given a 32-byte seed."""
    p, L, B = ed25519._p, ed25519._L, ed25519._B
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    A = ed25519._encode_point(ed25519._scalarmult(B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % L
    R = ed25519._encode_point(ed25519._scalarmult(B, r))
    k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % L
    s = (r + k * a) % L
    sig = R + s.to_bytes(32, "little")
    return A, sig


def _signed_manifest(seed: bytes, version: int, recipes=None):
    """Build a manifest dict + its detached signature over canonical bytes."""
    if recipes is None:
        recipes = [{"strategy": "wrong_seq", "score": 0.6, "enabled": True}]
    data = {"version": version, "updated": "2026-05-29", "recipes": recipes}
    raw = json.dumps(data).encode("utf-8")
    pub, sig = _sign(seed, canonical_bytes(raw))
    return raw, sig, pub


# --------------------------------------------------------------------------
#  Ed25519 verifier
# --------------------------------------------------------------------------

class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vector(self):
        pub = bytes.fromhex(
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
        msg = bytes.fromhex("72")
        sig = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertTrue(ed25519.verify(pub, msg, sig))
        self.assertFalse(ed25519.verify(pub, b"\x73", sig))

    def test_sign_verify_roundtrip(self):
        seed = b"\x01" * 32
        msg = b"hello dpi-bypass"
        pub, sig = _sign(seed, msg)
        self.assertTrue(ed25519.verify(pub, msg, sig))

    def test_tampered_signature_rejected(self):
        seed = b"\x02" * 32
        pub, sig = _sign(seed, b"payload")
        bad = bytearray(sig)
        bad[0] ^= 0x01
        self.assertFalse(ed25519.verify(pub, b"payload", bytes(bad)))

    def test_wrong_key_rejected(self):
        pub_a, sig = _sign(b"\x03" * 32, b"payload")
        pub_b, _ = _sign(b"\x04" * 32, b"payload")
        self.assertFalse(ed25519.verify(pub_b, b"payload", sig))

    def test_malformed_inputs_return_false(self):
        self.assertFalse(ed25519.verify(b"short", b"m", b"x" * 64))
        self.assertFalse(ed25519.verify(b"x" * 32, b"m", b"short"))


# --------------------------------------------------------------------------
#  Recipe / Manifest parsing
# --------------------------------------------------------------------------

class ManifestParseTest(unittest.TestCase):
    def test_recipe_key_composition(self):
        r = Recipe("fake_disorder", fragment_tcp=True, fragment_tls=True, tls_chunk=48)
        self.assertEqual(r.key, "fake_disorder+ftcp+ftls48")
        self.assertEqual(Recipe("wrong_seq").key, "wrong_seq")

    def test_parse_valid_manifest(self):
        raw = json.dumps({
            "version": 3, "updated": "2026-05-29",
            "recipes": [
                {"strategy": "wrong_seq", "score": 0.6},
                {"strategy": "fake_disorder", "fragment_tcp": True, "enabled": False},
            ],
        })
        m = Manifest.parse(raw)
        self.assertEqual(m.version, 3)
        self.assertEqual(len(m.recipes), 2)
        self.assertEqual(len(m.enabled_recipes()), 1)  # fake_disorder disabled

    def test_parse_rejects_non_object(self):
        with self.assertRaises(ManifestError):
            Manifest.parse("[]")

    def test_parse_rejects_bad_version(self):
        with self.assertRaises(ManifestError):
            Manifest.parse(json.dumps({"version": "x", "recipes": [
                {"strategy": "wrong_seq"}]}))

    def test_parse_rejects_empty_recipes(self):
        with self.assertRaises(ManifestError):
            Manifest.parse(json.dumps({"version": 1, "recipes": []}))

    def test_parse_rejects_recipe_without_strategy(self):
        with self.assertRaises(ManifestError):
            Manifest.parse(json.dumps({"version": 1, "recipes": [{"score": 1}]}))

    def test_parse_rejects_bad_tls_chunk(self):
        with self.assertRaises(ManifestError):
            Manifest.parse(json.dumps({"version": 1, "recipes": [
                {"strategy": "wrong_seq", "tls_chunk": 0}]}))

    def test_parse_rejects_invalid_json(self):
        with self.assertRaises(ManifestError):
            Manifest.parse("{not json")

    def test_canonical_bytes_stable_regardless_of_formatting(self):
        a = '{"version":1,"recipes":[{"strategy":"x"}]}'
        b = '{ "recipes":[ {"strategy":"x"} ], "version":1 }'
        self.assertEqual(canonical_bytes(a), canonical_bytes(b))


# --------------------------------------------------------------------------
#  StrategiesUpdater (mirror walk + trust + version policy)
# --------------------------------------------------------------------------

class _MemFetcher:
    """In-memory fetcher: url -> bytes, raising on unknown / 'down' urls."""

    def __init__(self, store: dict[str, bytes], down: set[str] | None = None):
        self.store = store
        self.down = down or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.down or url not in self.store:
            raise OSError(f"unreachable: {url}")
        return self.store[url]


class StrategiesUpdaterTest(unittest.TestCase):
    SEED = b"\x07" * 32

    def _store(self, version=2, recipes=None, url="https://m1/strategies.json"):
        raw, sig, pub = _signed_manifest(self.SEED, version, recipes)
        return {url: raw, url + ".sig": sig}, pub, url

    def test_update_accepts_valid_signed_manifest(self):
        store, pub, url = self._store(version=5)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        self.assertTrue(up.update())
        self.assertEqual(up.current_version, 5)
        self.assertEqual(up.current.recipes[0].strategy, "wrong_seq")

    def test_update_rejects_bad_signature(self):
        store, pub, url = self._store(version=5)
        # corrupt the signature
        store[url + ".sig"] = bytes(64)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        self.assertFalse(up.update())
        self.assertEqual(up.current_version, -1)

    def test_update_rejects_wrong_public_key(self):
        store, _pub, url = self._store(version=5)
        other_pub, _ = _sign(b"\x08" * 32, b"x")
        up = StrategiesUpdater(public_key=other_pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        self.assertFalse(up.update())

    def test_update_falls_through_to_working_mirror(self):
        store, pub, url2 = self._store(version=4, url="https://m2/s.json")
        fetcher = _MemFetcher(store, down={"https://m1/s.json",
                                           "https://m1/s.json.sig"})
        up = StrategiesUpdater(public_key=pub,
                               mirrors=["https://m1/s.json", url2],
                               fetcher=fetcher)
        self.assertTrue(up.update())
        self.assertEqual(up.current_version, 4)
        # it attempted m1 first
        self.assertIn("https://m1/s.json", fetcher.calls)

    def test_update_ignores_older_or_equal_version(self):
        store, pub, url = self._store(version=3)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        self.assertTrue(up.update())          # adopt v3
        # second run with same store: v3 is not strictly newer
        self.assertFalse(up.update())
        self.assertEqual(up.current_version, 3)

    def test_update_adopts_newer_on_second_round(self):
        store_old, pub, url = self._store(version=2)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store_old))
        self.assertTrue(up.update())
        # now the mirror serves a newer signed manifest
        raw, sig, _ = _signed_manifest(self.SEED, 9)
        up.fetcher = _MemFetcher({url: raw, url + ".sig": sig})
        self.assertTrue(up.update())
        self.assertEqual(up.current_version, 9)

    def test_update_rejects_malformed_manifest_but_survives(self):
        url = "https://m1/s.json"
        raw = b'{"version": 1, "recipes": []}'   # empty recipes -> invalid
        _pub, sig = _sign(self.SEED, canonical_bytes(raw))
        up = StrategiesUpdater(public_key=_pub, mirrors=[url],
                               fetcher=_MemFetcher({url: raw, url + ".sig": sig}))
        self.assertFalse(up.update())            # rejected, no crash
        self.assertIsNone(up.current)

    # -- bridge to prober / engine ---------------------------------------

    def test_to_candidates_filters_unknown_strategies(self):
        recipes = [
            {"strategy": "wrong_seq", "score": 0.6},
            {"strategy": "fake_disorder", "fragment_tcp": True, "score": 0.7},
            {"strategy": "does_not_exist", "score": 0.9},
        ]
        store, pub, url = self._store(version=2, recipes=recipes)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        self.assertTrue(up.update())
        cands = up.to_candidates()
        keys = {c.key for c in cands}
        self.assertIn("wrong_seq", keys)
        self.assertIn("fake_disorder+ftcp", keys)
        self.assertNotIn("does_not_exist", keys)   # unknown filtered out

    def test_score_priors_from_manifest(self):
        recipes = [
            {"strategy": "wrong_seq", "score": 0.6},
            {"strategy": "fake_disorder", "fragment_tcp": True, "score": 0.7},
        ]
        store, pub, url = self._store(version=2, recipes=recipes)
        up = StrategiesUpdater(public_key=pub, mirrors=[url],
                               fetcher=_MemFetcher(store))
        up.update()
        priors = up.score_priors()
        self.assertEqual(priors["wrong_seq"], 0.6)
        self.assertEqual(priors["fake_disorder+ftcp"], 0.7)

    def test_to_candidates_empty_when_no_manifest(self):
        up = StrategiesUpdater(public_key=b"\x00" * 32, mirrors=[])
        self.assertEqual(up.to_candidates(), [])
        self.assertEqual(up.score_priors(), {})


# --------------------------------------------------------------------------
#  standalone runner (also works under pytest)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (Ed25519Test, ManifestParseTest, StrategiesUpdaterTest):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
