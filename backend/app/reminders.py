"""Reminders for cases waiting on the requester (NEEDS_INFO).

Clock starts when the clarification was queued. At 24 h and 48 h the requester gets a reminder that repeats the open
questions; at 72 h a manager is told and the case is flagged for a human. Nothing is guessed and no status changes:
silence never turns into an answer. Every message has an idempotency key tied to the clarification it belongs to, so
a restart, a second worker or a repeated sweep never sends one twice. A newer clarification starts a fresh clock.
Time is read from `clock.now()` so the demo's time-travel can trigger these.
"""
from __future__ import annotations

from . import audit, clock, db, outbox
from .codes import Reason

REMIND_AFTER_S = (24 * 3600, 48 * 3600)
ESCALATE_AFTER_S = 72 * 3600


async def sweep(case_id: str | None = None) -> dict:
    res = {"reminded": 0, "escalated": 0, "errors": 0}
    try:
        async with db.pool.connection() as c:
            rows = await (await c.execute(
                "SELECT s.id, o.id AS clar_id, o.created_at AS asked_at, o.body AS asked, o.subject AS asked_subject "
                "FROM cases s JOIN LATERAL (SELECT id, created_at, body, subject FROM outbox WHERE case_id = s.id AND kind = 'clarification' "
                "ORDER BY id DESC LIMIT 1) o ON true "
                "WHERE s.status = 'NEEDS_INFO' AND (%s::text IS NULL OR s.id = %s) ORDER BY o.created_at LIMIT 200",
                (case_id, case_id))).fetchall()
    except Exception as exc:  # noqa: BLE001
        await audit.write(None, "system", "reminder-sweeper", "reminder_sweep", "failure", f"could not list waiting cases: {type(exc).__name__}",
                          severity="error", error=audit.error_obj(type(exc).__name__, "db", None, audit.trace_id_var.get(), str(exc)))
        res["errors"] += 1
        return res
    now = clock.now()
    for r in rows:
        age = (now - r["asked_at"]).total_seconds()
        if age < REMIND_AFTER_S[0]:
            continue
        try:
            await _one(r, age, res)
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the others
            res["errors"] += 1
            await audit.write(r["id"], "system", "reminder-sweeper", "reminder_sweep", "failure", f"reminder failed: {type(exc).__name__}",
                              severity="error", error=audit.error_obj(type(exc).__name__, "reminder-sweeper", None, audit.trace_id_var.get(), str(exc)))
    return res


async def _one(r: dict, age: float, res: dict) -> None:
    cid, clar = r["id"], r["clar_id"]
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (cid,))).fetchone()
            if case["status"] != "NEEDS_INFO":         # answered while we were looking
                return
            for i, after in enumerate(REMIND_AFTER_S, start=1):
                if age < after:
                    continue
                n = await outbox.notify_user(
                    c, cid, "reminder", f"rem-{cid}-{clar}-{i}", f"Reminder: we still need details for {cid}",
                    f"Reminder {i}. We are still waiting for your reply about {cid}.\n\nOur earlier message:\n\n{r['asked']}", case["requester_id"])
                if n:
                    res["reminded"] += 1
                    await audit.write_in_txn(c, cid, "system", "reminder-sweeper", "reminder_sent", "success",
                                             f"reminder {i} sent to the requester ({int(age // 3600)} h without a reply)",
                                             refs={"clarification_outbox_id": clar, "reminder": i})
            if age >= ESCALATE_AFTER_S:
                n = await outbox.notify_role(
                    c, case, "needs_info_escalation", f"nie-{cid}-{clar}", f"No reply yet on {cid}",
                    f"{cid} - {case['title']} has been waiting for the requester's reply for over 72 hours. Please follow up.", "manager")
                flagged = (await c.execute("UPDATE cases SET needs_human=true, updated_at=now() WHERE id=%s AND status='NEEDS_INFO' AND NOT needs_human",
                                           (cid,))).rowcount
                if n or flagged:
                    res["escalated"] += 1
                    await audit.write_in_txn(c, cid, "system", "reminder-sweeper", "needs_info_escalated", "success",
                                             f"no reply after {int(age // 3600)} h; {n} manager(s) told, case flagged for a human",
                                             reason=Reason.WORKFLOW_DEADLINE_MISSED, refs={"clarification_outbox_id": clar})
