"""Share-link & subscription parsers → :class:`core.profile.Profile`.

Supports the formats v2rayN / v2rayNG / Shadowrocket emit:

  * ``vless://<uuid>@host:port?<params>#<remark>``
  * ``vmess://<base64-json>``                      (v2rayN JSON schema)
  * ``trojan://<password>@host:port?<params>#<remark>``
  * ``ss://<base64(method:password)>@host:port#<remark>``  (and SIP002 variants)

Plus :func:`parse_subscription` which accepts a (possibly base64-wrapped)
newline-separated blob of links and returns a list of profiles, skipping any
line it can't understand.

The parsers are forgiving: missing padding on base64, URL-encoded values,
``security=reality`` extras, and both ``type=`` / ``net=`` spellings are all
handled. Anything unrecognised raises :class:`ShareLinkError`.
"""
from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import parse_qs, unquote, urlparse

from core.profile import Profile


class ShareLinkError(ValueError):
    """Raised when a share link cannot be parsed."""


# ---------------------------------------------------------------------------
#  base64 helpers (tolerant of missing padding & urlsafe alphabet)
# ---------------------------------------------------------------------------

def _b64decode(s: str) -> bytes:
    s = s.strip().replace("\n", "").replace("\r", "")
    # try urlsafe then standard, each with padding fixups
    for alt in (s, s.replace("-", "+").replace("_", "/")):
        pad = "=" * (-len(alt) % 4)
        try:
            return base64.b64decode(alt + pad)
        except (binascii.Error, ValueError):
            continue
    raise ShareLinkError("base64 نامعتبر")


def _b64decode_str(s: str) -> str:
    return _b64decode(s).decode("utf-8", errors="replace")


def _first(qs: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if k in qs and qs[k]:
            return unquote(qs[k][0])
    return default


# ---------------------------------------------------------------------------
#  Per-protocol parsers
# ---------------------------------------------------------------------------

def parse_vless(link: str) -> Profile:
    u = urlparse(link)
    if u.scheme != "vless":
        raise ShareLinkError("not a vless link")
    qs = parse_qs(u.query)
    p = Profile(
        protocol="vless",
        uuid=unquote(u.username or ""),
        address=u.hostname or "",
        port=u.port or 443,
        remark=unquote(u.fragment) if u.fragment else "",
        transport=_first(qs, "type", "net", default="tcp"),
        security=_first(qs, "security", default="none"),
        sni=_first(qs, "sni", "peer"),
        host=_first(qs, "host"),
        path=_first(qs, "path", default="/"),
        service_name=_first(qs, "serviceName"),
        header_type=_first(qs, "headerType", default="none"),
        flow=_first(qs, "flow"),
        alpn=_first(qs, "alpn"),
        fingerprint=_first(qs, "fp"),
        public_key=_first(qs, "pbk"),
        short_id=_first(qs, "sid"),
        spider_x=_first(qs, "spx"),
        allow_insecure=_first(qs, "allowInsecure", default="0") in ("1", "true"),
        raw=link,
    )
    return p


def parse_trojan(link: str) -> Profile:
    u = urlparse(link)
    if u.scheme != "trojan":
        raise ShareLinkError("not a trojan link")
    qs = parse_qs(u.query)
    return Profile(
        protocol="trojan",
        password=unquote(u.username or ""),
        address=u.hostname or "",
        port=u.port or 443,
        remark=unquote(u.fragment) if u.fragment else "",
        transport=_first(qs, "type", "net", default="tcp"),
        security=_first(qs, "security", default="tls"),
        sni=_first(qs, "sni", "peer"),
        host=_first(qs, "host"),
        path=_first(qs, "path", default="/"),
        service_name=_first(qs, "serviceName"),
        alpn=_first(qs, "alpn"),
        fingerprint=_first(qs, "fp"),
        allow_insecure=_first(qs, "allowInsecure", default="0") in ("1", "true"),
        raw=link,
    )


def parse_vmess(link: str) -> Profile:
    if not link.startswith("vmess://"):
        raise ShareLinkError("not a vmess link")
    body = link[len("vmess://"):]
    try:
        data = json.loads(_b64decode_str(body))
    except json.JSONDecodeError as exc:
        raise ShareLinkError(f"vmess JSON نامعتبر: {exc}") from exc

    def g(*keys, default=""):
        for k in keys:
            if k in data and data[k] not in ("", None):
                return data[k]
        return default

    net = str(g("net", default="tcp"))
    tls = str(g("tls", default=""))
    return Profile(
        protocol="vmess",
        uuid=str(g("id")),
        address=str(g("add")),
        port=int(g("port", default=443) or 443),
        alter_id=int(g("aid", default=0) or 0),
        remark=str(g("ps", "remark")),
        transport=net,
        security="tls" if tls in ("tls", "1", "true") else "none",
        sni=str(g("sni", "peer")),
        host=str(g("host")),
        path=str(g("path", default="/")),
        header_type=str(g("type", default="none")),
        alpn=str(g("alpn")),
        fingerprint=str(g("fp")),
        raw=link,
    )


def parse_shadowsocks(link: str) -> Profile:
    if not link.startswith("ss://"):
        raise ShareLinkError("not an ss link")
    body = link[len("ss://"):]
    remark = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        remark = unquote(frag)
    # strip plugin/query if present
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)

    method = password = host = ""
    port = 443
    if "@" in body:
        # SIP002:  base64(method:password)@host:port   OR   method:password@host:port
        userinfo, hostpart = body.rsplit("@", 1)
        if ":" in userinfo and not _looks_base64(userinfo):
            method, password = userinfo.split(":", 1)
        else:
            dec = _b64decode_str(userinfo)
            method, _, password = dec.partition(":")
        host, port = _split_hostport(hostpart)
    else:
        # legacy:  base64(method:password@host:port)
        dec = _b64decode_str(body)
        creds, _, hostpart = dec.rpartition("@")
        method, _, password = creds.partition(":")
        host, port = _split_hostport(hostpart)

    return Profile(
        protocol="shadowsocks",
        method=method,
        password=password,
        address=host,
        port=port,
        remark=remark,
        transport="tcp",
        security="none",
        raw=link,
    )


