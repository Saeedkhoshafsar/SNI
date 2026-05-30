"""FakeTTL strategy — send the fake ClientHello with a tiny IP TTL.

The bogus segment is given a TTL just large enough to reach the DPI box on the
operator's network but too small to reach the real server: an intermediate
router decrements the TTL to zero and discards it. The DPI therefore inspects
and records the spoofed SNI, while the genuine endpoint never receives the
fake. The subsequent real ClientHello (normal TTL) reaches the server through
the now-desynchronised DPI.

The right TTL depends on how many hops away the DPI sits; a small default is
used and can be tuned per-connection via ``connection.fake_ttl`` if present.
For IPv6 the field is ``hop_limit`` rather than ``ttl``.
"""
from __future__ import annotations

from typing import Any

from strategies.base import BypassStrategy, StrategyMeta, register


@register
class FakeTTLStrategy(BypassStrategy):
    meta = StrategyMeta(
        key="fake_ttl",
        title="Fake TTL",
        description="TTL کوتاه تا بسته‌ی جعلی فقط به DPI برسد نه به سرور",
        implemented=True,
        tags=("inject", "ttl", "hop-limited"),
    )

    # The fake is engineered to die in transit (TTL → 0 at an intermediate
    # router) so the genuine server NEVER receives it → it never ACKs. The
    # spoofer must therefore treat this as fire-and-forget and not block
    # waiting for an acknowledgement that will never arrive.
    expects_ack = False

    DEFAULT_TTL = 4  # hops to the censor's DPI; tunable per connection

    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        self.apply_fake_payload(packet, connection)
        # in-window seq: the server would accept it if it ever arrived, but it
        # won't — the low TTL kills it in transit.
        packet.tcp.seq_num = (connection.syn_seq + 1) & 0xFFFFFFFF
        ttl = getattr(connection, "fake_ttl", None) or self.DEFAULT_TTL
        # IPv4 uses ttl; IPv6 uses hop_limit. Set whichever the packet exposes.
        if getattr(packet, "ipv6", None) is not None:
            packet.ipv6.hop_limit = ttl
        else:
            packet.ip.ttl = ttl

    def score(self) -> float:
        return 0.55
