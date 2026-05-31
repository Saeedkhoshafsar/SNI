"""Tests for profile → share-link export (issue #2).

Run:  python -m pytest tests/test_share_link_export.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.profile import Profile
from core.share_link import parse_link, profile_to_link, ShareLinkError


def _roundtrip(link: str):
    p = parse_link(link)
    out = profile_to_link(p)
    p2 = parse_link(out)
    return p, p2


def _assert_same(p, p2, fields):
    for f in fields:
        assert getattr(p, f) == getattr(p2, f), (
            f"{f}: {getattr(p, f)!r} != {getattr(p2, f)!r}")


CORE_FIELDS = ("protocol", "address", "port", "transport", "security",
               "sni", "host", "path", "remark")


def test_vless_ws_tls_roundtrip_with_complex_path():
    # the exact shape the user reported: a ws/tls config whose path embeds a
    # full URL with ':', '/', '@' — all must survive URL-encoding round-trip.
    link = (
        "vless://e0f8189f-1ca1-429e-82d8-447d8b356846@104.18.151.71:8443"
        "?encryption=none&security=tls&sni=hammm2.pages.dev&fp=chrome"
        "&type=ws&host=hammm2.pages.dev"
        "&path=%2Fstars%2Fhttp%3A%2F%2FPQ3YjMsJql%3AfCfJXXbDcw%40vps.webtun.xyz%3A2087"
        "#AYYILDIZ")
    p, p2 = _roundtrip(link)
    _assert_same(p, p2, CORE_FIELDS + ("uuid", "fingerprint"))
    assert p2.path == "/stars/http://PQ3YjMsJql:fCfJXXbDcw@vps.webtun.xyz:2087"


def test_vless_reality_roundtrip():
    link = (
        "vless://uid-1234@example.com:443?security=reality&type=tcp"
        "&sni=www.microsoft.com&fp=chrome&pbk=PUBKEYDATA&sid=abcd&spx=%2F"
        "&flow=xtls-rprx-vision#R")
    p, p2 = _roundtrip(link)
    _assert_same(p, p2, CORE_FIELDS + ("uuid", "public_key", "short_id",
                                       "flow", "fingerprint"))


def test_trojan_grpc_roundtrip():
    link = ("trojan://secretpass@cdn.example.com:443?type=grpc&security=tls"
            "&sni=cdn.example.com&serviceName=mygrpc#T")
    p, p2 = _roundtrip(link)
    _assert_same(p, p2, CORE_FIELDS + ("password",))
    assert p2.service_name == "mygrpc"


def test_vmess_roundtrip():
    link = ("vmess://eyJ2IjoiMiIsInBzIjoibXkgdm1lc3MiLCJhZGQiOiIxLjIuMy40Iiwi"
            "cG9ydCI6IjQ0MyIsImlkIjoiYWFhYS1iYmJiIiwiYWlkIjoiMCIsIm5ldCI6Indz"
            "IiwidHlwZSI6Im5vbmUiLCJob3N0IjoiaC5jb20iLCJwYXRoIjoiL3AiLCJ0bHMi"
            "OiJ0bHMiLCJzbmkiOiJoLmNvbSJ9")
    p, p2 = _roundtrip(link)
    _assert_same(p, p2, ("protocol", "address", "port", "transport",
                         "security", "host", "path", "remark", "uuid"))


def test_shadowsocks_roundtrip():
    import base64
    creds = base64.b64encode(b"aes-256-gcm:mypassword").decode()
    link = f"ss://{creds}@1.2.3.4:8388#SS"
    p, p2 = _roundtrip(link)
    _assert_same(p, p2, ("protocol", "address", "port", "remark"))
    assert p2.method == "aes-256-gcm"
    assert p2.password == "mypassword"


def test_unknown_protocol_raises():
    p = Profile(protocol="wireguard", address="1.2.3.4", port=51820)
    with pytest.raises(ShareLinkError):
        profile_to_link(p)


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
