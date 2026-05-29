"""Unit tests for core.fragment (TCP segmentation + TLS record fragmentation).

Pure-data tests — no pydivert / OS dependency. Uses the real 517-byte
ClientHello produced by utils.packet_templates.ClientHelloMaker so the SNI
parser is exercised against the exact wire format the tool emits.

Run:  python tests/test_fragment.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fragment import (
    EXT_SERVER_NAME, TLS_CONTENT_HANDSHAKE, TLS_RECORD_HEADER_LEN,
    find_sni_offset, fragment_client_hello, is_tls_handshake,
    parse_record_header, reassemble_tls_records, split_tls_records,
    tcp_segment_at, tcp_segment_at_sni,
)
from utils.packet_templates import ClientHelloMaker


def _client_hello(sni: bytes = b"www.cloudflare.com") -> bytes:
    return ClientHelloMaker.get_client_hello_with(
        os.urandom(32), os.urandom(32), sni, os.urandom(32))


# ---------------------------------------------------------------------------
#  record header parsing
# ---------------------------------------------------------------------------

def test_parse_record_header_on_client_hello():
    ch = _client_hello()
    ct, ver, blen = parse_record_header(ch)
    assert ct == TLS_CONTENT_HANDSHAKE
    # record-layer version is whatever the maker emits (0x0301 for TLS1.3 CH)
    assert ver == struct.unpack("!H", ch[1:3])[0]
    assert blen == len(ch) - TLS_RECORD_HEADER_LEN


def test_parse_record_header_too_short():
    assert parse_record_header(b"\x16\x03") is None


def test_is_tls_handshake():
    assert is_tls_handshake(_client_hello()) is True
    assert is_tls_handshake(b"hello world") is False
    assert is_tls_handshake(b"") is False


# ---------------------------------------------------------------------------
#  SNI location
# ---------------------------------------------------------------------------

def test_find_sni_offset_matches_hostname():
    sni = b"www.cloudflare.com"
    ch = _client_hello(sni)
    off = find_sni_offset(ch)
    assert off is not None
    assert ch[off:off + len(sni)] == sni


def test_find_sni_offset_various_lengths():
    for host in (b"a.io", b"example.com", b"very-long-subdomain.example.co.uk"):
        ch = _client_hello(host)
        off = find_sni_offset(ch)
        assert off is not None, host
        assert ch[off:off + len(host)] == host


def test_find_sni_offset_non_tls_returns_none():
    assert find_sni_offset(b"not a tls record at all") is None
    assert find_sni_offset(b"") is None


def test_find_sni_offset_truncated_returns_none():
    ch = _client_hello()
    # cut off mid-handshake — parser must fail gracefully, never raise
    assert find_sni_offset(ch[:50]) is None


# ---------------------------------------------------------------------------
#  TCP segmentation
# ---------------------------------------------------------------------------

def test_tcp_segment_at_basic():
    data = b"0123456789"
    segs = tcp_segment_at(data, 4)
    assert segs == [b"0123", b"456789"]
    assert b"".join(segs) == data


def test_tcp_segment_at_out_of_range_returns_whole():
    data = b"abc"
    assert tcp_segment_at(data, 0) == [data]
    assert tcp_segment_at(data, 3) == [data]
    assert tcp_segment_at(data, 99) == [data]


def test_tcp_segment_at_sni_straddles_hostname():
    sni = b"www.cloudflare.com"
    ch = _client_hello(sni)
    segs = tcp_segment_at_sni(ch)
    assert len(segs) == 2
    assert b"".join(segs) == ch                 # lossless
    # the full SNI string must not survive in either half
    assert sni not in segs[0]
    assert sni not in segs[1]


def test_tcp_segment_at_sni_fallback_when_no_sni():
    data = b"\x16\x03\x03\x00\x05hello"          # tls-ish but no SNI
    segs = tcp_segment_at_sni(data, fallback=2)
    assert b"".join(segs) == data
    assert len(segs) == 2
    assert len(segs[0]) == 2


# ---------------------------------------------------------------------------
#  TLS record fragmentation
# ---------------------------------------------------------------------------

def test_split_tls_records_lossless():
    ch = _client_hello()
    recs = split_tls_records(ch, chunk_size=64)
    assert len(recs) > 1
    assert reassemble_tls_records(recs) == ch


def test_split_tls_records_each_has_valid_header():
    ch = _client_hello()
    orig_ver = struct.unpack("!H", ch[1:3])[0]
    recs = split_tls_records(ch, chunk_size=80)
    for r in recs:
        hdr = parse_record_header(r)
        assert hdr is not None
        ct, ver, blen = hdr
        assert ct == TLS_CONTENT_HANDSHAKE
        assert ver == orig_ver                          # version preserved
        assert blen == len(r) - TLS_RECORD_HEADER_LEN   # header matches body


def test_split_tls_records_chunk_sizes_respected():
    ch = _client_hello()
    chunk = 100
    recs = split_tls_records(ch, chunk_size=chunk)
    bodies = [len(r) - TLS_RECORD_HEADER_LEN for r in recs]
    assert all(b <= chunk for b in bodies)
    assert sum(bodies) == len(ch) - TLS_RECORD_HEADER_LEN


def test_split_tls_records_small_body_unchanged():
    # a record whose body already fits one chunk is returned as-is
    rec = bytes([TLS_CONTENT_HANDSHAKE]) + struct.pack("!H", 0x0303) \
        + struct.pack("!H", 4) + b"abcd"
    assert split_tls_records(rec, chunk_size=64) == [rec]


def test_split_tls_records_non_tls_unchanged():
    assert split_tls_records(b"xy") == [b"xy"]


def test_split_tls_records_rejects_bad_chunk_size():
    try:
        split_tls_records(_client_hello(), chunk_size=0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("chunk_size=0 must raise ValueError")


def test_split_then_segment_compose():
    # the two layers are independent and both reversible
    ch = _client_hello()
    recs = split_tls_records(ch, chunk_size=128)
    assert reassemble_tls_records(recs) == ch
    segs = tcp_segment_at_sni(ch)
    assert b"".join(segs) == ch


# ---------------------------------------------------------------------------
#  high-level entry point
# ---------------------------------------------------------------------------

def test_fragment_client_hello_noop_when_disabled():
    ch = _client_hello()
    out = fragment_client_hello(ch, tcp=False, tls=False)
    assert out == [ch]


def test_fragment_client_hello_tcp_only():
    ch = _client_hello()
    out = fragment_client_hello(ch, tcp=True, tls=False)
    assert len(out) == 2
    assert b"".join(out) == ch


def test_fragment_client_hello_tls_only():
    ch = _client_hello()
    out = fragment_client_hello(ch, tcp=False, tls=True, tls_chunk=64)
    stream = b"".join(out)
    # TLS fragmentation adds extra record headers, so the wire bytes GROW and
    # are NOT identical to the original ...
    assert stream != ch
    assert len(stream) > len(ch)
    # ... but they reassemble to the exact same handshake record (lossless).
    assert reassemble_tls_records(split_tls_records(ch, chunk_size=64)) == ch


def test_fragment_client_hello_both_layers():
    ch = _client_hello()
    out = fragment_client_hello(ch, tcp=True, tls=True, tls_chunk=48)
    # tcp split applied on top of the (larger) tls-fragmented stream
    assert len(out) == 2
    joined = b"".join(out)
    assert len(joined) > len(ch)          # grown by the extra TLS headers
    # the tls layer is still reversible to the original handshake record
    assert reassemble_tls_records(split_tls_records(ch, chunk_size=48)) == ch


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
