"""Capture audit writes in memory while an eval runs, so evals never write to (or need) the real audit chain."""
from __future__ import annotations

from contextlib import contextmanager

from .. import audit


class AuditRecorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def write(self, chain_key, actor_type, actor_id, action, outcome, summary, **kw):
        self.rows.append({"chain": chain_key, "actor_type": actor_type, "actor": actor_id, "action": action,
                          "outcome": outcome, "reason": getattr(kw.get("reason"), "value", kw.get("reason"))})
        return None

    def count(self, action: str) -> int:
        return sum(1 for r in self.rows if r["action"] == action)


@contextmanager
def capture_audit():
    """Swap audit.write for a recorder for the duration of the block, then restore it (even if the block raises)."""
    rec = AuditRecorder()
    original = audit.write
    audit.write = rec.write
    try:
        yield rec
    finally:
        audit.write = original
