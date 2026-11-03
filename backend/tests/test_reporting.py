"""Step 4: the reporting agent (closing and management summaries) and the scenario library."""
import json

import pytest

from app import clock, db, reporting, safety, scenarios
from app.codes import Reason
from app.schemas import SummaryOut
from tests.test_approvals import _reset_clock  # noqa: F401  (autouse fixture)
from tests.test_approvals import audit_rows, decide, new_case  # noqa: F401
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_closure import ready, tick_all
from tests.test_comms import _no_faults  # noqa: F401  (autouse fixture)

FACTS = {"case_id": "CASE-7", "shortfall": 380, "plan": {"cost_inr": 228000, "description": "Buy 380 units"}, "when": "03 Nov 14:20 UTC"}


def out(headline="Case closed", body="The plan was carried out.", decisions=(), open_items=()):
    return SummaryOut(headline=headline, what_happened=body, decisions=list(decisions), open_items=list(open_items))


def reasons(fails):
    return [c for c, _ in fails]


async def clear_cache():
    async with db.pool.connection() as c:
        await c.execute("DELETE FROM llm_cache")


async def closed_case(fake):
    req, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 200
    await clear_cache()
    return req, head, mgr, fin, cid


async def summaries(cid=None, kind=None):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM summaries WHERE case_id IS NOT DISTINCT FROM %s AND kind=%s ORDER BY id", (cid, kind))).fetchall()


# ───────────── pure: the gates ─────────────
def test_a_summary_that_uses_only_known_numbers_passes():
    assert reporting.gate(out(body="380 units short. INR 228,000 approved on 03 Nov at 14:20."), FACTS) == []
    assert reporting.gate(out(headline="CASE-7 closed"), FACTS) == []


def test_a_number_that_is_not_in_the_facts_is_rejected():
    assert reasons(reporting.gate(out(body="Cost was INR 999000."), FACTS)) == [Reason.GATE_NUMERIC_MISMATCH]
    assert reasons(reporting.gate(out(decisions=["approved 12 times"]), FACTS)) == [Reason.GATE_NUMERIC_MISMATCH]


def test_links_and_addresses_are_rejected():
    assert reasons(reporting.gate(out(body="See https://evil.example now"), FACTS)) == [Reason.GATE_COMMS_LEAK]
    assert reasons(reporting.gate(out(open_items=["write to boss@corp.example"]), FACTS)) == [Reason.GATE_COMMS_LEAK]
    assert reasons(reporting.gate(out(body="see www.example.com"), FACTS)) == [Reason.GATE_COMMS_LEAK]


def test_empty_or_oversized_summaries_are_rejected():
    for bad in (out(headline=" "), out(body=""), out(headline="x" * 161), out(body="x" * 1501), out(decisions=["a"] * 9),
                out(open_items=["x" * 301])):
        assert reasons(reporting.gate(bad, FACTS)) == [Reason.GATE_UNGROUNDED]
    assert reporting.gate(out(headline="x" * 160, body="x" * 1500, decisions=["a"] * 8), FACTS) == []


def test_every_failure_is_reported_not_only_the_first():
    assert reasons(reporting.gate(out(headline="", body="INR 999000 at https://x.example"), FACTS)) == [
        Reason.GATE_UNGROUNDED, Reason.GATE_NUMERIC_MISMATCH, Reason.GATE_COMMS_LEAK]


def test_the_closing_template_always_passes_its_own_gate():
    f = {"case_id": "CASE-0012", "status": "CLOSED", "priority": "P1", "risk_level": "Critical", "store": "Pune Store", "shortfall": 380,
         "plan": {"action": "x", "description": "Buy 380 units from BrightFab Displays", "cost_inr": 228000},
         "approvals": [{"role_required": "regional_head", "decision": "approved", "decided_at": "03 Nov 14:20 UTC"}],
         "rejected_plans": 1, "revisions": 2, "reopened": 1, "messages": {"sent": 3, "dead": 1, "failed_or_unconfirmed": 2, "discarded_by_reviewer": 0}}
    t = reporting.closing_template(f)
    assert reporting.gate(t, f) == [] and "CASE-0012" in t.headline and "228000" in t.what_happened
    assert any("message(s)" in i for i in t.open_items)
    bare = {"case_id": "CASE-1", "messages": {"sent": 0, "dead": 0, "failed_or_unconfirmed": 0, "discarded_by_reviewer": 0}}   # what the database always gives
    assert reporting.gate(reporting.closing_template(bare), bare) == []


