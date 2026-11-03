"""Execution tracking and closure: carry an approved plan to the end, and close it only when it is really done.

  start    APPROVED  -> EXECUTING        work on the approved plan has begun
  monitor  EXECUTING -> MONITORING       the order is placed; waiting for delivery
  close    APPROVED / EXECUTING / MONITORING -> CLOSED, only when the closure checklist is complete
  reopen   CLOSED    -> MONITORING       the problem came back; a reason is required and the manual checklist starts fresh

Checklist = items the platform works out from its own records (it cannot be ticked by hand) + items a person confirms:
  derived: plan approved; stakeholders informed (the update was really delivered); supplier contacted (when the plan has a
           supplier message: delivered, or discarded by a reviewer); nothing for this plan is still a draft, failed or dead.
  manual : resolution confirmed (goods received / shortfall solved); spend recorded (only when the plan costs money; finance only).
Rules (each has a test):
  * only a manager, regional head or finance director who can see the case's region may act. A requester, the admin and the
    AI identity may not, and a refusal is audited as POLICY_UNAUTHORIZED.
  * lock order is the case row first (same as everywhere); the status change is the CAS `transition`.
  * a refusal is audited after the business txn has ended (the audit chain lock rule).
  * a reopened case gets a new epoch (`cases.reopen_count`): yesterday's confirmations never close today's problem.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import audit, db, outbox
from .approvals import Denied
from .changes import is_approver
from .codes import Reason
from .orchestrator import Conflict, agent_message, transition
from .schemas import Role, UserOut

LIVE = ("APPROVED", "EXECUTING", "MONITORING")
UNRESOLVED = ("draft", "pending", "sending", "failed", "delivery_unknown", "dead")
MANUAL_KEYS = ("resolution_confirmed", "spend_recorded")
LABELS = {
    "plan_approved": "Plan approved by the right people",
    "stakeholders_informed": "Stakeholders informed (update delivered)",
    "supplier_contacted": "Supplier contacted (message delivered or handled by a reviewer)",
    "resolution_confirmed": "Resolution confirmed (goods received or shortfall solved)",
    "spend_recorded": "Spend recorded by finance",
}
MAX_NOTE = 500
MIN_REOPEN_REASON = 3


# ───────────────────────── checklist ─────────────────────────
async def _plan(c, case_id: str) -> dict | None:
    return await (await c.execute("SELECT id, total_cost, chosen_option FROM recommendations WHERE case_id=%s AND status='approved' "
                                  "ORDER BY id DESC LIMIT 1", (case_id,))).fetchone()


def _item(key: str, *, kind: str, required: bool, done: bool, detail: str, row: dict | None = None, label: str | None = None) -> dict:
    return {"key": key, "label": label or LABELS[key], "kind": kind, "required": required, "done": done, "detail": detail,
            "by": row["done_by"] if row else None, "by_name": row["done_by_name"] if row else None,
            "at": row["done_at"] if row else None, "note": row["note"] if row else None}


async def checklist(c, case: dict) -> dict:
    """Work out the closure checklist from the database. `c` is any connection (use the locked txn when closing)."""
    cid, epoch = case["id"], case["reopen_count"]
    rec = await _plan(c, cid)
    humans = 0
    if rec:
        humans = (await (await c.execute("SELECT count(*) n FROM approvals WHERE recommendation_id=%s AND status='approved'",
                                         (rec["id"],))).fetchone())["n"]
    msgs = await (await c.execute(
        "SELECT kind, status, reviewed_by FROM outbox WHERE case_id=%s AND kind IN ('stakeholder_update','supplier_request') "
        "AND starts_with(message_id, %s)", (cid, f"comm-{cid}-{rec['id']}-" if rec else "comm-none-"))).fetchall()
    manual = {r["key"]: r for r in await (await c.execute(
        "SELECT ci.*, u.name AS done_by_name FROM closure_items ci LEFT JOIN users u ON u.id = ci.done_by "
        "WHERE ci.case_id=%s AND ci.epoch=%s", (cid, epoch))).fetchall()}

    stake = [m for m in msgs if m["kind"] == "stakeholder_update"]
    sent = sum(m["status"] == "sent" for m in stake)
    stuck = sum(m["status"] in UNRESOLVED for m in stake)
    sup = [m for m in msgs if m["kind"] == "supplier_request"]
    sup_stuck = sum(m["status"] in UNRESOLVED for m in sup)
    sup_ok = sum(m["status"] == "sent" or (m["status"] == "cancelled" and m["reviewed_by"] is not None) for m in sup)
    cost = float(rec["total_cost"]) if rec else 0.0

    items = [
        _item("plan_approved", kind="derived", required=True, done=rec is not None,
              label=None if (rec is None or humans > 0) else "Plan cleared by policy (no approval was needed)",
              detail=(f"approved plan: {rec['chosen_option']}" if humans > 0 else f"plan cleared by policy, no human approval was required: {rec['chosen_option']}")
              if rec else "no approved plan on this case"),
        _item("stakeholders_informed", kind="derived", required=True, done=sent > 0 and stuck == 0,
              detail=f"{sent} delivered, {stuck} not delivered yet" if stake else "no stakeholder update has been written yet"),
    ]
    if sup:
        items.append(_item("supplier_contacted", kind="derived", required=True, done=sup_ok > 0 and sup_stuck == 0,
                           detail=f"{sup_ok} handled, {sup_stuck} still waiting (draft, failed or not delivered)"))
    items.append(_item("resolution_confirmed", kind="manual", required=True, done="resolution_confirmed" in manual,
                       detail="a manager confirms the problem is solved", row=manual.get("resolution_confirmed")))
    if cost > 0:
        items.append(_item("spend_recorded", kind="manual", required=True, done="spend_recorded" in manual,
                           detail=f"the finance director records the INR {int(cost)} spend", row=manual.get("spend_recorded")))
    return {"epoch": epoch, "items": items, "can_close": all(i["done"] for i in items if i["required"]),
            "missing": [i["label"] for i in items if i["required"] and not i["done"]]}


async def state(case_id: str) -> dict | None:
    async with db.pool.connection() as c:
        case = await (await c.execute("SELECT * FROM cases WHERE id=%s", (case_id,))).fetchone()
        if case is None:
            return None
        cl = await checklist(c, case)
        latest = await (await c.execute("SELECT id, kind, content, source, metrics_hash, created_at FROM summaries "
                                        "WHERE case_id=%s AND kind='closing' ORDER BY id DESC LIMIT 1", (case_id,))).fetchone()
    return {"status": case["status"], "reopen_count": case["reopen_count"], "closed_at": case["closed_at"], **cl,
            "can_close": cl["can_close"] and case["status"] in LIVE, "latest_summary": latest}


# ───────────────────────── the action frame ─────────────────────────
async def _act(user: UserOut, case_id: str, action: str, fn) -> dict:
    """Role + region check, then `fn(conn, case)` in one transaction with the case row locked. `fn` must check before it writes
    and raises Denied to refuse. Every refusal is audited after the transaction ends."""
    denial: Denied | None = None
    result: dict = {}
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (case_id,))).fetchone()
            if case is None:
                denial = Denied(404, "case not found", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            elif not is_approver(user, case):
                denial = Denied(403, f"a {user.role.value} may not {action.replace('_', ' ')} this case", Reason.POLICY_UNAUTHORIZED)
            else:
                try:
                    result = await fn(c, case)
                except Denied as d:
                    denial = d
                except Conflict:                       # cannot happen under the row lock, but a lost race must never look like success
                    denial = Denied(409, "the case moved on; refresh and try again", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
    if denial is not None:
        await audit.write(case_id, "human", user.id, f"closure_{action}", "blocked", f"{action.replace('_', ' ')} refused: {denial.detail}",
                          reason=denial.reason, refs={"role": user.role.value})
        raise denial
    return result


def _wrong_state(case: dict, wanted: str) -> Denied:
    st = case["status"]
    return Denied(409, f"case is {st}; {wanted}", Reason.WORKFLOW_CANCELLED if st == "CANCELLED" else Reason.WORKFLOW_CONCURRENCY_CONFLICT)


# ───────────────────────── progress ─────────────────────────
async def start(user: UserOut, case_id: str) -> dict:
    async def fn(c, case):
        if case["status"] != "APPROVED":
            raise _wrong_state(case, "work can only start on an approved plan")
        await transition(case_id, "APPROVED", "EXECUTING", "human", user.id, "work on the approved plan started", conn=c)
        await agent_message(c, case_id, "orchestrator", None, "event", {"event": "execution_started", "by_role": user.role.value})
        return {"status": "EXECUTING"}
    return await _act(user, case_id, "start", fn)


async def monitor(user: UserOut, case_id: str) -> dict:
    async def fn(c, case):
        if case["status"] != "EXECUTING":
            raise _wrong_state(case, "only a case that is being carried out can move to monitoring")
        await transition(case_id, "EXECUTING", "MONITORING", "human", user.id, "plan carried out; now watching for delivery", conn=c)
        await agent_message(c, case_id, "orchestrator", None, "event", {"event": "monitoring_started", "by_role": user.role.value})
        return {"status": "MONITORING"}
    return await _act(user, case_id, "monitor", fn)


async def mark_item(user: UserOut, case_id: str, key: str, note: str | None) -> dict:
    """A person confirms one manual closure item. Idempotent in effect: a second tick is refused, never double-counted."""
    note = (note or "").strip()[:MAX_NOTE] or None

    async def fn(c, case):
        if key not in MANUAL_KEYS:
            raise Denied(422, "unknown closure item", Reason.DATA_MISSING)
        if case["status"] not in LIVE:
            raise _wrong_state(case, "closure items can only be confirmed while the plan is live")
        if key == "spend_recorded":
            rec = await _plan(c, case_id)
            if not rec or float(rec["total_cost"]) <= 0:
                raise Denied(409, "this plan has no spend to record", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            if user.role != Role.finance_director:
                raise Denied(403, "only the finance director can record the spend", Reason.POLICY_UNAUTHORIZED)
        r = await c.execute("INSERT INTO closure_items(case_id,epoch,key,done_by,note) VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT (case_id,epoch,key) DO NOTHING", (case_id, case["reopen_count"], key, user.id, note))
        if r.rowcount == 0:
            raise Denied(409, "this item is already confirmed", Reason.WORKFLOW_IDEMPOTENT_REPLAY)
        await audit.write_in_txn(c, case_id, "human", user.id, "closure_item", "success", f"{LABELS[key]}: confirmed by a {user.role.value}",
                                 refs={"key": key, "epoch": case["reopen_count"], "note_len": len(note or "")})
        return {"key": key, "done": True}
    return await _act(user, case_id, "confirm_item", fn)


async def close(user: UserOut, case_id: str) -> dict:
    async def fn(c, case):
        if case["status"] not in LIVE:
            raise _wrong_state(case, "only a live plan can be closed")
        cl = await checklist(c, case)
        if not cl["can_close"]:
            raise Denied(409, "the closure checklist is not complete: " + "; ".join(cl["missing"]), Reason.WORKFLOW_CLOSURE_INCOMPLETE)
        await transition(case_id, set(LIVE), "CLOSED", "human", user.id, "closure checklist complete", conn=c,
                         refs={"epoch": cl["epoch"], "items": [i["key"] for i in cl["items"]]},
                         sets={"closed_at": datetime.now(timezone.utc), "needs_human": False})
        await agent_message(c, case_id, "orchestrator", None, "event", {"event": "closed", "epoch": cl["epoch"], "by_role": user.role.value})
        await outbox.notify_user(c, case_id, "closure_notice", f"closed-{case_id}-{cl['epoch']}", f"Closed: {case_id}",
                                 f"{case_id} - {case['title']} has been resolved and closed.", case["requester_id"])
        return {"status": "CLOSED"}
    return await _act(user, case_id, "close", fn)


async def reopen(user: UserOut, case_id: str, reason: str | None) -> dict:
    why = (reason or "").strip()[:300]

    async def fn(c, case):
        if case["status"] != "CLOSED":
            raise _wrong_state(case, "only a closed case can be reopened")
        if len(why) < MIN_REOPEN_REASON:
            raise Denied(422, "reopening needs a reason", Reason.DATA_MISSING)
        if await _plan(c, case_id) is None:
            raise Denied(409, "this case has no approved plan to reopen", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
        n = case["reopen_count"] + 1
        await transition(case_id, "CLOSED", "MONITORING", "human", user.id, f"reopened: {why}", conn=c,
                         refs={"reopen_count": n}, sets={"closed_at": None})
        await c.execute("UPDATE cases SET reopen_count=%s WHERE id=%s", (n, case_id))
        await agent_message(c, case_id, "orchestrator", None, "event", {"event": "reopened", "reopen_count": n, "by_role": user.role.value})
        await outbox.notify_user(c, case_id, "reopen_notice", f"reopen-{case_id}-{n}", f"Reopened: {case_id}",
                                 f"{case_id} - {case['title']} was reopened because the problem is not solved yet.", case["requester_id"])
        await outbox.notify_role(c, case, "reopen_notice", f"reopen-{case_id}-{n}", f"Reopened: {case_id}",
                                 f"{case_id} - {case['title']} was reopened: {why}", "manager")
        return {"status": "MONITORING", "reopen_count": n}
    return await _act(user, case_id, "reopen", fn)
