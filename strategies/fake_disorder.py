"""FakeDisorder strategy — inject the fake twice with deliberate disorder.

Instead of a single clean fake send, this technique emits the bogus segment,
then a second copy carrying a slightly different (still out-of-window) sequence
number. The intentional reordering/duplication confuses DPI reassembly logic
that tries to stitch a coherent stream: it may latch onto the spoofed SNI in
one of the disordered copies, while the real server discards both as old data.

Like wrong_seq / multi_fake the server never accepts the fakes (their sequence
numbers fall before the window); only the censor's reassembler is disturbed.
"""
from __future__ import annotations

from typing import Any

from strategies.base import BypassStrategy, StrategyMeta, register


@register
class FakeDisorderStrategy(BypassStrategy):
    meta = StrategyMeta(
        key="fake_disorder",
        title="Fake Disorder",
        description="تزریق نامرتب بسته‌های جعلی برای مختل‌کردن بازچینی DPI",
        implemented=True,
        tags=("inject", "disorder", "reassembly"),
    )

    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        self.apply_fake_payload(packet, connection)
        # first copy: the classic out-of-window seq
        packet.tcp.seq_num = (
            connection.syn_seq + 1 - len(packet.tcp.payload)) & 0xFFFFFFFF

    def send_fake(self, injector: Any, packet: Any, connection: Any) -> None:
        connection.fake_sent = True
        # copy #1 — as mutated (seq just before window)
        injector.w.send(packet, True)
        # copy #2 — nudge the seq further back so the two arrive "out of order"
        payload_len = len(packet.tcp.payload)
        packet.tcp.seq_num = (
            connection.syn_seq + 1 - 2 * payload_len) & 0xFFFFFFFF
        injector.w.send(packet, True)

    def score(self) -> float:
        return 0.6