def test_the_management_template_always_passes_its_own_gate():
    f = {"scope": "region West", "cases": {"open": 3, "closed": 2}, "open_p1": 1, "approvals": {"pending": 2, "overdue": 1, "avg_decision_hours": 2.5},
         "spend_inr": {"approved": 228000, "pending_approval": 50000}, "comms": {"sent": 5, "dead": 1, "failed": 0},
         "attention": [{"case_id": "CASE-1", "reasons": ["P1", "approval overdue"]}]}
    t = reporting.management_template(f)
    assert reporting.gate(t, f) == [] and "3 open" in t.headline and t.decisions == ["CASE-1: P1, approval overdue"]


# ───────────── the closing summary ─────────────
async def test_the_closing_facts_come_from_the_database_and_hold_no_message_text(fake):
    _, _, _, _, cid = await closed_case(fake)
    f = await reporting.closing_facts(cid)
    assert f["status"] == "CLOSED" and f["shortfall"] == 380 and f["priority"] == "P1" and f["risk_level"] == "Critical"
    assert f["plan"]["cost_inr"] == 228000 and f["plan"]["action"] == "expedite_alternate_supplier"
    assert [(a["role_required"], a["decision"], a["decided_by_role"]) for a in f["approvals"]] == [("regional_head", "approved", "regional_head")]
    assert f["messages"]["sent"] >= 3 and f["messages"]["dead"] == 0 and f["revisions"] == 0 and f["reopened"] == 0
    assert f["closure_confirmed_by_people"] == ["resolution_confirmed", "spend_recorded"]
    assert [t["step"] for t in f["timeline"]][-1] == "APPROVED -> CLOSED"
    async with db.pool.connection() as c:
        title = (await (await c.execute("SELECT title FROM cases WHERE id=%s", (cid,))).fetchone())["title"]
    text = reporting.facts_text(f)
    assert "Regional Operations Manager" not in text and "display units" not in text and (not title or title not in text)
    assert await reporting.closing_facts("CASE-NOPE") is None


async def test_the_model_never_sees_the_message_text_or_the_title(fake):
    _, head, _, _, cid = await closed_case(fake)
    seen = []
    fake.report = lambda facts: (seen.append(facts), {"headline": "Closed", "what_happened": "Done.", "decisions": [], "open_items": []})[1]
    row = await reporting.closing_summary(cid, None)
    assert row["source"] == "llm" and seen and seen[0]["case_id"] == cid and "title" not in seen[0]


async def test_a_summary_with_an_invented_number_is_replaced_by_the_template_and_the_rejection_is_audited(fake):
    _, _, _, _, cid = await closed_case(fake)
    fake.report = lambda facts: {"headline": "Closed", "what_happened": "The plan cost INR 777000 in total.", "decisions": [], "open_items": []}
    row = await reporting.closing_summary(cid, None)
    assert row["source"] == "template" and "777000" not in json.dumps(row["content"]) and "228000" in row["content"]["what_happened"]
    rej = [r for r in await audit_rows(cid, "gate_reject") if r["actor_id"] == "reporting"]
    assert len(rej) == 1 and rej[0]["reason_code"] == Reason.GATE_NUMERIC_MISMATCH.value and rej[0]["outcome"] == "blocked"
    done = (await audit_rows(cid, "summary_generated"))[-1]
    assert done["outcome"] == "degraded" and done["refs"]["source"] == "template" and done["refs"]["gate_events"] == [Reason.GATE_NUMERIC_MISMATCH.value]


@pytest.mark.parametrize("bad,code", [
    ({"headline": "Closed", "what_happened": "Details at https://evil.example/report", "decisions": [], "open_items": []}, Reason.GATE_COMMS_LEAK),
    ({"headline": "Closed", "what_happened": "x" * 2000, "decisions": [], "open_items": []}, Reason.GATE_UNGROUNDED),
    ({"headline": "", "what_happened": "ok", "decisions": [], "open_items": []}, Reason.GATE_UNGROUNDED),
])
async def test_links_and_oversized_or_empty_summaries_fall_back_to_the_template(fake, bad, code):
    _, _, _, _, cid = await closed_case(fake)
    fake.report = lambda facts: bad
    row = await reporting.closing_summary(cid, None)
    assert row["source"] == "template" and cid in row["content"]["headline"]
    assert [r["reason_code"] for r in await audit_rows(cid, "gate_reject")][-1] == code.value


