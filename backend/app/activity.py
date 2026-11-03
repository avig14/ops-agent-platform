"""Idle-aware pacing for the background loops.

Why: a hosted free Postgres sleeps after 5 idle minutes. Loops that query every 5 s would keep it awake all day and use up
its monthly compute. So the loops tick fast while someone is using the app and slowly when nobody is. Any /api request
wakes them at once. Deadlines are also checked on every case read, so a long idle nap never hides an overdue approval.
"""
from __future__ import annotations

import asyncio
import time

BUSY_WINDOW_S = 180.0

_last: float = float("-inf")
_event: asyncio.Event | None = None


def touch() -> None:
    """Call on every user-facing request."""
    global _last
    _last = time.monotonic()
    if _event is not None:
        _event.set()


def busy() -> bool:
    return time.monotonic() - _last < BUSY_WINDOW_S


async def nap(active_s: float, idle_s: float) -> None:
    """Sleep `active_s` while the app is in use; otherwise up to `idle_s`, woken early by the next request."""
    global _event
    if idle_s <= active_s or busy():
        await asyncio.sleep(active_s)
        return
    if _event is None:
        _event = asyncio.Event()
    _event.clear()
    try:
        await asyncio.wait_for(_event.wait(), idle_s)
    except asyncio.TimeoutError:
        pass
