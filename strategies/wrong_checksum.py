"""WrongChecksum strategy — corrupt the fake segment's TCP checksum.

The fake ClientHello is sent with a deliberately *invalid* TCP checksum. A
stateful DPI box typically does **not** verify L4 checksums (it just inspects
the payload and records the SNI), so it accepts the bogus segment — but the
genuine server's NIC/stack drops it as corrupt. The real ClientHello that
follows then sails through a DPI that has already been desynchronised, while
the endpoint never saw the spoofed SNI.

Implementation detail: WinDivert's ``send(packet, recalc)`` recomputes the
checksum when ``recalc=True``. To keep our bad checksum we **must** send with
``recalc=False`` — otherwise WinDivert would "fix" it back to a valid value.
"""
from __future__ import annotations

from typing import Any

from strategies.base import BypassStrategy, StrategyMeta, register


@register
class WrongChecksumStrategy(BypassStrategy):
    meta = StrategyMeta(
        key="wrong_checksum",
        title="Wrong Checksum",
        description="چک‌سام نامعتبر تا سرور دور بریزد ولی DPI پردازش کند",
        implemented=True,
        tags=("inject", "stateless-dpi", "no-recalc"),
    )

    # The server's NIC/stack drops the corrupt segment, so it NEVER ACKs the
    # fake → fire-and-forget. The spoofer must not block waiting for an ACK.
    expects_ack = False

    # an obviously-wrong constant; any non-correct value works because the
    # server drops it and the DPI ignores L4 checksums.
    BAD_CHECKSUM = 0x0000

    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        self.apply_fake_payload(packet, connection)
        # keep a plausible in-window seq so only the checksum is "wrong"
        packet.tcp.seq_num = (connection.syn_seq + 1) & 0xFFFFFFFF
        packet.tcp.checksum = self.BAD_CHECKSUM

    def send_fake(self, injector: Any, packet: Any, connection: Any) -> None:
        # CRITICAL: recalc=False so WinDivert does NOT repair the checksum.
        connection.fake_sent = True
        injector.w.send(packet, False)

    def score(self) -> float:
        return 0.6
