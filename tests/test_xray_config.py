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


def test_vless_xhttp_outbound():
    """XHTTP transport must produce xhttpSettings (not silently fall back to
    tcp) so configs added in-app actually connect — regression for the
    'still need v2rayN/V2RayTun' report."""
    link = ("vless://uuid-x@127.0.0.1:40443?encryption=none&security=tls"
            "&sni=worker.example.dev&fp=chrome&type=xhttp"
            "&host=worker.example.dev&path=%2Fvless-xhttp&mode=auto#X")
    ob = build_outbound(parse_link(link))
    ss = ob["streamSettings"]
    assert ss["network"] == "xhttp"
    assert ss["security"] == "tls"
    assert ss["tlsSettings"]["serverName"] == "worker.example.dev"
    assert ss["tlsSettings"]["fingerprint"] == "chrome"
    xh = ss["xhttpSettings"]
    assert xh["host"] == "worker.example.dev"
    assert xh["path"] == "/vless-xhttp"
    assert xh["mode"] == "auto"
    # We must emit the SAME minimal xhttpSettings that V2RayTun / v2rayN emit
    # for this link (host + path + mode only). The link works in V2RayTun, and
    # V2RayTun does NOT inject sc* upload-sizing knobs — it lets xray use its
    # own defaults. Hard-coding sc* previously diverged from the known-good
    # client and could stall/break the Worker tunnel, so when the link omits
    # them we must omit them too.
    assert "scMaxConcurrentPosts" not in xh
    assert "scMaxEachPostBytes" not in xh
    assert "scMinPostsIntervalMs" not in xh
    # no stale tcp settings leaked in
    assert "tcpSettings" not in ss and "wsSettings" not in ss


def test_xhttp_sc_params_overridable_via_extra():
    """Share links / clients can carry custom sc* upload sizing in `extra`;
    when present those win over our Worker-friendly defaults."""
    from core.profile import Profile
    p = Profile(
        protocol="vless", address="127.0.0.1", port=40443, uuid="u",
        transport="xhttp", security="tls", sni="h.example", host="h.example",
        path="/x", mode="auto",
        extra={"scMaxConcurrentPosts": 5, "scMaxEachPostBytes": 500000,
               "scMinPostsIntervalMs": 50})
    xh = build_outbound(p)["streamSettings"]["xhttpSettings"]
    assert xh["scMaxConcurrentPosts"] == 5
    assert xh["scMaxEachPostBytes"] == 500000
    assert xh["scMinPostsIntervalMs"] == 50


def test_splithttp_normalises_to_xhttp():
    link = ("vless://u@h.example:443?type=splithttp&security=tls&sni=h.example"
            "&host=h.example&path=%2Fs#S")
    ss = build_outbound(parse_link(link))["streamSettings"]
    assert ss["network"] == "xhttp"
    assert ss["xhttpSettings"]["path"] == "/s"


def test_xhttp_mode_defaults_auto():
    link = ("vless://u@h.example:443?type=xhttp&security=tls&sni=h.example"
            "&host=h.example&path=%2Fx#X")   # no mode= → defaults to auto
    ss = build_outbound(parse_link(link))["streamSettings"]
    assert ss["xhttpSettings"]["mode"] == "auto"


def test_httpupgrade_outbound():
    link = ("vless://u@h.example:443?type=httpupgrade&security=tls"
            "&sni=h.example&host=h.example&path=%2Fhu#HU")
    ss = build_outbound(parse_link(link))["streamSettings"]
    assert ss["network"] == "httpupgrade"
    assert ss["httpupgradeSettings"]["path"] == "/hu"
    assert ss["httpupgradeSettings"]["host"] == "h.example"


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


def test_inbounds_listen_localhost_by_default():
    cfg = build_config(_vless_ws_tls())
    for ib in cfg["inbounds"]:
        assert ib["listen"] == "127.0.0.1"


def test_inbounds_listen_lan_when_shared():
    """allow_lan → both inbounds bind 0.0.0.0 so a phone can connect."""
    cfg = build_config(_vless_ws_tls(), listen="0.0.0.0")
    for ib in cfg["inbounds"]:
        assert ib["listen"] == "0.0.0.0"


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


def test_manager_lan_listen_propagates():
    from core.xray_manager import XrayManager
    mgr = XrayManager(_vless_ws_tls(), listen="0.0.0.0")
    path = mgr.generate_config()
    with open(path, encoding="utf-8") as fp:
        cfg = json.load(fp)
    assert all(ib["listen"] == "0.0.0.0" for ib in cfg["inbounds"])


def test_lan_ip_address_returns_string():
    from core.xray_manager import lan_ip_address
    ip = lan_ip_address()
    assert isinstance(ip, str) and ip.count(".") == 3


def test_manager_direct_wiring():
    from core.xray_manager import XrayManager
    mgr = XrayManager(_vless_ws_tls(), spoof_port=None)
    path = mgr.generate_config()
    with open(path, encoding="utf-8") as fp:
        cfg = json.load(fp)
    v = cfg["outbounds"][0]["settings"]["vnext"][0]
    assert v["address"] == "real.example.com" and v["port"] == 443


# ---------------------------------------------------------------------------
#  SNI-spoof configs
#  ``vless://...@127.0.0.1:40443?...sni=foo.workers.dev&type=xhttp`` — the
#  ``127.0.0.1:40443`` target is OUR SNI spoofer, not the real server. xray
#  dials the spoofer; the spoofer forwards to a fixed Cloudflare IP and injects
#  a decoy ClientHello (fake SNI). The REAL sni/host/path ride end-to-end inside
#  xray's TLS, untouched. This matches what V2RayTun does (it dials our spoofer).
# ---------------------------------------------------------------------------

