"""Configure and manage an Xray-core subprocess from a unified Profile.

v2rayN-style integration: the caller hands in a :class:`core.profile.Profile`
(parsed from a share link / subscription) and the manager builds the full Xray
config and runs the bundled ``xray.exe``. No hand-entered ports, no separate
config file to wire up.

**Auto-chained SNI spoofing.** When ``spoof_port`` is provided, the outbound is
pointed at ``127.0.0.1:<spoof_port>`` instead of the real server. The SNI
spoofer (``main.ProxyServer``) listens there and forwards — with the DPI-bypass
injection — to the real ``profile.address:profile.port``. The TLS/SNI settings
in the outbound still describe the real server, so the upstream handshake is
correct. The user never sees or types ``127.0.0.1:40443``; the manager wires it
internally.
"""
import json
import os
import socket
import subprocess
import sys
import threading

from core.binary_utils import get_bin_dir, get_runtime_dir
from core.profile import Profile
from core.xray_config import build_config


def find_free_port(preferred: int | None = None) -> int:
    """Return a free localhost TCP port, trying *preferred* first."""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_ip_address() -> str:
    """Best-effort primary LAN IPv4 of this machine (for sharing to a phone).

    Uses the standard UDP-connect trick: no packet is actually sent, but the OS
    picks the outbound interface, whose address is the one a phone on the same
    Wi-Fi would reach. Falls back to ``127.0.0.1`` if it can't be determined.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except OSError:
            pass


def parse_stats_json(text: str) -> tuple[int, int] | None:
    """Parse ``xray api statsquery`` JSON → ``(uplink_bytes, downlink_bytes)``.

    Sums every per-inbound uplink/downlink counter (names look like
    ``inbound>>>socks-in>>>traffic>>>uplink``). Returns ``None`` for empty /
    invalid output so the poller can skip the tick. Pure & side-effect-free so
    it's unit-testable without spawning xray (issue #3).
    """
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None
    up = down = 0
    for stat in (data.get("stat") or []):
        name = str(stat.get("name", ""))
        try:
            val = int(stat.get("value", 0) or 0)
        except (TypeError, ValueError):
            val = 0
        if name.endswith(">>>uplink") and "inbound>>>" in name:
            up += val
        elif name.endswith(">>>downlink") and "inbound>>>" in name:
            down += val
    return up, down


class XrayManager:
    """Runs Xray-core for a given profile, optionally chained behind a spoofer."""

    def __init__(
        self,
        profile: Profile,
        socks_port: int = 10808,
        http_port: int = 10809,
        spoof_port: int | None = None,
        gaming_mode: bool = False,
        listen: str = "127.0.0.1",
        api_port: int | None = None,
    ):
        self.profile = profile
        self.socks_port = socks_port
        self.http_port = http_port
        # if a spoofer sits in front, the outbound connects here instead
        self.spoof_port = spoof_port
        self.gaming_mode = gaming_mode
        # inbound bind address: 0.0.0.0 shares the proxy with LAN devices
        self.listen = listen
        # local StatsService API port for live-usage polling (issue #3); when
        # None the stats API is not enabled.
        self.api_port = api_port

        self.xray_exe = os.path.join(get_bin_dir(), "xray.exe")
        self.config_path = os.path.join(get_runtime_dir(), "xray_config.json")
        self._process: subprocess.Popen | None = None
        self.on_log = None

    # ----------------------------------------------------------------

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    @property
    def is_available(self) -> bool:
        return os.path.isfile(self.xray_exe)

    @property
    def real_server(self) -> tuple[str, int]:
        """The transport hop xray's outbound dials when run *directly*.

        For ordinary configs that's ``profile.address:port``. (Spoof configs
        never run direct — they always chain through the spoofer, which dials
        the real CDN IP itself.)
        """
        return self.profile.dial_address, self.profile.dial_port

    # ----------------------------------------------------------------

    def generate_config(self) -> str:
        """Write the Xray JSON config for this profile and return its path."""
        if self.spoof_port is not None:
            # route the outbound through the local spoofer; the spoofer dials
            # the real CDN IP + injects the decoy ClientHello. The real SNI /
            # host / path still come from the profile (carried in TLS), so the
            # upstream Cloudflare handshake is correct end-to-end.
            dest_address: str | None = "127.0.0.1"
            dest_port: int | None = self.spoof_port
        else:
            # ordinary config, no spoofer — connect straight to the real server
            dest_address = self.profile.dial_address
            dest_port = self.profile.dial_port

        config = build_config(
            self.profile,
            socks_port=self.socks_port,
            http_port=self.http_port,
            dest_address=dest_address,
            dest_port=dest_port,
            gaming=self.gaming_mode,
            listen=self.listen,
            api_port=self.api_port,
        )
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fp:
            json.dump(config, fp, indent=2)
        return self.config_path

    # ----------------------------------------------------------------

    def start(self):
        if self.is_running:
            self._log("Xray already running")
            return
        if not self.is_available:
            self._log("ERROR: xray.exe not found (binary not bundled)")
            return

        errs = self.profile.validate()
        if errs:
            self._log("ERROR: پروفایل نامعتبر — " + "؛ ".join(errs))
            return

        self.generate_config()
        try:
            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            # Tell xray where to find geoip.dat / geosite.dat. Our config uses a
            # ``geoip:private`` routing rule, so when the first private-range
            # packet is routed xray loads geoip.dat — if it can't find it the
            # connection silently fails. xray searches XRAY_LOCATION_ASSET, then
            # its CWD. We point both at bin/ so the bundled .dat files are found
            # regardless of where the .exe (or PyInstaller temp dir) lives.
            bin_dir = get_bin_dir()
            env = dict(os.environ)
            env.setdefault("XRAY_LOCATION_ASSET", bin_dir)
            kwargs["env"] = env
            kwargs["cwd"] = bin_dir

            self._process = subprocess.Popen(
                [self.xray_exe, "run", "-config", self.config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
            threading.Thread(target=self._pump_output, daemon=True).start()

            # xray exits within milliseconds on a bad config / missing asset.
            # Detect that fast crash and surface it, instead of reporting
            # "Xray started" for a process that's already dead (which made the
            # tunnel look up while every request silently failed).
            import time as _time
            _time.sleep(0.4)
            if self._process.poll() is not None:
                code = self._process.returncode
                self._log(
                    f"ERROR: xray بلافاصله بسته شد (کد خروج {code}). "
                    f"لاگ‌های [xray] بالا را برای علت بررسی کنید "
                    f"(کانفیگ نامعتبر یا فایل geoip/geosite غایب).")
                self._process = None
                return
            chain = (f"  (chain → spoofer 127.0.0.1:{self.spoof_port}"
                     f" → {self.profile.address}:{self.profile.port})"
                     if self.spoof_port else
                     f"  (direct → {self.profile.address}:{self.profile.port})")
            host = self.listen if self.listen != "0.0.0.0" else "127.0.0.1"
            self._log(
                f"Xray started [{self.profile.protocol}]  →  "
                f"SOCKS5 {host}:{self.socks_port}"
                f"  |  HTTP {host}:{self.http_port}{chain}")
            if self.listen == "0.0.0.0":
                lan_ip = lan_ip_address()
                self._log(
                    f"اشتراک LAN روشن — از گوشی به این آدرس وصل شوید: "
                    f"SOCKS5 {lan_ip}:{self.socks_port}  |  "
                    f"HTTP {lan_ip}:{self.http_port}")
        except Exception as exc:
            self._log(f"Failed to start Xray: {exc}")

    def _pump_output(self):
        proc = self._process
        if not proc or not proc.stdout:
            return
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._log(f"[xray] {line}")
        except Exception:
            pass

    def query_stats(self) -> tuple[int, int] | None:
        """Return cumulative ``(uplink_bytes, downlink_bytes)`` from xray (#3).

        Sums the per-inbound uplink/downlink counters exposed by xray's
        StatsService via ``xray api statsquery``. Returns ``None`` when the
        stats API isn't enabled, xray isn't running, or the query fails — the
        caller then simply skips that poll tick (fail-soft).

        We deliberately use the bundled ``xray.exe api`` sub-command (a thin
        gRPC client) instead of pulling in a gRPC dependency: it needs nothing
        beyond the binary we already ship.
        """
        if self.api_port is None or not self.is_running or not self.is_available:
            return None
        try:
            kwargs: dict = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            out = subprocess.run(
                [self.xray_exe, "api", "statsquery",
                 f"--server=127.0.0.1:{self.api_port}", ""],
                capture_output=True, timeout=4, **kwargs,
            )
            text = out.stdout.decode("utf-8", "replace") if out.stdout else ""
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        return parse_stats_json(text)

    def stop(self):
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
            self._log("Xray stopped")

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None