async def test_a_summary_that_does_not_match_the_schema_falls_back_to_the_template(fake):
    _, _, _, _, cid = await closed_case(fake)
    fake.report = lambda facts: {"headline": "Closed"}                                            # what_happened missing
    row = await reporting.closing_summary(cid, None)
    assert row["source"] == "template"


async def test_with_the_model_down_the_template_is_stored_and_the_case_is_untouched(fake):
    _, _, _, _, cid = await closed_case(fake)
    fake.down = True
    row = await reporting.closing_summary(cid, None)
    assert row["source"] == "template" and row["kind"] == "closing"
    assert (await audit_rows(cid, "summary_generated"))[-1]["refs"]["gate_events"] == ["llm_unavailable"]
    async with db.pool.connection() as c:
        assert (await (await c.execute("SELECT status FROM cases WHERE id=%s", (cid,))).fetchone())["status"] == "CLOSED"


async def test_without_the_model_flag_no_call_is_made(fake):
    _, _, _, _, cid = await closed_case(fake)
    before = len(fake.calls)
    row = await reporting.closing_summary(cid, None, use_llm=False)
    assert row["source"] == "template" and len(fake.calls) == before


async def test_a_summary_for_a_missing_case_returns_nothing_and_never_raises(fake):
    assert await reporting.closing_summary("CASE-NOPE", None) is None


async def test_a_database_failure_while_writing_is_audited_and_does_not_raise(fake, monkeypatch):
    _, _, _, _, cid = await closed_case(fake)

    async def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(reporting, "_store", boom)
    assert await reporting.closing_summary(cid, None) is None
    last = (await audit_rows(cid, "summary_generated"))[-1]
    assert last["outcome"] == "failure" and last["severity"] == "error" and last["error"]["class"] == "RuntimeError"


# ───────────── management metrics ─────────────
async def test_only_managers_finance_and_admin_may_read_the_management_report(fake):
    await new_case(fake)
    req = await client_for("priya.requester@demo.test")
    r = await req.get("/api/reports/management")
    assert r.status_code == 403
    assert (await req.post("/api/reports/management/narrative")).status_code == 403
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM audit_log WHERE chain_key='SYSTEM' AND action IN ('report_view','report_narrative') "
                                      "AND outcome='blocked' ORDER BY seq DESC LIMIT 2")).fetchall()
    assert {r["action"] for r in rows} == {"report_view", "report_narrative"} and {r["reason_code"] for r in rows} == {Reason.POLICY_UNAUTHORIZED.value}
    for email in ("rahul.manager", "anita.regional", "vikram.finance", "admin"):
        c = await client_for(f"{email}@demo.test" if email != "admin" else "admin@demo.test")
        assert (await c.get("/api/reports/management")).status_code == 200, email


async def test_the_numbers_follow_the_case_through_approval_and_closure(fake):
    mgr = await client_for("rahul.manager@demo.test")
    m0 = (await mgr.get("/api/reports/management")).json()                                         # earlier tests leave history behind: compare changes
    req, cid, aid = await new_case(fake)
    m = (await mgr.get("/api/reports/management")).json()
    assert m["scope"] == "region West" and m["currency"] == "INR"
    assert m["cases"]["open"] == 1 and m["open_p1"] == 1 and m["open_by_risk"] == {"Critical": 1}
    assert (m["approvals"]["pending"], m["approvals"]["overdue"]) == (1, 0)
    assert m["approvals"]["approved"] == m0["approvals"]["approved"] and m["approvals"]["rejected"] == m0["approvals"]["rejected"]
    assert m["spend_inr"] == {"approved": m0["spend_inr"]["approved"], "pending_approval": 228000}
    assert [a["case_id"] for a in m["attention"]] == [cid] and set(m["attention"][0]["reasons"]) >= {"P1", "critical risk"}
    head = await client_for("anita.regional@demo.test")
    assert (await decide(head, cid, aid)).status_code == 200
    m = (await mgr.get("/api/reports/management")).json()
    assert m["spend_inr"] == {"approved": m0["spend_inr"]["approved"] + 228000, "pending_approval": 0}
    assert m["approvals"]["pending"] == 0 and m["approvals"]["approved"] == m0["approvals"]["approved"] + 1
    assert m["approvals"]["avg_decision_hours"] is not None
    d = {k: m["comms"][k] - m0["comms"][k] for k in m["comms"]}
    assert d["drafts"] == 1 and d["waiting"] >= 2 and d["sent"] == 0
    fin = await client_for("vikram.finance@demo.test")
    (draft,) = [r for r in (await mgr.get(f"/api/cases/{cid}/communications")).json()["messages"] if r["kind"] == "supplier_request"]
    await mgr.post(f"/api/outbox/{draft['id']}/release")
    from app import sender
    await sender.run_once(cid)
    await tick_all(mgr, fin, cid)
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 200
    m = (await mgr.get("/api/reports/management")).json()
    assert m["cases"]["open"] == 0 and m["closure"]["closed"] == m0["closure"]["closed"] + 1 and m["closure"]["avg_cycle_hours"] is not None
    assert m["attention"] == [] and m["comms"]["sent"] - m0["comms"]["sent"] >= 3 and m["spend_inr"]["approved"] == m0["spend_inr"]["approved"] + 228000


