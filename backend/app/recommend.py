"""Recommend agent. Code computes every option and every number from validated facts; the LLM only picks among them
and explains. Gates: chosen action must be an allowed candidate; every number in the rationale must come from the
facts/candidates (otherwise a code-written rationale is used). Degraded mode never recommends spend.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from . import audit
from .codes import Reason
from .extract import _NUM, _SCALE
from .llm import LLMUnavailable, gateway
from .schemas import RecommendLLMOut

FREIGHT_PER_UNIT = 150  # INR, documented assumption for inter-store transfer

REC_SYSTEM = (
    "You advise a retail operations manager. You receive validated case facts and a list of candidate actions whose "
    "numbers were computed by code. Choose the best candidate and rank all candidates. Explain in 2-4 sentences. "
    "Use ONLY numbers that appear in the facts or candidates. Never invent suppliers, quantities, prices or dates. "
    "Prefer options that cover the shortfall before the need date at the lowest additional cost; zero-cost options are "
    "preferred when equally effective."
)


@dataclass
class Recommendation:
    options: list[dict]
    recommended_action: str
    ranking: list[str]
    rationale: str
    rationale_source: str                       # llm | template
    total_cost: int
    inputs_used: int
    inputs_total: int
    sufficiency: str                            # High | Medium | Low
    abstained: bool = False
    degraded: bool = False
    gate_events: list[str] = field(default_factory=list)
    risks_of_choice: list[str] = field(default_factory=list)


def vendor_stock(facts: dict, supplier_id: str | None) -> tuple[int, int | None]:
    """Units a supplier holds for the requested item, read from its own `Vendor-<NAME>-stock` snapshot rows.
    SUP-BRIGHT matches `Vendor-BRIGHT-stock`. Returns (units, age of the oldest snapshot in seconds); (0, None) when unknown."""
    token = (supplier_id or "").lower().removeprefix("sup-").strip()
    if not token:
        return 0, None
    rows = [r for r in facts.get("inventory", [])
            if str(r.get("location", "")).lower().startswith("vendor-") and token in re.split(r"[^a-z0-9]+", str(r["location"]).lower())]
    return sum(max(int(r.get("on_hand") or 0), 0) for r in rows), (max((r.get("age_s") or 0) for r in rows) if rows else None)


def candidates(facts: dict) -> list[dict]:
    short = facts.get("shortfall") or 0
    need = facts.get("days_to_need")
    delay = facts.get("delay_days") or 0
    item = facts.get("item") or {}
    out: list[dict] = []

    def feasible(lead: int | None) -> bool:
        return need is None or lead is None or lead <= need

    po = facts.get("po") or {}
    # Spend basis. With an open PO the goods are already budgeted, so a replacement only adds the price premium. With no PO
    # nothing is budgeted: buying is a NEW purchase and the whole purchase value is spend the approval ladder must see.
    alts = sorted([a for a in facts.get("alternatives", []) if a["stock"] > 0], key=lambda a: a["lead_time_days"])
    if alts and short > 0:
        a = alts[0]
        units = min(short, a["stock"])
        if po:
            premium = max(a["unit_cost"] - item.get("unit_cost", a["unit_cost"]), 0)
            cost, basis = int(round(units * premium)), "premium_over_po"
            how = f"premium INR {int(premium)}/unit"
        else:
            cost, basis = int(round(units * a["unit_cost"])), "new_purchase"
            how = f"INR {int(a['unit_cost'])}/unit, new purchase INR {cost:,}"
        out.append({"action": "expedite_alternate_supplier", "supplier": a["supplier_name"], "supplier_id": a.get("supplier_id"),
                    "item": a["name"], "units": units, "unit_price": int(a["unit_cost"]), "additional_cost": cost, "spend_basis": basis,
                    "lead_time_days": a["lead_time_days"], "feasible_by_need": feasible(a["lead_time_days"]),
                    "covers_shortfall": units >= short, "substitute": True,
                    "description": f"Buy {units} x {a['name']} (a different product from the one requested) from {a['supplier_name']} ({how})"})
    # Same product, straight from the supplier that holds stock for it. Only when nothing is on order and the supplier is not
    # the one causing the problem (a delay or supplier issue is about THAT supplier, so re-ordering from them is no fix).
    if (short > 0 and not po and not delay and facts.get("intent") not in ("delivery_delay", "supplier_issue")
            and (item.get("unit_cost") or 0) > 0):
        best = None
        seen: set[str] = set()
        for s in (facts.get("supplier"), facts.get("item_supplier")):
            if not s or not s.get("id") or s["id"] in seen:
                continue
            seen.add(s["id"])
            stock, age = vendor_stock(facts, s["id"])
            if stock > 0:
                key = (0 if stock >= short else 1, s.get("lead_time_days") or 0)
                if best is None or key < best[0]:
                    best = (key, s, stock, age)
        if best:
            _, s, stock, age = best
            units = min(short, stock)
            price = int(round(item["unit_cost"]))
            cost = int(round(units * item["unit_cost"]))
            lead = s.get("lead_time_days")
            out.append({"action": "order_from_supplier", "supplier": s["name"], "supplier_id": s["id"], "item": item.get("name"),
                        "units": units, "unit_price": price, "additional_cost": cost, "spend_basis": "new_purchase",
                        "lead_time_days": lead, "feasible_by_need": feasible(lead), "covers_shortfall": units >= short,
                        "substitute": False, "stock_age_s": age,
                        "description": f"Order {units} x {item.get('name')} from {s['name']} (INR {price}/unit, new purchase INR {cost:,}; "
                                       f"{stock} in stock at the supplier)"})
    home = None
    store = facts.get("store") or {}
    if store:
        home = f"Warehouse-{store.get('region')}"
    own = f"Store-{store.get('id')}" if store.get("id") else None      # stock already at the requesting store cannot be moved to it
    movable = [r for r in facts.get("inventory", []) if r["location"] not in (home, own) and not r["location"].startswith("Vendor-")
               and r["on_hand"] > 0]
    movable_units = sum(r["on_hand"] for r in movable)
    if movable_units and short > 0:
        units = min(short, movable_units)
        out.append({"action": "transfer_from_other_store", "units": units, "additional_cost": int(units * FREIGHT_PER_UNIT),
                    "lead_time_days": 2, "feasible_by_need": feasible(2), "covers_shortfall": units >= short,
                    "description": f"Transfer {units} units from {', '.join(r['location'] for r in movable)} (freight INR {FREIGHT_PER_UNIT}/unit)"})
    avail = facts.get("qty_available") or 0
    if short > 0:
        reach = avail + movable_units
        out.append({"action": "partial_launch_reallocate", "units": min(reach, facts.get("qty_ordered") or reach),
                    "additional_cost": 0, "lead_time_days": 0, "feasible_by_need": True, "covers_shortfall": False,
                    "description": f"Open on time with {min(reach, facts.get('qty_ordered') or reach)} units; remainder follows the delayed shipment"})
    po = facts.get("po") or {}
    # Waiting is only an option when there is a supplier shipment to wait for. With no delay stated and no open PO there is
    # nothing to wait for, so offering it as a free "fix" would hide the shortage.
    if facts.get("shortfall") == 0 and not delay and not po:
        out.append({"action": "wait_for_original_supplier", "units": 0, "additional_cost": 0, "lead_time_days": 0, "feasible_by_need": True,
                    "covers_shortfall": True, "description": "No shortfall: stock already covers the request, so nothing to buy; keep monitoring"})
    elif delay > 0 or po:
        wait_ok = feasible(delay)
        if not delay and po.get("promised_date") and facts.get("needed_by"):
            wait_ok = str(po["promised_date"])[:10] <= str(facts["needed_by"])[:10]
        out.append({"action": "wait_for_original_supplier", "units": short, "additional_cost": 0, "lead_time_days": delay,
                    "feasible_by_need": wait_ok, "covers_shortfall": True,
                    "description": f"Wait for the original supplier ({delay} day delay)" if delay else "Wait for the open purchase order to arrive"})
    if (facts.get("launch_gap_days") or 0) > 0:
        out.append({"action": "reschedule_launch", "units": short, "additional_cost": 0,
                    "lead_time_days": facts.get("launch_gap_days"), "feasible_by_need": True, "covers_shortfall": True,
                    "last_resort": True,
                    "description": f"Postpone the launch by about {facts.get('launch_gap_days')} day(s) (business decision)"})
    out.append({"action": "escalate_to_management", "units": 0, "additional_cost": 0, "lead_time_days": 0, "feasible_by_need": True,
                "covers_shortfall": False, "description": "Escalate to management for a decision"})
    return out


def _effective(o: dict) -> bool:
    return bool(o["covers_shortfall"] and o["feasible_by_need"] and not o.get("last_resort"))


def _rank_key(o: dict):
    # effective first (covers shortfall in time); the requested product beats a substitute (swapping the product is a business
    # decision, not a cost saving); then cheapest, then fastest; last-resort options sink
    return (1 if o.get("last_resort") else 0, 0 if _effective(o) else 1, 1 if o.get("substitute") else 0,
            o["additional_cost"], o["lead_time_days"] or 0)


def deterministic_choice(opts: list[dict], zero_cost_only: bool) -> str:
    pool = [o for o in opts if o["action"] != "escalate_to_management" and (o["additional_cost"] == 0 or not zero_cost_only)]
    pool = [o for o in pool if not o.get("last_resort")] or pool
    if not pool:
        return "escalate_to_management"
    eff = [o for o in pool if _effective(o)]
    if eff:
        return sorted(eff, key=_rank_key)[0]["action"]
    return sorted(pool, key=lambda o: (0 if o["feasible_by_need"] else 1, -(o["units"] or 0), 1 if o.get("substitute") else 0,
                                       o["additional_cost"]))[0]["action"]


def allowed_numbers(facts: dict, opts: list[dict]) -> set[Decimal]:
    nums: set[Decimal] = set()
    for k in ("qty_ordered", "qty_available", "shortfall", "delay_days", "days_to_need", "launch_gap_days"):
        if facts.get(k) is not None:
            nums.add(Decimal(int(facts[k])))
    if facts.get("qty_ordered") and facts.get("shortfall") is not None:
        nums.add(Decimal(round(100 * facts["shortfall"] / facts["qty_ordered"])))
    for r in facts.get("inventory", []):
        nums.add(Decimal(r["on_hand"]))
    for o in opts:
        for k in ("units", "additional_cost", "lead_time_days", "unit_price"):
            if o.get(k) is not None:
                nums.add(Decimal(int(o[k])))
    for a in facts.get("alternatives", []):
        nums |= {Decimal(int(a["unit_cost"])), Decimal(a["lead_time_days"]), Decimal(a["stock"])}
    if (facts.get("item") or {}).get("unit_cost"):
        nums.add(Decimal(int(facts["item"]["unit_cost"])))
    nums.add(Decimal(FREIGHT_PER_UNIT))
    return nums


_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def allowed_dates(facts: dict) -> set[str]:
    """ISO dates the model was shown. It may quote these; any other date is invented."""
    return {str(facts[k])[:10] for k in ("needed_by",) if facts.get(k)}


def numbers_ok(text: str, allowed: set[Decimal], free_upto: int = 10, dates: set[str] | None = None) -> tuple[bool, list[str]]:
    """A date is checked as a whole, then removed, so '2026-11-10' is not read as the numbers 2026, 11 and 10."""
    bad: list[str] = []
    for d in _ISO_DATE.findall(text):
        if d not in (dates or set()):
            bad.append(d)
    text = _ISO_DATE.sub(" ", text)
    for m in _NUM.finditer(text):
        try:
            bare = Decimal(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            continue
        val = bare * _SCALE.get((m.group(2) or "").lower(), 1)
        if bare <= free_upto and not m.group(2):
            continue
        if val in allowed or bare in allowed:
            continue
        # a lakh figure such as "2.28 lakh" may be a rounded form of an allowed number
        if m.group(2) and any(abs(val - a) <= max(Decimal(1), a * Decimal("0.01")) for a in allowed):
            continue
        bad.append(m.group(0))
    return (not bad), bad


def sufficiency(facts: dict, inputs_used: int, inputs_total: int, degraded: bool) -> str:
    if facts.get("shortfall") is None and facts.get("intent") in ("inventory_shortage", "delivery_delay"):
        return "Low"      # cannot size the problem, so cannot cost a fix: ask a human
    checks = [facts.get("shortfall") is not None, facts.get("days_to_need") is not None, bool(facts.get("alternatives")),
              not facts.get("unresolved_conflicts"), not facts.get("missing"), not degraded, inputs_used >= inputs_total - 2]
    ok = sum(checks)
    return "High" if ok >= 6 else "Medium" if ok >= 4 else "Low"


def template_rationale(facts: dict, chosen: dict) -> str:
    short, need = facts.get("shortfall"), facts.get("days_to_need")
    parts = []
    if short is not None:
        parts.append(f"Shortfall is {short} units" + (f" with {need} day(s) to the need date" if need is not None else "") + ".")
    parts.append(f"{chosen['description']}.")
    if chosen["additional_cost"]:
        label = "New purchase cost" if chosen.get("spend_basis") == "new_purchase" else "Additional cost"
        parts.append(f"{label} INR {chosen['additional_cost']}.")
    else:
        parts.append("No additional spend.")
    if chosen.get("substitute"):
        parts.append("This is a different product from the one requested, so the requester must agree to the swap.")
    return " ".join(parts)


async def recommend(facts: dict, inputs_used: int, inputs_total: int, degraded: bool, case_id: str | None = None,
                    use_llm: bool = True, exclude_actions: tuple[str, ...] = ()) -> Recommendation:
    opts = [o for o in candidates(facts) if o["action"] not in exclude_actions or o["action"] == "escalate_to_management"]
    suff = sufficiency(facts, inputs_used, inputs_total, degraded)
    base = dict(options=opts, inputs_used=inputs_used, inputs_total=inputs_total, sufficiency=suff, degraded=degraded)
    if suff == "Low":    # abstain: no costed option, ask a human (A6)
        chosen = next(o for o in opts if o["action"] == "escalate_to_management")
        await audit.write(case_id, "agent", "recommend", "recommendation", "blocked",
                          f"evidence insufficient ({inputs_used}/{inputs_total} inputs); abstaining", reason=Reason.DATA_PARTIAL)
        return Recommendation(recommended_action=chosen["action"], ranking=[o["action"] for o in sorted(opts, key=_rank_key)],
                              rationale="Evidence is insufficient for a costed recommendation; a human decision is required.",
                              rationale_source="template", total_cost=0, abstained=True, **base)
    zero_only = degraded
    det = deterministic_choice(opts, zero_only)
    chosen_action, ranking, rationale, source, gate_events, risks = det, [o["action"] for o in sorted(opts, key=_rank_key)], "", "template", [], []
    if use_llm and not degraded:
        user = json.dumps({"facts": {k: facts.get(k) for k in ("store", "item", "supplier", "qty_ordered", "qty_available", "shortfall",
                                                              "delay_days", "needed_by", "days_to_need", "launch_gap_days")},
                           "candidates": opts}, default=str)
        try:
            r = await gateway.generate("recommend", REC_SYSTEM, "<case>\n" + user + "\n</case>", RecommendLLMOut, case_id=case_id,
                                       actor="recommend")
            allowed_actions = {o["action"] for o in opts}
            if r.data.recommended_action not in allowed_actions:
                gate_events.append(Reason.GATE_BAD_ACTION.value)
                await audit.write(case_id, "agent", "recommend", "gate_reject", "blocked",
                                  f"LLM chose action outside candidates: {r.data.recommended_action}", reason=Reason.GATE_BAD_ACTION)
            elif (pick := next(o for o in opts if o["action"] == r.data.recommended_action)) and (
                    (not pick["feasible_by_need"] and any(_effective(o) for o in opts))
                    or (pick.get("substitute") and any(_effective(o) and not o.get("substitute") for o in opts))):
                # an option that cannot arrive in time is wrong while one that can exists, and swapping the product is wrong
                # while the requested product can be had in time: code overrides the model
                gate_events.append(Reason.GATE_BAD_ACTION.value)
                why = ("is a different product while the requested one can be had in time" if pick.get("substitute") and pick["feasible_by_need"]
                       else "cannot arrive before the need date while a feasible option exists")
                await audit.write(case_id, "agent", "recommend", "gate_reject", "blocked",
                                  f"LLM chose {pick['action']}, which {why}; using the code's choice", reason=Reason.GATE_BAD_ACTION)
            else:
                chosen_action = r.data.recommended_action
                ranking = [a for a in r.data.ranking if a in allowed_actions] or ranking
                ok, bad = numbers_ok(r.data.rationale, allowed_numbers(facts, opts), dates=allowed_dates(facts))
                if ok:
                    rationale, source, risks = r.data.rationale, "llm", r.data.risks_of_choice[:4]
                else:
                    gate_events.append(Reason.GATE_COMMS_NUMBER.value)
                    await audit.write(case_id, "agent", "recommend", "gate_reject", "blocked",
                                      f"rationale contained unsupported numbers {bad[:4]}; using code-written rationale",
                                      reason=Reason.GATE_COMMS_NUMBER, refs={"numbers": bad[:6]})
        except LLMUnavailable:
            gate_events.append("llm_unavailable")   # already audited by the gateway
            base["degraded"] = True
            chosen_action = deterministic_choice(opts, True)   # outage => never recommend spend
    chosen = next(o for o in opts if o["action"] == chosen_action)
    if not rationale:
        rationale = template_rationale(facts, chosen)
    return Recommendation(recommended_action=chosen_action, ranking=ranking, rationale=rationale, rationale_source=source,
                          total_cost=chosen["additional_cost"], gate_events=gate_events, risks_of_choice=risks, **base)
