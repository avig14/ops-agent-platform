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


def parse_needed_by(value: str | None, received: datetime) -> tuple[date | None, bool]:
    if not value:
        return None, False
    v, base = value.strip().lower(), received.astimezone(IST).date()
    if v in ("today",):
        return base, False
    if v == "tomorrow":
        return base + timedelta(days=1), False
    if v == "next week":
        return base + timedelta(days=7), True
    if v == "this week":
        return base + timedelta(days=3), True
    m = re.match(r"in (\d+) days?", v)
    if m:
        return base + timedelta(days=int(m.group(1))), False
    m = re.match(r"on ([a-z]+) (\d{1,2})", v)
    if m and m.group(1)[:3] in MONTHS:
        d = date(base.year, MONTHS[m.group(1)[:3]], int(m.group(2)))
        return (d if d >= base else d.replace(year=base.year + 1)), False
    return None, False


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
        if facts["qty_ordered_email"] is not None and facts["qty_ordered_email"] != po["qty"]:
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
    facts["qty_ordered"], facts["qty_available"] = ordered, avail
    facts["shortfall"] = max(ordered - avail, 0) if ordered is not None and avail is not None else None
    if avail is not None:
        inputs_used += 1

    # ---- dates (IST) ----
    nb, approx = parse_needed_by(_val(fields, "needed_by"), now)
    if nb is None and store and store.get("opens_on"):
        nb, approx = store["opens_on"], False
        facts["sources"]["needed_by"] = "system:stores.opens_on"
    facts["needed_by"] = nb.isoformat() if nb else None
    facts["needed_by_approx"] = approx
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
