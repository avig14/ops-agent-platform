"""Orchestrator: an explicit state machine in code. `cases.status` is the single source of truth.

  RECEIVED > EXTRACTING > NEEDS_INFO | ANALYZED > RECOMMENDED > AWAITING_APPROVAL | APPROVED (auto-clear)

Every transition is a compare-and-swap on status inside one transaction together with its audit row. A lost race is
audited (WORKFLOW_CONCURRENCY_CONFLICT) through the independent audit connection and raised as `Conflict`.
Agents never move a case themselves; they return data and the orchestrator decides what happens next.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import audit, clarify, clock, db, dedupe, outbox, risk as risk_agent, recommend as rec_agent, split, validator
from .codes import Reason
from .extract import FIELDS, Field, run_intake
from .policy import CRITICAL_FIELDS, PolicyInput, evaluate
from .schemas import IngestIn, UserOut

DEFAULT_APPROVAL_SLA = timedelta(hours=4)     # documented assumption when the sender states no deadline
PRIORITY = {"Critical": "P1", "High": "P2", "Medium": "P3", "Low": "P4"}
_CASE_COLUMNS = {"risk_level", "priority", "degraded", "needs_human", "injection_flag", "intent", "title", "deadline_at",
                 "closed_at", "duplicate_of", "parent_id", "owner_id", "region"}
_TASKS: set[asyncio.Task] = set()


class Conflict(Exception):
    """The case is no longer in the state this step expected (someone else moved it first)."""
    audit: tuple | None = None
    recorded = False


async def record_conflict(exc: Conflict) -> None:
    """Audit a lost race exactly once. Must run outside any business txn (audit chain advisory lock is per-txn)."""
    if exc.audit and not exc.recorded:
        exc.recorded = True
        case_id, actor_type, actor_id, msg = exc.audit
        await audit.write(case_id, actor_type, actor_id, "status_change", "blocked", msg, reason=Reason.WORKFLOW_CONCURRENCY_CONFLICT)


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


# ───────────────────────── low-level helpers ─────────────────────────
async def transition(case_id: str, expect: str | set[str], to: str, actor_type: str, actor_id: str, summary: str, *,
                     reason: Reason | None = None, diff=None, refs=None, sets: dict | None = None, conn=None) -> str:
    """CAS status change + audit row in one transaction. Returns the previous status."""
    expect_l = sorted([expect] if isinstance(expect, str) else expect)
    sets = {k: v for k, v in (sets or {}).items() if k in _CASE_COLUMNS}
    assign = "".join(f", {k}=%s" for k in sets)
    sql = ("WITH old AS (SELECT status FROM cases WHERE id=%s FOR UPDATE) "
           f"UPDATE cases c SET status=%s, updated_at=now(){assign} FROM old "
           "WHERE c.id=%s AND old.status = ANY(%s) RETURNING old.status AS prev")
    params = (case_id, to, *sets.values(), case_id, expect_l)

    async def go(c) -> str | None:
        row = await (await c.execute(sql, params)).fetchone()
        if row is None:
            return None
        await audit.write_in_txn(c, case_id, actor_type, actor_id, "status_change", "success",
                                 f"{row['prev']} -> {to}: {summary}", reason=reason, diff=diff, refs=refs)
        return row["prev"]

    if conn is not None:
        prev = await go(conn)
    else:
        async with db.pool.connection() as c:
            async with c.transaction():
                prev = await go(c)
    if prev is None:
        exc = Conflict(f"{case_id}: expected {expect_l}, wanted {to}")
        exc.audit = (case_id, actor_type, actor_id, f"expected {expect_l} but case moved on; tried -> {to}")
        if conn is None:                 # no business txn open => safe to audit now
            await record_conflict(exc)
        raise exc                        # inside a caller's txn the audit chain lock is held; caller records after rollback
    return prev


async def agent_message(conn, case_id: str, frm: str, to: str | None, kind: str, payload: dict, task: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO agent_messages(case_id,task_id,from_agent,to_agent,kind,payload,trace_id,span_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (case_id, task, frm, to, kind, Jsonb(payload), audit.trace_id_var.get(), audit.span_id_var.get()))


async def _case(case_id: str) -> dict | None:
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM cases WHERE id=%s", (case_id,))).fetchone()


async def _origin_text(case_id: str) -> str:
    async with db.pool.connection() as c:
        row = await (await c.execute(
            "SELECT m.body FROM case_sources s JOIN source_messages m ON m.id=s.message_id "
            "WHERE s.case_id=%s AND s.role='origin'", (case_id,))).fetchone()
    return row["body"]


# ───────────────────────── ingest ─────────────────────────
async def ingest(user: UserOut, body: IngestIn) -> tuple[str, bool]:
    """Create a case from a new message. Returns (case_id, created). Same message_id twice => the same case (idempotent)."""
    sha = audit.sha256_text(body.body)
    msg_id = "MSG-" + audit.new_id()
    async with db.pool.connection() as c:
        async with c.transaction():
            if body.message_id:
                row = await (await c.execute(
                    "SELECT s.case_id FROM source_messages m JOIN case_sources s ON s.message_id=m.id "
                    "WHERE m.channel=%s AND m.message_id=%s LIMIT 1", (body.channel, body.message_id))).fetchone()
                if row:
                    await audit.write(row["case_id"], "human", user.id, "ingest", "skipped", "duplicate delivery of the same message id",
                                      reason=Reason.WORKFLOW_IDEMPOTENT_REPLAY, refs={"message_id": body.message_id})
                    return row["case_id"], False
            n = (await (await c.execute("SELECT nextval('case_seq') AS n")).fetchone())["n"]
            case_id = f"CASE-{n:04d}"
            await c.execute("INSERT INTO source_messages(id,channel,sender,message_id,subject,body,sha256) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (msg_id, body.channel, body.sender or user.email, body.message_id, body.subject, body.body, sha))
            await c.execute("INSERT INTO cases(id,status,title,requester_id,region) VALUES (%s,'RECEIVED',%s,%s,%s)",
                            (case_id, (body.subject or body.body[:70]).strip()[:120], user.id, user.region))
            await c.execute("INSERT INTO case_sources(case_id,message_id,role) VALUES (%s,%s,'origin')", (case_id, msg_id))
            await audit.write_in_txn(c, case_id, "human", user.id, "case_created", "success",
                                     f"case created from {body.channel} message", refs={"source_message": msg_id, "sha256": sha,
                                                                                    "chars": len(body.body)})
    return case_id, True


# ───────────────────────── pipeline ─────────────────────────
def _effective_intent(intent: str, mode: str, fields: dict[str, Field]) -> str:
    if intent == "unclear" and mode == "rules_only":      # rule-based extraction cannot classify; infer from what it found
        intent = "delivery_delay" if fields["delay_days"].usable else ("inventory_shortage" if fields["quantity_affected"].usable else "other")
    if intent == "other" and not any(f.usable for f in fields.values()):
        return "unclear"                                  # nothing actionable was found: ask, do not guess
    return intent


async def _save_fields(conn, case_id: str, version: int, fields: dict[str, Field]) -> None:
    for name, f in fields.items():
        if f.state not in ("ai", "dropped") or (f.state == "ai" and f.value is None):
            continue
        await conn.execute(
            "INSERT INTO extracted_fields(case_id,version,name,value,quote,confidence,signals,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (case_id,version,name) DO NOTHING",
            (case_id, version, name, Jsonb(f.value), f.quote, f.confidence, Jsonb({**f.signals, **(
                {"drop_reason": f.drop_reason.value} if f.drop_reason else {})}), f.state))


async def load_fields(case_id: str, version: int) -> dict[str, Field]:
    """Rebuild Field objects from the DB. Human-confirmed / corrected values are fully trusted (confidence 1.0)."""
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM extracted_fields WHERE case_id=%s AND version=%s", (case_id, version))).fetchall()
    out = {n: Field(n, None, None, state="missing") for n in FIELDS}
    for r in rows:
        if r["state"] == "dropped":
            sig = r["signals"] or {}
            out[r["name"]] = Field(r["name"], None, r["quote"], "dropped",
                                   Reason(sig["drop_reason"]) if sig.get("drop_reason") else None, 0.0, sig)
        else:
            human = r["state"] in ("confirmed", "corrected")
            out[r["name"]] = Field(r["name"], r["value"], r["quote"], "ai", None, 1.0 if human else float(r["confidence"] or 0), r["signals"] or {})
    return out


async def _crash(case_id: str, where: str, expect: set[str], exc: BaseException) -> None:
    """A step raised unexpectedly: audit it and hand the case to a human. Never raises."""
    await audit.write(case_id, "system", "orchestrator", "pipeline_crash", "failure", f"{type(exc).__name__} in {where}",
                      severity="critical", error=audit.error_obj(type(exc).__name__, "orchestrator", None, audit.trace_id_var.get(), str(exc)))
    try:
        await transition(case_id, expect, "NEEDS_INFO", "system", "orchestrator", "pipeline error; a human must review",
                         sets={"needs_human": True, "degraded": True})
    except Conflict:
        pass


async def run_pipeline(case_id: str) -> None:
    """Background entry point: extract (maybe split), screen for duplicates, then analyse each resulting case.
    Never raises (a crash is audited and the case flagged). Children of a split run one after another."""
    audit.start_trace()
    try:
        ids = await _extract(case_id)
    except Conflict as exc:
        await record_conflict(exc)          # another actor moved the case; nothing more to do
        return
    except Exception as exc:  # noqa: BLE001
        await _crash(case_id, "pipeline", {"RECEIVED", "EXTRACTING", "ANALYZED", "RECOMMENDED"}, exc)
        return
    for cid in ids:
        if cid != case_id:
            audit.start_trace()
        try:
            if not await _screen_duplicates(cid):
                await analyze(cid)
        except Conflict as exc:
            await record_conflict(exc)
        except Exception as exc:  # noqa: BLE001
            await _crash(cid, "pipeline", {"RECEIVED", "EXTRACTING", "ANALYZED", "RECOMMENDED"}, exc)


async def safe_analyze(case_id: str, **kw) -> None:
    """Background re-analysis after a human change. Never raises: a crash is audited and the case goes to a human."""
    audit.start_trace()
    try:
        await analyze(case_id, **kw)
    except Conflict as exc:
        await record_conflict(exc)
    except Exception as exc:  # noqa: BLE001
        await audit.write(case_id, "system", "orchestrator", "pipeline_crash", "failure", f"{type(exc).__name__} in re-analysis",
                          severity="critical", error=audit.error_obj(type(exc).__name__, "orchestrator", None, audit.trace_id_var.get(), str(exc)))
        try:
            await transition(case_id, {"REPLANNING", "ANALYZED", "RECOMMENDED", "EXTRACTING"}, "NEEDS_INFO", "system", "orchestrator",
                             "re-analysis error; a human must review", sets={"needs_human": True, "degraded": True})
        except Conflict:
            pass


def _intake_meta(res, **extra) -> dict:
    return {"mode": res.mode, "degraded": res.degraded, "injection": res.injection_flag, "patterns": res.scan.patterns,
            "issues_found": len(res.issues), "is_cancellation": res.is_cancellation, "failures": res.failures, **extra}


async def _record_intake(c, case_id: str, issue, res, intent: str, meta: dict) -> None:
    """Persist what intake found for ONE issue: gated fields, the intake artifact, version-1 facts and the audit row.
    The caller owns the transaction and has already created the case row."""
    await _save_fields(c, case_id, 1, issue.fields)
    await agent_message(c, case_id, "intake", "validator", "artifact", {
        "intent": intent, "mode": res.mode, "model": res.model, "degraded": res.degraded, "injection": res.injection_flag,
        "patterns": res.scan.patterns, "issues_found": len(res.issues), "is_cancellation": res.is_cancellation,
        "sender_role_claimed": res.sender_role_claimed, "dropped": [d["field"] for d in res.drops],
        "fields": {n: {"value": f.value, "state": f.state, "confidence": f.confidence} for n, f in issue.fields.items()}})
    await c.execute("UPDATE cases SET intent=%s, injection_flag=%s, degraded=%s, updated_at=now() WHERE id=%s",
                    (intent, res.injection_flag, res.degraded, case_id))
    await c.execute("INSERT INTO case_versions(case_id,version,facts,reason) VALUES (%s,1,%s,'intake') ON CONFLICT DO NOTHING",
                    (case_id, Jsonb({"intake": meta})))
    await audit.write_in_txn(c, case_id, "agent", "intake", "extraction", "success" if not res.degraded else "degraded",
                             f"extracted {sum(f.usable for f in issue.fields.values())} usable field(s), "
                             f"{len(res.drops)} dropped, mode={res.mode}", refs={"intent": intent, "issues": len(res.issues)},
                             model=res.model)


async def _extract(case_id: str) -> list[str]:
    """Intake. Returns the ids of the cases that now need screening and analysis: the case itself, or its children when
    the message held several separate problems (the parent then becomes a SPLIT container and is not analysed)."""
    await transition(case_id, "RECEIVED", "EXTRACTING", "system", "orchestrator", "extraction started")
    text = await _origin_text(case_id)
    res = await run_intake(text, case_id)
    issues = split.consolidate(res.issues)
    n = len(issues)
    if 1 < n <= split.MAX_ISSUES:
        return await _split_case(case_id, issues, res)
    primary = issues[0]
    intent = _effective_intent(primary.intent, res.mode, primary.fields)
    extra: dict = {"issues_after_merge": n}
    if n > split.MAX_ISSUES:                      # too many to open safely: ask the sender to send them separately
        intent, extra["too_many_issues"] = "unclear", n
    async with db.pool.connection() as c:
        async with c.transaction():
            await _record_intake(c, case_id, primary, res, intent, _intake_meta(res, **extra))
    return [case_id]


async def _origin_mid(c, case_id: str) -> str:
    return (await (await c.execute("SELECT message_id FROM case_sources WHERE case_id=%s AND role='origin'", (case_id,))).fetchone())["message_id"]


async def _split_case(parent_id: str, issues, res) -> list[str]:
    """B15: one message, several problems. Each issue becomes a child case built from that issue's own grounded fields only,
    so a quantity or store from one problem can never leak into another. All of it commits in one transaction."""
    kids: list[str] = []
    n = len(issues)
    async with db.pool.connection() as c:
        async with c.transaction():
            parent = await (await c.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (parent_id,))).fetchone()
            mid = await _origin_mid(c, parent_id)
            for i, issue in enumerate(issues, 1):
                num = (await (await c.execute("SELECT nextval('case_seq') AS n")).fetchone())["n"]
                cid = f"CASE-{num:04d}"
                intent = _effective_intent(issue.intent, res.mode, issue.fields)
                title = (issue.summary or parent["title"] or cid).strip()[:110] + f" [{i}/{n}]"
                await c.execute("INSERT INTO cases(id,status,title,requester_id,region,parent_id) VALUES (%s,'EXTRACTING',%s,%s,%s,%s)",
                                (cid, title, parent["requester_id"], parent["region"], parent_id))
                await c.execute("INSERT INTO case_sources(case_id,message_id,role) VALUES (%s,%s,'origin')", (cid, mid))
                await audit.write_in_txn(c, cid, "agent", "intake", "case_created", "success",
                                         f"split from {parent_id}: issue {i} of {n}", reason=Reason.INGEST_MULTI_ISSUE,
                                         refs={"parent": parent_id, "issue_index": i, "source_message": mid})
                await _record_intake(c, cid, issue, res, intent,
                                     _intake_meta(res, parent=parent_id, issue_index=i, issues_in_message=n, issues_after_merge=n))
                kids.append(cid)
            await c.execute("INSERT INTO case_versions(case_id,version,facts,reason) VALUES (%s,1,%s,'intake') ON CONFLICT DO NOTHING",
                            (parent_id, Jsonb({"intake": _intake_meta(res, split_into=kids, issues_after_merge=n)})))
            await agent_message(c, parent_id, "intake", "orchestrator", "artifact",
                                {"split_into": kids, "issues": [{"intent": i.intent, "summary": i.summary} for i in issues]})
            await outbox.notify_user(c, parent_id, "split_notice", f"split-{parent_id}", f"Your message was split into {n} requests: {parent_id}",
                                     f"Your message ({parent_id}) described {n} separate problems, so we opened {n} requests: " + ", ".join(kids)
                                     + ". Each one is handled and approved on its own.", parent["requester_id"])
            await transition(parent_id, "EXTRACTING", "SPLIT", "agent", "intake", f"one message, {n} separate problems: {', '.join(kids)}",
                             reason=Reason.INGEST_MULTI_ISSUE, conn=c, refs={"children": kids},
                             sets={"intent": "multi_issue", "injection_flag": res.injection_flag, "degraded": res.degraded, "needs_human": False})
    return kids


# ───────────────────────── duplicate screening (A4) ─────────────────────────
async def _screen_duplicates(case_id: str) -> bool:
    """True when the case was linked to an earlier one as a duplicate (then it must NOT be analysed)."""
    case = await _case(case_id)
    if case["status"] != "EXTRACTING":
        return False
    async with db.pool.connection() as c:
        row = await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=1", (case_id,))).fetchone()
    meta = ((row or {}).get("facts") or {}).get("intake", {})
    if meta.get("dedupe_override") or meta.get("is_cancellation") or case["intent"] == "cancellation":
        return False                                   # a cancellation must never be swallowed as a duplicate
    sc = await dedupe.screen(case_id, await _origin_text(case_id), await load_fields(case_id, case["version"]), case["parent_id"])
    if sc.duplicate:
        await _mark_duplicate(case, sc.duplicate)
        return True
    if sc.possible:
        await _note_possible(case_id, sc.possible)
    return False


async def _mark_duplicate(case: dict, match: dedupe.Match) -> None:
    cid, orig = case["id"], match.case_id
    async with db.pool.connection() as c:
        async with c.transaction():
            mid = await _origin_mid(c, cid)
            o = await (await c.execute("SELECT requester_id,status FROM cases WHERE id=%s", (orig,))).fetchone()
            await transition(cid, "EXTRACTING", "DUPLICATE", "agent", "dedupe", f"duplicate of {orig}: {match.why}", reason=Reason.WORKFLOW_DUPLICATE,
                             conn=c, refs=match.as_json(), diff={"duplicate_of": {"from": None, "to": orig}},
                             sets={"duplicate_of": orig, "needs_human": False})
            await c.execute("INSERT INTO case_sources(case_id,message_id,role) VALUES (%s,%s,'duplicate') ON CONFLICT DO NOTHING", (orig, mid))
            await audit.write_in_txn(c, orig, "agent", "dedupe", "duplicate_linked", "success",
                                     f"{cid} reported the same issue; linked as an extra report, no second workflow, no second approval",
                                     reason=Reason.WORKFLOW_DUPLICATE, refs={"duplicate": cid, **match.as_json()})
            await agent_message(c, cid, "dedupe", "orchestrator", "artifact", {"duplicate_of": orig, **match.as_json()})
            same = o and o["requester_id"] == case["requester_id"]
            await outbox.notify_user(c, cid, "duplicate_notice", f"dup-{cid}-{orig}", f"Already being handled: {cid}",
                                     f"Your request {cid} repeats " + (f"{orig}, which is already in progress" if same else "a request that is already in progress")
                                     + ". We linked them instead of opening a second workflow, so nothing is approved twice. "
                                     "If this is a different problem, open the request and choose 'Not a duplicate'.", case["requester_id"])


async def _note_possible(case_id: str, matches: list[dedupe.Match]) -> None:
    related = [m.as_json() for m in matches]
    async with db.pool.connection() as c:
        async with c.transaction():
            await c.execute("UPDATE case_versions SET facts=jsonb_set(facts, '{intake,related_cases}', %s, true) WHERE case_id=%s AND version=1",
                            (Jsonb(related), case_id))
            await audit.write_in_txn(c, case_id, "agent", "dedupe", "duplicate_check", "success",
                                     "possible duplicate of " + ", ".join(m.case_id for m in matches) + "; not merged, a human decides",
                                     reason=Reason.WORKFLOW_DUPLICATE, refs={"related": related})


def _deadline(facts: dict, now: datetime) -> datetime:
    iso = facts.get("approval_deadline")
    if iso and not facts.get("deadline_passed_at_ingest"):
        return datetime.fromisoformat(iso)
    return now + DEFAULT_APPROVAL_SLA


async def analyze(case_id: str, *, resolutions: dict | None = None, exclude_actions: tuple[str, ...] = (), round_no: int = 1,
                  prior_rejection: bool = False, revision_after_approval: bool = False, keep_deadline: datetime | None = None) -> None:
    """Validate > risk > recommend > policy, then persist and move the case. Safe to re-run after a human edit or reply.
    `keep_deadline`: a retry after an outage must not hand approvers a fresh clock, so it carries the original deadline over."""
    case = await _case(case_id)
    version = case["version"]
    fields = await load_fields(case_id, version)
    async with db.pool.connection() as c:
        row = await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=%s", (case_id, version))).fetchone()
        received = (await (await c.execute("SELECT m.received_at FROM case_sources s JOIN source_messages m ON m.id=s.message_id "
                                           "WHERE s.case_id=%s AND s.role='origin'", (case_id,))).fetchone())["received_at"]
    prior = (row or {}).get("facts") or {}
    intake_meta = prior.get("intake", {})
    resolutions = {**prior.get("resolutions", {}), **(resolutions or {})}

    val = await validator.validate(fields, case["intent"] or "other", received, case_id, resolutions)
    facts = {**val.facts, "intake": intake_meta, "resolutions": resolutions, "conflicts": val.conflicts, "warnings": val.warnings,
             "missing_detail": val.missing, "inputs_used": val.inputs_used, "inputs_total": val.inputs_total}
    unresolved = facts.get("unresolved_conflicts", [])
    now = clock.now()

    if val.missing or unresolved:                                   # block: ask a human, never guess
        open_conflicts = [x for x in val.conflicts if x["field"] in unresolved]
        ask = clarify.build(case_id, case["title"], val.missing, open_conflicts, intake_meta)
        facts["clarification"] = {"questions": ask["questions"], "key_hash": ask["key_hash"]}
        async with db.pool.connection() as c:
            async with c.transaction():
                await c.execute("UPDATE case_versions SET facts=%s WHERE case_id=%s AND version=%s", (Jsonb(facts), case_id, version))
                await agent_message(c, case_id, "validator", "orchestrator", "question",
                                    {"missing": val.missing, "conflicts": open_conflicts, "questions": ask["questions"]})
        await transition(case_id, {"EXTRACTING", "NEEDS_INFO", "REPLANNING"}, "NEEDS_INFO", "agent", "validator",
                         f"missing={facts['missing']} conflicts={unresolved}",
                         reason=validator.block_reason(val.missing), sets={"needs_human": True, "degraded": val.degraded or bool(intake_meta.get("degraded"))})
        async with db.pool.connection() as c:                       # ask the requester once per distinct set of questions
            async with c.transaction():
                queued = await outbox.notify_user(c, case_id, "clarification", f"clar-{case_id}-{version}-{ask['key_hash']}",
                                                  ask["subject"], ask["body"], case["requester_id"])
                if queued:
                    await audit.write_in_txn(c, case_id, "agent", "validator", "clarification_requested", "success",
                                             f"asked the requester {len(ask['questions'])} question(s)", refs={"questions": ask["questions"]})
        return

    rk = await risk_agent.assess(facts, case_id)
    await transition(case_id, {"EXTRACTING", "NEEDS_INFO", "REPLANNING"}, "ANALYZED", "agent", "risk",
                     f"risk={rk.level} (rule={rk.rule['level']}, llm={(rk.llm or {}).get('risk', 'n/a')})",
                     sets={"risk_level": rk.level, "priority": PRIORITY[rk.level],
                           **({"region": facts["store"]["region"]} if facts.get("store") else {})})
    degraded = bool(val.degraded or intake_meta.get("degraded") or rk.llm_failed)
    rec = await rec_agent.recommend(facts, val.inputs_used, val.inputs_total, degraded, case_id, exclude_actions=exclude_actions)
    degraded = degraded or rec.degraded
    conf = {n: f.confidence for n, f in fields.items() if f.usable and n in CRITICAL_FIELDS}
    pol = evaluate(PolicyInput(amount=rec.total_cost, risk_level=rk.level, injection_flag=bool(case["injection_flag"]), degraded=degraded,
                               agent_conflict=rk.conflict, unresolved_conflicts=len(unresolved), missing_fields=len(val.missing),
                               abstained=rec.abstained, field_confidence=conf, prior_rejection=prior_rejection,
                               revision_after_approval=revision_after_approval,
                               possible_duplicates=[r["id"] for r in intake_meta.get("related_cases", [])]))
    deadline = (keep_deadline or _deadline(facts, now)) if pol.requires_human else None

    async with db.pool.connection() as c:
        async with c.transaction():
            await c.execute("UPDATE recommendations SET status='superseded' WHERE case_id=%s AND status IN ('proposed','abstained')", (case_id,))
            rid = (await (await c.execute(
                "INSERT INTO recommendations(case_id,version,round,options,chosen_option,total_cost,rationale,evidence,inputs_used,inputs_total,"
                "sufficiency,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (case_id, version, round_no, Jsonb(rec.options), rec.recommended_action, rec.total_cost, rec.rationale,
                 Jsonb({"ranking": rec.ranking, "rationale_source": rec.rationale_source, "gate_events": rec.gate_events,
                        "risks_of_choice": rec.risks_of_choice, "risk": {"level": rk.level, "rule": rk.rule, "llm": rk.llm,
                                                                         "conflict": rk.conflict, "reasons": rk.reasons}}),
                 val.inputs_used, val.inputs_total, rec.sufficiency, "abstained" if rec.abstained else "proposed"))).fetchone())["id"]
            await c.execute("INSERT INTO policy_decisions(case_id,version,requires_human,rules_fired,inputs) VALUES (%s,%s,%s,%s,%s)",
                            (case_id, version, pol.requires_human, Jsonb(pol.rules),
                             Jsonb({"amount": rec.total_cost, "risk": rk.level, "degraded": degraded, "agent_conflict": rk.conflict,
                                    "abstained": rec.abstained, "field_confidence": conf, "tiers": pol.tiers})))
            await c.execute("UPDATE case_versions SET facts=%s WHERE case_id=%s AND version=%s", (Jsonb(facts), case_id, version))
            await agent_message(c, case_id, "risk", "recommend", "artifact", {"level": rk.level, "rule": rk.rule["level"],
                                                                              "llm": (rk.llm or {}).get("risk"), "conflict": rk.conflict})
            await agent_message(c, case_id, "recommend", "policy", "artifact", {"action": rec.recommended_action, "cost": rec.total_cost,
                                                                                "sufficiency": rec.sufficiency, "abstained": rec.abstained})
            await agent_message(c, case_id, "policy", "orchestrator", "artifact", pol.as_json())
            await audit.write_in_txn(c, case_id, "agent", "policy", "policy_decision", "success",
                                     ("human approval required: " + ", ".join(r["id"] for r in pol.rules)) if pol.requires_human
                                     else "auto-clear: zero spend, low/medium risk, clean", refs={"rules": [r["id"] for r in pol.rules]})
            await audit.write_in_txn(c, case_id, "agent", "recommend", "recommendation", "degraded" if degraded else "success",
                                     f"{rec.recommended_action} cost INR {int(rec.total_cost)}; sufficiency {rec.sufficiency}; "
                                     f"inputs {val.inputs_used}/{val.inputs_total}", refs={"recommendation": rid, "gates": rec.gate_events})
            await transition(case_id, "ANALYZED", "RECOMMENDED", "agent", "recommend", f"recommendation {rid} proposed", conn=c)
            if pol.requires_human:
                for t in pol.tiers:
                    await c.execute("INSERT INTO approvals(case_id,recommendation_id,round,tier,required_role,amount,deadline_at,created_at) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (case_id, rid, round_no, t["tier"], t["role"], rec.total_cost, deadline, now))
                await transition(case_id, "RECOMMENDED", "AWAITING_APPROVAL", "system", "policy", "waiting for " + ", ".join(
                    t["role"] for t in pol.tiers), conn=c, sets={"needs_human": True, "degraded": degraded, "deadline_at": deadline})
            else:
                await c.execute("UPDATE recommendations SET status='approved' WHERE id=%s", (rid,))
                await transition(case_id, "RECOMMENDED", "APPROVED", "system", "policy", "auto-cleared by policy", conn=c,
                                 sets={"needs_human": False, "degraded": degraded})
