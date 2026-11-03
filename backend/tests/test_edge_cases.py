"""Edge cases found by the CASE-0005 incident: a shortfall read as the total, an unreadable date, a free "wait" that hid the
shortage, an auto-clear with no approver. Each one is now handled by code, and each handling is pinned here."""
from datetime import date, datetime

import pytest

from app import extract, recommend, validator
from app.adapters import faults
from app.codes import Reason
from app.extract import Field, confidence, disagreements, shortfall_wording
from app.policy import PolicyInput, evaluate, situation_flags
from app.validator import IST, parse_needed_by
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401  (fixtures)
from tests.test_extract import fv, issue, out
from tests.test_pipeline import _seeded, f, received  # noqa: F401  (fixture)

BASE = datetime(2026, 11, 4, 10, 0, tzinfo=IST)      # a Wednesday


# ───────────── dates ─────────────
@pytest.mark.parametrize("text,want", [
    ("7 Nov", date(2026, 11, 7)), ("Sat 7 Nov", date(2026, 11, 7)), ("Saturday, 7 November 2026", date(2026, 11, 7)),
    ("7th Nov", date(2026, 11, 7)), ("Nov 7", date(2026, 11, 7)), ("November 7th, 2026", date(2026, 11, 7)),
    ("on 7 Nov", date(2026, 11, 7)), ("by 7 Nov", date(2026, 11, 7)), ("7 of November", date(2026, 11, 7)),
    ("2026-11-07", date(2026, 11, 7)), ("07/11/2026", date(2026, 11, 7)), ("7/11", date(2026, 11, 7)),
    ("7-11-26", date(2026, 11, 7)), ("today", date(2026, 11, 4)), ("tomorrow", date(2026, 11, 5)),
    ("tomorrow EOD", date(2026, 11, 5)), ("day after tomorrow", date(2026, 11, 6)), ("in 3 days", date(2026, 11, 7)),
    ("in 2 weeks", date(2026, 11, 18)), ("Saturday", date(2026, 11, 7)), ("by Saturday EOD", date(2026, 11, 7)),
    ("next Monday", date(2026, 11, 9)), ("Wednesday", date(2026, 11, 11)),        # same weekday means a week away
    ("7 Nov 2025", date(2025, 11, 7)),                                            # explicit past year stays in the past
    ("1 Nov", date(2027, 11, 1)),                                                 # no year and already gone: the next one
    ("  7   NOV  ", date(2026, 11, 7)),
])
def test_dates_are_read(text, want):
    assert parse_needed_by(text, BASE)[0] == want


@pytest.mark.parametrize("text", ["31 Feb", "32 Nov", "13/13/2026", "2026-02-30", "when the stars align", "soon", "asap", "", "   ", None,
                                  "Nov", "7", "the 7th"])
def test_unreadable_dates_return_none_and_never_raise(text):
    assert parse_needed_by(text, BASE)[0] is None


def test_year_rollover_in_december():
    dec = datetime(2026, 12, 30, 9, 0, tzinfo=IST)
    assert parse_needed_by("2 Jan", dec)[0] == date(2027, 1, 2)
    assert parse_needed_by("in 5 days", dec)[0] == date(2027, 1, 4)


# ───────────── "30 units short" is a gap, not the total ─────────────
@pytest.mark.parametrize("quote,want", [
    ("30 units short", True), ("we are 30 units short", True), ("shortfall of 30", True), ("short by 30 units", True),
    ("shortage of 40 units", True), ("missing 30 units", True), ("need 30 more units", True), ("30 more needed", True),
    ("deficit of 30", True),
    ("150 standard display units", False), ("500 display units", False), ("order of 150 units", False), ("", False), (None, False),
    ("no shortfall expected", False), ("we are not short on stock", False), ("zero shortage", False),
])
def test_shortfall_wording(quote, want):
    assert shortfall_wording(quote) is want