def _spoof_profile():
    return parse_link(
        "vless://84524180-c2d5-4bc1-83bb-c36f22d69a3b@127.0.0.1:40443"
        "?encryption=none&security=tls&type=xhttp&mode=auto"
        "&sni=lucky-union-b89c.hamijafayi.workers.dev"
        "&host=lucky-union-b89c.hamijafayi.workers.dev"
        "&path=%2Fvless-xhttp&fp=chrome#vls-cf-xhttp")


def test_profile_detects_spoof_config():
    p = _spoof_profile()
    assert p.is_spoof_config is True
    # xray's transport hop stays the loopback spoofer (NOT workers.dev)
    assert p.dial_address == "127.0.0.1"
    assert p.dial_port == 40443
    # the spoofer dials the fixed CF IP + injects the decoy SNI
    assert p.spoof_connect_ip == "104.19.229.21"
    assert p.spoof_connect_port == 443
    assert p.spoof_fake_sni == "www.hcaptcha.com"


def test_normal_profile_is_not_spoof_config():
    p = _vless_ws_tls()
    assert p.is_spoof_config is False
    assert p.dial_address == "real.example.com"
    assert p.dial_port == 443


def test_spoof_config_overrides_from_extra():
    p = _spoof_profile()
    p.extra = {"connect_ip": "104.16.0.1", "fake_sni": "www.bing.com",
               "connect_port": "8443"}
    assert p.spoof_connect_ip == "104.16.0.1"
    assert p.spoof_fake_sni == "www.bing.com"
    assert p.spoof_connect_port == 8443


def test_spoof_config_xray_dials_local_spoofer_with_real_sni():
    # Chained (spoof_port set): xray's outbound dials 127.0.0.1:<spoof_port>,
    # but the TLS serverName / xhttp host/path are the REAL CDN values so the
    # end-to-end handshake through the spoofer reaches Cloudflare correctly.
    from core.xray_manager import XrayManager
    mgr = XrayManager(_spoof_profile(), spoof_port=40443,
                      socks_port=10808, http_port=10809)
    path = mgr.generate_config()
    with open(path, encoding="utf-8") as fp:
        cfg = json.load(fp)
    v = cfg["outbounds"][0]["settings"]["vnext"][0]
    assert v["address"] == "127.0.0.1"
    assert v["port"] == 40443
    assert cfg["inbounds"][0]["port"] == 10808
    ss = cfg["outbounds"][0]["streamSettings"]
    assert ss["tlsSettings"]["serverName"] == \
        "lucky-union-b89c.hamijafayi.workers.dev"
    assert ss["xhttpSettings"]["host"] == \
        "lucky-union-b89c.hamijafayi.workers.dev"
    assert ss["xhttpSettings"]["path"] == "/vless-xhttp"


def test_parse_stats_json_sums_inbound_counters():
    """parse_stats_json sums per-inbound uplink/downlink → (up, down) (#3)."""
    from core.xray_manager import parse_stats_json
    payload = json.dumps({"stat": [
        {"name": "inbound>>>socks-in>>>traffic>>>uplink", "value": 100},
        {"name": "inbound>>>socks-in>>>traffic>>>downlink", "value": 900},
        {"name": "inbound>>>http-in>>>traffic>>>uplink", "value": 50},
        {"name": "inbound>>>http-in>>>traffic>>>downlink", "value": 200},
        # outbound counters must NOT be summed into the inbound totals
        {"name": "outbound>>>proxy>>>traffic>>>uplink", "value": 7777},
    ]})
    assert parse_stats_json(payload) == (150, 1100)


def test_parse_stats_json_handles_empty_and_garbage():
    from core.xray_manager import parse_stats_json
    assert parse_stats_json("") is None
    assert parse_stats_json("   ") is None
    assert parse_stats_json("not json{") is None
    # a valid-but-empty stat block → zero totals
    assert parse_stats_json('{"stat": []}') == (0, 0)


def test_no_stats_api_by_default():
    """Without an api_port the config has no stats/api blocks (unchanged path)."""
    p = _vless_ws_tls()
    cfg = build_config(p, socks_port=10808, http_port=10809)
    assert "api" not in cfg
    assert "stats" not in cfg
    assert all(ib.get("tag") != "api-in" for ib in cfg["inbounds"])


def test_stats_api_enabled_with_api_port():
    """An api_port wires StatsService + a loopback API inbound (#3 live usage)."""
    p = _vless_ws_tls()
    cfg = build_config(p, socks_port=10808, http_port=10809, api_port=10810)
    assert cfg["api"]["services"] == ["StatsService"]
    assert cfg["stats"] == {}
    assert cfg["policy"]["system"]["statsInboundUplink"] is True
    assert cfg["policy"]["system"]["statsInboundDownlink"] is True
    api_in = [ib for ib in cfg["inbounds"] if ib.get("tag") == "api-in"]
    assert len(api_in) == 1
    assert api_in[0]["port"] == 10810
    assert api_in[0]["listen"] == "127.0.0.1"
    assert api_in[0]["protocol"] == "dokodemo-door"
    # the api inbound is routed to the api outbound
    rules = cfg["routing"]["rules"]
    assert any(r.get("inboundTag") == ["api-in"]
               and r.get("outboundTag") == "api" for r in rules)


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
