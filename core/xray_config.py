"""Build a complete Xray-core JSON config from a :class:`core.profile.Profile`.

This is the multi-protocol successor to the old hard-coded trojan-only config
inside ``xray_manager``. It produces:

  * inbounds  — a local SOCKS5 + HTTP pair the OS / browser points at
  * outbound  — derived from the Profile (vless / vmess / trojan / shadowsocks),
    with full transport (tcp/ws/grpc/h2) and security (tls/reality) handling
  * routing   — proxy / direct / block tags

Crucially, the outbound's *destination* is overridable: when the SNI-spoofer is
chained in front, the manager points the outbound at the local spoofer port
instead of the real server, while the spoofer carries the real address. The
caller controls this via ``dest_override``.
"""
from __future__ import annotations

from typing import Any

from core.profile import Profile


# ---------------------------------------------------------------------------
#  streamSettings (transport + security) — shared by all protocols
# ---------------------------------------------------------------------------

def _stream_settings(p: Profile, *, gaming: bool = False) -> dict[str, Any]:
    net = p.transport if p.transport in (
        "tcp", "ws", "grpc", "http", "h2", "quic", "kcp",
        "xhttp", "splithttp", "httpupgrade") else "tcp"
    # xray uses "http" for h2
    if net == "h2":
        net = "http"
    # splithttp is the older Xray name for xhttp — normalise to the current one
    if net == "splithttp":
        net = "xhttp"

    stream: dict[str, Any] = {"network": net}

    # --- security layer ---
    if p.security == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName": p.sni,
            "fingerprint": p.fingerprint or "chrome",
            "publicKey": p.public_key,
            "shortId": p.short_id,
            "spiderX": p.spider_x or "/",
        }
    elif p.is_tls:  # tls / xtls
        tls: dict[str, Any] = {
            "serverName": p.sni or p.host or p.address,
            "allowInsecure": bool(p.allow_insecure),
        }
        if p.fingerprint:
            tls["fingerprint"] = p.fingerprint
        if p.alpn:
            tls["alpn"] = [a.strip() for a in p.alpn.split(",") if a.strip()]
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    else:
        stream["security"] = "none"

    # --- transport-specific ---
    if net == "ws":
        stream["wsSettings"] = {
            "path": p.path or "/",
            "headers": {"Host": p.host or p.sni or p.address},
        }
    elif net == "grpc":
        stream["grpcSettings"] = {
            "serviceName": (p.service_name or p.path).strip("/"),
            "multiMode": not gaming,
        }
    elif net == "http":  # h2
        stream["httpSettings"] = {
            "path": p.path or "/",
            "host": [p.host or p.sni or p.address],
        }
    elif net == "xhttp":
        # XHTTP (a.k.a. splithttp) — the modern CDN-friendly HTTP transport.
        xhttp: dict[str, Any] = {
            "host": p.host or p.sni or p.address,
            "path": p.path or "/",
            "mode": p.mode or "auto",
        }
        # Cloudflare-Worker XHTTP needs explicit upload-stream sizing or it
        # drip-feeds (a few KB/min) because xray's defaults don't chunk the
        # POST uploads in a way the Worker accepts. These match the values
        # Hiddify/HaPP/v2rayN emit for the same link and fix the stall.
        # Honour any values carried in the share link's `extra`, else default.
        ex = p.extra if isinstance(p.extra, dict) else {}

        def _num(*keys, default):
            for k in keys:
                v = ex.get(k)
                if v not in (None, "", 0):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
            return default

        xhttp["scMaxConcurrentPosts"] = _num(
            "scMaxConcurrentPosts", "scmaxconcurrentposts", default=10)
        xhttp["scMaxEachPostBytes"] = _num(
            "scMaxEachPostBytes", "scmaxeachpostbytes", default=1000000)
        xhttp["scMinPostsIntervalMs"] = _num(
            "scMinPostsIntervalMs", "scminpostsintervalms", default=30)
        stream["xhttpSettings"] = xhttp
    elif net == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": p.path or "/",
            "host": p.host or p.sni or p.address,
        }
    elif net == "tcp" and p.header_type == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [p.path or "/"],
                            "headers": {"Host": [p.host or p.address]}},
            }
        }

    if gaming:
        stream["sockopt"] = {
            "tcpNoDelay": True,
            "tcpFastOpen": True,
            "tcpKeepAliveInterval": 5,
        }
    return stream


# ---------------------------------------------------------------------------
#  outbound — protocol dispatch
# ---------------------------------------------------------------------------

def build_outbound(p: Profile, *, dest_address: str | None = None,
                   dest_port: int | None = None,
                   gaming: bool = False) -> dict[str, Any]:
    """Build the ``proxy`` outbound from a profile.

    ``dest_address`` / ``dest_port`` override where the outbound actually
    connects (used to route through the local spoofer). The TLS/SNI/transport
    settings still reflect the *real* server, so the upstream sees a correct
    handshake regardless of the physical hop.
    """
    addr = dest_address if dest_address is not None else p.address
    port = dest_port if dest_port is not None else p.port
    stream = _stream_settings(p, gaming=gaming)

    if p.protocol == "vless":
        user: dict[str, Any] = {"id": p.uuid, "encryption": "none"}
        if p.flow:
            user["flow"] = p.flow
        settings = {"vnext": [
            {"address": addr, "port": port, "users": [user]}]}
        proto = "vless"
    elif p.protocol == "vmess":
        settings = {"vnext": [{
            "address": addr, "port": port,
            "users": [{"id": p.uuid, "alterId": p.alter_id,
                       "security": "auto"}],
        }]}
        proto = "vmess"
    elif p.protocol == "trojan":
        settings = {"servers": [
            {"address": addr, "port": port, "password": p.password}]}
        proto = "trojan"
    elif p.protocol == "shadowsocks":
        settings = {"servers": [{
            "address": addr, "port": port,
            "method": p.method, "password": p.password,
        }]}
        proto = "shadowsocks"
    else:
        raise ValueError(f"پروتکل پشتیبانی‌نشده برای outbound: {p.protocol}")

    return {
        "tag": "proxy",
        "protocol": proto,
        "settings": settings,
        "streamSettings": stream,
        "mux": {"enabled": False},
    }


# ---------------------------------------------------------------------------
#  full config
# ---------------------------------------------------------------------------

def build_config(p: Profile, *, socks_port: int = 10808,
                 http_port: int = 10809, dest_address: str | None = None,
                 dest_port: int | None = None, gaming: bool = False,
                 listen: str = "127.0.0.1",
                 loglevel: str = "warning") -> dict[str, Any]:
    """Assemble the full Xray config (inbounds + outbound + routing).

    ``listen`` is the inbound bind address: ``127.0.0.1`` (default, local only)
    or ``0.0.0.0`` to share the proxy with other devices on the LAN (e.g. a
    phone). LAN sharing is opt-in because it exposes the proxy to the network.
    """
    outbound = build_outbound(
        p, dest_address=dest_address, dest_port=dest_port, gaming=gaming)

    return {
        "log": {"loglevel": loglevel},
        "inbounds": [
            {
                "tag": "socks-in",
                "port": socks_port,
                "listen": listen,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled": not gaming,
                    "destOverride": ["http", "tls"],
                },
            },
            {
                "tag": "http-in",
                "port": http_port,
                "listen": listen,
                "protocol": "http",
                "settings": {},
            },
        ],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "ip": ["geoip:private"],
                 "outboundTag": "direct"},
            ],
        },
    }
