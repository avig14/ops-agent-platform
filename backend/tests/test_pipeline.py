"""Validator -> Risk -> Recommend -> Policy on the sample email, plus fault, conflict, abstain and gate paths."""
from datetime import datetime

import pytest

from app import adapters, recommend, risk, validator
from app.adapters import faults
from app.codes import Reason
from app.extract import Field
from app.llm import Gateway, RateLimiter
from app.policy import PolicyInput, evaluate
from app.seed import seed
from app.validator import IST
from tests.test_extract import Script, reply


def f(name, value, quote=None):
    return Field(name, value, quote or value, "ai", None, 0.9, {})


def sample_fields(**over):
    base = {"location": f("location", "Pune"), "item": f("item", "display units"),
            "quantity_affected": f("quantity_affected", "500"), "quantity_available": f("quantity_available", "120"),
            "delay_days": f("delay_days", "10"), "needed_by": f("needed_by", "next week"),
            "approval_deadline": f("approval_deadline", "4:00 PM today")}
    base.update(over)
    return base


def received():
    return datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
async def _seeded():
    from app import db
    await seed()
    async with db.pool.connection() as c:
        await c.execute("DELETE FROM llm_cache")          # fake LLM replies must never leak between tests
    faults.modes.clear()
    yield
    faults.modes.clear()


@pytest.fixture
def rec_gw(monkeypatch):
    def install(script):
        g = Gateway(models=("m1",), transport=script)
        g.limiter = RateLimiter(60000)
        monkeypatch.setattr(recommend, "gateway", g)
        monkeypatch.setattr(risk, "gateway", g)
        return g
    return install


async def analyse(fields=None, resolutions=None):
    v = await validator.validate(fields or sample_fields(), "delivery_delay", received(), "CASE-P1", resolutions)
    return v


# ───────────── validator ─────────────
async def test_sample_email_facts():
    v = await analyse()
    fa = v.facts
    assert fa["shortfall"] == 380 and fa["qty_ordered"] == 500 and fa["qty_available"] == 120
    assert fa["supplier"]["id"] == "SUP-ACME"                       # derived from open PO, never invented
    assert fa["sources"]["supplier"] == "system:purchase_orders"
    assert fa["days_to_need"] == 7 and fa["launch_gap_days"] == 3
    assert fa["needed_by_approx"] is True
    assert not v.missing and not v.conflicts and not v.degraded
    assert fa["deadline_passed_at_ingest"] is False
    assert v.inputs_used >= 7


async def test_deadline_already_passed_at_ingest_warns():
    late = datetime.now(IST).replace(hour=18, minute=0, second=0, microsecond=0)
    v = await validator.validate(sample_fields(), "delivery_delay", late, "CASE-P2")
    assert v.facts["deadline_passed_at_ingest"] is True
    assert any(w["code"] == Reason.WORKFLOW_DEADLINE_MISSED.value for w in v.warnings)


async def test_missing_required_field_is_reported_not_invented():
    fields = sample_fields()
    del fields["location"]
    v = await analyse(fields)
    assert [m["field"] for m in v.missing] == ["location"]
    assert v.facts["store"] is None and "supplier" not in v.facts


async def test_unknown_store_is_flagged():
    v = await analyse(sample_fields(location=f("location", "Atlantis")))
    assert any(m["field"] == "location" for m in v.missing)
    assert any(w["code"] == Reason.GATE_UNKNOWN_ENTITY.value for w in v.warnings)


async def test_email_vs_system_conflict_is_unresolved_until_human_picks():
    v = await analyse(sample_fields(quantity_available=f("quantity_available", "80")))
    assert [c["field"] for c in v.conflicts] == ["quantity_available"]
    assert v.conflicts[0]["kind"] == "conflict" and v.conflicts[0]["system"] == 120
    assert v.facts["unresolved_conflicts"] == ["quantity_available"]
    v2 = await analyse(sample_fields(quantity_available=f("quantity_available", "80")), {"quantity_available": "system"})
    assert v2.facts["unresolved_conflicts"] == [] and v2.facts["qty_available"] == 120 and v2.facts["shortfall"] == 380
    v3 = await analyse(sample_fields(quantity_available=f("quantity_available", "80")), {"quantity_available": "email"})
    assert v3.facts["qty_available"] == 80 and v3.facts["shortfall"] == 420


async def test_stale_snapshot_is_distinguished_from_conflict():
    faults.modes["inventory"] = "stale"
    v = await analyse(sample_fields(quantity_available=f("quantity_available", "80")))
    assert v.conflicts[0]["kind"] == "stale_snapshot"
    assert v.facts["unresolved_conflicts"] == []                      # fresher human statement used, with a warning
    assert any(w["code"] == Reason.DATA_STALE.value for w in v.warnings)
    assert v.degraded                                                 # not_fetched entry recorded