async def test_a_region_only_sees_its_own_cases_and_finance_and_admin_see_all(fake):
    await new_case(fake)                                                                         # a West case
    south = (await (await client_for("sneha.manager@demo.test")).get("/api/reports/management")).json()
    west = (await (await client_for("rahul.manager@demo.test")).get("/api/reports/management")).json()
    fin = (await (await client_for("vikram.finance@demo.test")).get("/api/reports/management")).json()
    adm = (await (await client_for("admin@demo.test")).get("/api/reports/management")).json()
    assert south["scope"] == "region South" and south["cases"]["open"] == 0 and south["attention"] == [] and south["approvals"]["pending"] == 0
    assert south["spend_inr"]["pending_approval"] == 0
    assert west["cases"]["open"] == 1
    assert fin["scope"] == adm["scope"] == "all regions" and fin["cases"]["open"] == adm["cases"]["open"] == 1


async def test_an_approval_past_its_deadline_is_counted_overdue_and_listed(fake):
    _, cid, _ = await new_case(fake)
    clock.set_offset(10 * 86400)
    mgr = await client_for("rahul.manager@demo.test")
    m = (await mgr.get("/api/reports/management")).json()
    assert m["approvals"]["overdue"] == 1 and "approval overdue" in m["attention"][0]["reasons"]


async def test_a_message_that_died_and_a_flagged_case_show_up_in_attention(fake):
    mgr = await client_for("rahul.manager@demo.test")
    dead0 = (await mgr.get("/api/reports/management")).json()["comms"]["dead"]
    req, cid, _ = await new_case(fake)
    async with db.pool.connection() as c:
        await c.execute("UPDATE cases SET injection_flag=true, needs_human=true, degraded=true WHERE id=%s", (cid,))
        await c.execute("INSERT INTO outbox(case_id,kind,recipient,subject,body,message_id,status,audience,source) "
                        "VALUES (%s,'stakeholder_update','x@demo.test','s','b','dead-1','dead','internal','template')", (cid,))
    m = (await mgr.get("/api/reports/management")).json()
    assert set(m["attention"][0]["reasons"]) >= {"injection flagged", "needs a human", "degraded mode", "undeliverable message"}
    assert m["injection_flagged"] == 1 and m["needs_human"] == 1 and m["degraded"] == 1 and m["comms"]["dead"] == dead0 + 1


# ───────────── management narrative ─────────────
async def test_the_narrative_describes_the_numbers_and_is_stored_and_audited(fake):
    await new_case(fake)
    seen = []
    fake.report = lambda facts: (seen.append(facts), {"headline": "One case waits", "what_happened": "1 case is open.", "decisions": [], "open_items": []})[1]
    fin = await client_for("vikram.finance@demo.test")
    r = await fin.post("/api/reports/management/narrative")
    assert r.status_code == 200 and r.json()["source"] == "llm" and r.json()["kind"] == "management" and r.json()["gate_events"] == []
    assert seen and "generated_at" not in seen[0] and all("title" not in a for a in seen[0]["attention"])
    rows = await summaries(None, "management")
    assert len(rows) >= 1 and rows[-1]["case_id"] is None and rows[-1]["created_by"]
    async with db.pool.connection() as c:
        ev = await (await c.execute("SELECT * FROM audit_log WHERE chain_key='SYSTEM' AND action='summary_generated' ORDER BY seq DESC LIMIT 1")).fetchone()
    assert ev["actor_id"] == "reporting" and ev["refs"]["requested_by"] == rows[-1]["created_by"]


