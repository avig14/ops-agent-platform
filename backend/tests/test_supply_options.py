"""The CASE-0006 incident (Bengaluru, ITM-DISP-X): the plan never offered to order the SAME item from the supplier that holds
stock, costed a new purchase as INR 0 (premium only), and read "100 units" as an item. Each fix and its edge cases are pinned here."""
import copy

import pytest

from app import comms, recommend, validator
from app.policy import PolicyInput, evaluate, required_tiers, situation_flags
from app.recommend import candidates, deterministic_choice, vendor_stock
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401  (fixtures)
from app.extract import Field
from tests.test_extract import fv, issue, out
from tests.test_pipeline import _seeded, f, received  # noqa: F401  (fixture)

BRIGHT = {"id": "SUP-BRIGHT", "name": "BrightFab Displays", "lead_time_days": 4}
ACME = {"id": "SUP-ACME", "name": "Acme Display Systems", "lead_time_days": 10}
LOCAL = {"id": "SUP-LOCAL", "name": "Pune Local Fixtures", "lead_time_days": 2}
ITEM_X = {"id": "ITM-DISP-X", "name": "Display unit (express)", "unit_cost": 4600}
ALT_STD = {"supplier_id": "SUP-ACME", "supplier_name": "Acme Display Systems", "name": "Display unit (standard)", "stock": 210,
           "unit_cost": 4000, "lead_time_days": 10}


def facts(**over):
    base = {"intent": "inventory_shortage", "shortfall": 80, "days_to_need": 12, "delay_days": None, "qty_available": 20, "qty_ordered": 100,
            "item": dict(ITEM_X), "supplier": dict(BRIGHT), "item_supplier": dict(BRIGHT), "po": None,
            "store": {"id": "BLR-01", "region": "South"},
            "inventory": [{"location": "Vendor-BRIGHT-stock", "on_hand": 400, "age_s": 60}],
            "alternatives": [dict(ALT_STD)]}
    base.update(over)
    return base


def by(opts):
    return {o["action"]: o for o in opts}


# ───────────── vendor_stock ─────────────
@pytest.mark.parametrize("rows,sup,want", [
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}], "SUP-BRIGHT", 400),
    ([{"location": "vendor-bright-stock", "on_hand": 5}], "sup-bright", 5),                       # case does not matter
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}], "SUP-ACME", 0),                       # someone else's stock
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}, {"location": "Vendor-BRIGHT-east", "on_hand": 50}], "SUP-BRIGHT", 450),   # summed
    ([{"location": "Vendor-BRIGHTER-stock", "on_hand": 400}], "SUP-BRIGHT", 0),                   # a token match, not a substring match
    ([{"location": "Warehouse-West", "on_hand": 400}], "SUP-BRIGHT", 0),                          # not a vendor row
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": -7}], "SUP-BRIGHT", 0),                      # bad data never subtracts
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": None}], "SUP-BRIGHT", 0),
    ([], "SUP-BRIGHT", 0),
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}], None, 0),
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}], "", 0),
    ([{"location": "Vendor-BRIGHT-stock", "on_hand": 400}], "SUP-", 0),
])
def test_vendor_stock_matching(rows, sup, want):
    assert vendor_stock({"inventory": rows}, sup)[0] == want


def test_vendor_stock_reports_the_oldest_snapshot():
    rows = [{"location": "Vendor-BRIGHT-a", "on_hand": 1, "age_s": 10}, {"location": "Vendor-BRIGHT-b", "on_hand": 1, "age_s": 900}]
    assert vendor_stock({"inventory": rows}, "SUP-BRIGHT") == (2, 900)
    assert vendor_stock({}, "SUP-BRIGHT") == (0, None)


# ───────────── the new option ─────────────
def test_order_from_the_named_supplier_is_offered_and_costed_in_full():
    o = by(candidates(facts()))["order_from_supplier"]
    assert o["supplier_id"] == "SUP-BRIGHT" and o["units"] == 80 and o["unit_price"] == 4600
    assert o["additional_cost"] == 368_000 and o["spend_basis"] == "new_purchase"
    assert o["lead_time_days"] == 4 and o["feasible_by_need"] and o["covers_shortfall"] and o["substitute"] is False
    assert "new purchase INR 368,000" in o["description"] and "400 in stock" in o["description"]