async def test_inventory_down_degrades_and_records_what_was_not_fetched():
    faults.modes["inventory"] = "down"
    v = await analyse()
    assert v.degraded
    nf = [n for n in v.not_fetched if n["source"] == "inventory"]
    assert nf and nf[0]["reason_code"] == Reason.DATA_UNAVAILABLE.value and "shortfall" in nf[0]["affects"]
    assert v.facts["data_notes"]


async def test_inventory_partial_keeps_primary_location_only():
    faults.modes["inventory"] = "partial"
    v = await analyse()
    assert [r["location"] for r in v.facts["inventory"]] == ["Warehouse-West"]
    assert v.degraded


async def test_every_data_failure_is_audited_and_in_external_calls():
    from app import db
    faults.modes["purchase_orders"] = "down"
    await analyse()
    async with db.pool.connection() as c:
        a = await (await c.execute("SELECT count(*) n FROM audit_log WHERE chain_key='CASE-P1' AND action='data_fetch' "
                                   "AND outcome='failure' AND reason_code=%s", (Reason.DATA_UNAVAILABLE.value,))).fetchone()
        e = await (await c.execute("SELECT count(*) n FROM external_calls WHERE case_id='CASE-P1' AND target='purchase_orders' "
                                   "AND status='error'")).fetchone()
    assert a["n"] >= 1 and e["n"] >= 1


# ───────────── risk ─────────────
async def test_rule_risk_is_critical_on_sample():
    v = await analyse()
    r = risk.rule_score(v.facts)
    assert r["impact"] == "Critical" and r["urgency"] == "High" and r["level"] == "Critical"


def test_rule_risk_is_monotone_in_shortfall():
    base = {"qty_ordered": 500, "launch_gap_days": 3, "days_to_need": 7}
    prev = -1
    for short in (0, 50, 125, 250, 400):
        lvl = risk.LEVELS.index(risk.rule_score({**base, "shortfall": short})["level"])
        assert lvl >= prev
        prev = lvl


async def test_risk_llm_disagreement_raises_agent_conflict(rec_gw):
    rec_gw(Script(reply({"urgency": "Medium", "impact": "Medium", "risk": "Medium", "reasons": ["small"], "missing_information": []})))
    v = await analyse()
    r = await risk.assess(v.facts, "CASE-P1")
    assert r.conflict and r.level == "Critical"                       # higher wins provisionally
    assert r.llm["risk"] == "Medium" and r.rule["level"] == "Critical"


async def test_risk_llm_down_falls_back_to_rules(rec_gw):
    rec_gw(Script((503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})))
    v = await analyse()
    r = await risk.assess(v.facts, "CASE-P1")
    assert r.llm_failed and r.level == "Critical" and not r.conflict


# ───────────── recommend + policy ─────────────
async def test_sample_recommendation_costs_and_policy_tier():
    v = await analyse()
    opts = recommend.candidates(v.facts)
    ex = next(o for o in opts if o["action"] == "expedite_alternate_supplier")
    assert ex["units"] == 380 and ex["additional_cost"] == 380 * 600 and ex["feasible_by_need"] and ex["covers_shortfall"]
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1", use_llm=False)
    assert rec.recommended_action == "expedite_alternate_supplier" and rec.total_cost == 228000
    assert rec.sufficiency == "High" and not rec.abstained and rec.rationale_source == "template"
    d = evaluate(PolicyInput(amount=rec.total_cost, risk_level="Critical"))
    ids = {r["id"] for r in d.rules}
    assert {"SPEND_GT_ZERO", "RISK_HIGH_OR_CRITICAL"} <= ids
    assert [t["role"] for t in d.tiers] == ["regional_head"]


async def test_reschedule_launch_is_never_the_default_choice():
    v = await analyse()
    opts = recommend.candidates(v.facts)
    assert any(o["action"] == "reschedule_launch" for o in opts)
    assert recommend.deterministic_choice(opts, False) != "reschedule_launch"
    assert recommend.deterministic_choice(opts, True) != "reschedule_launch"


async def test_degraded_mode_recommends_zero_cost_only():
    v = await analyse()
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, True, "CASE-P1", use_llm=False)
    assert rec.total_cost == 0 and rec.recommended_action == "partial_launch_reallocate"


