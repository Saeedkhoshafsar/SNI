"""Remote, signed ``strategies.json`` — anti-dictation update channel (step 11).

When a censor blocks a working trick nation-wide, we must be able to ship a new
recipe **without releasing a new build**. The mechanism:

* A tiny JSON manifest (data, *not* code) lists strategy *recipes* — each a
  known inject-strategy key plus fragmentation parameters. Recipes compose the
  primitives already in :mod:`strategies` / :mod:`core.fragment`; they never
  carry executable code, so a poisoned mirror cannot run anything.
* The maintainer signs the canonical manifest bytes with an offline Ed25519
  key. The app embeds the matching *public* key and verifies every fetch
  (:mod:`core.ed25519`). An unsigned / mis-signed / tampered manifest is
  rejected and the previous good set is kept.
* The manifest is fetched from several **mirrors** (GitHub raw, IPFS gateway, a
  host reachable behind our own bypass, …). The first mirror that returns a
  *validly signed* payload with a newer ``version`` wins; the rest are skipped.

This module is pure-data and **network-injectable**: callers pass a ``fetcher``
(``url -> bytes``) so the whole load/verify/merge pipeline is unit-tested in the
sandbox without real HTTP. The Windows/runtime layer wires a real urllib fetcher.

Manifest shape (canonicalised for signing — see :func:`canonical_bytes`)::

    {
      "version": 7,                         # monotonically increasing
      "updated": "2026-05-29",              # informational
      "recipes": [
        {"strategy": "fake_ttl", "fragment_tcp": true,  "fragment_tls": false,
         "tls_chunk": 64, "score": 0.7, "title": "...", "enabled": true},
        ...
      ]
    }

The detached signature is the Ed25519 signature over :func:`canonical_bytes`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from core import ed25519

# A fetcher maps a mirror URL to the raw bytes it serves (or raises).
Fetcher = Callable[[str], bytes]
LogCb = Callable[[str], None]

# Embedded trusted Ed25519 *public* key (32 bytes, hex). The maintainer signs
# strategies.json with the matching offline private key and replaces this value
# at release time. Empty == remote updates are effectively disabled (every
# fetch fails verification), which is the safe default for an unconfigured build.
TRUSTED_PUBLIC_KEY_HEX = ""


def trusted_public_key() -> bytes:
    """Return the embedded trusted public key, or 32 zero bytes if unset."""
    if TRUSTED_PUBLIC_KEY_HEX:
        try:
            return bytes.fromhex(TRUSTED_PUBLIC_KEY_HEX)
        except ValueError:
            return b"\x00" * 32
    return b"\x00" * 32


def urllib_fetcher(timeout: float = 8.0) -> Fetcher:
    """A real HTTP(S) fetcher built on the stdlib (used on the Windows runtime).

    Kept out of the hot import path / unit tests; callers in the sandbox inject
    their own in-memory fetcher instead.
    """
    import urllib.request

    def _fetch(url: str) -> bytes:  # pragma: no cover - needs network
        req = urllib.request.Request(url, headers={"User-Agent": "spoofer/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return _fetch


class ManifestError(Exception):
    """Raised when a manifest is structurally invalid (not on the trust path)."""


@dataclass(frozen=True)
class Recipe:
    """One declarative bypass recipe: a known strategy + fragmentation params."""

    strategy: str
    fragment_tcp: bool = False
    fragment_tls: bool = False
    tls_chunk: int = 64
    score: float = 0.5
    title: str = ""
    enabled: bool = True

    @property
    def key(self) -> str:
        parts = [self.strategy]
        if self.fragment_tcp:
            parts.append("ftcp")
        if self.fragment_tls:
            parts.append(f"ftls{self.tls_chunk}")
        return "+".join(parts)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recipe":
        if not isinstance(d, dict):
            raise ManifestError("recipe must be an object")
        strategy = d.get("strategy")
        if not isinstance(strategy, str) or not strategy:
            raise ManifestError("recipe.strategy must be a non-empty string")
        try:
            tls_chunk = int(d.get("tls_chunk", 64))
        except (TypeError, ValueError):
            raise ManifestError("recipe.tls_chunk must be an integer")
        if tls_chunk <= 0:
            raise ManifestError("recipe.tls_chunk must be positive")
        try:
            score = float(d.get("score", 0.5))
        except (TypeError, ValueError):
            raise ManifestError("recipe.score must be a number")
        return cls(
            strategy=strategy,
            fragment_tcp=bool(d.get("fragment_tcp", False)),
            fragment_tls=bool(d.get("fragment_tls", False)),
            tls_chunk=tls_chunk,
            score=score,
            title=str(d.get("title", "")),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass(frozen=True)
class Manifest:
    """A parsed, structurally-valid strategies manifest."""

    version: int
    recipes: List[Recipe]
    updated: str = ""

    @classmethod
    def parse(cls, raw: bytes | str) -> "Manifest":
        """Parse + structurally validate JSON. Raises :class:`ManifestError`."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ManifestError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestError("manifest must be a JSON object")
        try:
            version = int(data.get("version"))
        except (TypeError, ValueError):
            raise ManifestError("manifest.version must be an integer")
        raw_recipes = data.get("recipes")
        if not isinstance(raw_recipes, list) or not raw_recipes:
            raise ManifestError("manifest.recipes must be a non-empty array")
        recipes = [Recipe.from_dict(r) for r in raw_recipes]
        return cls(version=version, recipes=recipes,
                   updated=str(data.get("updated", "")))

    def enabled_recipes(self) -> List[Recipe]:
        return [r for r in self.recipes if r.enabled]


