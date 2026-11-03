"""Outbox: every message the platform wants to send is a row here first (sandbox mailbox in this build).
`message_id` is the idempotency key, so a retried or replayed step never sends the same notice twice.
`sender.py` delivers these rows (claim, retry with backoff, dead letter); `comms.py` writes the drafts.
"""
from __future__ import annotations

VOIDABLE_KINDS = ("supplier_request", "stakeholder_update")      # messages that describe a plan; they die with the plan


async def recipients(c, role: str, region: str | None) -> list[dict]:
    return await (await c.execute(
        "SELECT id,email,name FROM users WHERE role=%s AND active AND (%s::text IS NULL OR region IS NULL OR region=%s) ORDER BY id",
        (role, region, region))).fetchall()


async def queue(c, case_id: str | None, kind: str, recipient: str, subject: str, body: str, key: str, *,
                audience: str = "internal", status: str = "pending", source: str = "template") -> int:
    """Insert one message. Returns 1 if queued, 0 if this key was already queued.
    External messages must be inserted as 'draft': the database refuses to let them leave without a human reviewer."""
    r = await c.execute("INSERT INTO outbox(case_id,kind,recipient,subject,body,message_id,audience,status,source) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (message_id) DO NOTHING",
                        (case_id, kind, recipient, subject, body, key, audience, status, source))
    return r.rowcount


async def void_plan_messages(c, case_id: str, why: str) -> int:
    """The plan these messages describe is gone (case cancelled or revised): nothing unsent may leave.
    A message already being sent cannot be recalled; it is left alone."""
    r = await c.execute("UPDATE outbox SET status='cancelled', last_error=%s, lease_until=NULL WHERE case_id=%s AND kind = ANY(%s) "
                        "AND status IN ('draft','pending','failed','delivery_unknown')", (why, case_id, list(VOIDABLE_KINDS)))
    return r.rowcount


async def notify_role(c, case: dict, kind: str, key: str, subject: str, body: str, role: str) -> int:
    n = 0
    for u in await recipients(c, role, case["region"]):
        n += await queue(c, case["id"], kind, u["email"], subject, body, f"{key}-{u['id']}")
    return n


async def notify_user(c, case_id: str, kind: str, key: str, subject: str, body: str, user_id: str) -> int:
    u = await (await c.execute("SELECT email FROM users WHERE id=%s AND active", (user_id,))).fetchone()
    return await queue(c, case_id, kind, u["email"], subject, body, f"{key}-{user_id}") if u else 0
