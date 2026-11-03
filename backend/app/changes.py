"""Change engine: everything that alters a case after it was created.

  edit_fields   human corrects an extracted value            (A5 incorrect extraction)
  add_reply     requester answers a "need info" question     (A2 missing info)
  add_revision  supplier / requester sends changed facts      (A9 changing circumstances, also after approval)
  cancel        withdraw the request                          (B14 cancellation, also after approvals started)
  resolve       human picks email-or-system for a conflict    (A3 conflicting info)
  retry         re-run analysis after an outage               (A10)
  not_duplicate / link_duplicate   human fixes a wrong or missed duplicate call (A4)

One primitive does the heavy lifting: `_bump` makes version N+1 inside a single transaction. It copies the fields forward,
applies the change, records the diff, supersedes the old recommendation, withdraws pending approvals and moves the case to
REPLANNING. A human value is never overwritten by re-extraction. Refusals are audited after the txn ends (advisory lock rule).
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import audit, auth, db, orchestrator, outbox
from .approvals import Denied
from .codes import Reason
from .extract import FIELDS, NUMERIC, run_intake, to_decimal
from .orchestrator import _effective_intent, agent_message, transition
from .schemas import IngestIn, Role, UserOut

REVISABLE = {"NEEDS_INFO", "AWAITING_APPROVAL", "ESCALATED", "APPROVED"}
TERMINAL = {"CANCELLED", "CLOSED", "DUPLICATE", "SUPERSEDED"}
APPROVER_ROLES = (Role.manager, Role.regional_head, Role.finance_director)
IN_FLIGHT_MSG = "case is being analysed right now; try again in a few seconds"
LINKABLE = {"NEEDS_INFO", "AWAITING_APPROVAL", "ESCALATED"}       # waiting cases a human may link as a duplicate (never APPROVED: spend is in motion)
VAGUE_INTENTS = ("unclear", "other")
MAX_DUP_DEPTH = 5


def refusal(status: str, already: bool = False) -> tuple[str, Reason]:
    """Message and reason code for a case that cannot take this change in its current status."""
    if status == "SPLIT":
        return "this request was split into separate cases; act on one of its child cases", Reason.WORKFLOW_CONCURRENCY_CONFLICT
    if status == "DUPLICATE":
        return ("this request is linked to an earlier one as a duplicate; choose 'Not a duplicate' if it is a different problem",
                Reason.WORKFLOW_DUPLICATE)
    if status in TERMINAL:
        return f"case is {'already ' if already else ''}{status}", Reason.WORKFLOW_CANCELLED if status == "CANCELLED" else Reason.WORKFLOW_CONCURRENCY_CONFLICT
    if status in ("EXECUTING", "MONITORING"):
        return ("the approved plan is already being carried out; cancel it, or raise a new request for the change",
                Reason.WORKFLOW_CONCURRENCY_CONFLICT)
    return IN_FLIGHT_MSG, Reason.WORKFLOW_CONCURRENCY_CONFLICT


# ───────────────────────── helpers ─────────────────────────
def is_approver(user: UserOut, case: dict) -> bool:
    return user.role in APPROVER_ROLES and auth.can_see_region(user, case["region"])


def can_act(user: UserOut, case: dict) -> bool:
    """Who may change a case: its requester, or an approver-role user who can see its region. Never admin or the AI identity."""
    return case["requester_id"] == user.id or is_approver(user, case)


async def guard(case_id: str, user: UserOut, action: str, coro):
    """Run a change; if it is refused, audit the refusal once the business txn has ended, then re-raise."""
    try:
        return await coro
    except Denied as d:
        await audit.write(case_id, "human", user.id, action, "blocked", d.detail, reason=d.reason, refs={"role": user.role.value})
        raise


async def _load(c, case_id: str, lock: bool = False) -> dict:
    return await (await c.execute(f"SELECT * FROM cases WHERE id=%s{' FOR UPDATE' if lock else ''}", (case_id,))).fetchone()


async def _next_round(c, case_id: str) -> int:
    return (await (await c.execute("SELECT coalesce(max(round),0)+1 n FROM recommendations WHERE case_id=%s", (case_id,))).fetchone())["n"]


async def _approval_started(c, case: dict) -> bool:
    if case["status"] == "APPROVED":
        return True
    row = await (await c.execute("SELECT 1 FROM approvals WHERE case_id=%s AND status='approved' "
                                 "AND round=(SELECT max(round) FROM approvals WHERE case_id=%s)", (case["id"], case["id"]))).fetchone()
    return row is not None


def _same(a, b) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def _check_actor(user: UserOut, case: dict) -> None:
    if not can_act(user, case):
        raise Denied(403, f"{user.role.value} may not change this case", Reason.POLICY_UNAUTHORIZED)


# ───────────────────────── the versioning primitive ─────────────────────────
async def _bump(case_id: str, user: UserOut | None, *, actor_type: str, actor_id: str, summary: str, reason: Reason,
                updates_fn, intake_patch: dict | None = None, case_sets: dict | None = None, allowed=REVISABLE,
                new_intent: str | None = None) -> dict | None:
    """Make version N+1. `updates_fn(old_rows) -> (updates, diff, extra)`; no updates and no intent change => nothing happens
    (returns None). `new_intent` only replaces a vague intent (unclear/other); a concrete intent is never rewritten by a message."""
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await _load(c, case_id, lock=True)
            if case["status"] not in allowed:
                raise Denied(409, *refusal(case["status"]))
            old_v = case["version"]
            old = {r["name"]: r for r in await (await c.execute(
                "SELECT * FROM extracted_fields WHERE case_id=%s AND version=%s", (case_id, old_v))).fetchall()}
            updates, diff, extra = updates_fn(old)
            retype = new_intent if (new_intent and case["intent"] in VAGUE_INTENTS and new_intent != case["intent"]) else None
            if not updates and not retype:
                return None
            if retype:
                diff = {**diff, "intent": {"from": case["intent"], "to": retype}}
                case_sets = {**(case_sets or {}), "intent": retype}
            new_v = old_v + 1
            was_approved = await _approval_started(c, case)
            nr = await _next_round(c, case_id)
            await c.execute(
                "INSERT INTO extracted_fields(case_id,version,name,value,quote,confidence,signals,state,corrected_by) "
                "SELECT case_id,%s,name,value,quote,confidence,signals,state,corrected_by FROM extracted_fields WHERE case_id=%s AND version=%s",
                (new_v, case_id, old_v))
            for name, u in updates.items():
                await c.execute(
                    "INSERT INTO extracted_fields(case_id,version,name,value,quote,confidence,signals,state,corrected_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (case_id,version,name) DO UPDATE SET value=EXCLUDED.value, "
                    "quote=EXCLUDED.quote, confidence=EXCLUDED.confidence, signals=EXCLUDED.signals, state=EXCLUDED.state, "
                    "corrected_by=EXCLUDED.corrected_by, updated_at=now()",
                    (case_id, new_v, name, Jsonb(u["value"]), u.get("quote"), u.get("confidence"), Jsonb(u.get("signals") or {}),
                     u["state"], u.get("by")))
            prior = (await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=%s", (case_id, old_v))).fetchone() or {}).get("facts") or {}
            intake = {**prior.get("intake", {}), **(intake_patch or {})}
            res = {k: v for k, v in (prior.get("resolutions") or {}).items() if k not in updates}
            await c.execute("INSERT INTO case_versions(case_id,version,facts,diff,reason) VALUES (%s,%s,%s,%s,%s)",
                            (case_id, new_v, Jsonb({"intake": intake, "resolutions": res}), Jsonb(diff), reason.value))
            await c.execute("UPDATE recommendations SET status='superseded' WHERE case_id=%s AND status IN ('proposed','abstained','approved')", (case_id,))
            await c.execute("UPDATE approvals SET status='withdrawn', reason=%s, decided_at=now() WHERE case_id=%s AND status='pending'",
                            (f"case changed (version {new_v})", case_id))
            voided = await outbox.void_plan_messages(c, case_id, f"plan superseded by version {new_v}")     # unsent mail about the old plan must not leave
            await c.execute("UPDATE cases SET version=%s WHERE id=%s", (new_v, case_id))
            await agent_message(c, case_id, "orchestrator", None, "event", {"event": "case_revised", "version": new_v, "diff": diff, **(extra or {})})
            await audit.write_in_txn(c, case_id, actor_type, actor_id, "case_revised", "success", f"v{old_v} -> v{new_v}: {summary}",
                                     reason=reason, diff=diff, refs={"version": new_v, "was_approved": was_approved, **(extra or {})})
            if voided:
                await audit.write_in_txn(c, case_id, "system", "orchestrator", "comms_voided", "success",
                                         f"{voided} unsent message(s) about the old plan cancelled", reason=Reason.WORKFLOW_REVISED, refs={"voided": voided})
            await transition(case_id, REVISABLE, "REPLANNING", actor_type, actor_id, f"re-analysing after: {summary}", conn=c,
                             sets={"needs_human": True, **(case_sets or {})})
    return {"version": new_v, "round": nr, "was_approved": was_approved, "diff": diff}


async def _reanalyse(case_id: str, r: dict, wait: bool) -> None:
    coro = orchestrator.safe_analyze(case_id, round_no=r["round"], revision_after_approval=r["was_approved"])
    if wait:
        await coro
    else:
        orchestrator.spawn(coro)


async def _status(case_id: str) -> str:
    async with db.pool.connection() as c:
        return (await _load(c, case_id))["status"]


# ───────────────────────── A5: human edit ─────────────────────────
async def edit_fields(user: UserOut, case: dict, values: dict[str, str], reason: str | None, wait: bool) -> dict:
    cid = case["id"]
    _check_actor(user, case)
    clean: dict[str, str] = {}
    for name, v in values.items():
        v = (v or "").strip()
        if name not in FIELDS:
            raise Denied(422, f"unknown field {name}", Reason.DATA_SCHEMA_DRIFT)
        if not v or len(v) > 200:
            raise Denied(422, f"{name}: value must be 1-200 characters", Reason.DATA_MISSING)
        if name in NUMERIC and (to_decimal(v) is None or to_decimal(v) < 0):
            raise Denied(422, f"{name}: must be a non-negative number", Reason.GATE_NUMERIC_MISMATCH)
        clean[name] = v
    if not clean:
        raise Denied(422, "no fields given", Reason.DATA_MISSING)

    def fn(old):
        ups, diff = {}, {}
        for name, v in clean.items():
            o = old.get(name)
            if o and o["state"] != "dropped" and o["value"] is not None and _same(o["value"], v):
                continue
            ups[name] = {"value": v, "quote": None, "confidence": 1.0, "signals": {"human_edit": True, "reason": reason}, "state": "corrected", "by": user.id}
            diff[name] = {"from": o["value"] if o and o["state"] != "dropped" else None, "to": v}
        return ups, diff, {"edited_by": user.id, "reason": reason}

    r = await _bump(cid, user, actor_type="human", actor_id=user.id, summary=f"{user.role.value} corrected {', '.join(clean)}",
                    reason=Reason.WORKFLOW_REVISED, updates_fn=fn)
    if r is None:
        return {"changed": False, "status": await _status(cid)}
    await _reanalyse(cid, r, wait)
    return {"changed": True, "version": r["version"], "diff": r["diff"], "status": await _status(cid)}


# ───────────────────────── messages: reply / revision / cancel ─────────────────────────
async def _store_message(c, case_id: str, user: UserOut, body: IngestIn, role: str) -> tuple[str, bool]:
    """Returns (message id, replay). Same channel+message_id twice is an idempotent replay."""
    if body.message_id:
        row = await (await c.execute("SELECT id FROM source_messages WHERE channel=%s AND message_id=%s", (body.channel, body.message_id))).fetchone()
        if row:
            return row["id"], True
    mid = "MSG-" + audit.new_id()
    sha = audit.sha256_text(body.body)
    await c.execute("INSERT INTO source_messages(id,channel,sender,message_id,subject,body,sha256) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (mid, body.channel, body.sender or user.email, body.message_id, body.subject, body.body, sha))
    await c.execute("INSERT INTO case_sources(case_id,message_id,role) VALUES (%s,%s,%s)", (case_id, mid, role))
    await audit.write_in_txn(c, case_id, "human", user.id, "message_received", "success", f"{role} received via {body.channel}",
                             refs={"source_message": mid, "sha256": sha, "chars": len(body.body)})
    return mid, False


async def add_message(user: UserOut, case: dict, body: IngestIn, wait: bool) -> dict:
    cid = case["id"]
    if body.kind == "cancel":
        return await cancel(user, case, body, wait)
    _check_actor(user, case)
    role = body.kind                                                      # reply | revision
    async with db.pool.connection() as c:
        cur = await _load(c, cid)
    if body.kind == "reply" and cur["status"] != "NEEDS_INFO":
        raise Denied(409, f"case is {cur['status']}, not waiting for information; send a revision instead", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
    if cur["status"] not in REVISABLE:
        raise Denied(409, *refusal(cur["status"]))
    async with db.pool.connection() as c:
        async with c.transaction():
            mid, replay = await _store_message(c, cid, user, body, role)
    if replay:
        await audit.write(cid, "human", user.id, role, "skipped", "duplicate delivery of the same message id",
                          reason=Reason.WORKFLOW_IDEMPOTENT_REPLAY, refs={"message_id": body.message_id})
        return {"changed": False, "replay": True, "status": await _status(cid)}

    res = await run_intake(body.body, cid)                               # quarantined LLM or rules; same gates as a new case
    primary = res.issues[0]
    kept_human: list[str] = []
    kept_existing: list[str] = []

    def fn(old):
        ups, diff = {}, {}
        for name, f in primary.fields.items():
            if not f.usable:
                continue
            o = old.get(name)
            have = o is not None and o["state"] != "dropped" and o["value"] is not None
            if have and _same(o["value"], f.value):
                continue
            if have and o["state"] in ("corrected", "confirmed"):
                kept_human.append(name)                                   # re-extraction never overwrites a human value
                continue
            if have and role == "reply":
                kept_existing.append(name)                                # a reply fills gaps; it does not rewrite known facts
                continue
            ups[name] = {"value": f.value, "quote": f.quote, "confidence": f.confidence, "signals": {**f.signals, "source_message": mid}, "state": "ai"}
            diff[name] = {"from": o["value"] if have else None, "to": f.value}
        return ups, diff, {"source_message": mid, "kept_human_values": kept_human, "kept_existing_values": kept_existing}

    patch = {"injection": True, "patterns": res.scan.patterns} if res.injection_flag else {}
    if res.degraded:
        patch["degraded"] = True
    sets = {"injection_flag": True} if res.injection_flag else {}
    if res.degraded:
        sets["degraded"] = True
    wanted = _effective_intent(primary.intent, res.mode, primary.fields)       # an answer can turn an unclear case into a concrete one
    r = await _bump(cid, user, actor_type="human", actor_id=user.id, summary=f"{role} message {mid} applied",
                    reason=Reason.WORKFLOW_REVISED, updates_fn=fn, intake_patch=patch, case_sets=sets,
                    new_intent=None if wanted in VAGUE_INTENTS or wanted == "cancellation" else wanted)
    if r is None:
        await audit.write(cid, "agent", "intake", role, "skipped", "message held no new usable facts; nothing changed",
                          refs={"source_message": mid, "kept_human_values": kept_human, "kept_existing_values": kept_existing,
                                "injection": res.injection_flag})
        if res.injection_flag:
            await _flag_injection(cid)
        return {"changed": False, "status": await _status(cid), "injection": res.injection_flag}
    await _reanalyse(cid, r, wait)
    return {"changed": True, "version": r["version"], "diff": r["diff"], "kept_human_values": kept_human, "status": await _status(cid)}


async def _flag_injection(cid: str) -> None:
    """Message carried an injection but changed nothing: flag the case and add INJECTION_FLAG to the policy record,
    so the approver sees why a human must look. Nothing is approved or sent by this path."""
    async with db.pool.connection() as c:
        async with c.transaction():
            await c.execute("UPDATE cases SET injection_flag=true, needs_human=true, updated_at=now() WHERE id=%s", (cid,))
            last = await (await c.execute("SELECT version,rules_fired,inputs FROM policy_decisions WHERE case_id=%s ORDER BY id DESC LIMIT 1", (cid,))).fetchone()
            if last and not any(r.get("id") == "INJECTION_FLAG" for r in last["rules_fired"]):
                rules = [*last["rules_fired"], {"id": "INJECTION_FLAG", "why": "a later message contained instructions aimed at the system"}]
                await c.execute("INSERT INTO policy_decisions(case_id,version,requires_human,rules_fired,inputs) VALUES (%s,%s,true,%s,%s)",
                                (cid, last["version"], Jsonb(rules), Jsonb(last["inputs"])))


# ───────────────────────── B14: cancellation ─────────────────────────
async def cancel(user: UserOut, case: dict, body: IngestIn | None, wait: bool = False) -> dict:
    """Cancel now, or only record a request when a human must confirm (approvals started, or sender not verified)."""
    cid = case["id"]
    verified = not (body and body.sender) or body.sender.strip().lower() == user.email.lower()
    out: dict = {}
    async with db.pool.connection() as c:
        async with c.transaction():
            case = await _load(c, cid, lock=True)
            mid = None
            if body is not None:
                mid, replay = await _store_message(c, cid, user, body, "cancel")
                if replay:
                    return {"outcome": "replay", "status": case["status"]}
            if case["status"] in TERMINAL or case["status"] in ("SPLIT",):
                raise Denied(409, *refusal(case["status"], already=True))
            owner, approver = case["requester_id"] == user.id, is_approver(user, case)
            if not (owner or approver):
                raise Denied(403, f"{user.role.value} may not cancel this case", Reason.POLICY_UNAUTHORIZED)
            started = await _approval_started(c, case)
            immediate = verified and (approver or (owner and not started))
            spend = (await (await c.execute("SELECT coalesce(max(total_cost),0) s FROM recommendations WHERE case_id=%s AND status='approved'", (cid,))).fetchone())["s"]
            if not immediate:
                why = "sender could not be verified" if not verified else "approvals already started; an approver must confirm"
                await c.execute("UPDATE cases SET needs_human=true, updated_at=now() WHERE id=%s", (cid,))
                n = 0
                for role in ("manager", "regional_head", "finance_director"):
                    n += await outbox.notify_role(c, case, "cancel_request", f"cxr-{mid or audit.new_id()}", f"Cancellation requested: {cid}",
                                                  f"{cid} - {case['title']}: a cancellation was requested ({why}). Please confirm or ignore.", role)
                await agent_message(c, cid, "orchestrator", None, "event", {"event": "cancel_requested", "why": why})
                await audit.write_in_txn(c, cid, "human", user.id, "cancel_requested", "blocked", f"cancellation not applied: {why}; {n} approver(s) notified",
                                         reason=Reason.POLICY_UNAUTHORIZED if not verified else Reason.WORKFLOW_CANCELLED,
                                         refs={"source_message": mid, "approvals_started": started, "verified": verified})
                return {"outcome": "cancel_requested", "status": case["status"], "reason": why}
            await c.execute("UPDATE approvals SET status='withdrawn', reason='case cancelled', decided_at=now() WHERE case_id=%s AND status='pending'", (cid,))
            await c.execute("UPDATE recommendations SET status='superseded' WHERE case_id=%s AND status IN ('proposed','abstained','approved')", (cid,))
            voided = await outbox.void_plan_messages(c, cid, "case cancelled")
            if voided:
                await audit.write_in_txn(c, cid, "system", "orchestrator", "comms_voided", "success",
                                         f"{voided} unsent message(s) about the cancelled plan stopped", reason=Reason.WORKFLOW_CANCELLED, refs={"voided": voided})
            notified = 0
            if started:                                                   # committed spend: finance must stop or reverse it
                await c.execute("INSERT INTO policy_decisions(case_id,version,requires_human,rules_fired,inputs) VALUES (%s,%s,true,%s,%s)",
                                (cid, case["version"], Jsonb([{"id": "CANCEL_AFTER_APPROVAL", "why": "cancelled after approvals started; confirmed by an approver"}]),
                                 Jsonb({"committed_spend": float(spend), "cancelled_by": user.id})))
                notified += await outbox.notify_role(c, case, "finance_task", f"fin-{cid}-{case['version']}", f"Stop or reverse spend: {cid}",
                                                     f"{cid} was cancelled after approval. Committed spend INR {int(spend)} must be stopped or reversed.", "finance_director")
                for ap in await (await c.execute("SELECT DISTINCT approver_id FROM approvals WHERE case_id=%s AND status='approved' AND approver_id IS NOT NULL", (cid,))).fetchall():
                    notified += await outbox.notify_user(c, cid, "cancel_notice", f"cxn-{cid}-{case['version']}", f"Cancelled after your approval: {cid}",
                                                         f"{cid} - {case['title']} was cancelled after you approved it.", ap["approver_id"])
            notified += await outbox.notify_user(c, cid, "cancel_notice", f"cxreq-{cid}-{case['version']}", f"Cancelled: {cid}",
                                                 f"{cid} - {case['title']} has been cancelled.", case["requester_id"])
            await agent_message(c, cid, "orchestrator", None, "event", {"event": "cancelled", "after_approval": started, "committed_spend": float(spend)})
            await transition(cid, set(CANCELLABLE), "CANCELLED", "human", user.id,
                             "cancelled after approvals started" if started else "cancelled", reason=Reason.WORKFLOW_CANCELLED, conn=c,
                             refs={"after_approval": started, "committed_spend": float(spend), "notified": notified, "source_message": mid},
                             sets={"needs_human": False, "closed_at": datetime.now(timezone.utc)})
            out = {"outcome": "cancelled", "status": "CANCELLED", "after_approval": started, "committed_spend": float(spend)}
    return out


CANCELLABLE = ["RECEIVED", "EXTRACTING", "NEEDS_INFO", "ANALYZED", "RECOMMENDED", "AWAITING_APPROVAL", "APPROVED", "REJECTED",
            "REPLANNING", "ESCALATED", "EXECUTING", "MONITORING"]


# ───────────────────────── A3 resolve, A10 retry ─────────────────────────
async def resolve_conflict(user: UserOut, case: dict, field: str, source: str, wait: bool) -> dict:
    cid = case["id"]
    _check_actor(user, case)
    async with db.pool.connection() as c:
        async with c.transaction():
            cur = await _load(c, cid, lock=True)
            facts = ((await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=%s", (cid, cur["version"]))).fetchone()) or {}).get("facts") or {}
            if cur["status"] != "NEEDS_INFO" or field not in facts.get("unresolved_conflicts", []):
                raise Denied(409, "no such unresolved conflict on this case", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            nr = await _next_round(c, cid)
            await audit.write_in_txn(c, cid, "human", user.id, "conflict_resolved", "success", f"{field}: human chose the {source} value",
                                     reason=Reason.DATA_CONFLICT, diff={field: {"chose": source}}, refs={"field": field})
    coro = orchestrator.safe_analyze(cid, resolutions={field: source}, round_no=nr)
    if wait:
        await coro
    else:
        orchestrator.spawn(coro)
    return {"status": await _status(cid)}


async def retry(user: UserOut, case: dict, wait: bool) -> dict:
    cid = case["id"]
    _check_actor(user, case)
    async with db.pool.connection() as c:
        cur = await _load(c, cid)
        nr = await _next_round(c, cid)
    if cur["status"] != "NEEDS_INFO":
        raise Denied(409, f"case is {cur['status']}; nothing to retry", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
    await audit.write(cid, "human", user.id, "retry_analysis", "success", "analysis re-run requested")
    coro = orchestrator.safe_analyze(cid, round_no=nr)
    if wait:
        await coro
    else:
        orchestrator.spawn(coro)
    return {"status": await _status(cid)}


# ───────────────────────── A4: fixing the duplicate call ─────────────────────────
async def not_duplicate(user: UserOut, case: dict, wait: bool) -> dict:
    """The screen was wrong: unlink and run the normal analysis. The decision is audited on both cases."""
    cid = case["id"]
    _check_actor(user, case)
    async with db.pool.connection() as c:
        async with c.transaction():
            cur = await _load(c, cid, lock=True)
            if cur["status"] != "DUPLICATE":
                raise Denied(409, f"case is {cur['status']}, not linked as a duplicate", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            orig = cur["duplicate_of"]
            nr = await _next_round(c, cid)
            mid = await orchestrator._origin_mid(c, cid)
            if orig:
                await c.execute("DELETE FROM case_sources WHERE case_id=%s AND message_id=%s AND role='duplicate'", (orig, mid))
            await c.execute("UPDATE case_versions SET facts=jsonb_set(facts,'{intake,dedupe_override}','true'::jsonb,true) "
                            "WHERE case_id=%s AND version=%s", (cid, cur["version"]))
            await transition(cid, "DUPLICATE", "EXTRACTING", "human", user.id, f"{user.role.value} says this is not a duplicate of {orig}",
                             reason=Reason.WORKFLOW_DUPLICATE, conn=c, diff={"duplicate_of": {"from": orig, "to": None}},
                             sets={"duplicate_of": None, "needs_human": False})
            if orig:
                await audit.write_in_txn(c, orig, "human", user.id, "duplicate_unlinked", "success",
                                         f"{cid} was not a duplicate; the link was removed", reason=Reason.WORKFLOW_DUPLICATE, refs={"case": cid})
    coro = orchestrator.safe_analyze(cid, round_no=nr)
    if wait:
        await coro
    else:
        orchestrator.spawn(coro)
    return {"status": await _status(cid), "duplicate_of": None}


async def link_duplicate(user: UserOut, case: dict, target_id: str, wait: bool = False) -> dict:
    """The screen missed it: a human links this waiting case to an earlier one. Pending approvals are withdrawn and the
    recommendation superseded, so nothing is decided twice. Chains are resolved to the root; cycles are refused."""
    cid = case["id"]
    _check_actor(user, case)
    if target_id == cid:
        raise Denied(422, "a case cannot be a duplicate of itself", Reason.WORKFLOW_DUPLICATE)
    async with db.pool.connection() as c:
        async with c.transaction():
            cur = await _load(c, cid, lock=True)
            if cur["status"] not in LINKABLE:
                raise Denied(409, f"case is {cur['status']}; only a case that is waiting (information or approval) can be linked as a duplicate",
                             Reason.WORKFLOW_CONCURRENCY_CONFLICT)
            root, seen, t = target_id, {cid}, None
            for _ in range(MAX_DUP_DEPTH):
                t = await _load(c, root)
                if t is None:
                    raise Denied(404, "target case not found", Reason.WORKFLOW_DUPLICATE)
                if t["id"] in seen:
                    raise Denied(409, "linking would create a duplicate loop", Reason.WORKFLOW_DUPLICATE)
                seen.add(t["id"])
                if t["status"] == "DUPLICATE" and t["duplicate_of"]:
                    root = t["duplicate_of"]
                    continue
                break
            else:
                raise Denied(409, "duplicate chain is too deep", Reason.WORKFLOW_DUPLICATE)
            if t["status"] in TERMINAL or t["status"] == "SPLIT":
                raise Denied(409, f"{root} is {t['status']} and cannot take duplicates", Reason.WORKFLOW_DUPLICATE)
            mid = await orchestrator._origin_mid(c, cid)
            await c.execute("UPDATE approvals SET status='withdrawn', reason=%s, decided_at=now() WHERE case_id=%s AND status='pending'",
                            (f"linked as a duplicate of {root}", cid))
            await c.execute("UPDATE recommendations SET status='superseded' WHERE case_id=%s AND status IN ('proposed','abstained','approved')", (cid,))
            await c.execute("UPDATE cases SET duplicate_of=%s WHERE duplicate_of=%s", (root, cid))           # earlier duplicates follow their new root
            await c.execute("INSERT INTO case_sources(case_id,message_id,role) VALUES (%s,%s,'duplicate') ON CONFLICT DO NOTHING", (root, mid))
            await transition(cid, LINKABLE, "DUPLICATE", "human", user.id, f"{user.role.value} linked this as a duplicate of {root}",
                             reason=Reason.WORKFLOW_DUPLICATE, conn=c, refs={"duplicate_of": root, "manual": True},
                             diff={"duplicate_of": {"from": None, "to": root}}, sets={"duplicate_of": root, "needs_human": False})
            await audit.write_in_txn(c, root, "human", user.id, "duplicate_linked", "success",
                                     f"{cid} linked as an extra report by a {user.role.value}; no second workflow",
                                     reason=Reason.WORKFLOW_DUPLICATE, refs={"duplicate": cid, "manual": True})
            await outbox.notify_user(c, cid, "duplicate_notice", f"dup-{cid}-{root}", f"Linked to an existing request: {cid}",
                                     f"Your request {cid} was linked to {root}, which is already being handled, so it will not be approved twice.",
                                     case["requester_id"])
    return {"status": "DUPLICATE", "duplicate_of": root}