def test_the_requested_item_beats_a_cheaper_different_product():
    opts = candidates(facts())
    sub = by(opts)["expedite_alternate_supplier"]
    assert sub["substitute"] is True and sub["additional_cost"] == 320_000 and "different product" in sub["description"]
    assert sub["additional_cost"] < by(opts)["order_from_supplier"]["additional_cost"]            # cheaper, yet not chosen
    assert deterministic_choice(opts, zero_cost_only=False) == "order_from_supplier"


def test_a_substitute_is_chosen_only_when_the_requested_item_cannot_arrive_in_time():
    slow = {"id": "SUP-BRIGHT", "name": "BrightFab Displays", "lead_time_days": 9}
    quick_alt = dict(ALT_STD, lead_time_days=3)
    opts = candidates(facts(supplier=slow, item_supplier=slow, days_to_need=5, alternatives=[quick_alt]))
    assert by(opts)["order_from_supplier"]["feasible_by_need"] is False
    assert deterministic_choice(opts, False) == "expedite_alternate_supplier"


@pytest.mark.parametrize("over", [
    {"po": {"id": "PO-1", "promised_date": "2026-11-06"}},          # something is already on order
    {"shortfall": 0}, {"shortfall": None},
    {"delay_days": 4},                                              # the supplier is the one delaying
    {"intent": "delivery_delay"}, {"intent": "supplier_issue"},
    {"item": {"id": "ITM-DISP-X", "name": "x", "unit_cost": 0}}, {"item": {"id": "ITM-DISP-X", "name": "x", "unit_cost": None}}, {"item": {}},
    {"supplier": None, "item_supplier": None},
    {"inventory": []},
    {"inventory": [{"location": "Vendor-BRIGHT-stock", "on_hand": 0}]},
    {"inventory": [{"location": "Vendor-ACME-stock", "on_hand": 400}]},    # stock belongs to a supplier nobody named
    {"supplier": {"id": None, "name": "?"}, "item_supplier": None},
])
def test_order_from_supplier_is_not_offered_when_it_would_be_wrong(over):
    assert "order_from_supplier" not in by(candidates(facts(**over)))


def test_partial_vendor_stock_is_offered_as_not_covering():
    o = by(candidates(facts(inventory=[{"location": "Vendor-BRIGHT-stock", "on_hand": 50}])))["order_from_supplier"]
    assert o["units"] == 50 and o["covers_shortfall"] is False and o["additional_cost"] == 230_000


def test_a_lead_time_past_the_need_date_is_shown_but_not_chosen_when_something_works():
    f_ = facts(days_to_need=2, inventory=[{"location": "Vendor-BRIGHT-stock", "on_hand": 400}, {"location": "Warehouse-North", "on_hand": 100, "age_s": 1}])
    opts = candidates(f_)
    assert by(opts)["order_from_supplier"]["feasible_by_need"] is False
    assert deterministic_choice(opts, False) == "transfer_from_other_store"


def test_unknown_need_date_does_not_block_the_order():
    o = by(candidates(facts(days_to_need=None)))["order_from_supplier"]
    assert o["feasible_by_need"] is True


def test_the_named_supplier_is_used_even_when_it_is_not_the_items_default():
    named = dict(LOCAL)
    f_ = facts(supplier=named, item_supplier=dict(BRIGHT),
               inventory=[{"location": "Vendor-LOCAL-stock", "on_hand": 500}, {"location": "Vendor-BRIGHT-stock", "on_hand": 10}])
    o = by(candidates(f_))["order_from_supplier"]
    assert o["supplier_id"] == "SUP-LOCAL"                       # has stock for it, and the faster lead time


def test_a_supplier_that_covers_the_shortfall_beats_one_that_does_not():
    f_ = facts(supplier=dict(LOCAL), item_supplier=dict(BRIGHT),
               inventory=[{"location": "Vendor-LOCAL-stock", "on_hand": 10}, {"location": "Vendor-BRIGHT-stock", "on_hand": 400}])
    o = by(candidates(f_))["order_from_supplier"]
    assert o["supplier_id"] == "SUP-BRIGHT" and o["covers_shortfall"] is True


def test_the_same_supplier_named_twice_is_one_option():
    assert [o["action"] for o in candidates(facts())].count("order_from_supplier") == 1


def test_degraded_mode_never_picks_a_spending_option():
    opts = candidates(facts())
    assert deterministic_choice(opts, zero_cost_only=True) in ("partial_launch_reallocate", "reschedule_launch", "escalate_to_management")


def test_the_input_facts_are_not_mutated():
    f_ = facts()
    before = copy.deepcopy(f_)
    candidates(f_)
    assert f_ == before


