"""Unified proxy-profile model.

A single :class:`Profile` dataclass represents *any* supported outbound —
VLESS / VMess / Trojan / Shadowsocks — across any transport (tcp / ws / grpc /
http / quic) and security (none / tls / reality). Share-link parsers
(:mod:`core.share_link`) and the subscription fetcher decode external formats
into this model, and :class:`core.xray_manager.XrayManager` consumes it to
build an Xray outbound. Keeping one model in the middle means the rest of the
app never has to care which wire format a server came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# Supported enumerations (kept as plain strings for JSON friendliness)
PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks")
TRANSPORTS = ("tcp", "ws", "grpc", "http", "h2", "quic", "kcp",
              "xhttp", "splithttp", "httpupgrade")
SECURITIES = ("none", "tls", "reality", "xtls")


def _is_local_addr(addr: str) -> bool:
    """True when *addr* is a loopback / unspecified placeholder, not routable.

    Covers the values that show up in "webtun"/CDN-helper share links where the
    host slot is a stand-in for a local helper port: ``127.0.0.1``,
    ``localhost``, ``0.0.0.0``, ``::1`` and the unspecified ``::``.
    """
    a = (addr or "").strip().strip("[]").lower()
    if not a:
        return True
    if a in ("localhost", "0.0.0.0", "::", "::1"):
        return True
    if a.startswith("127."):
        return True
    return False


@dataclass
class Profile:
    """A single, transport-agnostic outbound definition."""

    # --- identity / wire ---
    protocol: str = "vless"          # one of PROTOCOLS
    address: str = ""                # server host / IP
    port: int = 443
    remark: str = ""                 # human label (link fragment)

    # --- credentials ---
    uuid: str = ""                   # vless/vmess user id
    password: str = ""               # trojan / shadowsocks password
    method: str = ""                 # shadowsocks cipher (e.g. aes-256-gcm)
    alter_id: int = 0                # vmess aid (legacy)
    flow: str = ""                   # vless flow (e.g. xtls-rprx-vision)

    # --- transport ---
    transport: str = "tcp"           # one of TRANSPORTS
    host: str = ""                   # ws/h2 Host header
    path: str = "/"                  # ws/h2 path  OR  grpc serviceName
    service_name: str = ""           # grpc serviceName (if distinct)
    header_type: str = "none"        # tcp header type (e.g. http)
    mode: str = ""                   # xhttp/splithttp mode (auto/packet-up/stream-up/stream-one)

    # --- security / TLS ---
    security: str = "none"           # one of SECURITIES
    sni: str = ""                    # TLS server name
    alpn: str = ""                   # comma list, e.g. "h2,http/1.1"
    fingerprint: str = ""            # uTLS fp (chrome/firefox/…)
    allow_insecure: bool = False

    # --- reality extras ---
    public_key: str = ""             # reality pbk
    short_id: str = ""               # reality sid
    spider_x: str = ""               # reality spx

    # --- bookkeeping ---
    raw: str = ""                    # original share link (for debugging)
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def __post_init__(self):
        if self.protocol == "vmess" and self.protocol not in PROTOCOLS:
            pass
        # normalise empties
        if not self.host:
            self.host = self.sni or self.address
        if self.transport == "grpc" and not self.service_name:
            self.service_name = self.path.strip("/")

    # ------------------------------------------------------------------
    @property
    def is_tls(self) -> bool:
        return self.security in ("tls", "reality", "xtls")

    @property
    def is_webtun(self) -> bool:
        """True for "webtun"/CDN configs whose host slot is a local placeholder.

        These share links (e.g. ``vless://...@127.0.0.1:40443?...host=foo.workers.dev``)
        are *designed* to talk to a local helper on the placeholder port; that
        helper forwards the connection to the real CDN endpoint carried in the
        SNI / Host header (Cloudflare ``workers.dev`` / ``pages.dev``). This is
        exactly the V2RayTun internal-helper pattern. Our app fills the helper
        role with its own SNI-spoofer running on the placeholder port, so we
        must (a) recognise these configs and (b) know the real upstream the
        spoofer should dial.
        """
        if not _is_local_addr(self.address):
            return False
        for cand in (self.sni, self.host):
            if (cand or "").strip() and not _is_local_addr(cand):
                return True
        return False

    @property
    def upstream_address(self) -> str:
        """The real, routable host the *spoofer* must forward to.

        For ordinary configs this is just ``address``. For webtun/CDN configs
        the literal address is a loopback placeholder, so we resolve the real
        endpoint from the SNI / Host header instead (the CDN domain).
        """
        if self.is_webtun:
            for cand in (self.sni, self.host):
                cand = (cand or "").strip()
                if cand and not _is_local_addr(cand):
                    return cand
        return (self.address or "").strip()

    @property
    def upstream_port(self) -> int:
        """The real port the *spoofer* must forward to.

        For webtun configs the placeholder port (e.g. ``40443``) is the local
        helper port, not reachable on the CDN. Cloudflare-style WS/XHTTP+TLS
        endpoints are served on the standard HTTPS port, so we forward to
        ``443`` (or ``80`` for a plaintext fallback).
        """
        if self.is_webtun:
            return 443 if self.is_tls else 80
        return self.port

    @property
    def display_name(self) -> str:
        if self.remark:
            return self.remark
        return f"{self.protocol}://{self.address}:{self.port}"

    @property
    def secret(self) -> str:
        """The credential that identifies the user for this protocol."""
        if self.protocol in ("vless", "vmess"):
            return self.uuid
        return self.password

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in d.items() if k in known}
        return cls(**clean)

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        errs: list[str] = []
        if self.protocol not in PROTOCOLS:
            errs.append(f"پروتکل ناشناخته: {self.protocol}")
        if not self.address:
            errs.append("آدرس سرور خالی است")
        if not (0 < self.port < 65536):
            errs.append(f"پورت نامعتبر: {self.port}")
        if self.protocol in ("vless", "vmess") and not self.uuid:
            errs.append("UUID خالی است")
        if self.protocol == "trojan" and not self.password:
            errs.append("رمز Trojan خالی است")
        if self.protocol == "shadowsocks" and not (self.password and self.method):
            errs.append("رمز یا روش Shadowsocks خالی است")
        return errs