def test_a_disagreement_records_both_values():
    score, sig = confidence(True, "150", None, "quantity_affected", "30", True, False)
    assert sig["rule"] == "conflict" and sig["rule_value"] == "150" and score < 0.8
    _, sig2 = confidence(True, None, "99", "quantity_affected", "30", False, True)
    assert sig2["second_opinion"] == "disagree" and sig2["second_value"] == "99"


def test_disagreements_only_count_critical_ai_fields():
    flds = {
        "quantity_affected": Field("quantity_affected", "30", "30", "ai", None, 0.57, {"rule": "conflict"}),
        "location": Field("location", "Pune", "Pune", "ai", None, 0.9, {"rule": "agree", "second_opinion": "agree"}),
        "delay_days": Field("delay_days", "4", "4", "ai", None, 0.5, {"second_opinion": "disagree"}),
        "needed_by": Field("needed_by", "x", "x", "ai", None, 0.5, {"rule": "conflict"}),            # not a critical field
        "additional_cost": Field("additional_cost", "9", "9", "ai", None, 1.0, {"rule": "conflict", "human": True}),   # a person set it
        "approval_deadline": Field("approval_deadline", None, None, "missing"),
    }
    assert disagreements(flds) == ["quantity_affected", "delay_days"]


# ───────────── validator: the Mumbai facts ─────────────
def mumbai(**over):
    qty = Field("quantity_affected", "30", "30 units short", "ai", None, 0.57, {"is_shortfall": True})
    base = {"location": f("location", "Mumbai Andheri"), "item": f("item", "standard display units"), "quantity_affected": qty,
            "needed_by": f("needed_by", "in 3 days")}
    base.update(over)
    return base


async def val(fields, intent="inventory_shortage", resolutions=None):
    return await validator.validate(fields, intent, received(), "CASE-E1", resolutions)


async def test_gap_wording_gives_the_real_shortfall_and_total():
    v = (await val(mumbai())).facts
    assert v["qty_is_shortfall"] is True
    assert v["shortfall"] == 30 and v["qty_available"] == 120 and v["qty_ordered"] == 150
    assert v["days_to_need"] == 3 and v["needed_by_unreadable"] is False


async def test_gap_with_an_email_availability_uses_it():
    v = (await val(mumbai(quantity_available=f("quantity_available", "100")))).facts
    assert v["shortfall"] == 30 and v["qty_ordered"] == 130


async def test_gap_with_no_inventory_still_knows_the_shortfall_but_not_the_total():
    v = (await val(mumbai(item=Field("item", None, None, "missing")))).facts
    assert v["shortfall"] == 30 and v["qty_available"] is None and v["qty_ordered"] is None


async def test_gap_wording_is_not_a_conflict_with_the_purchase_order_quantity():
    flds = mumbai(location=f("location", "Pune"), item=f("item", "display units"))
    v = await val(flds, "delivery_delay")
    assert not v.conflicts and v.facts["shortfall"] == 30


async def test_a_human_corrected_total_is_taken_as_the_total():
    typed = Field("quantity_affected", "150", "30 units short", "ai", None, 1.0, {"human": True})      # is_shortfall removed on correction
    v = (await val(mumbai(quantity_affected=typed))).facts
    assert v["qty_is_shortfall"] is False and v["qty_ordered"] == 150 and v["shortfall"] == 30


async def test_a_total_that_is_below_stock_means_no_shortfall():
    v = (await val(mumbai(quantity_affected=f("quantity_affected", "100")))).facts
    assert v["shortfall"] == 0


async def test_unreadable_date_is_flagged_not_guessed():
    v = await val(mumbai(needed_by=f("needed_by", "when the stars align")))
    assert v.facts["needed_by_unreadable"] is True and v.facts["days_to_need"] is None
    assert any("could not read" in w["msg"] for w in v.warnings)


async def test_no_date_stated_is_not_an_unreadable_date():
    v = await val(mumbai(needed_by=Field("needed_by", None, None, "missing")))
    assert v.facts["needed_by_unreadable"] is False


async def test_impossible_calendar_date_does_not_crash():
    v = await val(mumbai(needed_by=f("needed_by", "31 Feb")))
    assert v.facts["needed_by_unreadable"] is True