# ───────────── cost basis: a premium only when a purchase order already budgets the goods ─────────────
def test_with_an_open_po_a_replacement_only_costs_the_premium():
    po = {"id": "PO-7781", "promised_date": "2026-11-06"}
    alt = {"supplier_id": "SUP-BRIGHT", "supplier_name": "BrightFab", "name": "Display unit (standard)", "stock": 400, "unit_cost": 4600, "lead_time_days": 4}
    o = by(candidates(facts(po=po, item={"id": "ITM-DISP", "name": "Display unit (standard)", "unit_cost": 4000}, alternatives=[alt])))["expedite_alternate_supplier"]
    assert o["spend_basis"] == "premium_over_po" and o["additional_cost"] == 80 * 600 and "premium INR 600/unit" in o["description"]


def test_with_no_po_the_whole_purchase_is_spend():
    o = by(candidates(facts()))["expedite_alternate_supplier"]
    assert o["spend_basis"] == "new_purchase" and o["additional_cost"] == 80 * 4000


def test_a_replacement_cheaper_than_the_po_item_costs_zero_premium_not_negative():
    po = {"id": "PO-1", "promised_date": "2026-11-06"}
    alt = dict(ALT_STD, unit_cost=3000)
    o = by(candidates(facts(po=po, alternatives=[alt])))["expedite_alternate_supplier"]
    assert o["additional_cost"] == 0


# ───────────── numbers the wording may use ─────────────
def test_the_unit_price_and_new_purchase_cost_are_allowed_numbers_in_the_wording():
    f_ = facts()
    opts = candidates(f_)
    allowed = recommend.allowed_numbers(f_, opts)
    ok, bad = recommend.numbers_ok("Order 80 units at INR 4600 each. New purchase cost INR 368000.", allowed)
    assert ok, bad
    ok2, bad2 = recommend.numbers_ok("Order 80 units at INR 4700 each.", allowed)
    assert not ok2 and bad2


def test_the_template_says_new_purchase_and_flags_a_different_product():
    f_ = facts()
    opts = by(candidates(f_))
    assert "New purchase cost INR 368000" in recommend.template_rationale(f_, opts["order_from_supplier"]).replace(",", "")
    t = recommend.template_rationale(f_, opts["expedite_alternate_supplier"])
    assert "different product" in t and "agree" in t


# ───────────── policy ─────────────
def test_choosing_a_substitute_forces_a_person_even_when_it_is_free():
    chosen = {"action": "expedite_alternate_supplier", "substitute": True, "covers_shortfall": True, "feasible_by_need": True}
    fl = situation_flags({"intent": "inventory_shortage", "shortfall": 5}, chosen)
    assert fl["substitute_chosen"] is True
    d = evaluate(PolicyInput(amount=0, **fl))
    assert d.requires_human and "SUBSTITUTE_PRODUCT" in {r["id"] for r in d.rules}
    assert situation_flags({"intent": "inventory_shortage", "shortfall": 5}, {**chosen, "substitute": False})["substitute_chosen"] is False
    assert situation_flags({}, None)["substitute_chosen"] is False


@pytest.mark.parametrize("amount,roles", [
    (0.0, ["manager"]), (50_000, ["manager"]), (50_001, ["regional_head"]), (368_000, ["regional_head"]), (500_000, ["regional_head"]),
    (500_001, ["regional_head", "finance_director"]), (5_000_000, ["regional_head", "finance_director"]),
])
def test_the_ladder_sees_the_full_purchase_value(amount, roles):
    assert [t["role"] for t in required_tiers(amount, "Medium")] == roles


async def test_a_huge_order_goes_to_regional_head_and_finance_director():
    big = {"id": "ITM-DISP-X", "name": "Display unit (express)", "unit_cost": 4600}
    o = by(candidates(facts(shortfall=300, qty_ordered=320, item=big, inventory=[{"location": "Vendor-BRIGHT-stock", "on_hand": 400}])))["order_from_supplier"]
    assert o["additional_cost"] == 1_380_000
    assert [t["role"] for t in required_tiers(o["additional_cost"], "High")] == ["regional_head", "finance_director"]


# ───────────── validator: the real Bengaluru facts ─────────────
async def bengaluru(**over):
    flds = {"location": f("location", "Bengaluru Indiranagar Store"), "item": f("item", "ITM-DISP-X"),
            "supplier": f("supplier", "BrightFab"), "quantity_affected": f("quantity_affected", "100"),
            "quantity_available": f("quantity_available", "20"), "needed_by": f("needed_by", "in 12 days")}
    flds.update(over)
    return await validator.validate(flds, "inventory_shortage", received(), "CASE-S1", None)