def canonical_bytes(raw: bytes | str) -> bytes:
    """Canonical byte form of a manifest, used as the *signed message*.

    We re-serialise the parsed JSON with sorted keys and no insignificant
    whitespace so the signer and verifier agree byte-for-byte regardless of how
    the file was formatted on disk or in transit.
    """
    data = json.loads(raw)
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def verify_manifest(raw: bytes | str, signature: bytes,
                    public_key: bytes) -> bool:
    """True iff *signature* is a valid Ed25519 sig over the canonical manifest."""
    try:
        msg = canonical_bytes(raw)
    except (ValueError, TypeError):
        return False
    return ed25519.verify(public_key, msg, signature)


@dataclass
class StrategiesUpdater:
    """Fetch + verify a signed manifest from mirrors, keeping the best version.

    Policy:
    * try mirrors in order; for each, fetch the manifest bytes *and* its
      detached signature (``<url>.sig``), verify against the embedded public
      key, and parse;
    * accept the first mirror whose payload is validly signed **and** carries a
      ``version`` strictly greater than the one we already hold;
    * any fetch/verify/parse failure is logged and skipped — a bad mirror can
      never downgrade or corrupt the active set (fail-closed on trust).
    """

    public_key: bytes
    mirrors: Sequence[str] = field(default_factory=tuple)
    fetcher: Optional[Fetcher] = None
    sig_suffix: str = ".sig"
    on_log: Optional[LogCb] = None
    current: Optional[Manifest] = None

    def _log(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    @property
    def current_version(self) -> int:
        return self.current.version if self.current is not None else -1

    def _load_one(self, url: str) -> Optional[Manifest]:
        if self.fetcher is None:
            raise RuntimeError("no fetcher configured")
        raw = self.fetcher(url)
        sig = self.fetcher(url + self.sig_suffix)
        if not verify_manifest(raw, sig, self.public_key):
            self._log(f"امضای نامعتبر از {url} — رد شد")
            return None
        manifest = Manifest.parse(raw)
        return manifest

    def update(self) -> bool:
        """Walk the mirrors; adopt the first newer, validly-signed manifest.

        Returns True if :pyattr:`current` was updated.
        """
        for url in self.mirrors:
            try:
                candidate = self._load_one(url)
            except ManifestError as exc:
                self._log(f"manifest نامعتبر از {url} ({exc}) — رد شد")
                continue
            except Exception as exc:  # network / fetcher failure
                self._log(f"دریافت از {url} ناموفق ({exc}) — mirror بعدی")
                continue
            if candidate is None:
                continue
            if candidate.version <= self.current_version:
                self._log(
                    f"نسخه‌ی {candidate.version} از {url} جدیدتر نیست "
                    f"(فعلی {self.current_version}) — رد شد")
                continue
            self.current = candidate
            self._log(
                f"✓ strategies.json نسخه‌ی {candidate.version} از {url} "
                f"اعمال شد ({len(candidate.recipes)} رسپی)")
            return True
        return False

    # -- bridge to the prober / engine -----------------------------------
    def to_candidates(self) -> list:
        """Map the active manifest's enabled recipes to prober ``Candidate``s.

        Only recipes whose strategy key exists in the local registry are kept,
        so an unknown/typo'd strategy from a remote file is silently ignored
        rather than crashing the run.
        """
        if self.current is None:
            return []
        from core.prober import Candidate
        from strategies import REGISTRY

        out = []
        for r in self.current.enabled_recipes():
            if r.strategy not in REGISTRY:
                self._log(f"رسپی با استراتژی ناشناخته رد شد: {r.strategy}")
                continue
            out.append(Candidate(
                strategy=r.strategy,
                fragment_tcp=r.fragment_tcp,
                fragment_tls=r.fragment_tls,
                tls_chunk=r.tls_chunk,
            ))
        return out

    def score_priors(self) -> dict:
        """``{candidate.key: score}`` priors from the manifest, for ranking."""
        if self.current is None:
            return {}
        from strategies import REGISTRY
        priors = {}
        for r in self.current.enabled_recipes():
            if r.strategy in REGISTRY:
                priors[r.key] = r.score
        return priors
