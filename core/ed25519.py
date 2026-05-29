"""Minimal, dependency-free Ed25519 (RFC 8032) — *verification only*.

The remote-strategies feature (step 11) must verify a detached signature on the
downloaded ``strategies.json`` so a hijacked mirror cannot feed us malicious
recipes. We deliberately avoid pulling in ``cryptography`` / ``PyNaCl``:

* the app ships as a single PyInstaller exe — every extra native wheel bloats it
  and complicates the build;
* we only ever *verify* (the private signing key lives offline with the
  maintainer), so we need a tiny, auditable, pure-Python verifier — not a full
  crypto suite.

This is the standard textbook Ed25519 over Curve25519 / SHA-512, sufficient for
signature verification. It is **not** constant-time and must not be used for
secret-key operations; for verifying public, already-published data that is
fine. The implementation follows RFC 8032 §5.1.

Public API:
    verify(public_key: bytes, message: bytes, signature: bytes) -> bool
"""
from __future__ import annotations

import hashlib

# Curve / field constants (RFC 8032, edwards25519)
_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = (-121665 * pow(121666, _p - 2, _p)) % _p
_I = pow(2, (_p - 1) // 4, _p)            # sqrt(-1)


def _sha512(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()


def _sha512_int(b: bytes) -> int:
    return int.from_bytes(_sha512(b), "little")


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


# Base point B
_By = (4 * _inv(5)) % _p


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_Bx = _x_recover(_By)
_B = (_Bx % _p, _By % _p, 1, (_Bx * _By) % _p)   # extended coords (X,Y,Z,T)


def _edwards_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = ((y1 - x1) * (y2 - x2)) % _p
    b = ((y1 + x1) * (y2 + x2)) % _p
    c = (t1 * 2 * _d * t2) % _p
    dd = (z1 * 2 * z2) % _p
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    x3 = (e * f) % _p
    y3 = (g * h) % _p
    t3 = (e * h) % _p
    z3 = (f * g) % _p
    return (x3, y3, z3, t3)


def _scalarmult(P, e: int):
    Q = (0, 1, 1, 0)   # neutral element
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _encode_point(P) -> bytes:
    x, y, z, _t = P
    zi = _inv(z)
    x = (x * zi) % _p
    y = (y * zi) % _p
    val = y | ((x & 1) << 255)
    return val.to_bytes(32, "little")


def _decode_point(s: bytes):
    if len(s) != 32:
        raise ValueError("point must be 32 bytes")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _p:
        raise ValueError("y out of range")
    x = _x_recover(y)
    if x & 1 != sign:
        x = _p - x
    P = (x, y, 1, (x * y) % _p)
    return P


def _is_on_curve(P) -> bool:
    x, y, z, t = P
    zi = _inv(z)
    x = (x * zi) % _p
    y = (y * zi) % _p
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _p == 0


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True iff *signature* is a valid Ed25519 sig over *message*.

    Never raises on malformed input — returns False instead, so callers can
    treat any failure (bad key, bad sig, tampering) uniformly as "untrusted".
    """
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        A = _decode_point(public_key)
        if not _is_on_curve(A):
            return False
        Rs = signature[:32]
        R = _decode_point(Rs)
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        h = _sha512_int(Rs + public_key + message) % _L
        sB = _scalarmult(_B, s)
        hA = _scalarmult(A, h)
        rhs = _edwards_add(R, hA)
        return _encode_point(sB) == _encode_point(rhs)
    except Exception:
        return False