async def test_bengaluru_facts_carry_the_item_supplier_and_vendor_stock():
    v = (await bengaluru()).facts
    assert v["shortfall"] == 80 and v["days_to_need"] == 12 and v["item"]["id"] == "ITM-DISP-X"
    assert v["supplier"]["id"] == "SUP-BRIGHT" and v["item_supplier"]["id"] == "SUP-BRIGHT"
    o = by(candidates(v))["order_from_supplier"]
    assert o["additional_cost"] == 368_000 and o["lead_time_days"] == 4 and o["feasible_by_need"]
    assert by(candidates(v))["expedite_alternate_supplier"]["substitute"] is True


async def test_with_no_supplier_in_the_email_the_items_own_supplier_is_used():
    v = (await bengaluru(supplier=Field("supplier", None, None, "missing"))).facts
    assert v["item_supplier"]["id"] == "SUP-BRIGHT"
    assert by(candidates(v))["order_from_supplier"]["supplier_id"] == "SUP-BRIGHT"


async def test_a_standard_item_with_no_vendor_row_gets_no_order_option():
    v = (await bengaluru(item=f("item", "ITM-SHELF"), supplier=f("supplier", "Pune Local Fixtures"))).facts
    assert "order_from_supplier" not in by(candidates(v))          # no Vendor-LOCAL-stock snapshot exists: nothing is invented


async def test_the_pune_sample_keeps_its_premium_cost():
    from tests.test_pipeline import analyse
    v = await analyse()
    ex = by(candidates(v.facts))["expedite_alternate_supplier"]
    assert ex["spend_basis"] == "premium_over_po" and ex["additional_cost"] == 228_000 and "order_from_supplier" not in by(candidates(v.facts))


# ───────────── comms ─────────────
def test_the_order_email_to_the_supplier_is_clean_and_not_called_an_alternate():
    opt = by(candidates(facts()))["order_from_supplier"]
    subj, body = comms.supplier_template("CASE-9", "Bengaluru Indiranagar Store", "BrightFab Displays", "Display unit (express)", opt, None)
    assert "order" in subj.lower() and "80" in body and "4" in body
    assert "alternat" not in (subj + body).lower() and "delayed" not in body
    assert comms.gate((subj, body), (subj, body), True, ["Acme Display Systems"]) == []


def test_the_substitute_email_uses_order_wording_too():
    opt = by(candidates(facts()))["expedite_alternate_supplier"]
    subj, body = comms.supplier_template("CASE-9", None, "Acme Display Systems", "Display unit (standard)", opt, None)
    assert "order" in subj.lower() and "following up" not in body


def test_the_supplier_is_found_by_id_then_by_name():
    sups = [{"id": "SUP-ACME", "name": "Acme Display Systems", "email": "a"}, {"id": "SUP-BRIGHT", "name": "BrightFab Displays", "email": "b"}]
    ctx = {"suppliers": sups, "facts": {"supplier": {"id": "SUP-ACME"}}}
    assert comms._supplier_for({**ctx, "opt": {"action": "order_from_supplier", "supplier_id": "SUP-BRIGHT"}})["email"] == "b"
    assert comms._supplier_for({**ctx, "opt": {"action": "order_from_supplier", "supplier": "BrightFab Displays"}})["email"] == "b"
    assert comms._supplier_for({**ctx, "opt": {"action": "order_from_supplier", "supplier_id": "SUP-GONE", "supplier": "Nobody"}}) is None
    assert comms._supplier_for({**ctx, "opt": {"action": "wait_for_original_supplier"}})["email"] == "a"


# ───────────── the whole case through the API ─────────────
BLR_BODY = ("Hi team,\nBengaluru Indiranagar Store needs 100 units of ITM-DISP-X for the festive display. "
            "Only 20 units are in stock. Please arrange it with BrightFab. We need them in 12 days.\n")


def blr_intake():
    return out(issue(intent="inventory_shortage", item=fv("ITM-DISP-X"), supplier=fv("BrightFab"),
                     quantity_affected=fv("100", "100 units of ITM-DISP-X"), quantity_available=fv("20", "Only 20 units are in stock"),
                     location=fv("Bengaluru Indiranagar Store"), needed_by=fv("in 12 days")))


async def submit_blr(fake, who="karthik.requester@demo.test"):
    fake.intake = blr_intake()
    fake.risk = {"urgency": "Medium", "impact": "Medium", "risk": "Medium", "reasons": ["80 units short"], "missing_information": []}
    c = await client_for(who)
    return c, *(await submit(c, subject="Bengaluru display shortfall", body=BLR_BODY))


