"""Reporting agent: the closing summary of one case and the management summary of the whole operation.

Same rule as the rest of the platform: code decides, the model only words it.
  * The model sees ONLY facts read from the database (never the raw e-mail, never free text typed by people), wrapped in <facts> tags.
  * Every model answer passes gates before anyone sees it: a number the facts do not contain, a link or e-mail address,
    an empty or oversized answer. A failed gate (or any model outage) falls back to a plain template built from the same facts,
    and the rejection is audited. The template itself always passes the gates.
  * Management numbers are plain SQL. The narrative only describes them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import audit, clock, db
from .approvals import Denied
from .codes import Reason
from .comms import _LINK, numbers
from .llm import LLMUnavailable, gateway
from .schemas import Role, SummaryOut, UserOut

CLOSING_SYSTEM = (
    "You are a reporting assistant for a retail operations team. You receive facts about one closed case between <facts> tags. "
    "Write a short factual closing summary for the team. Use only the facts given. Do not add, change or guess any number, name or date. "
    "Do not add links or e-mail addresses. Do not give advice. Return a headline, a short account of what happened, "
    "the decisions that were made, and any open items."
)
MANAGEMENT_SYSTEM = (
    "You are a reporting assistant for a retail operations team. You receive the current operations figures between <facts> tags. "
    "Write a short factual summary for senior management. Use only the facts given. Do not add, change or guess any number, name or date. "
    "Do not add links or e-mail addresses. Do not give advice. Return a headline, a short account of the situation, "
    "the decisions that are waiting, and the items that need attention."
)
MAX_HEADLINE, MAX_BODY, MAX_ITEMS, MAX_ITEM = 160, 1500, 8, 300
OPEN_NOT = ("CLOSED", "CANCELLED", "DUPLICATE", "SUPERSEDED", "SPLIT")
MANAGEMENT_ROLES = (Role.manager, Role.regional_head, Role.finance_director, Role.admin)
CLOSING_ROLES = (Role.manager, Role.regional_head, Role.finance_director)


# ───────────────────────── pure: gates and templates ─────────────────────────
def facts_text(facts: dict) -> str:
    return json.dumps(facts, sort_keys=True, default=str)


def gate(out: SummaryOut, facts: dict) -> list[tuple[Reason, str]]:
    """Failures for a model-written summary (empty list = it may be shown)."""
    fails: list[tuple[Reason, str]] = []
    items = [*out.decisions, *out.open_items]
    if (not out.headline.strip() or not out.what_happened.strip() or len(out.headline) > MAX_HEADLINE or len(out.what_happened) > MAX_BODY
            or len(out.decisions) > MAX_ITEMS or len(out.open_items) > MAX_ITEMS or any(len(i) > MAX_ITEM for i in items)):
        fails.append((Reason.GATE_UNGROUNDED, "empty or oversized summary"))
    text = "\n".join([out.headline, out.what_happened, *items])
    extra = numbers(text) - numbers(facts_text(facts))
    if extra:
        fails.append((Reason.GATE_NUMERIC_MISMATCH, f"numbers not in the facts: {sorted(str(x) for x in extra)[:5]}"))
    if _LINK.search(text):
        fails.append((Reason.GATE_COMMS_LEAK, "link or e-mail address in the summary"))
    return fails


def _when(ts) -> str:
    return ts.astimezone(timezone.utc).strftime("%d %b %H:%M UTC") if ts else "unknown time"


def closing_template(f: dict) -> SummaryOut:
    plan = f.get("plan") or {}
    where = f" at {f['store']}" if f.get("store") else ""
    head = f"{f['case_id']} closed: {plan['description']}" if plan.get("description") else f"{f['case_id']} closed"
    parts = [f"{f['case_id']} was a {f.get('priority') or 'unrated'} case{where} with {f.get('risk_level') or 'unrated'} risk."]
    if f.get("shortfall"):
        parts.append(f"The shortfall was {f['shortfall']} units.")
    if plan.get("description"):
        parts.append(f"The approved plan: {plan['description']}. Cost: INR {plan.get('cost_inr', 0)}.")
    if f.get("rejected_plans"):
        parts.append(f"{f['rejected_plans']} earlier plan(s) were rejected before this one.")
    if f.get("revisions"):
        parts.append(f"The facts changed {f['revisions']} time(s) after the case was opened.")
    if f.get("reopened"):
        parts.append(f"The case was reopened {f['reopened']} time(s).")
    m = f.get("messages") or {}
    parts.append(f"Messages delivered: {m.get('sent', 0)}. Not delivered: {m.get('dead', 0) + m.get('failed_or_unconfirmed', 0)}.")
    decisions = [f"{a['role_required']} {a['decision']} on {a['decided_at']}" for a in f.get("approvals", []) if a.get("decision")]
    open_items = [f"{n} message(s) are {k.replace('_', ' ')}" for k, n in (("dead", m.get("dead", 0)), ("failed_or_unconfirmed", m.get("failed_or_unconfirmed", 0))) if n]
    return SummaryOut(headline=head[:MAX_HEADLINE], what_happened=" ".join(parts)[:MAX_BODY], decisions=decisions[:MAX_ITEMS], open_items=open_items)


def management_template(f: dict) -> SummaryOut:
    cs, ap, sp, cm = f["cases"], f["approvals"], f["spend_inr"], f["comms"]
    head = f"{f['scope']}: {cs['open']} open case(s), {ap['pending']} awaiting a decision"
    parts = [f"{cs['open']} cases are open and {cs['closed']} are closed. {f['open_p1']} open case(s) are P1.",
             f"INR {sp['approved']} is approved and INR {sp['pending_approval']} is waiting for approval.",
             f"{ap['overdue']} approval(s) are overdue."]
    if ap["avg_decision_hours"] is not None:
        parts.append(f"A decision takes {ap['avg_decision_hours']} hour(s) on average.")
    parts.append(f"Messages: {cm['sent']} delivered, {cm['dead']} undeliverable, {cm['failed']} failing or unconfirmed.")
    waiting = [f"{a['case_id']}: {', '.join(a['reasons'])}" for a in f["attention"] if "approval overdue" in a["reasons"] or "needs a human" in a["reasons"]]
    attention = [f"{a['case_id']}: {', '.join(a['reasons'])}" for a in f["attention"]]
    return SummaryOut(headline=head[:MAX_HEADLINE], what_happened=" ".join(parts)[:MAX_BODY], decisions=waiting[:MAX_ITEMS], open_items=attention[:MAX_ITEMS])


# ───────────────────────── the model call with gates and fallback ─────────────────────────
async def _write(case_id: str | None, system: str, facts: dict, template: SummaryOut, use_llm: bool) -> tuple[SummaryOut, str, list[str]]:
    """Returns (summary, source, gate_events). Never raises for model problems: the template is always a valid answer."""
    if not use_llm:
        return template, "template", []
    try:
        r = await gateway.generate("report", system, "<facts>\n" + facts_text(facts) + "\n</facts>", SummaryOut, case_id=case_id, actor="reporting")
    except LLMUnavailable:
        return template, "template", ["llm_unavailable"]                       # already audited by the gateway
    fails = gate(r.data, facts)
    if fails:
        for code, why in fails:
            await audit.write(case_id, "agent", "reporting", "gate_reject", "blocked", f"model summary rejected: {why}; using the template",
                              reason=code)
        return template, "template", [c.value for c, _ in fails]
    return r.data, "llm", []


async def _store(case_id: str | None, kind: str, content: dict, source: str, mhash: str, user_id: str | None) -> dict:
    from psycopg.types.json import Jsonb
    async with db.pool.connection() as c:
        return await (await c.execute(
            "INSERT INTO summaries(case_id,kind,content,source,metrics_hash,created_by) VALUES (%s,%s,%s,%s,%s,%s) "
            "RETURNING id, kind, content, source, metrics_hash, created_at", (case_id, kind, Jsonb(content), source, mhash, user_id))).fetchone()


# ───────────────────────── closing summary ─────────────────────────
async def closing_facts(case_id: str) -> dict | None:
    """Everything the closing summary may say, read from the database. No raw message text, no titles, no human free text."""
    async with db.pool.connection() as c:
        case = await (await c.execute("SELECT * FROM cases WHERE id=%s", (case_id,))).fetchone()
        if case is None:
            return None
        ver = ((await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=%s", (case_id, case["version"]))).fetchone()) or {}).get("facts") or {}
        rec = await (await c.execute("SELECT * FROM recommendations WHERE case_id=%s AND status='approved' ORDER BY id DESC LIMIT 1", (case_id,))).fetchone()
        rejected = (await (await c.execute("SELECT count(*) n FROM recommendations WHERE case_id=%s AND status='rejected'", (case_id,))).fetchone())["n"]
        appr = await (await c.execute(
            "SELECT a.required_role, a.status, a.decided_at, u.role AS decided_by_role FROM approvals a LEFT JOIN users u ON u.id=a.approver_id "
            "WHERE a.case_id=%s AND a.recommendation_id IS NOT DISTINCT FROM %s AND a.status IN ('approved','rejected') ORDER BY a.decided_at, a.id",
            (case_id, rec["id"] if rec else None))).fetchall()
        msgs = await (await c.execute("SELECT status, reviewed_by FROM outbox WHERE case_id=%s AND kind IN ('stakeholder_update','supplier_request')", (case_id,))).fetchall()
        corrected = (await (await c.execute("SELECT count(*) n FROM extracted_fields WHERE case_id=%s AND version=%s AND state='corrected'", (case_id, case["version"]))).fetchone())["n"]
        events = await (await c.execute("SELECT ts, split_part(summary, ':', 1) AS step FROM audit_log WHERE chain_key=%s AND action='status_change' "
                                        "AND outcome='success' ORDER BY seq", (case_id,))).fetchall()
        items = await (await c.execute("SELECT key FROM closure_items WHERE case_id=%s AND epoch=%s ORDER BY id", (case_id, case["reopen_count"]))).fetchall()
    opt = next((o for o in (rec["options"] if rec else []) if o["action"] == rec["chosen_option"]), None)
    return {
        "case_id": case_id, "status": case["status"], "priority": case["priority"], "risk_level": case["risk_level"], "region": case["region"],
        "store": (ver.get("store") or {}).get("name"), "item": (ver.get("item") or {}).get("name"), "shortfall": ver.get("shortfall"),
        "plan": {"action": rec["chosen_option"], "description": (opt or {}).get("description"), "cost_inr": int(rec["total_cost"] or 0)} if rec else None,
        "approvals": [{"role_required": a["required_role"], "decision": a["status"], "decided_by_role": a["decided_by_role"],
                       "decided_at": _when(a["decided_at"])} for a in appr],
        "rejected_plans": rejected, "revisions": case["version"] - 1, "reopened": case["reopen_count"], "fields_corrected_by_humans": corrected,
        "messages": {"sent": sum(m["status"] == "sent" for m in msgs), "dead": sum(m["status"] == "dead" for m in msgs),
                     "failed_or_unconfirmed": sum(m["status"] in ("failed", "delivery_unknown") for m in msgs),
                     "discarded_by_reviewer": sum(m["status"] == "cancelled" and m["reviewed_by"] is not None for m in msgs)},
        "closure_confirmed_by_people": [i["key"] for i in items],
        "timeline": [{"at": _when(e["ts"]), "step": e["step"].strip()} for e in events][:30],
    }


async def closing_summary(case_id: str, user: UserOut | None, use_llm: bool = True) -> dict | None:
    """Write and store the closing summary. Never raises: a closed case must not be affected by a reporting problem."""
    try:
        facts = await closing_facts(case_id)
        if facts is None:
            return None
        out, source, events = await _write(case_id, CLOSING_SYSTEM, facts, closing_template(facts), use_llm)
        mhash = audit.sha256_text(facts_text(facts))
        row = await _store(case_id, "closing", out.model_dump(), source, mhash, user.id if user else None)
        await audit.write(case_id, "agent", "reporting", "summary_generated", "success" if source == "llm" else "degraded",
                          f"closing summary written ({source} wording)", model=None,
                          refs={"summary_id": row["id"], "source": source, "metrics_hash": mhash[:16], "gate_events": events})
        return row
    except Exception as exc:  # noqa: BLE001
        await audit.write(case_id, "agent", "reporting", "summary_generated", "failure", f"could not write the closing summary: {type(exc).__name__}",
                          severity="error", error=audit.error_obj(type(exc).__name__, "reporting", None, audit.trace_id_var.get(), str(exc)))
        return None


async def latest_closing(case_id: str) -> dict | None:
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT id, kind, content, source, metrics_hash, created_at FROM summaries WHERE case_id=%s AND kind='closing' "
                                      "ORDER BY id DESC LIMIT 1", (case_id,))).fetchone()


# ───────────────────────── management report ─────────────────────────
def _scope(user: UserOut) -> tuple[str, list, str]:
    """(sql condition on cases alias c, params, label). Managers and regional heads see their region; finance and admin see all."""
    if user.role in (Role.finance_director, Role.admin):
        return "true", [], "all regions"
    return "(c.region = %s OR c.region IS NULL)", [user.region], f"region {user.region}"


def check_management_role(user: UserOut) -> None:
    if user.role not in MANAGEMENT_ROLES:
        raise Denied(403, f"a {user.role.value} may not view the management report", Reason.POLICY_UNAUTHORIZED)


def _round(x) -> float | None:
    return None if x is None else round(float(x), 1)


async def management_metrics(user: UserOut) -> dict:
    """Pure database arithmetic. Nothing here is written by a model."""
    check_management_role(user)
    cond, params, label = _scope(user)
    now = clock.now()
    async with db.pool.connection() as c:
        q = lambda sql, extra=(): c.execute(sql, [*params, *extra])  # noqa: E731
        by_status = {r["status"]: r["n"] for r in await (await q(f"SELECT c.status, count(*) n FROM cases c WHERE {cond} GROUP BY c.status")).fetchall()}
        open_rows = await (await q(
            "SELECT c.id,c.title,c.priority,c.risk_level,c.status,c.needs_human,c.degraded,c.injection_flag,c.created_at FROM cases c "
            f"WHERE {cond} AND c.status <> ALL(%s) ORDER BY c.created_at", [list(OPEN_NOT)])).fetchall()
        pend = await (await q(
            "SELECT a.case_id, a.deadline_at FROM approvals a JOIN cases c ON c.id=a.case_id "
            f"WHERE {cond} AND a.status='pending' AND c.status IN ('AWAITING_APPROVAL','ESCALATED')")).fetchall()
        dec = {r["status"]: r for r in await (await q(
            "SELECT a.status, count(*) n, avg(extract(epoch FROM (a.decided_at - a.created_at))/3600.0) h FROM approvals a JOIN cases c ON c.id=a.case_id "
            f"WHERE {cond} GROUP BY a.status")).fetchall()}
        spend = await (await q(
            "SELECT coalesce(sum(r.total_cost) FILTER (WHERE r.status='approved' AND c.status IN ('APPROVED','EXECUTING','MONITORING','CLOSED')),0) approved, "
            "coalesce(sum(r.total_cost) FILTER (WHERE r.status='proposed' AND c.status IN ('AWAITING_APPROVAL','ESCALATED')),0) pending "
            f"FROM recommendations r JOIN cases c ON c.id=r.case_id WHERE {cond}")).fetchone()
        comm = {r["status"]: r["n"] for r in await (await q(
            f"SELECT o.status, count(*) n FROM outbox o JOIN cases c ON c.id=o.case_id WHERE {cond} GROUP BY o.status")).fetchall()}
        dead_cases = {r["case_id"] for r in await (await q(
            f"SELECT DISTINCT o.case_id FROM outbox o JOIN cases c ON c.id=o.case_id WHERE {cond} AND o.status='dead'")).fetchall()}
        closed = await (await q(
            "SELECT count(*) n, avg(extract(epoch FROM (c.closed_at - c.created_at))/3600.0) h, count(*) FILTER (WHERE c.reopen_count > 0) re "
            f"FROM cases c WHERE {cond} AND c.status='CLOSED' AND c.closed_at IS NOT NULL")).fetchone()

    overdue_cases = {p["case_id"] for p in pend if p["deadline_at"] is not None and p["deadline_at"] < now}
    by_risk: dict[str, int] = {}
    for r in open_rows:
        by_risk[r["risk_level"] or "Unrated"] = by_risk.get(r["risk_level"] or "Unrated", 0) + 1
    attention = []
    for r in open_rows:
        why = [w for w, hit in (("P1", r["priority"] == "P1"), ("critical risk", r["risk_level"] == "Critical"),
                                ("approval overdue", r["id"] in overdue_cases), ("escalated", r["status"] == "ESCALATED"),
                                ("needs a human", r["needs_human"]), ("degraded mode", r["degraded"]),
                                ("injection flagged", r["injection_flag"]), ("undeliverable message", r["id"] in dead_cases)) if hit]
        if why:
            attention.append({"case_id": r["id"], "title": r["title"], "reasons": why})
    attention.sort(key=lambda a: (-len(a["reasons"]), a["case_id"]))
    avg_h = [float(dec[s]["h"]) for s in ("approved", "rejected") if s in dec and dec[s]["h"] is not None]
    n_dec = sum(dec[s]["n"] for s in ("approved", "rejected") if s in dec)
    weighted = sum(float(dec[s]["h"]) * dec[s]["n"] for s in ("approved", "rejected") if s in dec and dec[s]["h"] is not None)
    return {
        "scope": label, "currency": "INR", "generated_at": now.isoformat(),
        "cases": {"total": sum(by_status.values()), "open": len(open_rows), "closed": by_status.get("CLOSED", 0),
                  "cancelled": by_status.get("CANCELLED", 0), "by_status": by_status},
        "open_by_risk": by_risk, "open_p1": sum(r["priority"] == "P1" for r in open_rows),
        "needs_human": sum(bool(r["needs_human"]) for r in open_rows), "degraded": sum(bool(r["degraded"]) for r in open_rows),
        "injection_flagged": sum(bool(r["injection_flag"]) for r in open_rows),
        "approvals": {"pending": len(pend), "overdue": len(overdue_cases), "approved": dec.get("approved", {}).get("n", 0),
                      "rejected": dec.get("rejected", {}).get("n", 0),
                      "avg_decision_hours": _round(weighted / n_dec) if avg_h and n_dec else None},
        "spend_inr": {"approved": int(spend["approved"]), "pending_approval": int(spend["pending"])},
        "comms": {"sent": comm.get("sent", 0), "waiting": comm.get("pending", 0) + comm.get("sending", 0), "drafts": comm.get("draft", 0),
                  "failed": comm.get("failed", 0) + comm.get("delivery_unknown", 0), "dead": comm.get("dead", 0)},
        "closure": {"closed": closed["n"], "avg_cycle_hours": _round(closed["h"]), "reopened": closed["re"]},
        "attention": attention[:10],
    }


def narrative_facts(metrics: dict) -> dict:
    """What the model may see: the numbers and case ids. No titles (free text), no clock reading (so the same numbers hit the cache)."""
    f = {k: v for k, v in metrics.items() if k != "generated_at"}
    f["attention"] = [{"case_id": a["case_id"], "reasons": a["reasons"]} for a in metrics["attention"]]
    return f


async def management_narrative(user: UserOut, use_llm: bool = True) -> dict:
    metrics = await management_metrics(user)                                    # also checks the role
    facts = narrative_facts(metrics)
    out, source, events = await _write(None, MANAGEMENT_SYSTEM, facts, management_template(facts), use_llm)
    mhash = audit.sha256_text(facts_text(facts))
    row = await _store(None, "management", out.model_dump(), source, mhash, user.id)
    await audit.write(None, "agent", "reporting", "summary_generated", "success" if source == "llm" else "degraded",
                      f"management summary written for {metrics['scope']} ({source} wording)",
                      refs={"summary_id": row["id"], "source": source, "metrics_hash": mhash[:16], "gate_events": events, "requested_by": user.id})
    return {**row, "gate_events": events, "generated_at": datetime.now(timezone.utc).isoformat()}
