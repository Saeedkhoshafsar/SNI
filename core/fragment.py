"""Fragmentation layer — split the *real* ClientHello so DPI can't read the SNI.

Unlike the ``strategies/`` package (which injects a *fake* packet via WinDivert),
fragmentation operates on the genuine ClientHello bytes themselves and is
therefore **pure data, no pydivert, no OS dependency** — fully unit-testable in
the sandbox and reusable on any platform (the future cross-platform path).

Two complementary techniques live here:

* **TCP segmentation** — cut the outgoing TCP payload right in the middle of the
  SNI string so a DPI matching ``\\x00<host>`` in a single segment never sees a
  complete hostname. The two halves are sent as separate TCP segments; the
  server's stack reassembles them transparently.

* **TLS record fragmentation** — a single TLS handshake record (header
  ``16 <ver_hi> <ver_lo> <len_hi> <len_lo>`` — the record-layer version is
  whatever the client emits, e.g. ``03 01`` for a TLS 1.3 ClientHello) is
  rewritten as several smaller TLS records, each carrying the *same* content
  type + version and its own valid length. A DPI that only inspects the first
  record
  (or assumes one record == one ClientHello) misses the SNI, while a compliant
  server concatenates the record bodies before parsing the handshake.

Both return lists of ``bytes`` chunks ready to be written to the socket /
re-injected in order.
"""
from __future__ import annotations

import struct
from typing import List, Optional

# TLS record content types / handshake constants
TLS_CONTENT_HANDSHAKE = 0x16
TLS_RECORD_HEADER_LEN = 5          # type(1) + version(2) + length(2)
EXT_SERVER_NAME = 0x0000           # SNI extension id


# ---------------------------------------------------------------------------
#  TLS record header helpers
# ---------------------------------------------------------------------------

def parse_record_header(data: bytes) -> Optional[tuple[int, int, int]]:
    """Return (content_type, version, body_len) for the first TLS record.

    ``None`` if *data* is too short to contain a full 5-byte header.
    """
    if len(data) < TLS_RECORD_HEADER_LEN:
        return None
    content_type = data[0]
    version = struct.unpack("!H", data[1:3])[0]
    body_len = struct.unpack("!H", data[3:5])[0]
    return content_type, version, body_len


def is_tls_handshake(data: bytes) -> bool:
    """True if *data* starts with a TLS handshake record header."""
    hdr = parse_record_header(data)
    return hdr is not None and hdr[0] == TLS_CONTENT_HANDSHAKE


# ---------------------------------------------------------------------------
#  SNI location
# ---------------------------------------------------------------------------

def find_sni_offset(client_hello: bytes) -> Optional[int]:
    """Find the absolute offset of the SNI hostname string in *client_hello*.

    Walks the TLS record + handshake structure properly (record header →
    handshake header → random → session_id → cipher_suites → compression →
    extensions) and, inside the server_name extension, returns the offset of
    the first hostname byte. Returns ``None`` if no SNI is present or the buffer
    is malformed/too short.
    """
    try:
        # record header
        hdr = parse_record_header(client_hello)
        if hdr is None or hdr[0] != TLS_CONTENT_HANDSHAKE:
            return None
        pos = TLS_RECORD_HEADER_LEN

        # handshake header: msg_type(1) + length(3); msg_type 0x01 == ClientHello
        if len(client_hello) < pos + 4 or client_hello[pos] != 0x01:
            return None
        pos += 4
        pos += 2          # client_version
        pos += 32         # random

        # session_id
        if pos >= len(client_hello):
            return None
        sid_len = client_hello[pos]
        pos += 1 + sid_len

        # cipher_suites
        if pos + 2 > len(client_hello):
            return None
        cs_len = struct.unpack("!H", client_hello[pos:pos + 2])[0]
        pos += 2 + cs_len

        # compression_methods
        if pos >= len(client_hello):
            return None
        comp_len = client_hello[pos]
        pos += 1 + comp_len

        # extensions block
        if pos + 2 > len(client_hello):
            return None
        ext_total = struct.unpack("!H", client_hello[pos:pos + 2])[0]
        pos += 2
        end = min(len(client_hello), pos + ext_total)

        while pos + 4 <= end:
            ext_type = struct.unpack("!H", client_hello[pos:pos + 2])[0]
            ext_len = struct.unpack("!H", client_hello[pos + 2:pos + 4])[0]
            body = pos + 4
            if ext_type == EXT_SERVER_NAME:
                # server_name_list: list_len(2) + entry_type(1) + name_len(2) + name
                p = body
                if p + 2 > len(client_hello):
                    return None
                p += 2                       # server_name_list length
                if p + 3 > len(client_hello):
                    return None
                p += 1                       # name_type (0 == host_name)
                p += 2                       # host name length
                return p                     # first byte of the hostname
            pos = body + ext_len
        return None
    except (struct.error, IndexError):
        return None


