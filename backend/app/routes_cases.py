"""Case API: ingest, list, detail, audit. Visibility = region scope + requesters see only their own cases.
A case the caller may not see answers 404 (no existence leak) and the attempt is audited as POLICY_UNAUTHORIZED.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from . import approvals, audit, auth, changes, comms, db, orchestrator
from .codes import Reason
from .schemas import DecisionIn, FieldsIn, IngestIn, LinkIn, ResolveIn, Role, UserOut

router = APIRouter(prefix="/api/cases", tags=["cases"])
ACTORS = (Role.requester, Role.manager, Role.regional_head, Role.finance_director)
anyone = Depends(auth.current_user)


def visible(user: UserOut, case: dict) -> bool:
    if user.role == Role.requester:
        return case["requester_id"] == user.id
    return auth.can_see_region(user, case["region"])


async def get_visible(case_id: str, user: UserOut) -> dict:
    async with db.pool.connection() as c:
        case = await (await c.execute("SELECT * FROM cases WHERE id=%s", (case_id,))).fetchone()
    if not case or not visible(user, case):
        if case:
            await audit.write(case_id, "human", user.id, "access_denied", "blocked", f"{user.role.value} may not view this case",
                              reason=Reason.POLICY_UNAUTHORIZED)
        raise HTTPException(404, "case not found")
    return case


@router.post("", status_code=202)
async def create_case(body: IngestIn, wait: bool = Query(False, description="run the pipeline before answering (tests/demo)"),
                      user: UserOut = Depends(auth.require_roles(*ACTORS))):
    if body.kind != "new":
        raise HTTPException(422, "use /api/cases/{id}/messages for replies, revisions and cancellations")
    case_id, created = await orchestrator.ingest(user, body)
    if created:
        if wait:
            await orchestrator.run_pipeline(case_id)
        else:
            orchestrator.spawn(orchestrator.run_pipeline(case_id))
    case = await get_visible(case_id, user)
    return {"case_id": case_id, "created": created, "status": case["status"]}


@router.get("")
async def list_cases(status: str | None = None, user: UserOut = anyone):
    where, params = [], []
    if user.role == Role.requester:
        where.append("requester_id=%s")
        params.append(user.id)
    elif user.role not in (Role.admin, Role.finance_director, Role.service):
        where.append("(region=%s OR region IS NULL)")
        params.append(user.region)
    if status:
        where.append("status=%s")
        params.append(status)
    sql = "SELECT id,status,title,intent,priority,risk_level,degraded,needs_human,injection_flag,region,deadline_at,created_at,updated_at," \
          "parent_id,duplicate_of " \
          "FROM cases" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT 200"
    async with db.pool.connection() as c:
        return {"cases": await (await c.execute(sql, params)).fetchall()}


@router.get("/{case_id}")
async def case_detail(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    await approvals.sweep_deadlines(case_id)                  # lazy deadline check: correct even if the 5 s tick was asleep
    case = await get_visible(case_id, user)
    async with db.pool.connection() as c:
        q = lambda sql: c.execute(sql, (case_id,))  # noqa: E731
        fields = await (await c.execute(
            "SELECT name,value,quote,confidence,signals,state FROM extracted_fields WHERE case_id=%s AND version=%s ORDER BY id",
            (case_id, case["version"]))).fetchall()
        facts = await (await c.execute("SELECT facts,diff,reason FROM case_versions WHERE case_id=%s AND version=%s",
                                       (case_id, case["version"]))).fetchone()
        recs = await (await q("SELECT * FROM recommendations WHERE case_id=%s ORDER BY id")).fetchall()
        appr = await (await q("SELECT a.*, u.name AS approver_name FROM approvals a LEFT JOIN users u ON u.id=a.approver_id "
                                   "WHERE a.case_id=%s ORDER BY a.round, a.tier, a.id")).fetchall()
        policy = await (await q("SELECT * FROM policy_decisions WHERE case_id=%s ORDER BY id DESC LIMIT 1")).fetchone()
        msgs = await (await q("SELECT id,from_agent,to_agent,kind,payload,created_at FROM agent_messages WHERE case_id=%s ORDER BY id")).fetchall()
        sources = await (await q("SELECT s.role, m.id, m.channel, m.sender, m.subject, m.body, m.received_at FROM case_sources s "
                                 "JOIN source_messages m ON m.id=s.message_id WHERE s.case_id=%s ORDER BY m.received_at")).fetchall()
    return {"case": case, "fields": fields, "facts": (facts or {}).get("facts"), "recommendations": recs, "approvals": appr,
            "policy": policy, "agent_messages": msgs, "sources": sources, "related": await _related(case, facts, user)}


async def _related(case: dict, facts: dict | None, user: UserOut) -> dict:
    """Linked cases (original, split children, possible duplicates), limited to those this user may see."""
    meta = ((facts or {}).get("facts") or {}).get("intake", {})
    async with db.pool.connection() as c:
        kids = await (await c.execute("SELECT id,status,title,requester_id,region FROM cases WHERE parent_id=%s ORDER BY id", (case["id"],))).fetchall()
        ids = [i for i in [case["duplicate_of"], case["parent_id"]] + [r["id"] for r in meta.get("related_cases", [])] if i]
        others = await (await c.execute("SELECT id,status,title,requester_id,region FROM cases WHERE id = ANY(%s)", (ids,))).fetchall() if ids else []
    by_id = {o["id"]: o for o in others if visible(user, o)}
    brief = lambda o: {"id": o["id"], "status": o["status"], "title": o["title"]}  # noqa: E731
    return {"duplicate_of": brief(by_id[case["duplicate_of"]]) if case["duplicate_of"] in by_id else None,
            "parent": brief(by_id[case["parent_id"]]) if case["parent_id"] in by_id else None,
            "children": [brief(k) for k in kids if visible(user, k)],
            "possible_duplicates": [{**r, **brief(by_id[r["id"]])} for r in meta.get("related_cases", []) if r["id"] in by_id]}


@router.get("/{case_id}/audit")
async def case_audit(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    async with db.pool.connection() as c:
        rows = await (await c.execute(
            "SELECT seq,ts,sim_ts,actor_type,actor_id,action,outcome,severity,reason_code,summary,diff,refs,error,not_fetched,model,"
            "trace_id,row_hash FROM audit_log WHERE chain_key=%s ORDER BY seq", (case_id,))).fetchall()
        ver = await (await c.execute("SELECT * FROM verify_audit_chain(%s)", (case_id,))).fetchall()
    return {"events": rows, "chain": ver[0] if ver else None}


@router.post("/{case_id}/approvals/{approval_id}/decision")
async def decide(case_id: str, approval_id: int, body: DecisionIn, wait: bool = Query(False), user: UserOut = anyone):
    await get_visible(case_id, user)
    try:
        out = await approvals.decide(user, case_id, approval_id, body.decision, body.reason)
    except approvals.Denied as d:
        raise HTTPException(d.status, d.detail)
    if out["replan"] is not None:
        if wait:
            await approvals.after_rejection(case_id, out["replan"])
        else:
            orchestrator.spawn(approvals.after_rejection(case_id, out["replan"]))
    if out["status"] == "APPROVED":                            # the plan is final: write the messages that carry it out
        if wait:
            await comms.draft_for_approval(case_id)
        else:
            orchestrator.spawn(comms.draft_for_approval(case_id))
    case = await get_visible(case_id, user)
    return {"case_id": case_id, "status": case["status"], "decision": body.decision}


async def _change(case_id: str, user: UserOut, action: str, fn):
    """Run a case change; refusals are audited once and surface as the right HTTP status."""
    case = await get_visible(case_id, user)
    try:
        return await changes.guard(case_id, user, action, fn(case))
    except approvals.Denied as d:
        raise HTTPException(d.status, d.detail)


@router.patch("/{case_id}/fields")
async def edit_fields(case_id: str, body: FieldsIn, wait: bool = Query(False), user: UserOut = anyone):
    return await _change(case_id, user, "field_edit", lambda case: changes.edit_fields(user, case, body.fields, body.reason, wait))


@router.post("/{case_id}/messages", status_code=202)
async def add_message(case_id: str, body: IngestIn, wait: bool = Query(False), user: UserOut = anyone):
    if body.kind == "new":
        raise HTTPException(422, "use POST /api/cases for a new request")
    return await _change(case_id, user, body.kind, lambda case: changes.add_message(user, case, body, wait))


@router.post("/{case_id}/resolve")
async def resolve(case_id: str, body: ResolveIn, wait: bool = Query(False), user: UserOut = anyone):
    return await _change(case_id, user, "conflict_resolved", lambda case: changes.resolve_conflict(user, case, body.field, body.source, wait))


@router.post("/{case_id}/not-duplicate")
async def not_duplicate(case_id: str, wait: bool = Query(False), user: UserOut = anyone):
    return await _change(case_id, user, "not_duplicate", lambda case: changes.not_duplicate(user, case, wait))


@router.post("/{case_id}/link-duplicate")
async def link_duplicate(case_id: str, body: LinkIn, wait: bool = Query(False), user: UserOut = anyone):
    return await _change(case_id, user, "link_duplicate", lambda case: changes.link_duplicate(user, case, body.of, wait))


@router.post("/{case_id}/retry")
async def retry(case_id: str, wait: bool = Query(False), user: UserOut = anyone):
    return await _change(case_id, user, "retry_analysis", lambda case: changes.retry(user, case, wait))
