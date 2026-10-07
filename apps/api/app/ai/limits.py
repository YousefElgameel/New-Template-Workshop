"""A cap on how many asks run at once in this process (api 4.2/4.3, threat model G1/G2).

The chat endpoints have no login yet (ADR-0014), and every ask spends shared model and search quota.
A per-request rate limit belongs at the BFF or the ingress; what the api can do on its own is to
refuse to run more than ``AI_MAX_CONCURRENT_ASKS`` asks at the same time, so one client cannot
open an unbounded number of model calls.

Two ways to hold a slot:

- ``ask_slot`` — a context manager, for a call that finishes inside the request (``/ask``);
- ``take_slot`` — returns an idempotent ``release`` function, for a stream that outlives the route
  function (``/ask/stream``): the response calls it when it ends (``app.ai.streaming.SseResponse``).

Both are a counter with no awaits between the check and the increment, so they are atomic on the
event loop, and the decrement happens when the slot is released — also when the request is cancelled
(a client that hangs up) or fails. The count is per process: with more than one replica each has its
own cap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager


class NoFreeSlot(Exception):
    """Every slot is in use; the caller answers ``429 busy``."""


class _Slots:
    def __init__(self) -> None:
        self.in_use = 0


_slots = _Slots()


def take_slot(limit: int) -> Callable[[], None]:
    """Take one of ``limit`` slots and return the function that gives it back (safe to call twice),
    or raise ``NoFreeSlot``."""
    if _slots.in_use >= limit:
        raise NoFreeSlot
    _slots.in_use += 1
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            _slots.in_use -= 1

    return release


@contextmanager
def ask_slot(limit: int) -> Iterator[None]:
    """Hold one of ``limit`` slots for the duration of the block, or raise ``NoFreeSlot``."""
    release = take_slot(limit)
    try:
        yield
    finally:
        release()


def slots_in_use() -> int:
    return _slots.in_use


def _reset_for_tests() -> None:
    _slots.in_use = 0