def _looks_base64(s: str) -> bool:
    """Heuristic: SIP002 plain method:password contains a known cipher word."""
    return not any(c in s for c in ("-", "_")) and (
        s.count(":") == 1 and any(
            m in s for m in ("aes-", "chacha20", "rc4", "salsa", "2022-blake3")))


def _split_hostport(hp: str) -> tuple[str, int]:
    hp = hp.strip("/")
    if hp.startswith("["):  # IPv6 literal
        host, _, rest = hp[1:].partition("]")
        port = int(rest.lstrip(":") or 443)
        return host, port
    host, _, port_s = hp.rpartition(":")
    if not host:  # no port present
        return port_s, 443
    return host, int(port_s or 443)


# ---------------------------------------------------------------------------
#  Dispatcher + subscription
# ---------------------------------------------------------------------------

_PARSERS = {
    "vless": parse_vless,
    "vmess": parse_vmess,
    "trojan": parse_trojan,
    "ss": parse_shadowsocks,
}


def parse_link(link: str) -> Profile:
    """Parse a single share link of any supported scheme."""
    link = link.strip()
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""
    parser = _PARSERS.get(scheme)
    if parser is None:
        raise ShareLinkError(f"اسکیمای پشتیبانی‌نشده: {scheme or '?'}")
    return parser(link)


def parse_subscription(blob: str) -> list[Profile]:
    """Parse a subscription body into a list of profiles.

    The body may be base64-wrapped (the common case) or plain newline text.
    Lines that fail to parse are skipped rather than aborting the whole list.
    """
    text = blob.strip()
    # Try to base64-decode the whole blob first (typical sub format)
    if "://" not in text:
        try:
            text = _b64decode_str(text)
        except ShareLinkError:
            pass

    profiles: list[Profile] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "://" not in line:
            continue
        try:
            profiles.append(parse_link(line))
        except ShareLinkError:
            continue
    return profiles