async def test_a_need_date_in_the_past_warns_and_is_critical_urgency():
    from app import risk
    v = await val(mumbai(needed_by=f("needed_by", "7 Nov 2025")))
    assert v.facts["days_to_need"] < 0 and any("in the past" in w["msg"] for w in v.warnings)
    assert risk.rule_score(v.facts)["urgency"] == "Critical"


# ───────────── recommend: the free "wait" must not hide a shortage ─────────────
async def test_mumbai_has_no_wait_option_and_picks_the_feasible_transfer():
    facts = (await val(mumbai())).facts
    opts = recommend.candidates(facts)
    acts = {o["action"]: o for o in opts}
    assert "wait_for_original_supplier" not in acts
    assert acts["expedite_alternate_supplier"]["additional_cost"] == 138000 and acts["expedite_alternate_supplier"]["feasible_by_need"] is False
    t = acts["transfer_from_other_store"]
    assert t["units"] == 30 and t["additional_cost"] == 4500 and t["feasible_by_need"] is True and t["covers_shortfall"] is True
    assert "Store-MUM-01" not in t["description"] and "Warehouse-West" not in t["description"]
    assert recommend.deterministic_choice(opts, False) == "transfer_from_other_store"


def test_stock_at_the_requesting_store_is_never_a_transfer_source():
    facts = {"shortfall": 30, "qty_ordered": 150, "qty_available": 120, "days_to_need": 3, "store": {"id": "MUM-01", "region": "West"},
             "inventory": [{"location": "Warehouse-West", "on_hand": 120}, {"location": "Store-MUM-01", "on_hand": 30}]}
    assert "transfer_from_other_store" not in {o["action"] for o in recommend.candidates(facts)}
    facts["inventory"].append({"location": "Store-PUN-01", "on_hand": 30})
    assert "transfer_from_other_store" in {o["action"] for o in recommend.candidates(facts)}


def test_wait_is_offered_only_when_there_is_something_to_wait_for():
    base = {"shortfall": 30, "days_to_need": 5}
    assert "wait_for_original_supplier" not in {o["action"] for o in recommend.candidates(base)}
    assert "wait_for_original_supplier" in {o["action"] for o in recommend.candidates({**base, "delay_days": 2})}
    assert "wait_for_original_supplier" in {o["action"] for o in recommend.candidates({**base, "po": {"id": "P", "promised_date": "2026-11-05"}})}


def test_a_known_zero_shortfall_gets_a_free_monitor_plan_not_an_escalation():
    facts = {"intent": "inventory_shortage", "shortfall": 0, "days_to_need": 10}
    opts = recommend.candidates(facts)
    assert recommend.deterministic_choice(opts, False) == "wait_for_original_supplier"
    assert not situation_flags(facts, next(o for o in opts if o["action"] == "wait_for_original_supplier"))["escalation_chosen"]


def test_wait_for_a_po_is_infeasible_when_it_lands_after_the_need_date():
    facts = {"shortfall": 30, "days_to_need": 5, "needed_by": "2026-11-09", "po": {"id": "P", "promised_date": "2026-11-12"}}
    w = next(o for o in recommend.candidates(facts) if o["action"] == "wait_for_original_supplier")
    assert w["feasible_by_need"] is False
    facts["po"]["promised_date"] = "2026-11-08"
    w = next(o for o in recommend.candidates(facts) if o["action"] == "wait_for_original_supplier")
    assert w["feasible_by_need"] is True


@pytest.mark.parametrize("intent,short,want", [("inventory_shortage", None, "Low"), ("delivery_delay", None, "Low")])
def test_unknown_shortfall_on_a_shortage_abstains(intent, short, want):
    assert recommend.sufficiency({"intent": intent, "shortfall": short, "days_to_need": 3, "alternatives": [1]}, 8, 8, False) == want


def test_unknown_shortfall_on_other_intents_is_not_forced_low():
    assert recommend.sufficiency({"intent": "supplier_issue", "shortfall": None, "days_to_need": 3, "alternatives": [1]}, 8, 8, False) != "Low"


