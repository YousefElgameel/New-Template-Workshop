"""A cap on how many asks run at once in this process (api 4.2, threat model G1/G2).

The chat endpoint has no login yet (ADR-0014), and every ask spends shared model and search quota.
A per-request rate limit belongs at the BFF or the ingress; what the api can do on its own is to
refuse to run more than ``AI_MAX_CONCURRENT_ASKS`` asks at the same time, so one client cannot
open an unbounded number of model calls.

``ask_slot`` is a context manager around a counter — no awaits, so it is atomic on the event loop —
and the counter is decremented in ``finally``, which also runs when the request is cancelled (a
client that hangs up) or fails. The count is per process: with more than one replica each has its
own cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class NoFreeSlot(Exception):
    """Every slot is in use; the caller answers ``429 busy``."""


class _Slots:
    def __init__(self) -> None:
        self.in_use = 0


_slots = _Slots()


@contextmanager
def ask_slot(limit: int) -> Iterator[None]:
    """Hold one of ``limit`` slots for the duration of the block, or raise ``NoFreeSlot``."""
    if _slots.in_use >= limit:
        raise NoFreeSlot
    _slots.in_use += 1
    try:
        yield
    finally:
        _slots.in_use -= 1


def slots_in_use() -> int:
    return _slots.in_use


def _reset_for_tests() -> None:
    _slots.in_use = 0
