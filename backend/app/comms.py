"""Comms agent: drafts the messages that follow an approved plan.

Code writes the facts and a plain template. The LLM may only polish the wording, and every polished draft passes gates:
  * numbers: the numbers in the polished text must be exactly the numbers in the template (none added, none dropped);
  * no links or e-mail addresses;
  * supplier-facing (external) drafts: no money amounts, internal terms (budget, approval, risk, policy...) or other suppliers' names.
A draft that fails a gate, or any LLM outage, falls back to the template wording. The failure is audited either way.
External drafts are saved as `draft`: a human must review and release them. Internal updates are queued directly.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from . import audit, db, outbox
from .codes import Reason
from .llm import LLMUnavailable, gateway
from .orchestrator import agent_message
from .schemas import CommsDraft

SENDER_NAME = "Retail Operations"
SYSTEM = (
    "You are an email copy editor for a retail operations team. You receive a draft email. Improve the wording so it is clear, "
    "polite and concise. Keep every fact exactly as given. Do not add, remove or change any number, name or commitment. "
    "Do not add links or e-mail addresses. Do not mention internal costs, budgets, approvals, risk or other suppliers. "
    "Return the new subject and the new body."
)
SUPPLIER_ACTIONS = ("expedite_alternate_supplier", "wait_for_original_supplier")
LIVE_STATES = ("APPROVED", "EXECUTING", "MONITORING")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+", re.I)
_INTERNAL = re.compile(r"(₹|\b(inr|rs\.?|rupees?|budget\w*|approv\w*|margin\w*|risk\w*|penalt\w*|cheap\w*|cost\w*|polic\w*|escalat\w*|"
                       r"regional|finance\w*|confiden\w*|competitor\w*|alternat\w*|shortfall|internal)\b)", re.I)


# ───────────── pure: templates and gates ─────────────
def numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in _NUMBER.finditer(text):
        try:
            out.add(Decimal(m.group(0).replace(",", "").rstrip(".")))
        except Exception:  # noqa: BLE001
            continue
    return out


def stakeholder_template(case_id: str, store: str | None, opt: dict, cost: int) -> tuple[str, str]:
    lines = [f"The plan for {case_id} is approved.", "", f"Action: {opt['description']}."]
    if store:
        lines.append(f"Store: {store}.")
    lines.append(f"Additional cost: INR {cost}." if cost else "No additional spend.")
    lines += ["", "Next: the team carries the plan out and keeps the case up to date. "
                  "Any message to a supplier waits for a manager's review before it is sent."]
    return f"{case_id} approved: {opt['description'][:70]}", "Hello,\n\n" + "\n".join(lines) + f"\n\n{SENDER_NAME}"


def supplier_template(case_id: str, store: str | None, supplier: str, item: str, opt: dict, delay_days: int | None) -> tuple[str, str]:
    where = f" for our {store} store" if store else ""
    if opt["action"] == "expedite_alternate_supplier":
        ask = (f"We would like to order {opt['units']} x {item}{where}. Your listed lead time is {opt['lead_time_days']} day(s); "
               "please confirm availability, the delivery date and the order terms.")
        subject = f"Order request: {item} (ref {case_id})"
    else:
        ask = (f"We are following up on our open order for {item}{where}. We understand it is delayed"
               + (f" by {delay_days} day(s)" if delay_days else "") + ". Please confirm the revised delivery date and quantity.")
        subject = f"Delivery update needed: {item} (ref {case_id})"
    return subject, f"Hello {supplier} team,\n\n{ask}\n\nReference: {case_id}\n\nThank you,\n{SENDER_NAME}"


def gate(tpl: tuple[str, str], out: tuple[str, str], external: bool, other_names: list[str]) -> list[tuple[Reason, str]]:
    """Returns the gate failures for a polished draft (empty list = passes)."""
    subj, body = out
    text, base = f"{subj}\n{body}", f"{tpl[0]}\n{tpl[1]}"
    fails: list[tuple[Reason, str]] = []
    if not subj.strip() or not body.strip() or len(body) > 2 * len(tpl[1]) + 400:
        fails.append((Reason.GATE_COMMS_NUMBER, "empty or oversized draft"))
    have, want = numbers(text), numbers(base)
    if have - want:
        fails.append((Reason.GATE_COMMS_NUMBER, f"numbers not in the facts: {sorted(str(x) for x in have - want)[:5]}"))
    if want - have:
        fails.append((Reason.GATE_COMMS_NUMBER, f"numbers dropped from the facts: {sorted(str(x) for x in want - have)[:5]}"))
    if _LINK.search(text):
        fails.append((Reason.GATE_COMMS_LEAK, "link or e-mail address in the draft"))
    if external:
        m = _INTERNAL.search(text)
        if m:
            fails.append((Reason.GATE_COMMS_LEAK, f"internal term in a supplier-facing draft: '{m.group(0)}'"))
        low = text.lower()
        for n in other_names:
            if n.lower() in low:
                fails.append((Reason.GATE_COMMS_LEAK, "another supplier is named in a supplier-facing draft"))
                break
    return fails


# ───────────── LLM polish with fallback ─────────────
async def polish(case_id: str, audience: str, tpl: tuple[str, str], other_names: list[str], use_llm: bool = True) -> tuple[str, str, str, list[str]]:
    """Returns (subject, body, source, gate_events). Never raises for model problems: the template is always a valid answer."""
    external = audience == "external"
    if external and (bad := gate(tpl, tpl, True, other_names)):       # the template itself must be clean
        raise ValueError(f"template failed the {bad[0][0].value} gate: {bad[0][1]}")
    if not use_llm:
        return tpl[0], tpl[1], "template", []
    try:
        r = await gateway.generate("comms", SYSTEM, "<draft>\n" + json.dumps({"audience": audience, "subject": tpl[0], "body": tpl[1]}) + "\n</draft>",
                                   CommsDraft, case_id=case_id, actor="comms")
    except LLMUnavailable:
        return tpl[0], tpl[1], "template", ["llm_unavailable"]             # already audited by the gateway
    out = (r.data.subject.strip(), r.data.body.strip())
    fails = gate(tpl, out, external, other_names)
    if fails:
        for code, why in fails:
            await audit.write(case_id, "agent", "comms", "gate_reject", "blocked", f"polished draft rejected: {why}; using the template wording",
                              reason=code, refs={"audience": audience})
        return tpl[0], tpl[1], "template", [c.value for c, _ in fails]
    return out[0], out[1], "llm", []


# ───────────── the agent ─────────────
async def _load(case_id: str) -> dict | None:
    async with db.pool.connection() as c:
        case = await (await c.execute("SELECT * FROM cases WHERE id=%s", (case_id,))).fetchone()
        if not case or case["status"] not in LIVE_STATES:
            return None
        rec = await (await c.execute("SELECT * FROM recommendations WHERE case_id=%s AND status='approved' ORDER BY id DESC LIMIT 1", (case_id,))).fetchone()
        if not rec:
            return None
        ver = await (await c.execute("SELECT facts FROM case_versions WHERE case_id=%s AND version=%s", (case_id, case["version"]))).fetchone()
        sup = await (await c.execute("SELECT id,name,email FROM suppliers")).fetchall()
    opt = next((o for o in rec["options"] if o["action"] == rec["chosen_option"]), None)
    return {"case": case, "rec": rec, "opt": opt, "facts": (ver or {}).get("facts") or {}, "suppliers": sup} if opt else None


def _supplier_for(ctx: dict) -> dict | None:
    opt, facts = ctx["opt"], ctx["facts"]
    by_id = {s["id"]: s for s in ctx["suppliers"]}
    if opt["action"] == "expedite_alternate_supplier":
        return next((s for s in ctx["suppliers"] if s["name"] == opt.get("supplier")), None)
    return by_id.get((facts.get("supplier") or {}).get("id"))


async def draft_for_approval(case_id: str, use_llm: bool = True) -> dict:
    """Called once a plan is fully approved. Idempotent: a repeat call creates nothing new and costs no LLM call."""
    audit.start_trace()
    res = {"queued": 0, "drafts": 0, "skipped": 0}
    try:
        ctx = await _load(case_id)
        if ctx is None:
            return res
        case, rec, opt, facts = ctx["case"], ctx["rec"], ctx["opt"], ctx["facts"]
        store = (facts.get("store") or {}).get("name")
        cost = int(rec["total_cost"] or 0)
        async with db.pool.connection() as c:
            internal = {}
            for to in [(await (await c.execute("SELECT email FROM users WHERE id=%s AND active", (case["requester_id"],))).fetchone() or {}).get("email"),
                       *[u["email"] for u in await outbox.recipients(c, "manager", case["region"])]]:
                if to:
                    internal.setdefault(to.lower(), to)
        plan = [("stakeholder_update", "internal", to, stakeholder_template(case_id, store, opt, cost)) for to in internal.values()]
        if opt["action"] in SUPPLIER_ACTIONS:
            sup = _supplier_for(ctx)
            item = opt.get("item") or (facts.get("item") or {}).get("name")
            if sup and sup["email"] and item:
                plan.append(("supplier_request", "external", sup["email"],
                             supplier_template(case_id, store, sup["name"], item, opt, facts.get("delay_days"))))
            else:
                res["skipped"] += 1
                await audit.write(case_id, "agent", "comms", "comms_skipped", "skipped", "no supplier contact on file; no supplier draft written",
                                  reason=Reason.DATA_MISSING, refs={"rec_id": rec["id"]})
        for kind, audience, to, tpl in plan:
            key = f"comm-{case_id}-{rec['id']}-{kind}-{audit.sha256_text(to.lower())[:10]}"
            async with db.pool.connection() as c:
                if await (await c.execute("SELECT 1 FROM outbox WHERE message_id=%s", (key,))).fetchone():
                    continue
            others = [s["name"] for s in ctx["suppliers"] if s["email"] != to]
            try:
                subject, body, source, events = await polish(case_id, audience, tpl, others, use_llm)
            except ValueError as exc:                                      # a template that breaks its own gate is a bug: do not send it
                res["skipped"] += 1
                await audit.write(case_id, "agent", "comms", "comms_skipped", "failure", str(exc), severity="error", reason=Reason.GATE_COMMS_LEAK)
                continue
            external, stale = audience == "external", False
            async with db.pool.connection() as c:
                async with c.transaction():
                    # the plan may have been cancelled or revised while the model was writing: re-check under the case lock
                    cur = await (await c.execute("SELECT s.status, r.status AS rec_status FROM cases s JOIN recommendations r ON r.id=%s "
                                                 "WHERE s.id=%s FOR UPDATE OF s", (rec["id"], case_id))).fetchone()
                    stale = not cur or cur["status"] not in LIVE_STATES or cur["rec_status"] != "approved"
                    n = 0 if stale else await outbox.queue(c, case_id, kind, to, subject, body, key, audience=audience,
                                                           status="draft" if external else "pending", source=source)
                    if n:
                        await agent_message(c, case_id, "comms", "orchestrator", "artifact",
                                            {"kind": kind, "audience": audience, "source": source, "gate_events": events, "rec_id": rec["id"]})
                        if external:
                            await outbox.notify_role(c, case, "draft_review", f"drv-{key}", f"Supplier message to review: {case_id}",
                                                     f"A message to a supplier for {case_id} is waiting for your review. Open the case, check the wording and release or discard it.",
                                                     "manager")
            if stale:
                res["skipped"] += 1
                await audit.write(case_id, "agent", "comms", "comms_skipped", "skipped", "the plan changed while the message was being written; nothing queued",
                                  reason=Reason.WORKFLOW_REVISED, refs={"rec_id": rec["id"], "kind": kind})
                break
            if n:
                res["drafts" if external else "queued"] += 1
                await audit.write(case_id, "agent", "comms", "comms_drafted", "success",
                                  f"{kind} {'saved as a draft for human review' if external else 'queued'} ({source} wording)",
                                  refs={"message_key": key, "kind": kind, "rec_id": rec["id"], "source": source, "gate_events": events})
    except Exception as exc:  # noqa: BLE001 - the approval is already committed; a comms problem must never undo or hide it
        await audit.write(case_id, "agent", "comms", "comms_drafted", "failure", f"could not draft messages: {type(exc).__name__}",
                          severity="error", reason=Reason.COMM_SEND_FAILED,
                          error=audit.error_obj(type(exc).__name__, "comms", None, audit.trace_id_var.get(), str(exc)))
    return res