async def test_bengaluru_end_to_end_orders_from_brightfab_and_goes_to_the_south_regional_head(fake):
    c, cid, d = await submit_blr(fake)
    assert d["case"]["status"] == "AWAITING_APPROVAL"
    rec = [r for r in d["recommendations"] if r["status"] == "proposed"][0]
    assert rec["chosen_option"] == "order_from_supplier" and float(rec["total_cost"]) == 368_000
    chosen = next(o for o in rec["options"] if o["action"] == "order_from_supplier")
    assert chosen["supplier"] == "BrightFab Displays" and chosen["spend_basis"] == "new_purchase"
    sub = next(o for o in rec["options"] if o["action"] == "expedite_alternate_supplier")
    assert sub["substitute"] is True
    # the scripted AI picked the substitute; code kept the requested product and recorded why
    assert "GATE_BAD_ACTION" in rec["evidence"]["gate_events"]
    assert [(a["required_role"], a["status"]) for a in d["approvals"]] == [("regional_head", "pending")]
    assert "SPEND_GT_ZERO" in {r["id"] for r in d["policy"]["rules_fired"]}


async def test_only_the_regional_head_of_that_region_can_approve_it(fake):
    c, cid, d = await submit_blr(fake)
    aid = d["approvals"][0]["id"]

    async def go(email):
        cl = await client_for(email)
        return (await cl.post(f"/api/cases/{cid}/approvals/{aid}/decision?wait=true", json={"decision": "approve", "reason": "ok"})).status_code

    assert await go("karthik.requester@demo.test") == 403          # separation of duties
    assert await go("sneha.manager@demo.test") == 403              # below the tier
    assert await go("anita.regional@demo.test") == 404             # the West head cannot even see a South case
    assert await go("meera.regional@demo.test") == 200
    after = (await c.get(f"/api/cases/{cid}")).json()
    assert after["case"]["status"] in ("APPROVED", "EXECUTING", "MONITORING")


async def test_rejecting_the_order_replans_without_it(fake):
    c, cid, d = await submit_blr(fake)
    aid = d["approvals"][0]["id"]
    head = await client_for("meera.regional@demo.test")
    r = await head.post(f"/api/cases/{cid}/approvals/{aid}/decision?wait=true", json={"decision": "reject", "reason": "too expensive"})
    assert r.status_code == 200
    after = (await c.get(f"/api/cases/{cid}")).json()
    live = [x for x in after["recommendations"] if x["status"] in ("proposed", "abstained")]
    assert live and live[0]["chosen_option"] != "order_from_supplier"
    assert "PRIOR_REJECTION" in {x["id"] for x in after["policy"]["rules_fired"]}


async def test_the_ai_picking_the_requested_product_is_kept(fake):
    """When the scripted AI picks an option code also built, and it is the best one, nothing is overridden."""
    from tests.test_cases_api import ok
    orig = fake.__class__.__call__

    async def call(self, model, body):
        sys = body["systemInstruction"]["parts"][0]["text"]
        if "operations manager" in sys:
            self.calls.append("recommend")
            return ok({"recommended_action": "order_from_supplier", "ranking": ["order_from_supplier"],
                       "rationale": "Shortfall is 80 units with 12 days left. Ordering 80 units from BrightFab costs INR 368000 and arrives in 4 days.",
                       "risks_of_choice": ["stock may sell out"]})
        return await orig(self, model, body)

    fake.__class__.__call__ = call
    try:
        c, cid, d = await submit_blr(fake)
    finally:
        fake.__class__.__call__ = orig
    rec = [r for r in d["recommendations"] if r["status"] == "proposed"][0]
    assert rec["chosen_option"] == "order_from_supplier" and rec["evidence"]["rationale_source"] == "llm"
    assert "GATE_BAD_ACTION" not in (rec["evidence"].get("gate_events") or [])


# ───────────── rule extractor: a count word is not a product ─────────────
@pytest.mark.parametrize("text,item", [
    ("We need 100 units of ITM-DISP-X.", None),
    ("We have 100 units short.", None),
    ("50 pieces are needed", None),
    ("We need 40 racks urgently", "racks"),
    ("We need 12 display units urgently", "display units"),
])
def test_rule_extract_never_reports_a_bare_count_word_as_the_item(text, item):
    from app.extract import rule_extract
    got = rule_extract(text).get("item")
    assert (got[0] if got else None) == item
