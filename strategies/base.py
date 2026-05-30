"""BypassStrategy interface, metadata, and the strategy registry.

A *strategy* is a packet-mutation technique used to slip a "fake" TCP segment
past a DPI box so the real ClientHello (carrying the spoofed SNI) is treated as
part of an already-established / ignored flow. Each strategy is a tiny class
that knows how to mutate the fake packet just before it's injected, and carries
human-facing metadata for the UI.

Design goals
------------
* **Zero hard dependency on pydivert at import time** — so the registry can be
  enumerated, unit-tested and shown in the UI on any OS (the sandbox has no
  WinDivert). Strategies receive a duck-typed ``packet`` / ``connection`` and
  only touch attributes that exist at runtime on Windows.
* **Self-registering** — subclasses call :func:`register` (or use the
  ``@register`` decorator) so adding a file under ``strategies/`` is enough.
* **Probe hook** — :meth:`BypassStrategy.score` lets the future auto-prober
  rank techniques; the default is neutral so non-probing strategies still work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class StrategyMeta:
    """Human-facing description of a strategy (shown in the UI)."""

    key: str                      # stable id, e.g. "wrong_seq"
    title: str                    # display name, e.g. "Wrong Sequence"
    description: str              # one-line Farsi explanation
    implemented: bool = True      # False == placeholder/coming-soon
    tags: tuple[str, ...] = field(default_factory=tuple)


class BypassStrategy:
    """Base class for a single DPI-bypass technique.

    Subclasses must set :pyattr:`meta` and override
    :meth:`mutate_fake_packet`. The mutation is applied to the *fake* segment
    that is injected before the real ClientHello; it should set sequence /
    checksum / TTL / payload fields such that the DPI is desynchronised while
    the genuine endpoint still ignores the bogus segment.
    """

    meta: StrategyMeta = StrategyMeta(
        key="base", title="Base", description="(abstract)", implemented=False)

    # -- handshake contract -----------------------------------------------
    # Whether the *server* is expected to acknowledge the injected fake
    # segment.  ``True`` (the default) describes the classic out-of-window
    # techniques (wrong_seq / multi_fake / fake_disorder): the server still
    # receives the bogus segment, treats it as old/duplicate data and replies
    # with a *duplicate ACK*.  The spoofer waits for that ACK
    # (``fake_data_ack_recv``) as positive confirmation before relaying the
    # real ClientHello.
    #
    # ``False`` would describe a "fire-and-forget" technique whose fake the
    # genuine server **never** receives (dies in transit / dropped as corrupt),
    # so no ACK ever returns. The early ``fake_ttl`` / ``wrong_checksum``
    # experiments were of this kind but have been removed: because no ACK comes
    # back the spoofer could never *confirm* the decoy reached the DPI box,
    # which made them unreliable. The flag is kept as an extension point for any
    # future technique, and the spoofer still honours it.
    expects_ack: bool = True

    # -- shared building block --------------------------------------------
    @staticmethod
    def apply_fake_payload(packet: Any, connection: Any) -> None:
        """Common mutation shared by every fake-injection technique.

        Turns the cloned ACK into a PSH segment carrying ``connection.fake_data``
        (the bogus 517-byte ClientHello), grows the IP length accordingly, and
        bumps the IPv4 identification field so the fake looks like a fresh
        datagram. This is exactly the prologue that the original code ran for
        *every* method before the wrong-seq tweak; strategies layer their own
        distinguishing mutation (seq / checksum / TTL / ordering) on top.
        """
        packet.tcp.psh = True
        packet.ip.packet_len = packet.ip.packet_len + len(connection.fake_data)
        packet.tcp.payload = connection.fake_data
        if getattr(packet, "ipv4", None) is not None:
            packet.ipv4.ident = (packet.ipv4.ident + 1) & 0xFFFF

    # -- core hook --------------------------------------------------------
    def mutate_fake_packet(self, packet: Any, connection: Any) -> None:
        """Mutate *packet* in place for injection. Override in subclasses.

        ``packet``      — a pydivert.Packet-like object (duck-typed).
        ``connection``  — the FakeInjectiveConnection carrying syn_seq etc.
        """
        raise NotImplementedError(
            f"strategy '{self.meta.key}' has no mutate_fake_packet()")

    # -- send hook (default: standard injected send) ----------------------
    def send_fake(self, injector: Any, packet: Any, connection: Any) -> None:
        """Send the mutated fake packet. Default = single injected send.

        Strategies that need multiple sends or reordering (multi_fake,
        fake_disorder) override this. ``injector.w.send(packet, recalc)`` is the
        WinDivert send primitive; ``recalc=True`` recomputes checksums.
        """
        connection.fake_sent = True
        injector.w.send(packet, True)

    # -- probe hook (default: neutral) ------------------------------------
    def score(self) -> float:
        """Return a static suitability score (0..1). Higher == preferred.

        The runtime auto-prober (later step) measures real success; this is
        only a static prior used to order candidates before probing.
        """
        return 0.5

    # -- convenience ------------------------------------------------------
    @property
    def key(self) -> str:
        return self.meta.key

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Strategy {self.meta.key} implemented={self.meta.implemented}>"


# ---------------------------------------------------------------------------
#  Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, BypassStrategy] = {}


def register(cls: type[BypassStrategy]) -> type[BypassStrategy]:
    """Class decorator: instantiate and register a strategy by its meta.key."""
    instance = cls()
    key = instance.meta.key
    if key in REGISTRY:
        raise ValueError(f"strategy key already registered: {key}")
    REGISTRY[key] = instance
    return cls


def get_strategy(key: str) -> BypassStrategy:
    """Return the registered strategy for *key* or raise KeyError."""
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise KeyError(
            f"استراتژی ناشناخته: {key!r} (موجود: {', '.join(sorted(REGISTRY))})"
        ) from exc


def all_strategies(*, implemented_only: bool = False) -> list[BypassStrategy]:
    """Return all registered strategies (optionally only implemented ones)."""
    items = list(REGISTRY.values())
    if implemented_only:
        items = [s for s in items if s.meta.implemented]
    return items
