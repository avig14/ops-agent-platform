"""Business clock. Only deadline/SLA logic reads this, so a demo can move time forward ("time travel") to trigger
reminders and escalations without waiting. The audit log keeps the REAL database time in `ts` and records the
simulated time in `sim_ts` (+ `clock_offset_active`), so time travel can never hide or reorder history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

offset_s: int = 0


def now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_s)


def set_offset(seconds: int) -> None:
    global offset_s
    offset_s = int(seconds)