async def test_llm_pick_that_cannot_arrive_in_time_is_overridden(monkeypatch):
    from tests.test_pipeline import Script, reply
    from app.llm import Gateway, RateLimiter
    g = Gateway(models=("m1",), transport=Script(reply({"recommended_action": "expedite_alternate_supplier",
                                                        "ranking": ["expedite_alternate_supplier"], "rationale": "Buy it.", "risks_of_choice": []})))
    g.limiter = RateLimiter(60000)
    monkeypatch.setattr(recommend, "gateway", g)
    facts = (await val(mumbai())).facts
    rec = await recommend.recommend(facts, 8, 8, False, "CASE-E1")
    assert rec.recommended_action == "transfer_from_other_store" and Reason.GATE_BAD_ACTION.value in rec.gate_events
    assert rec.rationale_source == "template"


# ───────────── policy ─────────────
@pytest.mark.parametrize("kw,rule", [
    ({"field_disagreements": ["quantity_affected"]}, "FIELD_DISAGREEMENT"), ({"shortfall_unverified": True}, "SHORTFALL_UNVERIFIED"),
    ({"shortfall_uncovered": True}, "SHORTFALL_NOT_COVERED"), ({"escalation_chosen": True}, "ESCALATION_RECOMMENDED"),
    ({"date_unreadable": True}, "DATE_UNREADABLE"),
])
@pytest.mark.parametrize("amount", [0, 1, 60_000])
def test_each_new_rule_forces_a_human_on_its_own(kw, rule, amount):
    d = evaluate(PolicyInput(amount=amount, **kw))
    assert d.requires_human and not d.auto_clear and rule in {r["id"] for r in d.rules}


def test_a_clean_zero_spend_case_still_auto_clears():
    assert evaluate(PolicyInput(**situation_flags({"intent": "inventory_shortage", "shortfall": 0}, None))).auto_clear


TRANSFER = {"action": "transfer_from_other_store", "covers_shortfall": True, "feasible_by_need": True}


@pytest.mark.parametrize("facts,chosen,want", [
    ({"intent": "inventory_shortage", "shortfall": None}, None, {"shortfall_unverified": True}),
    ({"intent": "delivery_delay", "shortfall": None}, TRANSFER, {"shortfall_unverified": True}),
    ({"intent": "supplier_issue", "shortfall": None}, None, {"shortfall_unverified": False}),
    ({"intent": "inventory_shortage", "shortfall": 30}, TRANSFER, {"shortfall_uncovered": False, "shortfall_unverified": False}),
    ({"intent": "inventory_shortage", "shortfall": 30}, {**TRANSFER, "feasible_by_need": False}, {"shortfall_uncovered": True}),
    ({"intent": "inventory_shortage", "shortfall": 30}, {**TRANSFER, "covers_shortfall": False}, {"shortfall_uncovered": True}),
    ({"intent": "inventory_shortage", "shortfall": 30}, None, {"shortfall_uncovered": True}),
    ({"intent": "inventory_shortage", "shortfall": 30}, {"action": "escalate_to_management", "covers_shortfall": True, "feasible_by_need": True},
     {"shortfall_uncovered": True, "escalation_chosen": True}),
    ({"intent": "inventory_shortage", "shortfall": 0}, {"action": "escalate_to_management"}, {"shortfall_uncovered": False, "escalation_chosen": True}),
    ({"intent": "inventory_shortage", "shortfall": 0, "needed_by_unreadable": True}, None, {"date_unreadable": True}),
])
def test_situation_flags(facts, chosen, want):
    got = situation_flags(facts, chosen)
    assert {k: got[k] for k in want} == want


# ───────────── end to end through the API: the Mumbai manager scenario ─────────────
MUMBAI_BODY = ("Hi team, the Mumbai Andheri store needs 150 standard display units, ready in 3 days. The West warehouse has only 120, "
               "so we are 30 units short. Please approve the fix by 6:00 PM today.")


