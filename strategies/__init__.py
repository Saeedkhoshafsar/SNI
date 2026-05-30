"""Modular DPI-bypass strategy engine.

The original tool hard-coded a single ``wrong_seq`` injection technique inside
``fake_tcp.FakeTcpInjector.fake_send_thread`` (everything else called
``sys.exit("not implemented")``). This package extracts that into a clean,
extensible :class:`~strategies.base.BypassStrategy` interface plus a registry,
so new techniques (multi_fake, fake_disorder, fragmentation, …) can be added as
small self-contained classes and selected by name at runtime — and later
auto-probed (the "final boss" auto-prober).

Note: the early *fire-and-forget* experiments ``fake_ttl`` and
``wrong_checksum`` were removed — by design the genuine server never ACKs them,
so the spoofer could never *confirm* the decoy actually reached the DPI box,
which made them unreliable in practice. Only ACK-confirmed techniques remain.

Public API::

    from strategies import get_strategy, all_strategies, REGISTRY

    strat = get_strategy("wrong_seq")
    strat.mutate_fake_packet(packet, connection)   # applied on the fake send
"""
from strategies.base import (
    BypassStrategy, StrategyMeta, register, get_strategy, all_strategies,
    REGISTRY,
)

# Importing the implementations registers them as a side effect.
from strategies import wrong_seq as _wrong_seq  # noqa: F401
from strategies import multi_fake as _multi_fake  # noqa: F401
from strategies import fake_disorder as _fake_disorder  # noqa: F401

__all__ = [
    "BypassStrategy", "StrategyMeta", "register", "get_strategy",
    "all_strategies", "REGISTRY",
]
