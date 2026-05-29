"""WrongSeq strategy — the original (and currently only live) technique.

Extracted verbatim from ``fake_tcp.FakeTcpInjector.fake_send_thread``::

    packet.tcp.seq_num = (connection.syn_seq + 1 - len(payload)) & 0xffffffff

The fake segment is given a sequence number that lands *before* the connection
window, so a stateful DPI accepts/records it (and the SNI it claims) while the
genuine server silently drops it as old/duplicate. The real ClientHello that
follows then carries the spoofed SNI through a DPI that has already been
desynchronised.
"""
from __future__ import annotations

from typing import Any

from strategies.base import BypassStrategy, StrategyMeta, register


@register
class WrongSeqStrategy(BypassStrategy):
    meta = StrategyMeta(
        key="wrong_seq",
        title="Wrong Sequence",
        description="تزریق ClientHello جعلی با seq خارج از پنجره",
        implemented=True,
        tags=("inject", "stateful-dpi"),
    )

    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        # mark as data push and append the fake payload
        packet.tcp.psh = True
        packet.ip.packet_len = packet.ip.packet_len + len(connection.fake_data)
        packet.tcp.payload = connection.fake_data
        if getattr(packet, "ipv4", None) is not None:
            packet.ipv4.ident = (packet.ipv4.ident + 1) & 0xFFFF
        # the defining move: a sequence number before the window
        packet.tcp.seq_num = (
            connection.syn_seq + 1 - len(packet.tcp.payload)) & 0xFFFFFFFF

    def score(self) -> float:
        # the proven default; ranked first until the prober learns otherwise
        return 0.8
