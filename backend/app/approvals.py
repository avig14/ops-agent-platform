"""Approvals: decisions, rejection re-plan, deadline reminders and escalation.

Rules that never bend:
  * Only a human with the right role, region and independence can approve. The AI identity cannot.
  * A deadline passing can only ESCALATE. It never approves anything.
  * Every refusal (403/409) is audited through the independent audit connection, after the business txn has ended.
Lock order everywhere: case row first, then approval row (decide and sweep agree, so they cannot deadlock).
"""
from __future__ import annotations

import asyncio

from . import activity, audit, clock, db, orchestrator, outbox
from .codes import Reason
from .config import settings
from .orchestrator import DEFAULT_APPROVAL_SLA, Conflict, agent_message, record_conflict, transition
from .policy import check_approver
from .schemas import Role, UserOut

MAX_ROUNDS = 3
NEXT_ROLE = {Role.manager.value: Role.regional_head.value, Role.regional_head.value: Role.finance_director.value}
OPEN_STATES = ("AWAITING_APPROVAL", "ESCALATED")


class Denied(Exception):
    def __init__(self, status: int, detail: str, reason: Reason):
        self.status, self.detail, self.reason = status, detail, reason


# ───────────────────────── decisions ─────────────────────────
async def decide(user: UserOut, case_id: str, approval_id: int, decision: str, reason: str | None) -> dict:
    """Record one approve/reject. Returns {"status", "replan": (round, exclude) | None}. Raises Denied (already audited)."""
    reason = (reason or "").strip() or None
    if decision == "reject" and (not reason or len(reason) < 3):
        raise Denied(422, "a rejection needs a reason", Reason.DATA_MISSING)
    denial: Denied | None = None
    out: dict = {}
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (case_id,))).fetchone()
            a = await (await c.execute("SELECT * FROM approvals WHERE id=%s AND case_id=%s FOR UPDATE", (approval_id, case_id))).fetchone()
            if a is None:
                denial = Denied(404, "approval not found", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            elif case["status"] not in OPEN_STATES:
                denial = Denied(409, f"case is {case['status']}; this approval can no longer be decided",
                                Reason.WORKFLOW_CANCELLED if case["status"] == "CANCELLED" else Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            elif a["status"] != "pending":
                denial = Denied(409, f"approval already {a['status']}", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            else:
                cur = (await (await c.execute("SELECT max(round) m FROM approvals WHERE case_id=%s", (case_id,))).fetchone())["m"]
                rec = await (await c.execute("SELECT status FROM recommendations WHERE id=%s", (a["recommendation_id"],))).fetchone()
                if a["round"] != cur or not rec or rec["status"] != "proposed":
                    denial = Denied(409, "this approval belongs to a superseded plan", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            if denial is None:
                done_by = [r["approver_id"] for r in await (await c.execute(
                    "SELECT approver_id FROM approvals WHERE case_id=%s AND round=%s AND status='approved'", (case_id, a["round"]))).fetchall()]
                ok, code, msg = check_approver(user, a["required_role"], case["requester_id"], case["region"], done_by)
                if not ok:
                    denial = Denied(403, msg, code)
            if denial is None:
                out = await _apply(c, case, a, user, decision, reason)
    if denial is not None:
        await audit.write(case_id, "human", user.id, "approval_decision", "blocked", f"{decision} refused: {denial.detail}",
                          reason=denial.reason, refs={"approval_id": approval_id, "role": user.role.value})
        raise denial
    return out


async def _apply(c, case: dict, a: dict, user: UserOut, decision: str, reason: str | None) -> dict:
    cid, rnd = case["id"], a["round"]
    new_status = "approved" if decision == "approve" else "rejected"
    await c.execute("UPDATE approvals SET status=%s, approver_id=%s, reason=%s, decided_at=now() WHERE id=%s",
                    (new_status, user.id, reason, a["id"]))
    await audit.write_in_txn(c, cid, "human", user.id, "approval_decision", "success",
                             f"{user.role.value} {new_status} the {a['required_role']}-tier approval (INR {int(a['amount'])})",
                             refs={"approval_id": a["id"], "round": rnd, "tier": a["tier"], "reason": reason})
    if decision == "approve":
        left = (await (await c.execute("SELECT count(*) n FROM approvals WHERE case_id=%s AND round=%s AND status='pending'",
                                       (cid, rnd))).fetchone())["n"]
        if left == 0:
            await c.execute("UPDATE recommendations SET status='approved' WHERE id=%s", (a["recommendation_id"],))
            await agent_message(c, cid, "orchestrator", None, "event", {"event": "all_tiers_approved", "round": rnd})
            await transition(cid, set(OPEN_STATES), "APPROVED", "human", user.id, "all required approvals given", conn=c,
                             sets={"needs_human": False})
            return {"status": "APPROVED", "replan": None}
        return {"status": case["status"], "replan": None}

    # rejection: withdraw the rest, reject the recommendation, record why, then re-plan outside this txn
    await c.execute("UPDATE approvals SET status='withdrawn', reason='another approver rejected this plan', decided_at=now() "
                    "WHERE case_id=%s AND round=%s AND status='pending'", (cid, rnd))
    rec = await (await c.execute("UPDATE recommendations SET status='rejected' WHERE id=%s RETURNING chosen_option", (a["recommendation_id"],))).fetchone()
    await agent_message(c, cid, "approver", "recommend", "event", {"event": "rejected", "action": rec["chosen_option"], "reason": reason, "round": rnd})
    await transition(cid, set(OPEN_STATES), "REJECTED", "human", user.id, f"rejected: {reason}", conn=c, sets={"needs_human": True})
    return {"status": "REJECTED", "replan": rnd}


async def after_rejection(case_id: str, rnd: int) -> None:
    """Re-plan with the rejected options excluded. After MAX_ROUNDS the case goes to a human instead of looping forever."""
    audit.start_trace()
    try:
        if rnd >= MAX_ROUNDS:
            await transition(case_id, "REJECTED", "ESCALATED", "system", "orchestrator",
                             f"plan rejected {rnd} times; no more automatic re-plans", sets={"needs_human": True})
            return
        async with db.pool.connection() as c:
            ex = tuple(r["chosen_option"] for r in await (await c.execute(
                "SELECT DISTINCT chosen_option FROM recommendations WHERE case_id=%s AND status='rejected' AND chosen_option IS NOT NULL",
                (case_id,))).fetchall())
        await transition(case_id, "REJECTED", "REPLANNING", "system", "orchestrator", f"re-planning round {rnd + 1} without {list(ex)}")
        await orchestrator.analyze(case_id, round_no=rnd + 1, exclude_actions=ex, prior_rejection=True)
    except Conflict as exc:
        await record_conflict(exc)
    except Exception as exc:  # noqa: BLE001
        await audit.write(case_id, "system", "orchestrator", "pipeline_crash", "failure", f"{type(exc).__name__} while re-planning",
                          severity="critical", error=audit.error_obj(type(exc).__name__, "orchestrator", None, audit.trace_id_var.get(), str(exc)))
        try:
            await transition(case_id, {"REJECTED", "REPLANNING"}, "ESCALATED", "system", "orchestrator",
                             "re-plan failed; a human must decide", sets={"needs_human": True, "degraded": True})
        except Conflict:
            pass


# ───────────────────────── deadlines ─────────────────────────
def _line(case: dict, a: dict) -> str:
    return f"{case['id']} - {case['title']} (INR {int(a['amount'])}, risk {case['risk_level']}). Approval deadline {a['deadline_at']:%d %b %H:%M} UTC."


async def sweep_deadlines(case_id: str | None = None) -> dict:
    """Send due reminders and escalate overdue approvals. Idempotent; safe to run from any worker at any time.
    Lazy on read (one case) and from a 5 s tick (all cases); after a cold start the first tick catches up."""
    res = {"reminded": 0, "escalated": 0, "breached": 0, "errors": 0}
    try:
        async with db.pool.connection() as c:
            ids = await (await c.execute(
                "SELECT a.id, a.case_id FROM approvals a JOIN cases s ON s.id=a.case_id "
                "WHERE a.status='pending' AND a.deadline_at IS NOT NULL AND s.status = 'AWAITING_APPROVAL' "
                "AND (%s::text IS NULL OR a.case_id=%s) ORDER BY a.deadline_at LIMIT 200", (case_id, case_id))).fetchall()
    except Exception as exc:  # noqa: BLE001
        await audit.write(None, "system", "deadline-sweeper", "deadline_sweep", "failure", f"could not list due approvals: {type(exc).__name__}",
                          severity="error", error=audit.error_obj(type(exc).__name__, "db", None, audit.trace_id_var.get(), str(exc)))
        res["errors"] += 1
        return res
    for r in ids:
        try:
            kind = await _process(r["case_id"], r["id"])
            if kind:
                res[kind] += 1
        except Exception as exc:  # noqa: BLE001
            res["errors"] += 1
            await audit.write(r["case_id"], "system", "deadline-sweeper", "deadline_sweep", "failure",
                              f"approval {r['id']}: {type(exc).__name__}", severity="error",
                              error=audit.error_obj(type(exc).__name__, "deadline", None, audit.trace_id_var.get(), str(exc)))
    return res


async def _process(case_id: str, approval_id: int) -> str | None:
    now = clock.now()
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (case_id,))).fetchone()
            a = await (await c.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (approval_id,))).fetchone()
            if not case or not a or a["status"] != "pending" or a["deadline_at"] is None or case["status"] != "AWAITING_APPROVAL":
                return None                                      # someone decided or moved it meanwhile
            window = max((a["deadline_at"] - a["created_at"]).total_seconds(), 1.0)
            used = (now - a["created_at"]).total_seconds() / window
            if now >= a["deadline_at"]:
                return await _escalate(c, case, a, now)
            for pct, flag, thr in ((90, "reminded_90", 0.9), (50, "reminded_50", 0.5)):
                if used >= thr and not a[flag]:
                    sent = await outbox.notify_role(c, case, "approval_notice", f"rem{pct}-A{a['id']}", f"Reminder: approval needed ({pct}% of time used)",
                                                  "Approval still pending. " + _line(case, a), a["required_role"])
                    await c.execute("UPDATE approvals SET reminded_50=true, reminded_90=(reminded_90 OR %s) WHERE id=%s", (pct == 90, a["id"]))
                    await audit.write_in_txn(c, case_id, "system", "deadline-sweeper", "approval_reminder", "success",
                                             f"{pct}% reminder queued for {a['required_role']} ({sent} recipient(s))",
                                             refs={"approval_id": a["id"], "pct": pct})
                    return "reminded"
    return None


async def _escalate(c, case: dict, a: dict, now) -> str:
    cid = case["id"]
    active = {r["required_role"] for r in await (await c.execute(
        "SELECT required_role FROM approvals WHERE case_id=%s AND round=%s AND status IN ('pending','approved') AND id<>%s",
        (cid, a["round"], a["id"]))).fetchall()}
    nxt = NEXT_ROLE.get(a["required_role"])
    if nxt and nxt not in active:                                # hand the same tier to the next authority
        await c.execute("UPDATE approvals SET status='escalated', reason='deadline passed; escalated', decided_at=now() WHERE id=%s", (a["id"],))
        await c.execute("INSERT INTO approvals(case_id,recommendation_id,round,tier,required_role,amount,deadline_at,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (cid, a["recommendation_id"], a["round"], a["tier"], nxt, a["amount"], now + DEFAULT_APPROVAL_SLA, now))
        sent = await outbox.notify_role(c, case, "approval_notice", f"esc-A{a['id']}", f"Escalated: approval overdue for {cid}",
                                      f"The {a['required_role']} did not decide in time, so this now needs you. " + _line(case, a), nxt)
        await agent_message(c, cid, "orchestrator", None, "event", {"event": "escalated", "from": a["required_role"], "to": nxt})
        await audit.write_in_txn(c, cid, "system", "deadline-sweeper", "approval_escalated", "blocked",
                                 f"{a['required_role']} missed the deadline; escalated to {nxt} ({sent} notified). Nothing was auto-approved",
                                 reason=Reason.WORKFLOW_DEADLINE_MISSED, refs={"approval_id": a["id"], "to": nxt})
        return "escalated"
    # top of the ladder: stop the clock, flag the breach, keep waiting for a human. Never approve.
    await c.execute("UPDATE approvals SET deadline_at=NULL WHERE id=%s", (a["id"],))
    await c.execute("INSERT INTO outbox(case_id,kind,recipient,subject,body,message_id) "
                    "SELECT %s,'sla_breach',u.email,%s,%s,%s||'-'||u.id FROM users u WHERE u.id=%s ON CONFLICT (message_id) DO NOTHING",
                    (cid, f"Approval overdue: {cid}", "Your request is waiting past its deadline and has been escalated to management. " + f"{cid} - {case['title']}.",
                     f"breach-A{a['id']}", case["requester_id"]))
    await audit.write_in_txn(c, cid, "system", "deadline-sweeper", "approval_escalated", "blocked",
                             f"{a['required_role']} missed the deadline and nobody is above; case ESCALATED. Nothing was auto-approved",
                             reason=Reason.WORKFLOW_DEADLINE_MISSED, severity="error", refs={"approval_id": a["id"], "top": True})
    await transition(cid, "AWAITING_APPROVAL", "ESCALATED", "system", "deadline-sweeper", "approval deadline missed at top of ladder",
                     reason=Reason.WORKFLOW_DEADLINE_MISSED, conn=c, sets={"needs_human": True})
    return "breached"


_first_tick = True


async def tick_loop(every: float = 5.0) -> None:
    """Background loop. The first run after startup is a catch-up: anything that came due while we were asleep is handled now."""
    global _first_tick
    while True:
        try:
            audit.start_trace()
            res = await sweep_deadlines()
            if _first_tick and (res["escalated"] or res["breached"] or res["reminded"]):
                await audit.write(None, "system", "deadline-sweeper", "deadline_catchup", "success",
                                  f"startup catch-up handled overdue timers: {res}", reason=Reason.INFRA_TIMER_MISSED_CATCHUP)
            _first_tick = False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass                                              # sweep_deadlines audits its own failures
        await activity.nap(every, settings.loop_idle_s)
