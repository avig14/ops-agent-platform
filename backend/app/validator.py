"""Validator agent (code only): resolves entities, parses numbers/dates, cross-checks the email against the systems of
record, and reports what is missing, conflicting or stale. No LLM, so it cannot be talked into anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import adapters, audit
from .codes import Reason
from .extract import Field, to_decimal

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

REQUIRED = {
    "delivery_delay": ["location", "quantity_affected", "delay_days"],
    "inventory_shortage": ["location", "quantity_affected"],
    "supplier_issue": ["location"],
    "urgent_approval": ["location"],
    "customer_commitment": ["location"],
    "cancellation": [],
    "status_update": [],
    "other": ["location"],
    "unclear": [],
}


@dataclass
class Validation:
    facts: dict
    missing: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    not_fetched: list[dict] = field(default_factory=list)
    inputs_used: int = 0
    inputs_total: int = 0
    degraded: bool = False


WEEKDAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"


def _mk(y: int, m: int, d: int) -> date | None:
    """A real calendar date or None ('31 Feb' must not crash the pipeline)."""
    try:
        return date(y + 2000 if y < 100 else y, m, d)
    except ValueError:
        return None


def parse_needed_by(value: str | None, received: datetime) -> tuple[date | None, bool]:
    """Read a need date in IST. Returns (date, approximate). None means unreadable: the caller must flag it, not guess.
    Day-first numeric dates (7/11 is 7 Nov), as written in India."""
    if not value:
        return None, False
    base = received.astimezone(IST).date()
    v = re.sub(r"\s+", " ", value.strip().lower().replace(",", " ")).strip(" .")
    v = re.sub(r"^(?:on|by|before|latest|till|until|no later than|not later than)\s+", "", v)
    v = re.sub(r"\s+(?:eod|end of day|cob|morning|evening|night|noon|midnight|at \d.*|by \d.*)$", "", v)
    if v == "today":
        return base, False
    if v == "tomorrow":
        return base + timedelta(days=1), False
    if v == "day after tomorrow":
        return base + timedelta(days=2), False
    if v in ("next week", "next wk"):
        return base + timedelta(days=7), True
    if v in ("this week", "end of week", "end of the week"):
        return base + timedelta(days=3), True
    m = re.fullmatch(r"in (\d{1,3}) (day|days|week|weeks)", v)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return base + timedelta(days=n), False
    wd = re.fullmatch(r"(?:(this|next|coming)\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*", v)
    if wd:
        ahead = (WEEKDAYS[wd.group(2)] - base.weekday()) % 7 or 7
        return base + timedelta(days=ahead + (7 if wd.group(1) == "next" and ahead <= 7 - base.weekday() - 1 else 0)), bool(wd.group(1) == "next")
    v = re.sub(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+", "", v)           # "sat 7 nov"
    y = m_ = d_ = None
    if (m := re.fullmatch(_DAY + r"(?: of)? " + _MON + r"(?: (\d{4}))?", v)):          # 7 nov / 7th november 2026
        d_, m_, y = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
    elif (m := re.fullmatch(_MON + r" " + _DAY + r"(?: (\d{4}))?", v)):                # nov 7 / november 7th 2026
        m_, d_, y = MONTHS[m.group(1)], int(m.group(2)), m.group(3)
    elif (m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", v)):              # 2026-11-07
        got = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return got, False
    elif (m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2}|\d{4}))?", v)):   # 7/11 or 07-11-2026 (day first)
        d_, m_, y = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        return None, False
    if y:
        yy = int(y)
        return _mk(yy, m_, d_), False
    got = _mk(base.year, m_, d_)
    if got is None:
        return None, False
    if got < base:                       # no year and already past: the next one coming
        nxt = _mk(base.year + 1, m_, d_)
        return nxt, True
    return got, False


def parse_deadline(value: str | None, received: datetime) -> datetime | None:
    if not value:
        return None
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", value.lower())
    if not m:
        return None
    h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3) == "p":
        h += 12
    day = received.astimezone(IST).date() + (timedelta(days=1) if "tomorrow" in value.lower() else timedelta(0))
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=IST)


def block_reason(missing: list[dict]) -> Reason:
    """Why analysis is blocked: only an unclear intent is a specification problem; otherwise data is missing or conflicting."""
    if missing and all(m["field"] == "intent" for m in missing):
        return Reason.INGEST_UNCLEAR_INTENT
    return Reason.DATA_MISSING if missing else Reason.DATA_CONFLICT


def _val(fields: dict[str, Field], name: str) -> str | None:
    f = fields.get(name)
    return f.value if f is not None and f.usable else None


async def validate(fields: dict[str, Field], intent: str, received_at: datetime, case_id: str | None = None,
                   resolutions: dict | None = None) -> Validation:
    resolutions = resolutions or {}
    v = Validation(facts={"sources": {}, "intent": intent})
    facts = v.facts
    now = received_at

    # ---- unclear intent blocks analysis: we ask what is wanted instead of guessing ----
    if intent == "unclear":
        v.missing.append({"field": "intent", "why": "the request does not say what is needed"})

    # ---- required fields (a missing required field blocks analysis; we never invent) ----
    for name in REQUIRED.get(intent, ["location"]):
        if _val(fields, name) is None:
            v.missing.append({"field": name, "why": "not stated in the request" if fields.get(name) is None or fields[name].state == "missing"
                              else f"value rejected by quality gate ({fields[name].drop_reason.value if fields[name].drop_reason else 'low confidence'})"})

    # ---- entities ----
    store = await adapters.resolve_store(_val(fields, "location"))
    if _val(fields, "location") and not store:
        v.missing.append({"field": "location", "why": f"'{_val(fields, 'location')}' is not a known store"})
        v.warnings.append({"code": Reason.GATE_UNKNOWN_ENTITY.value, "msg": f"unknown location {_val(fields, 'location')!r}"})
    item = await adapters.resolve_item(_val(fields, "item"))
    supplier = await adapters.resolve_supplier(_val(fields, "supplier"))
    if _val(fields, "supplier") and not supplier:
        v.warnings.append({"code": Reason.GATE_UNKNOWN_ENTITY.value, "msg": f"supplier {_val(fields, 'supplier')!r} not in supplier master"})
    facts["store"] = {k: store[k] for k in ("id", "name", "city", "region")} if store else None
    facts["delay_days"] = int(to_decimal(_val(fields, "delay_days")) or 0) or None
    facts["qty_ordered_email"] = int(d) if (d := to_decimal(_val(fields, "quantity_affected"))) is not None else None
    # "30 units short" states the gap, not the total ordered. The number is read by code from the quote, not guessed by the model.
    qf = fields.get("quantity_affected")
    is_gap = bool(qf is not None and qf.usable and qf.signals.get("is_shortfall") and facts["qty_ordered_email"] is not None)
    facts["qty_is_shortfall"] = is_gap
    facts["qty_available_email"] = int(d) if (d := to_decimal(_val(fields, "quantity_available"))) is not None else None
    cost = to_decimal(_val(fields, "additional_cost"))
    facts["additional_cost_stated"] = float(cost) if cost is not None else None

    # ---- systems of record (fault-injectable; every failure is recorded with what it affects) ----
    inputs_total, inputs_used = 8, 0
    inputs_used += 1 if store else 0
    inputs_used += 1 if facts["qty_ordered_email"] is not None else 0
    po = None
    if store and item:
        f = await adapters.get_po(case_id, store["id"], item["id"])
        if f.status == "failed":
            v.not_fetched.append(f.nf)
            v.degraded = True
        elif f.usable and f.data:
            po = f.data[0]
            inputs_used += 1
    if po:
        supplier = supplier or {"id": po["supplier_id"], "name": po["supplier_name"], "lead_time_days": po["lead_time_days"],
                                "reliability": float(po["reliability"])}
        facts["po"] = {"id": po["id"], "qty": po["qty"], "promised_date": str(po["promised_date"])}
        facts["sources"]["supplier"] = "system:purchase_orders" if not _val(fields, "supplier") else "email+system"
        if facts["qty_ordered_email"] is not None and not is_gap and facts["qty_ordered_email"] != po["qty"]:
            v.conflicts.append({"field": "quantity_affected", "email": facts["qty_ordered_email"], "system": po["qty"],
                                "system_source": f"PO {po['id']}", "kind": "conflict"})
    if supplier:
        inputs_used += 1
        facts["supplier"] = {"id": supplier["id"], "name": supplier["name"], "lead_time_days": supplier["lead_time_days"]}
    elif intent in ("delivery_delay", "supplier_issue") and store and not v.degraded:
        v.missing.append({"field": "supplier", "why": "not stated and no open purchase order matches the store and item"})
    if item:
        inputs_used += 1
        facts["item"] = {"id": item["id"], "name": item["name"], "unit_cost": float(item["unit_cost"])}
        # the supplier that normally makes this item: a source of stock even when the sender named nobody (or the wrong one)
        home_sup = await adapters.resolve_supplier(item.get("supplier_id")) if item.get("supplier_id") else None
        if home_sup and home_sup["id"] == item["supplier_id"]:
            facts["item_supplier"] = {"id": home_sup["id"], "name": home_sup["name"], "lead_time_days": home_sup["lead_time_days"]}

    inv_rows = []
    if item:
        inv = await adapters.get_inventory(case_id, item["id"], store["region"] if store else None)
        if inv.status == "failed":
            v.not_fetched.append(inv.nf)
            v.degraded = True
        elif inv.usable:
            inv_rows = inv.data
            inputs_used += 1
            if inv.status != "ok" and inv.nf:
                v.not_fetched.append(inv.nf)
                v.warnings.append({"code": (inv.reason or Reason.DATA_STALE).value,
                                   "msg": f"inventory data {inv.status}" + (f" ({inv.stale_age_s // 3600}h old)" if inv.stale_age_s else "")})
        facts["inventory"] = [{"location": r["location"], "on_hand": r["on_hand"], "age_s": r["age_s"]} for r in inv_rows]
        home = f"Warehouse-{store['region']}" if store else None
        sys_qty = next((r["on_hand"] for r in inv_rows if r["location"] == home), None)
        facts["qty_available_system"] = sys_qty
        if sys_qty is not None and facts["qty_available_email"] is not None and sys_qty != facts["qty_available_email"]:
            stale = inv.status == "stale"
            v.conflicts.append({"field": "quantity_available", "email": facts["qty_available_email"], "system": sys_qty,
                                "system_source": f"inventory {home}", "kind": "stale_snapshot" if stale else "conflict",
                                "snapshot_age_s": max((r["age_s"] for r in inv_rows), default=None)})
        alts = await adapters.get_alternatives(case_id, item["id"], item["name"])
        if alts.status == "failed":
            v.not_fetched.append(alts.nf)
            v.degraded = True
        elif alts.usable:
            inputs_used += 1
            facts["alternatives"] = [{"id": a["id"], "name": a["name"], "unit_cost": float(a["unit_cost"]), "supplier_id": a["supplier_id"],
                                      "supplier_name": a["supplier_name"], "lead_time_days": a["lead_time_days"],
                                      "reliability": float(a["reliability"]), "stock": int(a["stock"])} for a in alts.data]

    # ---- resolve conflicts (human decision recorded in resolutions) ----
    unresolved = []
    for c in v.conflicts:
        choice = resolutions.get(c["field"])
        if choice in ("email", "system"):
            c["resolved"] = choice
            facts["sources"][c["field"]] = choice
        elif c["kind"] == "stale_snapshot":
            c["resolved"] = "email"           # snapshot is old: trust the fresher human statement, but warn
            facts["sources"][c["field"]] = "email(stale-system)"
            v.warnings.append({"code": Reason.DATA_STALE.value, "msg": f"{c['field']}: system snapshot is stale, using email value"})
        else:
            unresolved.append(c)
    facts["unresolved_conflicts"] = [c["field"] for c in unresolved]

    # ---- chosen numbers ----
    ordered = facts["qty_ordered_email"] if facts["qty_ordered_email"] is not None else (po["qty"] if po else None)
    choice = resolutions.get("quantity_available")
    if choice == "system" or (choice is None and facts["qty_available_email"] is None):
        avail = facts.get("qty_available_system")
    else:
        avail = facts["qty_available_email"] if facts["qty_available_email"] is not None else facts.get("qty_available_system")
    if is_gap:
        gap = facts["qty_ordered_email"]
        ordered = (avail + gap) if avail is not None else (po["qty"] if po else None)
        facts["qty_ordered"], facts["qty_available"], facts["shortfall"] = ordered, avail, gap
    else:
        facts["qty_ordered"], facts["qty_available"] = ordered, avail
        facts["shortfall"] = max(ordered - avail, 0) if ordered is not None and avail is not None else None
    if avail is not None:
        inputs_used += 1

    # ---- dates (IST) ----
    nb, approx = parse_needed_by(_val(fields, "needed_by"), now)
    if nb is None and store and store.get("opens_on"):
        nb, approx = store["opens_on"], False
        facts["sources"]["needed_by"] = "system:stores.opens_on"
    stated_nb = _val(fields, "needed_by")
    facts["needed_by_unreadable"] = bool(stated_nb and parse_needed_by(stated_nb, now)[0] is None)
    if facts["needed_by_unreadable"]:
        v.warnings.append({"code": Reason.DATA_PARTIAL.value, "msg": f"could not read the need date {stated_nb!r}; timing is unverified"})
    facts["needed_by"] = nb.isoformat() if nb else None
    facts["needed_by_approx"] = approx
    if nb and nb < now.astimezone(IST).date():
        v.warnings.append({"code": Reason.WORKFLOW_DEADLINE_MISSED.value, "msg": f"the need date {nb.isoformat()} is already in the past"})
    facts["days_to_need"] = (nb - now.astimezone(IST).date()).days if nb else None
    dl = parse_deadline(_val(fields, "approval_deadline"), now)
    facts["approval_deadline"] = dl.isoformat() if dl else None
    facts["deadline_passed_at_ingest"] = bool(dl and dl < now.astimezone(IST))
    if facts["deadline_passed_at_ingest"]:
        v.warnings.append({"code": Reason.WORKFLOW_DEADLINE_MISSED.value, "msg": "stated approval deadline had already passed when the request arrived"})
    if dl:
        inputs_used += 1
    facts["launch_gap_days"] = (facts["delay_days"] - facts["days_to_need"]) if facts["delay_days"] is not None and facts["days_to_need"] is not None else None

    v.inputs_used, v.inputs_total = min(inputs_used, inputs_total), inputs_total
    v.degraded = v.degraded or bool(v.not_fetched)
    facts["missing"] = [m["field"] for m in v.missing]
    facts["data_notes"] = [{"source": n["source"], "reason": n["reason_code"], "affects": n["affects"]} for n in v.not_fetched]
    if v.missing or unresolved:
        await audit.write(case_id, "agent", "validator", "validation", "blocked",
                          f"missing={facts['missing']} unresolved_conflicts={facts['unresolved_conflicts']}",
                          reason=block_reason(v.missing),
                          refs={"missing": facts["missing"], "conflicts": [c["field"] for c in unresolved]})
    return v
