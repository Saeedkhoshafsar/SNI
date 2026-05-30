"""Unit tests for the strategy engine (registry + WrongSeqStrategy).

These tests deliberately avoid importing ``fake_tcp`` (which needs pydivert /
WinDivert, unavailable off-Windows). The ``strategies`` package has no hard
pydivert dependency, so the registry and packet mutation are exercised with
small duck-typed fakes.

Run:  python tests/test_strategies.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import (
    REGISTRY, BypassStrategy, StrategyMeta, all_strategies, get_strategy,
    register,
)
from strategies.base import register as _register  # same object, sanity
from strategies.wrong_seq import WrongSeqStrategy


# ---------------------------------------------------------------------------
#  duck-typed fakes mimicking pydivert.Packet / FakeInjectiveConnection
# ---------------------------------------------------------------------------

class _FakeTcp:
    def __init__(self):
        self.psh = False
        self.payload = b""
        self.seq_num = 0
        self.checksum = 0x1234  # a "valid-looking" starting value


class _FakeIp:
    def __init__(self, packet_len=40, ttl=64):
        self.packet_len = packet_len
        self.ttl = ttl


class _FakeIpv4:
    def __init__(self, ident=100):
        self.ident = ident


class _FakeIpv6:
    def __init__(self, hop_limit=64):
        self.hop_limit = hop_limit


class _FakePacket:
    """Minimal stand-in for pydivert.Packet."""

    def __init__(self, packet_len=40, ident=100, with_ipv4=True,
                 with_ipv6=False):
        self.tcp = _FakeTcp()
        self.ip = _FakeIp(packet_len)
        self.ipv4 = _FakeIpv4(ident) if with_ipv4 else None
        self.ipv6 = _FakeIpv6() if with_ipv6 else None


class _FakeConnection:
    """Minimal stand-in for FakeInjectiveConnection."""

    def __init__(self, syn_seq=1000, fake_data=b"\x16\x03\x01hello"):
        self.syn_seq = syn_seq
        self.fake_data = fake_data
        self.fake_sent = False
        self.monitor = True


class _FakeInjector:
    """Captures what send_fake() would push through WinDivert."""

    def __init__(self):
        self.sent = []  # list of (packet, recalc)

        class _W:
            def __init__(self, outer):
                self._outer = outer

            def send(self, packet, recalc):
                self._outer.sent.append((packet, recalc))

        self.w = _W(self)


# ---------------------------------------------------------------------------
#  registry
# ---------------------------------------------------------------------------

def test_register_is_shared_object():
    # the decorator re-exported from the package is the same callable
    assert register is _register


def test_wrong_seq_registered():
    assert "wrong_seq" in REGISTRY
    assert isinstance(REGISTRY["wrong_seq"], WrongSeqStrategy)


def test_get_strategy_returns_instance():
    s = get_strategy("wrong_seq")
    assert isinstance(s, WrongSeqStrategy)
    assert s.key == "wrong_seq"


def test_get_strategy_unknown_raises_keyerror():
    try:
        get_strategy("does_not_exist")
    except KeyError as exc:
        # Farsi message must name the bad key and list available keys
        msg = str(exc)
        assert "does_not_exist" in msg
        assert "wrong_seq" in msg
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")


EXPECTED_KEYS = {
    "wrong_seq", "wrong_checksum", "fake_ttl", "multi_fake", "fake_disorder",
}


def test_all_strategies_contains_wrong_seq():
    keys = [s.meta.key for s in all_strategies()]
    assert "wrong_seq" in keys


def test_all_expected_strategies_registered():
    assert EXPECTED_KEYS <= set(REGISTRY)


def test_all_strategies_implemented_only():
    impl = all_strategies(implemented_only=True)
    assert all(s.meta.implemented for s in impl)
    assert "wrong_seq" in [s.meta.key for s in impl]


def test_register_duplicate_raises():
    # registering the same key twice must fail loudly
    try:
        @register
        class _Dup(BypassStrategy):
            meta = StrategyMeta(key="wrong_seq", title="dup",
                                description="dup")
    except ValueError as exc:
        assert "wrong_seq" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on duplicate key")


# ---------------------------------------------------------------------------
#  metadata
# ---------------------------------------------------------------------------

def test_wrong_seq_metadata():
    m = WrongSeqStrategy.meta
    assert m.key == "wrong_seq"
    assert m.title == "Wrong Sequence"
    assert m.implemented is True
    assert "inject" in m.tags and "stateful-dpi" in m.tags


def test_wrong_seq_score():
    assert get_strategy("wrong_seq").score() == 0.8


def test_strategy_meta_frozen():
    m = StrategyMeta(key="k", title="t", description="d")
    try:
        m.key = "x"  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("StrategyMeta must be frozen")


# ---------------------------------------------------------------------------
#  mutate_fake_packet behaviour
# ---------------------------------------------------------------------------

def test_mutate_sets_psh_and_payload():
    s = get_strategy("wrong_seq")
    pkt = _FakePacket(packet_len=40)
    conn = _FakeConnection(syn_seq=1000, fake_data=b"abcdef")
    s.mutate_fake_packet(pkt, conn)
    assert pkt.tcp.psh is True
    assert pkt.tcp.payload == b"abcdef"


def test_mutate_increases_packet_len_by_fake_data():
    s = get_strategy("wrong_seq")
    pkt = _FakePacket(packet_len=40)
    conn = _FakeConnection(fake_data=b"1234567890")  # 10 bytes
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ip.packet_len == 50


def test_mutate_wrong_seq_formula():
    # seq_num = (syn_seq + 1 - len(payload)) & 0xffffffff
    s = get_strategy("wrong_seq")
    data = b"hello!"  # 6 bytes
    pkt = _FakePacket()
    conn = _FakeConnection(syn_seq=1000, fake_data=data)
    s.mutate_fake_packet(pkt, conn)
    expected = (1000 + 1 - len(data)) & 0xFFFFFFFF
    assert pkt.tcp.seq_num == expected == 995


def test_mutate_wrong_seq_wraps_unsigned():
    s = get_strategy("wrong_seq")
    data = b"x" * 10
    pkt = _FakePacket()
    conn = _FakeConnection(syn_seq=0, fake_data=data)  # forces wrap below 0
    s.mutate_fake_packet(pkt, conn)
    expected = (0 + 1 - 10) & 0xFFFFFFFF
    assert pkt.tcp.seq_num == expected
    assert 0 <= pkt.tcp.seq_num <= 0xFFFFFFFF


def test_mutate_bumps_ipv4_ident():
    s = get_strategy("wrong_seq")
    pkt = _FakePacket(ident=100)
    conn = _FakeConnection()
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ipv4.ident == 101


def test_mutate_ipv4_ident_wraps_16bit():
    s = get_strategy("wrong_seq")
    pkt = _FakePacket(ident=0xFFFF)
    conn = _FakeConnection()
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ipv4.ident == 0  # (0xFFFF + 1) & 0xFFFF


def test_mutate_no_ipv4_does_not_crash():
    # IPv6 path: packet.ipv4 is None -> ident bump skipped
    s = get_strategy("wrong_seq")
    pkt = _FakePacket(with_ipv4=False)
    conn = _FakeConnection()
    s.mutate_fake_packet(pkt, conn)  # must not raise
    assert pkt.tcp.payload == conn.fake_data


# ---------------------------------------------------------------------------
#  send_fake behaviour (default impl)
# ---------------------------------------------------------------------------

def test_send_fake_marks_and_sends_with_recalc():
    s = get_strategy("wrong_seq")
    pkt = _FakePacket()
    conn = _FakeConnection()
    inj = _FakeInjector()
    s.send_fake(inj, pkt, conn)
    assert conn.fake_sent is True
    assert inj.sent == [(pkt, True)]  # recalc=True


def test_base_mutate_not_implemented():
    base = BypassStrategy()
    try:
        base.mutate_fake_packet(_FakePacket(), _FakeConnection())
    except NotImplementedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("base strategy must not implement mutate")


def test_base_default_score():
    assert BypassStrategy().score() == 0.5


# ---------------------------------------------------------------------------
#  expects_ack contract — fire-and-forget vs wait-for-server-ACK
# ---------------------------------------------------------------------------

def test_base_expects_ack_default_true():
    # The safe default: assume the server ACKs the fake (classic techniques).
    assert BypassStrategy().expects_ack is True


def test_out_of_window_strategies_expect_ack():
    # wrong_seq / multi_fake / fake_disorder all leave the fake within reach of
    # the server (out-of-window seq) → it replies with a duplicate-ACK, so the
    # spoofer waits for confirmation.
    for key in ("wrong_seq", "multi_fake", "fake_disorder"):
        assert get_strategy(key).expects_ack is True, key


def test_fire_and_forget_strategies_do_not_expect_ack():
    # fake_ttl (packet TTL-dies before the server) and wrong_checksum (server
    # drops the corrupt segment) are designed so the server NEVER receives the
    # fake → no ACK ever returns. They must declare expects_ack=False so the
    # spoofer relays immediately instead of timing out after 5s.
    for key in ("fake_ttl", "wrong_checksum"):
        assert get_strategy(key).expects_ack is False, key


# ---------------------------------------------------------------------------
#  shared helper
# ---------------------------------------------------------------------------

def test_apply_fake_payload_shared_prologue():
    pkt = _FakePacket(packet_len=40, ident=7)
    conn = _FakeConnection(fake_data=b"12345")  # 5 bytes
    BypassStrategy.apply_fake_payload(pkt, conn)
    assert pkt.tcp.psh is True
    assert pkt.tcp.payload == b"12345"
    assert pkt.ip.packet_len == 45
    assert pkt.ipv4.ident == 8


# ---------------------------------------------------------------------------
#  wrong_checksum
# ---------------------------------------------------------------------------

def test_wrong_checksum_metadata_and_score():
    s = get_strategy("wrong_checksum")
    assert s.meta.title == "Wrong Checksum"
    assert s.score() == 0.6
    assert "no-recalc" in s.meta.tags


def test_wrong_checksum_corrupts_checksum_and_keeps_inwindow_seq():
    s = get_strategy("wrong_checksum")
    pkt = _FakePacket()
    conn = _FakeConnection(syn_seq=2000, fake_data=b"hello")
    s.mutate_fake_packet(pkt, conn)
    assert pkt.tcp.payload == b"hello"
    # the fake must carry a deliberately-wrong, NON-ZERO checksum. We avoid
    # 0x0000 because in TCP (unlike UDP) zero is a legitimate checksum value and
    # some NIC offload paths treat it specially, so it wasn't reliably dropped.
    assert pkt.tcp.checksum == s.BAD_CHECKSUM
    assert pkt.tcp.checksum != 0x0000
    assert pkt.tcp.checksum != 0x1234          # differs from the valid starting value
    assert pkt.tcp.seq_num == (2000 + 1) & 0xFFFFFFFF  # in-window


def test_wrong_checksum_avoids_accidental_match():
    # if the stale checksum already equals BAD_CHECKSUM, the strategy must
    # perturb it so the emitted value is still definitely "wrong".
    s = get_strategy("wrong_checksum")
    pkt = _FakePacket()
    pkt.tcp.checksum = s.BAD_CHECKSUM
    conn = _FakeConnection(syn_seq=2000, fake_data=b"hello")
    s.mutate_fake_packet(pkt, conn)
    assert pkt.tcp.checksum != 0x0000
    # perturbed away from the constant (XOR 0xFFFF) yet still non-zero
    assert pkt.tcp.checksum == (s.BAD_CHECKSUM ^ 0xFFFF)


def test_wrong_checksum_sends_without_recalc():
    # CRITICAL: must send recalc=False so WinDivert keeps the bad checksum
    s = get_strategy("wrong_checksum")
    pkt = _FakePacket()
    conn = _FakeConnection()
    inj = _FakeInjector()
    s.send_fake(inj, pkt, conn)
    assert conn.fake_sent is True
    assert inj.sent == [(pkt, False)]


# ---------------------------------------------------------------------------
#  fake_ttl
# ---------------------------------------------------------------------------

def test_fake_ttl_metadata_and_score():
    s = get_strategy("fake_ttl")
    assert s.meta.title == "Fake TTL"
    assert s.score() == 0.55


def test_fake_ttl_sets_low_ipv4_ttl():
    s = get_strategy("fake_ttl")
    pkt = _FakePacket()  # ipv4 packet, no ipv6
    conn = _FakeConnection(syn_seq=500)
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ip.ttl == s.DEFAULT_TTL
    assert pkt.tcp.seq_num == (500 + 1) & 0xFFFFFFFF


def test_fake_ttl_per_connection_override():
    s = get_strategy("fake_ttl")
    pkt = _FakePacket()
    conn = _FakeConnection()
    conn.fake_ttl = 9
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ip.ttl == 9


def test_fake_ttl_sets_ipv6_hop_limit():
    s = get_strategy("fake_ttl")
    pkt = _FakePacket(with_ipv4=False, with_ipv6=True)
    conn = _FakeConnection()
    s.mutate_fake_packet(pkt, conn)
    assert pkt.ipv6.hop_limit == s.DEFAULT_TTL


# ---------------------------------------------------------------------------
#  multi_fake
# ---------------------------------------------------------------------------

def test_multi_fake_metadata_and_score():
    s = get_strategy("multi_fake")
    assert s.meta.title == "Multi Fake"
    assert s.score() == 0.65


def test_multi_fake_uses_wrong_seq_formula():
    s = get_strategy("multi_fake")
    data = b"abcd"  # 4 bytes
    pkt = _FakePacket()
    conn = _FakeConnection(syn_seq=1000, fake_data=data)
    s.mutate_fake_packet(pkt, conn)
    assert pkt.tcp.seq_num == (1000 + 1 - 4) & 0xFFFFFFFF


def test_multi_fake_sends_multiple_copies():
    s = get_strategy("multi_fake")
    pkt = _FakePacket()
    conn = _FakeConnection()
    inj = _FakeInjector()
    s.send_fake(inj, pkt, conn)
    assert conn.fake_sent is True
    assert len(inj.sent) == s.REPEAT
    assert all(recalc is True for _, recalc in inj.sent)


def test_multi_fake_per_connection_repeat():
    s = get_strategy("multi_fake")
    pkt = _FakePacket()
    conn = _FakeConnection()
    conn.fake_repeat = 5
    inj = _FakeInjector()
    s.send_fake(inj, pkt, conn)
    assert len(inj.sent) == 5


# ---------------------------------------------------------------------------
#  fake_disorder
# ---------------------------------------------------------------------------

def test_fake_disorder_metadata_and_score():
    s = get_strategy("fake_disorder")
    assert s.meta.title == "Fake Disorder"
    assert s.score() == 0.6


def test_fake_disorder_sends_two_copies_with_different_seq():
    s = get_strategy("fake_disorder")
    data = b"payload!"  # 8 bytes
    pkt = _FakePacket()
    conn = _FakeConnection(syn_seq=3000, fake_data=data)
    inj = _FakeInjector()
    s.mutate_fake_packet(pkt, conn)
    first_seq = pkt.tcp.seq_num
    s.send_fake(inj, pkt, conn)
    assert conn.fake_sent is True
    assert len(inj.sent) == 2
    # after send_fake the second copy's seq is nudged further back
    expected_first = (3000 + 1 - len(data)) & 0xFFFFFFFF
    expected_second = (3000 + 1 - 2 * len(data)) & 0xFFFFFFFF
    assert first_seq == expected_first
    assert pkt.tcp.seq_num == expected_second
    assert expected_first != expected_second


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
