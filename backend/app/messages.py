"""Human actions on outgoing messages: retry a failed one, release (and optionally edit) a supplier draft, discard a draft.

Rules (each has a test):
  * only a manager, regional head or finance director may release or discard; a requester or the admin may not.
    Retry is also open to the admin (an ops action that changes no wording and sends nothing new).
  * lock order is the case row first, then the message row (same as the rest of the platform: no deadlocks with the audit lock).
  * external mail is released only while the plan is live; a cancelled or revised plan leaves no way to send its draft.
  * a manual retry grants five more attempts; `attempts` itself is never reset because it is the sender's fencing token.
  * every action, refusals included, is written to the case audit chain. An edit logs hashes and lengths, never the raw text.
"""
from __future__ import annotations

from . import audit, db
from .approvals import Denied
from .codes import Reason
from .schemas import Role, UserOut
from .sender import SENDABLE_CASE_STATES

RETRY_ROLES = (Role.manager, Role.regional_head, Role.finance_director, Role.admin)
REVIEW_ROLES = (Role.manager, Role.regional_head, Role.finance_director)
RETRYABLE = ("dead", "failed", "delivery_unknown")
EXTRA_ATTEMPTS = 5
MAX_SUBJECT, MAX_BODY = 200, 5000


async def load(msg_id: int) -> dict | None:
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM outbox WHERE id=%s", (msg_id,))).fetchone()


async def _act(user: UserOut, row: dict, action: str, roles: tuple, fn) -> dict:
    """Common frame: role check, then `fn(conn, case, msg)` inside one transaction with both rows locked.
    `fn` returns (result, audit_summary, diff) or raises Denied. Refusals are audited after the transaction ends."""
    cid = row["case_id"]
    denial: Denied | None = None
    result: dict = {}
    if user.role not in roles:
        denial = Denied(403, f"a {user.role.value} may not {action} a message", Reason.POLICY_UNAUTHORIZED)
    else:
        async with db.pool.connection() as c:
            async with c.transaction():
                case = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (cid,))).fetchone() if cid else None
                msg = await (await c.execute("SELECT * FROM outbox WHERE id=%s FOR UPDATE", (row["id"],))).fetchone()
                try:
                    result, summary, diff = await fn(c, case, msg)
                    await audit.write_in_txn(c, cid, "human", user.id, f"message_{action}", "success", summary,
                                             diff=diff, refs={"outbox_id": msg["id"], "kind": msg["kind"], "audience": msg["audience"]})
                except Denied as d:
                    denial = d
    if denial is not None:
        await audit.write(cid, "human", user.id, f"message_{action}", "blocked", f"{action} refused: {denial.detail}",
                          reason=denial.reason, refs={"outbox_id": row["id"], "role": user.role.value})
        raise denial
    return result


def _plan_is_live(case: dict | None) -> bool:
    return case is not None and case["status"] in SENDABLE_CASE_STATES


async def retry(user: UserOut, row: dict) -> dict:
    async def fn(c, case, msg):
        if msg["status"] not in RETRYABLE:
            raise Denied(409, f"message is {msg['status']}; only a failed, dead or unconfirmed message can be retried", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
        if msg["audience"] == "external" and not _plan_is_live(case):
            raise Denied(409, f"case is {case['status'] if case else 'unknown'}; an external message is not sent for it", Reason.WORKFLOW_CANCELLED)
        await c.execute("UPDATE outbox SET status='pending', next_attempt_at=now(), max_attempts=attempts + %s, lease_until=NULL WHERE id=%s",
                        (EXTRA_ATTEMPTS, msg["id"]))
        return ({"id": msg["id"], "status": "pending"},
                f"manual retry of a {msg['kind']} message that was {msg['status']}; {EXTRA_ATTEMPTS} more attempts allowed",
                {"status": {"before": msg["status"], "after": "pending"}})
    return await _act(user, row, "retry", RETRY_ROLES, fn)


async def release(user: UserOut, row: dict, subject: str | None, body: str | None) -> dict:
    async def fn(c, case, msg):
        if msg["audience"] != "external" or msg["status"] != "draft":
            raise Denied(409, "only a supplier draft waiting for review can be released", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
        if not _plan_is_live(case):
            raise Denied(409, f"case is {case['status'] if case else 'unknown'}; its supplier messages can no longer be sent", Reason.WORKFLOW_CANCELLED)
        new_subject = msg["subject"] if subject is None else subject.strip()
        new_body = msg["body"] if body is None else body.strip()
        if not new_subject or not new_body:
            raise Denied(422, "subject and body cannot be empty", Reason.DATA_MISSING)
        if len(new_subject) > MAX_SUBJECT or len(new_body) > MAX_BODY:
            raise Denied(422, f"subject up to {MAX_SUBJECT} and body up to {MAX_BODY} characters", Reason.DATA_MISSING)
        edited = (new_subject, new_body) != (msg["subject"], msg["body"])
        await c.execute("UPDATE outbox SET status='pending', reviewed_by=%s, reviewed_at=now(), edited=%s, subject=%s, body=%s, "
                        "next_attempt_at=now() WHERE id=%s",
                        (user.id, edited, new_subject, new_body, msg["id"]))
        diff = None
        if edited:
            diff = {"subject": {"before_sha": audit.sha256_text(msg["subject"] or "")[:16], "after_sha": audit.sha256_text(new_subject)[:16],
                                "before_len": len(msg["subject"] or ""), "after_len": len(new_subject)},
                    "body": {"before_sha": audit.sha256_text(msg["body"] or "")[:16], "after_sha": audit.sha256_text(new_body)[:16],
                             "before_len": len(msg["body"] or ""), "after_len": len(new_body)}}
        return ({"id": msg["id"], "status": "pending", "edited": edited},
                f"supplier message reviewed and released{' with edits' if edited else ''}", diff)
    return await _act(user, row, "release", REVIEW_ROLES, fn)


async def discard(user: UserOut, row: dict, reason: str | None) -> dict:
    async def fn(c, case, msg):
        if msg["status"] != "draft":
            raise Denied(409, f"message is {msg['status']}; only a draft can be discarded", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
        why = (reason or "").strip()[:300] or "discarded by reviewer"
        await c.execute("UPDATE outbox SET status='cancelled', last_error=%s, reviewed_by=%s, reviewed_at=now() WHERE id=%s", (why, user.id, msg["id"]))
        return ({"id": msg["id"], "status": "cancelled"}, f"draft discarded by the reviewer: {why}", {"status": {"before": "draft", "after": "cancelled"}})
    return await _act(user, row, "discard", REVIEW_ROLES, fn)
