#!/usr/bin/env python3
"""Maintainer tool: generate an Ed25519 keypair and sign ``strategies.json``.

The app only *verifies* (pure-Python, no crypto deps). Signing is an offline,
maintainer-only step, so this helper carries its own tiny RFC 8032 signer built
on the verifier's field math — no third-party dependency required.

Usage
-----
Generate a keypair (prints hex public key to embed in
``core.strategies_remote.TRUSTED_PUBLIC_KEY_HEX``; writes the 32-byte seed to
``signing_seed.bin`` — keep this OFFLINE and secret)::

    python tools/sign_strategies.py keygen

Sign a manifest (writes ``<file>.sig`` next to it; verifies before exit)::

    python tools/sign_strategies.py sign strategies.json --seed signing_seed.bin
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ed25519
from core.strategies_remote import canonical_bytes


def _public_from_seed(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return ed25519._encode_point(ed25519._scalarmult(ed25519._B, a))


def _sign(seed: bytes, msg: bytes) -> bytes:
    L, B = ed25519._L, ed25519._B
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    A = _public_from_seed(seed)
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % L
    R = ed25519._encode_point(ed25519._scalarmult(B, r))
    k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % L
    s = (r + k * a) % L
    return R + s.to_bytes(32, "little")


def cmd_keygen(args: argparse.Namespace) -> int:
    seed = os.urandom(32)
    with open(args.out, "wb") as fh:
        fh.write(seed)
    pub = _public_from_seed(seed)
    print(f"seed written to {args.out} (KEEP OFFLINE/SECRET)")
    print(f"TRUSTED_PUBLIC_KEY_HEX = \"{pub.hex()}\"")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    with open(args.seed, "rb") as fh:
        seed = fh.read()
    if len(seed) != 32:
        print("seed must be exactly 32 bytes", file=sys.stderr)
        return 2
    with open(args.manifest, "rb") as fh:
        raw = fh.read()
    msg = canonical_bytes(raw)
    sig = _sign(seed, msg)
    pub = _public_from_seed(seed)
    if not ed25519.verify(pub, msg, sig):
        print("self-verify FAILED — refusing to write", file=sys.stderr)
        return 3
    out = args.manifest + ".sig"
    with open(out, "wb") as fh:
        fh.write(sig)
    print(f"signature written to {out} ({len(sig)} bytes); self-verify OK")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("keygen", help="generate a new signing keypair")
    g.add_argument("--out", default="signing_seed.bin")
    g.set_defaults(func=cmd_keygen)

    s = sub.add_parser("sign", help="sign a strategies.json manifest")
    s.add_argument("manifest")
    s.add_argument("--seed", default="signing_seed.bin")
    s.set_defaults(func=cmd_sign)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
