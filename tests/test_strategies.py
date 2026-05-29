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


class _FakeIp:
    def __init__(self, packet_len=40):
        self.packet_len = packet_len


class _FakeIpv4:
    def __init__(self, ident=100):
        self.ident = ident


class _FakePacket:
    """Minimal stand-in for pydivert.Packet."""

    def __init__(self, packet_len=40, ident=100, with_ipv4=True):
        self.tcp = _FakeTcp()
        self.ip = _FakeIp(packet_len)
        self.ipv4 = _FakeIpv4(ident) if with_ipv4 else None


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


def test_all_strategies_contains_wrong_seq():
    keys = [s.meta.key for s in all_strategies()]
    assert "wrong_seq" in keys


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