async def test_a_narrative_with_an_invented_number_or_with_the_model_down_uses_the_template(fake):
    await new_case(fake)
    mgr = await client_for("rahul.manager@demo.test")
    fake.report = lambda facts: {"headline": "All good", "what_happened": "There are 41 open cases.", "decisions": [], "open_items": []}
    r = await mgr.post("/api/reports/management/narrative")
    assert r.json()["source"] == "template" and r.json()["gate_events"] == [Reason.GATE_NUMERIC_MISMATCH.value]
    assert "41" not in json.dumps(r.json()["content"]) and "1 open" in r.json()["content"]["headline"]
    await clear_cache()
    fake.down = True
    r = await mgr.post("/api/reports/management/narrative")
    assert r.status_code == 200 and r.json()["source"] == "template" and r.json()["gate_events"] == ["llm_unavailable"]


async def test_the_narrative_for_one_region_does_not_describe_another(fake):
    await new_case(fake)                                                                         # West case only
    south = await client_for("sneha.manager@demo.test")
    fake.down = True                                                                             # the template states the scope in its headline
    r = await south.post("/api/reports/management/narrative")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert r.json()["content"]["headline"].startswith("region South: 0 open") and r.json()["content"]["open_items"] == []


# ───────────── scenario library ─────────────
def test_the_library_covers_the_sample_and_every_one_of_the_fifteen_edge_cases_once():
    ids = [s["id"] for s in scenarios.SCENARIOS]
    assert len(ids) == len(set(ids)) == 16 and ids[0] == "sample"
    edges = [s["edge_case"] for s in scenarios.SCENARIOS if s["edge_case"]]
    assert sorted(edges) == sorted(scenarios.EDGE_CASES) and len(edges) == len(set(edges)) == 15


def test_every_scenario_is_complete_and_fits_the_intake_limits():
    for s in scenarios.SCENARIOS:
        assert set(s) == {"id", "edge_case", "title", "description", "expected", "channel", "subject", "body", "setup", "steps"}, s["id"]
        assert all(s[k].strip() for k in ("title", "description", "expected", "body", "subject")) and s["steps"], s["id"]
        assert 0 < len(s["body"]) <= 20000 and s["channel"] in ("email", "manual", "supplier_update")


def test_only_the_injection_scenario_trips_the_scanner():
    flagged = {s["id"] for s in scenarios.SCENARIOS if safety.scan(s["body"]).flagged}
    assert flagged == {"prompt_injection"}


async def test_the_scenario_list_needs_a_login_and_serves_the_library(fake):
    import httpx
    from app.main import app
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    assert (await anon.get("/api/scenarios")).status_code == 401
    r = await (await client_for("priya.requester@demo.test")).get("/api/scenarios")
    assert r.status_code == 200 and r.json()["scenarios"] == scenarios.SCENARIOS


async def test_the_injection_scenario_is_flagged_and_nothing_is_approved_by_the_email(fake):
    s = next(x for x in scenarios.SCENARIOS if x["id"] == "prompt_injection")
    req = await client_for("priya.requester@demo.test")
    import uuid
    r = await req.post("/api/cases?wait=true", json={"channel": s["channel"], "subject": s["subject"], "body": s["body"], "message_id": uuid.uuid4().hex})
    assert r.status_code == 202
    d = (await req.get(f"/api/cases/{r.json()['case_id']}")).json()
    assert d["case"]["injection_flag"] is True and d["case"]["status"] != "APPROVED"
    assert all(a["approver_id"] is None and a["status"] != "approved" for a in d["approvals"])


async def test_the_duplicate_scenario_is_linked_to_the_sample_case(fake):
    import uuid
    req = await client_for("priya.requester@demo.test")
    first, second = (next(x for x in scenarios.SCENARIOS if x["id"] == i) for i in ("sample", "duplicate"))
    ids = []
    for s in (first, second):
        r = await req.post("/api/cases?wait=true", json={"channel": s["channel"], "subject": s["subject"], "body": s["body"], "message_id": uuid.uuid4().hex})
        ids.append(r.json()["case_id"])
    d = (await req.get(f"/api/cases/{ids[1]}")).json()
    assert d["case"]["status"] == "DUPLICATE" and d["case"]["duplicate_of"] == ids[0]
