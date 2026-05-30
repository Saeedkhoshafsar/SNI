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
    "socks_port": 10808,
    "http_port": 10809,
    "allow_lan": False,           # bind socks/http on 0.0.0.0 so LAN devices (phone) can use it
    "system_proxy": True,         # set the Windows OS-wide proxy → local HTTP port on start (ON by default)
    "self_test": True,            # after Start, probe xray→spoofer→CDN via the local HTTP port
    "bypass_method": "wrong_seq",
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

        self.config: dict[str, Any] = dict(DEFAULT_CONFIG)
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
            # merge over defaults so new keys always exist
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            self.config = merged
        else:
            self.config = dict(DEFAULT_CONFIG)

    def save_config(self) -> None:
        _write_json(self.config_path, self.config)

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.config[key] = value

    def update(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

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