async def test_insufficient_evidence_abstains_and_escalates():
    facts = {"shortfall": None, "days_to_need": None, "alternatives": [], "unresolved_conflicts": [], "missing": ["location"]}
    rec = await recommend.recommend(facts, 1, 8, False, "CASE-P1", use_llm=False)
    assert rec.abstained and rec.sufficiency == "Low" and rec.recommended_action == "escalate_to_management" and rec.total_cost == 0
    assert evaluate(PolicyInput(abstained=True)).requires_human


async def test_rejection_replan_excludes_rejected_action():
    v = await analyse()
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1", use_llm=False,
                                    exclude_actions=("expedite_alternate_supplier",))
    assert "expedite_alternate_supplier" not in [o["action"] for o in rec.options]
    assert rec.recommended_action != "expedite_alternate_supplier"


async def test_llm_choice_outside_candidates_is_rejected(rec_gw):
    v = await analyse()
    rec_gw(Script(reply({"recommended_action": "partial_launch_reallocate", "ranking": ["partial_launch_reallocate"],
                         "rationale": "x", "risks_of_choice": []})))
    opts_without = ("partial_launch_reallocate",)
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1", exclude_actions=opts_without)
    assert Reason.GATE_BAD_ACTION.value in rec.gate_events
    assert rec.recommended_action == "expedite_alternate_supplier"      # code-chosen fallback


async def test_rationale_with_invented_number_falls_back_to_template(rec_gw):
    v = await analyse()
    rec_gw(Script(reply({"recommended_action": "expedite_alternate_supplier", "ranking": ["expedite_alternate_supplier"],
                         "rationale": "Expedite 380 units. This saves INR 777777 overall.", "risks_of_choice": []})))
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1")
    assert Reason.GATE_COMMS_NUMBER.value in rec.gate_events and rec.rationale_source == "template"
    assert "777777" not in rec.rationale


async def test_faithful_llm_rationale_is_kept(rec_gw):
    v = await analyse()
    rec_gw(Script(reply({"recommended_action": "expedite_alternate_supplier", "ranking": ["expedite_alternate_supplier"],
                         "rationale": "Shortfall is 380 units with 7 days left. Buying 380 units from the alternate supplier costs INR 228000 "
                                      "and lands in 4 days.", "risks_of_choice": ["premium price"]})))
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1")
    assert rec.rationale_source == "llm" and not rec.gate_events and rec.risks_of_choice == ["premium price"]


async def test_llm_rationale_may_quote_the_need_date(rec_gw):
    v = await analyse()
    need = str(v.facts["needed_by"])[:10]
    rec_gw(Script(reply({"recommended_action": "expedite_alternate_supplier", "ranking": ["expedite_alternate_supplier"],
                         "rationale": f"Stock is needed by {need}. Buying 380 units from the alternate supplier costs INR 228000.",
                         "risks_of_choice": ["premium price"]})))
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1")
    assert rec.rationale_source == "llm" and not rec.gate_events


async def test_llm_outage_never_recommends_spend(rec_gw):
    v = await analyse()
    rec_gw(Script((503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})))
    rec = await recommend.recommend(v.facts, v.inputs_used, v.inputs_total, v.degraded, "CASE-P1")
    assert rec.degraded and rec.total_cost == 0 and "llm_unavailable" in rec.gate_events


def test_numbers_gate_accepts_lakh_rounding_and_small_counts():
    allowed = {recommend.Decimal(228000), recommend.Decimal(380)}
    assert recommend.numbers_ok("About 2.28 lakh for 380 units in 4 days", allowed)[0]
    assert not recommend.numbers_ok("About 5 lakh", allowed)[0]


def test_numbers_gate_reads_a_date_as_a_date_not_as_three_numbers():
    allowed = {recommend.Decimal(380)}
    text = "Needed by 2026-11-10, so 380 units must land first."
    assert recommend.numbers_ok(text, allowed, dates={"2026-11-10"}) == (True, [])
    # with no known date the same text is rejected, and the whole date is reported once
    ok, bad = recommend.numbers_ok(text, allowed)
    assert not ok and bad == ["2026-11-10"]
    # an invented date is rejected even when a real one is known
    ok, bad = recommend.numbers_ok("Delivery on 2026-12-01.", allowed, dates={"2026-11-10"})
    assert not ok and bad == ["2026-12-01"]
    # a number elsewhere in the sentence is still checked
    assert not recommend.numbers_ok("By 2026-11-10 we need 999 units.", allowed, dates={"2026-11-10"})[0]


def test_allowed_dates_come_from_needed_by_only():
    assert recommend.allowed_dates({"needed_by": "2026-11-10T00:00:00"}) == {"2026-11-10"}
    assert recommend.allowed_dates({}) == set()
