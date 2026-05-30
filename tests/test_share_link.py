"""Unit tests for core.share_link parsers + core.profile model.

Run:  python -m pytest tests/test_share_link.py -q
  or:  python tests/test_share_link.py   (self-running fallback)
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.profile import Profile
from core.share_link import (
    ShareLinkError, parse_link, parse_subscription, parse_vless,
    parse_vmess, parse_trojan, parse_shadowsocks,
)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------------------
#  VLESS
# ---------------------------------------------------------------------------

def test_vless_ws_tls():
    link = ("vless://11111111-2222-3333-4444-555555555555@example.com:443"
            "?type=ws&security=tls&sni=cdn.example.com&host=cdn.example.com"
            "&path=%2Fws&fp=chrome&alpn=h2#MyServer")
    p = parse_vless(link)
    assert p.protocol == "vless"
    assert p.uuid == "11111111-2222-3333-4444-555555555555"
    assert p.address == "example.com" and p.port == 443
    assert p.transport == "ws" and p.security == "tls"
    assert p.sni == "cdn.example.com" and p.host == "cdn.example.com"
    assert p.path == "/ws" and p.fingerprint == "chrome"
    assert p.alpn == "h2" and p.remark == "MyServer"
    assert p.is_tls and not p.validate()


def test_vless_xhttp_cloudflare():
    """A real-world VLESS+XHTTP-over-Cloudflare link (regression for the
    'added configs don't work / still need v2rayN' bug)."""
    link = ("vless://84524180-c2d5-4bc1-83bb-c36f22d69a3b@127.0.0.1:40443"
            "?encryption=none&security=tls"
            "&sni=lucky-union-b89c.hamijafayi.workers.dev&fp=chrome"
            "&insecure=0&allowInsecure=0&type=xhttp"
            "&host=lucky-union-b89c.hamijafayi.workers.dev"
            "&path=%2Fvless-xhttp&mode=auto#vls-cf-xhttp")
    p = parse_vless(link)
    assert p.transport == "xhttp"
    assert p.mode == "auto"
    assert p.security == "tls"
    assert p.sni == "lucky-union-b89c.hamijafayi.workers.dev"
    assert p.host == "lucky-union-b89c.hamijafayi.workers.dev"
    assert p.path == "/vless-xhttp"
    assert p.fingerprint == "chrome"
    assert p.is_tls and not p.validate()


def test_trojan_xhttp_mode_captured():
    link = ("trojan://pw@h.example:443?type=xhttp&security=tls&sni=h.example"
            "&host=h.example&path=%2Fx&mode=stream-up#TJ")
    p = parse_link(link)
    assert p.transport == "xhttp" and p.mode == "stream-up"


def test_vmess_xhttp_mode_captured():
    payload = {
        "v": "2", "ps": "VM", "add": "v.example.com", "port": "443",
        "id": "aaaa-bbbb", "aid": "0", "net": "xhttp", "type": "none",
        "host": "v.example.com", "path": "/vm", "tls": "tls",
        "sni": "v.example.com", "mode": "packet-up",
    }
    link = "vmess://" + _b64(json.dumps(payload))
    p = parse_vmess(link)
    assert p.transport == "xhttp" and p.mode == "packet-up"


def test_vless_reality():
    link = ("vless://uuid-abc@1.2.3.4:8443?type=tcp&security=reality"
            "&sni=microsoft.com&fp=chrome&pbk=PUBKEY&sid=ab12&flow=xtls-rprx-vision#R")
    p = parse_vless(link)
    assert p.security == "reality"
    assert p.public_key == "PUBKEY" and p.short_id == "ab12"
    assert p.flow == "xtls-rprx-vision"
    assert p.is_tls


# ---------------------------------------------------------------------------
#  VMess
# ---------------------------------------------------------------------------

def test_vmess_json():
    payload = {
        "v": "2", "ps": "VM-Server", "add": "v.example.com", "port": "443",
        "id": "aaaa-bbbb", "aid": "0", "net": "ws", "type": "none",
        "host": "v.example.com", "path": "/vm", "tls": "tls", "sni": "v.example.com",
    }
    link = "vmess://" + _b64(json.dumps(payload))
    p = parse_vmess(link)
    assert p.protocol == "vmess"
    assert p.uuid == "aaaa-bbbb" and p.address == "v.example.com"
    assert p.port == 443 and p.transport == "ws"
    assert p.security == "tls" and p.path == "/vm"
    assert p.remark == "VM-Server"
    assert not p.validate()


# ---------------------------------------------------------------------------
#  Trojan
# ---------------------------------------------------------------------------

def test_trojan():
    link = ("trojan://secretpass@t.example.com:443"
            "?security=tls&sni=t.example.com&type=tcp#Trojan-1")
    p = parse_trojan(link)
    assert p.protocol == "trojan"
    assert p.password == "secretpass"
    assert p.address == "t.example.com" and p.port == 443
    assert p.security == "tls" and p.remark == "Trojan-1"
    assert p.secret == "secretpass"
    assert not p.validate()


# ---------------------------------------------------------------------------
#  Shadowsocks
# ---------------------------------------------------------------------------

def test_ss_sip002_base64_userinfo():
    userinfo = _b64("aes-256-gcm:mypassword")
    link = f"ss://{userinfo}@ss.example.com:8388#SS-Node"
    p = parse_shadowsocks(link)
    assert p.protocol == "shadowsocks"
    assert p.method == "aes-256-gcm" and p.password == "mypassword"
    assert p.address == "ss.example.com" and p.port == 8388
    assert p.remark == "SS-Node"
    assert not p.validate()


def test_ss_legacy_fully_base64():
    inner = _b64("chacha20-ietf-poly1305:pw@host.example.com:1234")
    link = f"ss://{inner}#Legacy"
    p = parse_shadowsocks(link)
    assert p.method == "chacha20-ietf-poly1305"
    assert p.password == "pw"
    assert p.address == "host.example.com" and p.port == 1234


# ---------------------------------------------------------------------------
#  Dispatcher + errors
# ---------------------------------------------------------------------------

def test_dispatch_and_errors():
    assert parse_link("trojan://p@h:443#x").protocol == "trojan"
    try:
        parse_link("magnet://whatever")
        assert False, "should have raised"
    except ShareLinkError:
        pass


# ---------------------------------------------------------------------------
#  Subscription (base64-wrapped multi-line)
# ---------------------------------------------------------------------------

def test_subscription_base64():
    links = "\n".join([
        "vless://u1@a.com:443?type=ws&security=tls#A",
        "trojan://pw@b.com:443?security=tls#B",
        "garbage-not-a-link",
        "ss://" + _b64("aes-256-gcm:pw") + "@c.com:8388#C",
    ])
    blob = _b64(links)
    profiles = parse_subscription(blob)
    assert len(profiles) == 3  # garbage skipped
    assert [p.protocol for p in profiles] == ["vless", "trojan", "shadowsocks"]


def test_subscription_plaintext():
    links = "vless://u1@a.com:443#A\nvmess://" + _b64(json.dumps(
        {"add": "v.com", "port": "443", "id": "x", "net": "tcp"})) + "\n"
    profiles = parse_subscription(links)
    assert len(profiles) == 2


# ---------------------------------------------------------------------------
#  Profile round-trip
# ---------------------------------------------------------------------------

def test_profile_roundtrip():
    p = parse_vless("vless://u@h.com:443?type=grpc&security=tls&sni=h.com"
                    "&serviceName=grpcsvc#G")
    d = p.to_dict()
    p2 = Profile.from_dict(d)
    assert p2.to_dict() == d
    assert p2.transport == "grpc" and p2.service_name == "grpcsvc"


def test_profile_from_dict_ignores_unknown():
    p = Profile.from_dict({"protocol": "trojan", "address": "h", "port": 443,
                           "password": "x", "BOGUS_KEY": 1})
    assert p.protocol == "trojan" and not hasattr(p, "BOGUS_KEY")


# ---------------------------------------------------------------------------
#  self-running fallback (no pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
