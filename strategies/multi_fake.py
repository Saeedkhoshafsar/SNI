"""MultiFake strategy — inject several wrong-seq fakes back-to-back.

Some DPI boxes only lock onto a flow after they have seen a couple of segments,
or they keep re-evaluating. Sending the bogus ClientHello several times (each
with the out-of-window sequence number so the server keeps ignoring them)
raises the odds that the censor records the spoofed SNI while the real endpoint
discards every copy. It combines the proven wrong-seq mutation with a repeated
send loop.
"""
from __future__ import annotations

from typing import Any

from strategies.base import BypassStrategy, StrategyMeta, register


@register
class MultiFakeStrategy(BypassStrategy):
    meta = StrategyMeta(
        key="multi_fake",
        title="Multi Fake",
        description="ارسال چندباره‌ی بسته‌ی جعلی با seq خارج از پنجره",
        implemented=True,
        tags=("inject", "repeat", "stateful-dpi"),
    )

    REPEAT = 3  # number of fake copies to emit

    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        # same defining mutation as wrong_seq; the repetition happens in send.
        self.apply_fake_payload(packet, connection)
        packet.tcp.seq_num = (
            connection.syn_seq + 1 - len(packet.tcp.payload)) & 0xFFFFFFFF

    def send_fake(self, injector: Any, packet: Any, connection: Any) -> None:
        repeat = getattr(connection, "fake_repeat", None) or self.REPEAT
        connection.fake_sent = True
        for _ in range(max(1, int(repeat))):
            injector.w.send(packet, True)

    def score(self) -> float:
        return 0.65
