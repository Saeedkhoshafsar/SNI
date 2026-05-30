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

    Covers the values that show up in CDN-placeholder share links where the
    host slot is a loopback stand-in: ``127.0.0.1``, ``localhost``,
    ``0.0.0.0``, ``::1`` and the unspecified ``::``.
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

    # Default decoy SNI + Cloudflare anycast IP the spoofer dials when the
    # share link doesn't override them. These match the values the user runs
    # in V2RayTun: the spoofer connects to ``104.19.229.21:443`` and injects a
    # fake ClientHello carrying ``www.hcaptcha.com`` to fool the DPI box.
    SPOOF_DEFAULT_CONNECT_IP = "104.19.229.21"
    SPOOF_DEFAULT_FAKE_SNI = "www.hcaptcha.com"

    @property
    def is_spoof_config(self) -> bool:
        """True for configs whose server address points at *our* SNI spoofer.

        Share links such as
        ``vless://...@127.0.0.1:40443?...sni=foo.workers.dev&type=xhttp`` are
        authored so that the proxy *core* (xray, or V2RayTun) dials a **local**
        loopback port — that port is **our SNI spoofer**, not the real server.

        The architecture (verified against the original SNI-Spoofing project)::

            xray  →  127.0.0.1:40443  (our spoofer: ProxyServer)
                  →  CONNECT_IP:443   (a fixed Cloudflare anycast IP)
                     + injects a fake ClientHello (FAKE_SNI) to beat DPI
                  →  Cloudflare CDN  →  the real Worker (from the real SNI)

        The real TLS handshake (real SNI / Host / path from the link) travels
        end-to-end between xray and Cloudflare; the spoofer never decrypts it —
        it only rewrites the *destination IP* and injects a decoy ClientHello so
        DPI sees ``www.hcaptcha.com`` while Cloudflare's edge still reads the
        real ``workers.dev`` SNI inside the genuine TLS and routes to the Worker.

        So for these configs xray must dial the loopback address **as-is** and
        we must run the spoofer; the previous attempts that made xray dial the
        ``workers.dev`` host directly were wrong (they bypassed the spoofer, so
        DPI saw the real SNI and blocked it — only V2RayTun, which kept the
        spoofer in the path, worked).
        """
        return _is_local_addr(self.address)

    @property
    def spoof_connect_ip(self) -> str:
        """The fixed CDN IP the spoofer dials for a spoof config.

        Taken from the share link's ``extra`` (keys ``connect_ip`` / ``ip``)
        when present, else the default Cloudflare anycast IP. Only meaningful
        for ``is_spoof_config`` profiles.
        """
        if isinstance(self.extra, dict):
            for key in ("connect_ip", "CONNECT_IP", "ip"):
                v = (self.extra.get(key) or "").strip()
                if v:
                    return v
        return self.SPOOF_DEFAULT_CONNECT_IP

    @property
    def spoof_connect_port(self) -> int:
        """The port the spoofer dials on the fixed CDN IP (default 443)."""
        if isinstance(self.extra, dict):
            for key in ("connect_port", "CONNECT_PORT"):
                v = self.extra.get(key)
                if v:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
        return 443 if self.is_tls else 80

    @property
    def spoof_fake_sni(self) -> str:
        """The decoy SNI the spoofer injects (default ``www.hcaptcha.com``)."""
        if isinstance(self.extra, dict):
            for key in ("fake_sni", "FAKE_SNI"):
                v = (self.extra.get(key) or "").strip()
                if v:
                    return v
        return self.SPOOF_DEFAULT_FAKE_SNI

    @property
    def dial_address(self) -> str:
        """The host xray's outbound connects to (the *transport* hop).

        For ordinary configs this is the real ``address``. For spoof configs
        it stays the loopback address **as-is** — xray must dial our spoofer
        (e.g. ``127.0.0.1``), which then forwards to the real CDN. We never
        rewrite it to the ``workers.dev`` host, because the real SNI must reach
        Cloudflare *through* the spoofer, not be dialled directly.
        """
        return (self.address or "").strip()

    @property
    def dial_port(self) -> int:
        """The port xray's outbound connects to (the *transport* hop).

        For spoof configs this is the loopback spoofer port from the link
        (e.g. ``40443``); for ordinary configs it's the real ``port``.
        """
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