def mumbai_intake():
    return out(issue(intent="inventory_shortage", item=fv("standard display units"), location=fv("Mumbai Andheri", "Mumbai Andheri store"),
                     quantity_affected=fv("30", "30 units short"), quantity_available=fv("120", "has only 120"),
                     needed_by=fv("in 3 days", "in 3 days"), approval_deadline=fv("6:00 PM today")))


async def submit_mumbai(fake_llm, who="rahul.manager@demo.test"):
    fake_llm.intake = mumbai_intake()
    fake_llm.risk = {"urgency": "High", "impact": "Medium", "risk": "High", "reasons": ["30 units short"], "missing_information": []}
    c = await client_for(who)
    return c, *(await submit(c, subject="Mumbai Andheri display shortfall", body=MUMBAI_BODY))


async def test_mumbai_case_is_sized_correctly_and_cannot_auto_clear(fake):
    c, cid, d = await submit_mumbai(fake)
    facts = d["facts"]
    assert facts["shortfall"] == 30 and facts["qty_ordered"] == 150 and facts["days_to_need"] == 3
    assert d["case"]["status"] == "AWAITING_APPROVAL"                      # never APPROVED by itself
    rec = [r for r in d["recommendations"] if r["status"] == "proposed"][0]
    assert rec["chosen_option"] == "transfer_from_other_store" and float(rec["total_cost"]) == 4500
    ids = {r["id"] for r in d["policy"]["rules_fired"]}
    assert "SPEND_GT_ZERO" in ids
    assert [(a["required_role"], a["status"]) for a in d["approvals"]] == [("manager", "pending")] or d["approvals"][0]["required_role"] in ("manager", "regional_head")


async def test_the_requester_manager_cannot_approve_their_own_request_and_it_is_audited(fake):
    c, cid, d = await submit_mumbai(fake)
    aid = d["approvals"][0]["id"]
    r = await c.post(f"/api/cases/{cid}/approvals/{aid}/decision", json={"decision": "approve", "reason": "self"})
    assert r.status_code == 403
    audit = (await c.get(f"/api/cases/{cid}/audit")).json()
    assert any(e.get("reason_code") == Reason.POLICY_SOD.value for e in audit["events"])


async def test_an_unreadable_date_sends_the_case_to_a_human(fake):
    fake.intake = out(issue(intent="inventory_shortage", item=fv("standard display units"), location=fv("Mumbai Andheri", "Mumbai Andheri store"),
                            quantity_affected=fv("30", "30 units short"), needed_by=fv("in 3 days", "in 3 days")))
    c = await client_for("priya.requester@demo.test")
    cid, d = await submit(c, subject="Mumbai", body=MUMBAI_BODY.replace("in 3 days", "in three days"))
    # the quote "in 3 days" is no longer in the text, so the date is dropped by the grounding gate: nothing is invented
    assert d["facts"]["days_to_need"] is None
    assert d["case"]["status"] in ("AWAITING_APPROVAL", "NEEDS_INFO")


async def test_a_case_cleared_by_policy_says_so_in_the_closure_checklist(fake):
    from app import closure, db
    fake.intake = out(issue(intent="inventory_shortage", item=fv("standard display units"), location=fv("Mumbai Andheri", "Mumbai Andheri store"),
                            quantity_affected=fv("100", "needs 100 standard display units"), needed_by=fv("in 10 days", "in 10 days")))
    fake.risk = {"urgency": "Medium", "impact": "Low", "risk": "Medium", "reasons": [], "missing_information": []}
    c = await client_for("priya.requester@demo.test")
    body = "Mumbai Andheri store needs 100 standard display units, ready in 10 days. Please confirm."
    cid, d = await submit(c, subject="Mumbai", body=body)
    assert d["case"]["status"] == "APPROVED"          # 100 <= 120 in stock: no shortfall, zero spend, low risk
    async with db.pool.connection() as conn:
        case = await (await conn.execute("SELECT * FROM cases WHERE id=%s", (cid,))).fetchone()
        items = (await closure.checklist(conn, case))["items"]
    plan = next(i for i in items if i["key"] == "plan_approved")
    assert plan["done"] and "cleared by policy" in plan["label"].lower() and "no human approval" in plan["detail"]