# ---------------------------------------------------------------------------
#  TCP segmentation
# ---------------------------------------------------------------------------

def tcp_segment_at(data: bytes, offset: int) -> List[bytes]:
    """Split *data* into two TCP segments at byte *offset* (1..len-1)."""
    if offset <= 0 or offset >= len(data):
        return [data]
    return [data[:offset], data[offset:]]


def tcp_segment_at_sni(data: bytes, *, fallback: int = 2) -> List[bytes]:
    """Segment the TCP payload so the cut lands inside the SNI hostname.

    If the SNI can be located, the split point is placed a couple of bytes into
    the hostname (so ``\\x00<host>`` straddles two segments). Otherwise it falls
    back to splitting after *fallback* bytes, which still desynchronises naive
    DPI that keys on the record header.
    """
    sni = find_sni_offset(data)
    if sni is not None and 0 < sni < len(data):
        # cut a little way *into* the hostname, never before it
        cut = min(sni + 1, len(data) - 1)
        return tcp_segment_at(data, cut)
    return tcp_segment_at(data, min(fallback, max(1, len(data) - 1)))


# ---------------------------------------------------------------------------
#  TLS record fragmentation
# ---------------------------------------------------------------------------

def split_tls_records(data: bytes, *, chunk_size: int = 64) -> List[bytes]:
    """Rewrite the first TLS record of *data* as several smaller records.

    Each output record keeps the original content type + version and carries a
    ``chunk_size`` slice of the original body with its own valid length header.
    Anything in *data* after the first record is appended unchanged. If *data*
    isn't a TLS record (or the body already fits one chunk) it's returned as-is.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    hdr = parse_record_header(data)
    if hdr is None:
        return [data]
    content_type, version, body_len = hdr
    body_start = TLS_RECORD_HEADER_LEN
    body_end = body_start + body_len
    body = data[body_start:body_end]
    trailer = data[body_end:]                # extra records, if any

    if len(body) <= chunk_size:
        return [data]

    out: List[bytes] = []
    for i in range(0, len(body), chunk_size):
        piece = body[i:i + chunk_size]
        out.append(
            bytes([content_type]) + struct.pack("!H", version)
            + struct.pack("!H", len(piece)) + piece
        )
    if trailer:
        out.append(trailer)
    return out


def fragment_client_hello(
    data: bytes,
    *,
    tcp: bool = False,
    tls: bool = False,
    tls_chunk: int = 64,
) -> List[bytes]:
    """High-level entry point: apply the enabled fragmentation layers in order.

    This is what the engine / auto-prober calls. The two layers compose:

    1. **TLS record** fragmentation first (rewrites the single handshake record
       into several smaller records), then
    2. **TCP** segmentation at the SNI on the resulting byte stream.

    With both off, the original ``data`` is returned unchanged as a single
    chunk, so callers can always iterate the result and ``send`` each piece.
    """
    stream = data
    if tls:
        stream = b"".join(split_tls_records(data, chunk_size=tls_chunk))
    if tcp:
        return tcp_segment_at_sni(stream)
    return [stream]


def reassemble_tls_records(records: List[bytes]) -> bytes:
    """Inverse of :func:`split_tls_records` for the handshake body.

    Concatenates the *bodies* of consecutive same-type records back into a
    single record (used by tests to prove the split is lossless). Non-record
    trailing bytes are appended verbatim.
    """
    if not records:
        return b""
    bodies = b""
    content_type = version = None
    leftover = b""
    for rec in records:
        hdr = parse_record_header(rec)
        if hdr is None:
            leftover += rec
            continue
        ct, ver, blen = hdr
        if content_type is None:
            content_type, version = ct, ver
        bodies += rec[TLS_RECORD_HEADER_LEN:TLS_RECORD_HEADER_LEN + blen]
    rebuilt = (
        bytes([content_type]) + struct.pack("!H", version)
        + struct.pack("!H", len(bodies)) + bodies
    )
    return rebuilt + leftover
