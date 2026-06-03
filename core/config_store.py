"""Persistent application state — settings + saved profiles.

A thin, dependency-free layer over two JSON files that live next to the exe
(or in the project root during development, via :func:`get_runtime_dir`):

  * ``config.json``   — connection settings (mode, ports, fake SNI, …)
  * ``profiles.json`` — the list of imported :class:`~core.profile.Profile`
    objects plus which one is currently selected.

The UI never touches the filesystem directly; it goes through a single
:class:`ConfigStore` instance so loading/saving is centralised and easy to
test. All methods degrade gracefully: a missing or corrupt file falls back to
sane defaults rather than raising.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from core.binary_utils import get_runtime_dir
from core.profile import Profile


# Default connection settings — mirror the legacy ``config.json`` so existing
# behaviour is preserved when no file is present yet.
DEFAULT_CONFIG: dict[str, Any] = {
    "connection_mode": "Tunnel",
    "LISTEN_HOST": "127.0.0.1",
    "LISTEN_PORT": 40443,
    "CONNECT_IP": "104.19.229.21",
    "CONNECT_PORT": 443,
    "FAKE_SNI": "www.hcaptcha.com",
    # Multi-IP / Multi-SNI pool (ported from SNISPF-HJ). When these lists hold
    # more than one entry their cartesian product becomes a self-healing pool of
    # (IP, SNI) pairs (see core.pool). Empty lists fall back transparently to the
    # singular CONNECT_IP / FAKE_SNI above, so the single-target path is never
    # broken — the pool simply stays disabled when there is only one pair.
    "CONNECT_IPS": [],            # list[str] of upstream IPs; [] ⇒ use CONNECT_IP
    "FAKE_SNIS": [],              # list[str] of fake SNIs; [] ⇒ use FAKE_SNI
    # Background route optimiser (redesign). When True the pool runs *only* as a
    # background optimiser: we always connect first with the known-working single
    # route (CONNECT_IP / FAKE_SNI, or the saved per-config best), and the pool
    # probes in the background — swapping in a strictly-better route losslessly
    # (new connections only). When False the single fixed route is used and NO
    # testing happens at all (the user is happy with the SNI they already found).
    "POOL_OPTIMIZE_ENABLED": True,
    # Background route AUTO-SWAP. The user's decision (see issue: "به جای
    # جایگزینی بهترین مسیر بنظرم جایگزین نکن فقط به لیست جفت sni/ip ها اضافه کن"):
    # the optimiser may keep *exploring* in the background (so the Pool page shows
    # live health), but it must NOT silently replace the live route. Good pairs
    # are surfaced for the user to add to ``sni_ip_pairs`` via the manual scan
    # ("شروع تست") instead. Default OFF so the live route is never swapped without
    # the user's say-so. Flip to True to restore the old auto-promote behaviour.
    "POOL_AUTO_SWAP": False,
    # Per-config best (IP, SNI) results found by the optimiser. Keyed by a stable
    # config-identity string (see ConfigStore.config_identity). Each value is
    # {"ip": str, "sni": str, "loss": float, "ts": float}. On the next Start with
    # the same config this is loaded as the DEFAULT route so we never search from
    # scratch again — the best result of the last connection becomes the default.
    "POOL_BEST_RESULTS": {},
    # Background health-check loop knobs (only active when the pool is enabled).
    "HEALTH_CHECK_INTERVAL": 30,  # seconds between health-check cycles (+ jitter)
    "HEALTH_CHECK_TIMEOUT": 3,    # per-probe TCP connect timeout (seconds)
    "PROBE_COUNT": 5,             # TCP probes per pair per cycle
    "ACTIVE_SLOTS": 3,            # warm (IP, SNI) pairs kept ready to serve
    "LOSS_THRESHOLD": 0.20,       # combined-loss above this ⇒ pair is drained
    "DEAD_THRESHOLD": 0.80,       # probe-loss above this ⇒ pair marked dead
    # SNI ↔ connect-IP pairs (issue #3): when the user picks a fake SNI that has
    # a saved pairing, the matching connect IP auto-fills. Seeded with a couple
    # of known-good Cloudflare front IPs so it works out of the box.
    "sni_ip_pairs": [
        {"sni": "www.hcaptcha.com", "ip": "104.19.229.21"},
        {"sni": "www.speedtest.net", "ip": "104.19.229.21"},
    ],
    "socks_port": 10808,
    "http_port": 10809,
    "allow_lan": False,           # bind socks/http on 0.0.0.0 so LAN devices (phone) can use it
    "system_proxy": True,         # set the Windows OS-wide proxy → local HTTP port on start (ON by default)
    "self_test": True,            # after Start, probe xray→spoofer→CDN via the local HTTP port
    "bypass_method": "wrong_seq",
    "force_spoof": False,         # route even ordinary (routable) configs through the SNI spoofer (issue #1)
    "gaming_mode": False,
    "verbose_conn_log": False,    # log every per-connection lifecycle line (#5: off = readable log)
    "auto_prober": False,
    "probe_timeout": 5.0,         # per-candidate probe timeout (seconds)
    # ping / latency measurement (core.ping) — done *before* connecting
    "ping_samples": 3,            # latency samples per server
    "ping_timeout": 3.0,          # per-sample TCP timeout (seconds)
    "ping_measure_download": True,  # also estimate download quality per server
    "ping_strategy": "",          # pinned strategy for strategy-ping ("" = test all)
    # fragmentation layer (core.fragment) — independent of the inject method
    "fragment_tcp": False,        # split the real ClientHello across TCP segments
    "fragment_tls": False,        # rewrite the ClientHello as smaller TLS records
    "fragment_tls_chunk": 64,     # bytes per TLS record when fragment_tls is on
    # resilience layer (core.resilience) — survive active censorship
    "resilience": True,           # detect forged RSTs / throttling and rotate
    "rst_budget": 3,              # forged RSTs to ignore before rotating strategy
    "throttle_ratio": 0.4,        # recent_rate < ratio*baseline ⇒ throttled
    # remote signed strategies.json (core.strategies_remote) — anti-dictation
    "remote_strategies": False,   # fetch + verify a signed manifest on Start
    "strategies_mirrors": [],     # ordered mirror URLs serving strategies.json
    "theme": "dark",
}


class ConfigStore:
    """Load / save settings and profiles as JSON next to the executable."""

    def __init__(self, runtime_dir: str | None = None):
        self.runtime_dir = runtime_dir or get_runtime_dir()
        self.config_path = os.path.join(self.runtime_dir, "config.json")
        self.profiles_path = os.path.join(self.runtime_dir, "profiles.json")

        # deep-copy so each store gets its OWN copy of the mutable defaults
        # (lists/dicts like POOL_BEST_RESULTS) — a shallow dict() would share
        # those nested objects across every instance and leak state.
        self.config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.profiles: list[Profile] = []
        self.selected_index: int = -1

        self.load()

    # ------------------------------------------------------------------ config

    def load(self) -> None:
        """Load both config and profiles, tolerating missing/corrupt files."""
        self._load_config()
        self._load_profiles()

    def _load_config(self) -> None:
        data = _read_json(self.config_path)
        if isinstance(data, dict):
            # merge over defaults so new keys always exist (deep-copy defaults so
            # mutable nested values are never shared across instances)
            merged = copy.deepcopy(DEFAULT_CONFIG)
            merged.update(data)
            self.config = merged
        else:
            self.config = copy.deepcopy(DEFAULT_CONFIG)

    def save_config(self) -> None:
        _write_json(self.config_path, self.config)

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.config[key] = value

    def update(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    # ------------------------------------------------------------ pool helpers

    def connect_ips(self) -> list[str]:
        """Effective upstream IP list.

        Prefers the plural ``CONNECT_IPS`` list; when it is empty (or absent)
        falls back to the singular ``CONNECT_IP`` so the legacy single-target
        config keeps working unchanged. Blank entries are dropped and order is
        preserved with duplicates removed.
        """
        ips = self.config.get("CONNECT_IPS") or []
        if not isinstance(ips, list):
            ips = []
        cleaned = [str(ip).strip() for ip in ips if str(ip).strip()]
        if not cleaned:
            single = str(self.config.get("CONNECT_IP", "")).strip()
            if single:
                cleaned = [single]
        return _dedupe(cleaned)

    def fake_snis(self) -> list[str]:
        """Effective fake-SNI list (plural ``FAKE_SNIS`` ⇒ singular fallback)."""
        snis = self.config.get("FAKE_SNIS") or []
        if not isinstance(snis, list):
            snis = []
        cleaned = [str(s).strip() for s in snis if str(s).strip()]
        if not cleaned:
            single = str(self.config.get("FAKE_SNI", "")).strip()
            if single:
                cleaned = [single]
        return _dedupe(cleaned)

    def pool_enabled(self) -> bool:
        """True only when the resolved lists yield more than one (IP, SNI) pair.

        A single pair gains nothing from the pool (and would needlessly spawn a
        background thread), so the pool stays disabled — exactly mirroring the
        reference ``build_connection_manager`` returning ``None``.
        """
        return len(self.connect_ips()) * len(self.fake_snis()) > 1

    def pool_optimize_enabled(self) -> bool:
        """True when the user has opted into background route-optimisation.

        This is the redesign checkbox: when ``False`` the engine uses the single
        fixed route (the saved per-config best, or CONNECT_IP / FAKE_SNI) and
        spawns **no** background testing at all — for users happy with the SNI
        they already found. When ``True`` the pool runs as a background optimiser
        that only swaps in strictly-better routes, losslessly.
        """
        return bool(self.config.get("POOL_OPTIMIZE_ENABLED", True))

    def config_identity(self) -> str:
        """A stable key identifying the *current* config for best-result storage.

        Best results are saved per-config so re-selecting the same config loads
        its previously-found best route as the default. We key on the fake-SNI ↔
        IP pairing universe (the part that actually determines which routes are
        valid) plus the connect port, which is stable across restarts and does
        not change just because the optimiser swapped the live route.
        """
        ips = ",".join(sorted(self.connect_ips()))
        snis = ",".join(sorted(self.fake_snis()))
        port = self.config.get("CONNECT_PORT", 443)
        return f"{ips}|{snis}|{port}"

    def best_result_for(self, identity: str | None = None) -> dict | None:
        """Return the saved best ``{"ip","sni","loss","ts"}`` for a config, or None."""
        key = identity if identity is not None else self.config_identity()
        store = self.config.get("POOL_BEST_RESULTS") or {}
        if not isinstance(store, dict):
            return None
        rec = store.get(key)
        if isinstance(rec, dict) and rec.get("ip") and rec.get("sni"):
            return rec
        return None

    def save_best_result(self, ip: str, sni: str, loss: float = 0.0,
                         identity: str | None = None, ts: float | None = None,
                         *, persist: bool = True) -> None:
        """Persist the best (IP, SNI) found for a config so it becomes the default.

        Only overwrites an existing record when the new ``loss`` is not worse
        (``<=``) than the stored one, so a transient bad measurement never erases
        a genuinely good saved route.
        """
        import time as _time
        ip = str(ip).strip()
        sni = str(sni).strip()
        if not ip or not sni:
            return
        key = identity if identity is not None else self.config_identity()
        store = self.config.get("POOL_BEST_RESULTS")
        if not isinstance(store, dict):
            store = {}
        prev = store.get(key)
        if isinstance(prev, dict) and isinstance(prev.get("loss"), (int, float)):
            if float(loss) > float(prev["loss"]) and (prev.get("ip") == ip
                                                      and prev.get("sni") == sni):
                # same pair, but measured worse now — keep the better stored loss
                loss = float(prev["loss"])
        store[key] = {
            "ip": ip,
            "sni": sni,
            "loss": float(loss),
            "ts": float(ts) if ts is not None else _time.time(),
        }
        self.config["POOL_BEST_RESULTS"] = store
        if persist:
            try:
                self.save_config()
            except Exception:
                pass

    # ---------------------------------------------------------------- profiles

    def _load_profiles(self) -> None:
        data = _read_json(self.profiles_path)
        profiles: list[Profile] = []
        selected = -1
        if isinstance(data, dict):
            for d in data.get("profiles", []):
                if isinstance(d, dict):
                    try:
                        profiles.append(Profile.from_dict(d))
                    except Exception:
                        continue
            selected = int(data.get("selected_index", -1))
        elif isinstance(data, list):  # tolerate a bare list
            for d in data:
                if isinstance(d, dict):
                    try:
                        profiles.append(Profile.from_dict(d))
                    except Exception:
                        continue
        self.profiles = profiles
        if profiles:
            self.selected_index = selected if 0 <= selected < len(profiles) else 0
        else:
            self.selected_index = -1

    def save_profiles(self) -> None:
        _write_json(self.profiles_path, {
            "selected_index": self.selected_index,
            "profiles": [p.to_dict() for p in self.profiles],
        })

    # -- mutation helpers (each persists immediately) ---------------------

    def add_profile(self, profile: Profile, *, select: bool = False) -> int:
        """Append a profile. Returns its index.

        By default a freshly-added profile does **not** steal the active
        selection (#1): if a server is already active it stays active, so
        adding new configs never silently switches the engine target. The
        very first profile (when nothing is selected yet) is auto-selected so
        the app is never left with profiles but no active one.
        """
        self.profiles.append(profile)
        idx = len(self.profiles) - 1
        if select or self.selected_index < 0:
            self.selected_index = idx
        self.save_profiles()
        return idx

    def add_profiles(self, profiles: list[Profile]) -> int:
        """Append several profiles. Returns how many were added.

        Like :meth:`add_profile`, the active selection is preserved (#1) —
        only when nothing is selected yet does the first new profile become
        active, so the user's currently-running server is never replaced by a
        bulk import.
        """
        if not profiles:
            return 0
        first_new = len(self.profiles)
        self.profiles.extend(profiles)
        if self.selected_index < 0:
            self.selected_index = first_new
        self.save_profiles()
        return len(profiles)

    def remove_profile(self, index: int) -> None:
        if not (0 <= index < len(self.profiles)):
            return
        self.profiles.pop(index)
        if not self.profiles:
            self.selected_index = -1
        elif self.selected_index >= len(self.profiles):
            self.selected_index = len(self.profiles) - 1
        elif index < self.selected_index:
            self.selected_index -= 1
        self.save_profiles()

    def remove_profiles(self, indexes) -> int:
        """Delete several profiles at once by index (#7 bulk delete).

        Accepts any iterable of indexes (duplicates / out-of-range entries are
        ignored). The active selection is kept pointing at the *same* profile
        when possible; if the active profile itself is deleted, the selection
        clamps to a sane neighbour (or -1 when the list becomes empty). The
        store is persisted once, not once per removal. Returns the number of
        profiles actually removed.
        """
        valid = sorted(
            {i for i in indexes if 0 <= i < len(self.profiles)})
        if not valid:
            return 0
        # remember which underlying profile object was active so we can find it
        # again after the list is rebuilt (its index will shift).
        active_obj = (self.profiles[self.selected_index]
                      if 0 <= self.selected_index < len(self.profiles)
                      else None)
        active_removed = self.selected_index in valid
        # remove from the back so earlier indexes stay valid while popping
        for i in reversed(valid):
            self.profiles.pop(i)
        if not self.profiles:
            self.selected_index = -1
        elif active_removed or active_obj is None:
            # the previously-active profile is gone — clamp to a valid neighbour
            self.selected_index = min(max(valid[0] - 1, 0),
                                      len(self.profiles) - 1)
        else:
            # keep the same active profile selected at its new index
            try:
                self.selected_index = self.profiles.index(active_obj)
            except ValueError:
                self.selected_index = min(self.selected_index,
                                          len(self.profiles) - 1)
        self.save_profiles()
        return len(valid)

    def select(self, index: int) -> None:
        if 0 <= index < len(self.profiles):
            self.selected_index = index
            self.save_profiles()

    @property
    def selected_profile(self) -> Profile | None:
        if 0 <= self.selected_index < len(self.profiles):
            return self.profiles[self.selected_index]
        return None

    def clear_profiles(self) -> None:
        self.profiles.clear()
        self.selected_index = -1
        self.save_profiles()


# ---------------------------------------------------------------------------
#  tiny JSON helpers (fail-soft)
# ---------------------------------------------------------------------------

def _dedupe(items: list[str]) -> list[str]:
    """Return *items* with duplicates removed, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, ensure_ascii=False)
    except OSError:
        pass
