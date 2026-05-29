"""Unit tests for core.xray_config (multi-protocol outbound + auto-chain).

Run:  python tests/test_xray_config.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.profile import Profile
from core.share_link import parse_link
from core.xray_config import build_config, build_outbound


def _vless_ws_tls():
    return parse_link(
        "vless://uuid-1@real.example.com:443?type=ws&security=tls"
        "&sni=cdn.example.com&host=cdn.example.com&path=%2Fws&fp=chrome#A")


# ---------------------------------------------------------------------------
#  per-protocol outbound shape
# ---------------------------------------------------------------------------

def test_vless_outbound():
    ob = build_outbound(_vless_ws_tls())
    assert ob["protocol"] == "vless"
    v = ob["settings"]["vnext"][0]
    assert v["address"] == "real.example.com" and v["port"] == 443
    assert v["users"][0]["id"] == "uuid-1"
    ss = ob["streamSettings"]
    assert ss["network"] == "ws" and ss["security"] == "tls"
    assert ss["wsSettings"]["path"] == "/ws"
    assert ss["wsSettings"]["headers"]["Host"] == "cdn.example.com"
    assert ss["tlsSettings"]["serverName"] == "cdn.example.com"
    assert ss["tlsSettings"]["fingerprint"] == "chrome"


def test_vmess_outbound():
    import base64
    payload = {"add": "v.com", "port": "443", "id": "vm-id", "aid": "0",
               "net": "tcp", "tls": "tls", "sni": "v.com", "ps": "VM"}
    link = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
    ob = build_outbound(parse_link(link))
    assert ob["protocol"] == "vmess"
    u = ob["settings"]["vnext"][0]["users"][0]
    assert u["id"] == "vm-id" and u["alterId"] == 0


def test_trojan_outbound():
    ob = build_outbound(parse_link(
        "trojan://pass1@t.com:443?security=tls&sni=t.com#T"))
    assert ob["protocol"] == "trojan"
    srv = ob["settings"]["servers"][0]
    assert srv["address"] == "t.com" and srv["password"] == "pass1"
    assert ob["streamSettings"]["security"] == "tls"


def test_shadowsocks_outbound():
    import base64
    ui = base64.b64encode(b"aes-256-gcm:sspw").decode()
    ob = build_outbound(parse_link(f"ss://{ui}@s.com:8388#S"))
    assert ob["protocol"] == "shadowsocks"
    srv = ob["settings"]["servers"][0]
    assert srv["method"] == "aes-256-gcm" and srv["password"] == "sspw"
    assert srv["port"] == 8388


def test_reality_outbound():
    ob = build_outbound(parse_link(
        "vless://u@1.2.3.4:8443?type=tcp&security=reality&sni=microsoft.com"
        "&fp=chrome&pbk=KEY&sid=ab12&flow=xtls-rprx-vision#R"))
    ss = ob["streamSettings"]
    assert ss["security"] == "reality"
    assert ss["realitySettings"]["publicKey"] == "KEY"
    assert ss["realitySettings"]["shortId"] == "ab12"
    assert ob["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"


def test_grpc_outbound():
    ob = build_outbound(parse_link(
        "vless://u@h.com:443?type=grpc&security=tls&sni=h.com&serviceName=gsvc#G"))
    ss = ob["streamSettings"]
    assert ss["network"] == "grpc"
    assert ss["grpcSettings"]["serviceName"] == "gsvc"


# ---------------------------------------------------------------------------
#  auto-chain: outbound dest overridden to the local spoofer
# ---------------------------------------------------------------------------

def test_chain_dest_override():
    p = _vless_ws_tls()
    ob = build_outbound(p, dest_address="127.0.0.1", dest_port=40443)
    v = ob["settings"]["vnext"][0]
    # physical hop is the spoofer …
    assert v["address"] == "127.0.0.1" and v["port"] == 40443
    # … but TLS/SNI still describe the REAL server
    assert ob["streamSettings"]["tlsSettings"]["serverName"] == "cdn.example.com"


def test_full_config_valid_json_and_inbounds():
    cfg = build_config(_vless_ws_tls(), socks_port=10808, http_port=10809,
                       dest_address="127.0.0.1", dest_port=40443)
    # serialisable
    s = json.dumps(cfg)
    assert json.loads(s) == cfg
    tags = {ib["tag"] for ib in cfg["inbounds"]}
    assert tags == {"socks-in", "http-in"}
    assert cfg["inbounds"][0]["port"] == 10808
    out_tags = {o.get("tag") for o in cfg["outbounds"]}
    assert {"proxy", "direct", "block"} <= out_tags
    # routing keeps private IPs direct
    assert cfg["routing"]["rules"][0]["outboundTag"] == "direct"


def test_gaming_mode_sockopt():
    cfg = build_config(_vless_ws_tls(), gaming=True)
    ss = cfg["outbounds"][0]["streamSettings"]
    assert ss["sockopt"]["tcpNoDelay"] is True
    # sniffing disabled in gaming mode for lower latency
    assert cfg["inbounds"][0]["sniffing"]["enabled"] is False


# ---------------------------------------------------------------------------
#  XrayManager wiring (no subprocess launch — just config generation logic)
# ---------------------------------------------------------------------------

def test_manager_chain_wiring(tmp_path=None):
    from core.xray_manager import XrayManager, find_free_port
    p = _vless_ws_tls()
    port = find_free_port(40443)
    assert 0 < port < 65536
    mgr = XrayManager(p, spoof_port=port)
    assert mgr.real_server == ("real.example.com", 443)
    path = mgr.generate_config()
    with open(path, encoding="utf-8") as fp:
        cfg = json.load(fp)
    v = cfg["outbounds"][0]["settings"]["vnext"][0]
    assert v["address"] == "127.0.0.1" and v["port"] == port


def test_manager_direct_wiring():
    from core.xray_manager import XrayManager
    mgr = XrayManager(_vless_ws_tls(), spoof_port=None)
    path = mgr.generate_config()
    with open(path, encoding="utf-8") as fp:
        cfg = json.load(fp)
    v = cfg["outbounds"][0]["settings"]["vnext"][0]
    assert v["address"] == "real.example.com" and v["port"] == 443


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL  {fn.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
