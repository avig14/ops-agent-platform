"""Comms agent: what is written after approval, the gates on polished wording, the human review of supplier mail, and voiding on change."""
import pytest

from app import comms, db, sender
from app.codes import Reason
from tests.test_approvals import audit_rows, decide, new_case, status  # noqa: F401
from tests.test_approvals import _reset_clock  # noqa: F401  (autouse fixture)
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_changes import outbox, post_msg
from tests.test_extract import fv, issue, out


@pytest.fixture(autouse=True)
def _no_faults():
    sender.faults.set(None)
    yield
    sender.faults.set(None)


async def approved(fake):
    """The sample case, approved by the regional head. Returns (requester client, head client, case id)."""
    req, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    r = await decide(head, cid, aid)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    return req, head, cid


async def option_of(cid):
    async with db.pool.connection() as c:
        rec = await (await c.execute("SELECT * FROM recommendations WHERE case_id=%s AND status='approved'", (cid,))).fetchone()
        sup = await (await c.execute("SELECT * FROM suppliers")).fetchall()
    opt = next(o for o in rec["options"] if o["action"] == rec["chosen_option"])
    return opt, next(s for s in sup if s["name"] == opt["supplier"]), [s for s in sup if s["name"] != opt["supplier"]]


async def mailbox(cid):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM mailbox WHERE case_id=%s ORDER BY id", (cid,))).fetchall()


OPT = {"action": "expedite_alternate_supplier", "units": 380, "lead_time_days": 4, "description": "Buy 380 units from BrightFab Displays"}


def tpl():
    return comms.supplier_template("CASE-1", "Pune", "BrightFab Displays", "Display unit (express)", OPT, 10)


def reasons(fails):
    return [c for c, _ in fails]


# ───────────── pure: templates and gates ─────────────
def test_numbers_are_compared_as_values_not_text():
    assert comms.numbers("order 1,000 units, 2.50 each, ref 7") == comms.numbers("ref 7. 2.5 each for 1000 units")
    assert comms.numbers("no digits here") == set()


def test_the_supplier_templates_are_clean_and_carry_the_facts():
    s, b = tpl()
    assert "380" in b and "Display unit (express)" in b and "BrightFab Displays" in b and "Pune" in b
    assert comms.gate((s, b), (s, b), True, ["Acme Display Systems"]) == []                     # a template always passes its own gate
    s2, b2 = comms.supplier_template("CASE-1", None, "Acme Display Systems", "Display unit (standard)",
                                     {"action": "wait_for_original_supplier"}, 10)
    assert "delayed by 10 day(s)" in b2 and comms.gate((s2, b2), (s2, b2), True, []) == []
    _, b3 = comms.supplier_template("CASE-1", None, "Acme Display Systems", "Display unit (standard)", {"action": "wait_for_original_supplier"}, None)
    assert "delayed." in b3                                                                         # no delay figure invented


def test_the_internal_template_states_cost_or_no_spend():
    _, b = comms.stakeholder_template("CASE-1", "Pune", OPT, 228000)
    assert "INR 228000" in b and "Store: Pune." in b
    _, b = comms.stakeholder_template("CASE-1", None, OPT, 0)
    assert "No additional spend." in b and "Store:" not in b


def test_gate_accepts_a_faithful_rewrite():
    s, b = tpl()
    assert comms.gate((s, b), (s, b.replace("Hello", "Dear").replace("Thank you", "Many thanks")), True, ["Acme Display Systems"]) == []


@pytest.mark.parametrize("change", [lambda b: b + " We also need 500 more.", lambda b: b.replace("380", "many")])
def test_gate_rejects_a_number_added_or_dropped(change):
    s, b = tpl()
    assert Reason.GATE_COMMS_NUMBER in reasons(comms.gate((s, b), (s, change(b)), True, []))


@pytest.mark.parametrize("extra", [" See http://pay.example now.", " Visit www.example.com", " Write to boss@evil.example"])
def test_gate_rejects_links_and_addresses_for_every_audience(extra):
    s, b = tpl()
    for external in (True, False):
        assert Reason.GATE_COMMS_LEAK in reasons(comms.gate((s, b), (s, b + extra), external, []))


@pytest.mark.parametrize("word", ["budget", "approval", "risk", "policy", "INR", "₹", "Rs.", "shortfall", "cheaper", "margin", "internal"])
def test_gate_blocks_internal_terms_for_suppliers_only(word):
    s, b = tpl()
    leaky = b + f" Our {word} matters here."
    assert Reason.GATE_COMMS_LEAK in reasons(comms.gate((s, b), (s, leaky), True, []))
    assert comms.gate((s, b), (s, leaky), False, []) == []                                           # internal readers may see them


def test_gate_blocks_naming_another_supplier():
    s, b = tpl()
    fails = comms.gate((s, b), (s, b + " Unlike acme display systems, you are fast."), True, ["Acme Display Systems"])
    assert Reason.GATE_COMMS_LEAK in reasons(fails)
    assert comms.gate((s, b), (s, b + " Unlike acme display systems."), False, ["Acme Display Systems"]) == []


def test_gate_rejects_empty_and_oversized_drafts():
    s, b = tpl()
    assert comms.gate((s, b), ("", b), False, []) and comms.gate((s, b), (s, "  "), False, [])
    assert comms.gate((s, b), (s, b + " word" * 400), False, [])


async def test_a_supplier_template_that_leaks_is_a_bug_and_is_never_used():
    bad = ("subject", "Hello team, the budget for 5 units is fixed.")
    with pytest.raises(ValueError, match="template failed"):
        await comms.polish("CASE-1", "external", bad, [], use_llm=False)
    assert (await comms.polish("CASE-1", "internal", bad, [], use_llm=False))[2] == "template"


# ───────────── after approval ─────────────
async def test_approval_writes_internal_updates_and_a_supplier_draft_that_waits_for_a_human(fake):
    req, head, cid = await approved(fake)
    opt, sup, others = await option_of(cid)
    rows = await outbox(cid)
    internal = [r for r in rows if r["kind"] == "stakeholder_update"]
    assert {r["recipient"] for r in internal} == {"priya.requester@demo.test", "rahul.manager@demo.test"}
    assert {(r["status"], r["audience"], r["source"]) for r in internal} == {("pending", "internal", "llm")}
    assert all("INR 228000" in r["body"] and cid in r["subject"] for r in internal)
    (draft,) = [r for r in rows if r["kind"] == "supplier_request"]
    assert draft["recipient"] == sup["email"] and draft["status"] == "draft" and draft["audience"] == "external"
    assert draft["reviewed_by"] is None and draft["source"] == "llm"
    text = draft["subject"] + draft["body"]
    assert str(opt["units"]) in text and sup["name"] in text
    assert "228000" not in text and "INR" not in text and not any(o["name"] in text for o in others)     # nothing internal leaks out
    assert comms._INTERNAL.search(text) is None
    # the reviewer is told, once
    notes = [r for r in rows if r["kind"] == "draft_review"]
    assert [n["recipient"] for n in notes] == ["rahul.manager@demo.test"]
    # internal mail goes out; the supplier mail does not
    assert (await sender.run_once(cid))["sent"] >= 3
    assert {m["recipient"] for m in await mailbox(cid)} >= {"priya.requester@demo.test", "rahul.manager@demo.test"}
    assert sup["email"] not in {m["recipient"] for m in await mailbox(cid)}
    assert (await outbox(cid, "supplier_request"))[0]["status"] == "draft"
    (a,) = [x for x in await audit_rows(cid, "comms_drafted") if x["refs"]["kind"] == "supplier_request"]
    assert a["actor_type"] == "agent" and a["refs"]["source"] == "llm" and a["outcome"] == "success"
    msgs = (await req.get(f"/api/cases/{cid}")).json()["agent_messages"]
    assert [m for m in msgs if m["from_agent"] == "comms"]


async def test_calling_the_agent_again_creates_nothing_and_costs_no_model_call(fake):
    _, _, cid = await approved(fake)
    before, n = len(await outbox(cid)), fake.calls.count("comms")
    assert n == 2                                                                # the two identical internal drafts share one cached model call
    assert await comms.draft_for_approval(cid) == {"queued": 0, "drafts": 0, "skipped": 0}
    assert len(await outbox(cid)) == before and fake.calls.count("comms") == n


async def test_nothing_is_written_for_a_case_that_is_not_approved(fake):
    _, cid, _ = await new_case(fake)
    assert await comms.draft_for_approval(cid) == {"queued": 0, "drafts": 0, "skipped": 0}
    assert not await outbox(cid, "stakeholder_update") and "comms" not in fake.calls
    assert await comms.draft_for_approval("CASE-DOES-NOT-EXIST") == {"queued": 0, "drafts": 0, "skipped": 0}


async def test_with_the_model_down_the_template_wording_is_used_and_the_approval_stands(fake):
    req, cid, aid = await new_case(fake)
    fake.down = True                                                             # the outage starts after analysis
    head = await client_for("anita.regional@demo.test")
    r = await decide(head, cid, aid)
    assert r.json()["status"] == "APPROVED" and await status(cid) == "APPROVED"
    rows = [r for r in await outbox(cid) if r["kind"] in ("stakeholder_update", "supplier_request")]
    assert len(rows) == 3 and {r["source"] for r in rows} == {"template"}
    assert all("INR 228000" in r["body"] for r in rows if r["audience"] == "internal")
    assert [r["status"] for r in rows if r["audience"] == "external"] == ["draft"]
    assert all(a["refs"]["gate_events"] == ["llm_unavailable"] for a in await audit_rows(cid, "comms_drafted"))


async def test_wording_that_adds_a_number_is_replaced_by_the_template(fake):
    fake.comms = lambda d: {"subject": d["subject"], "body": d["body"] + " Quantity 999."}
    _, _, cid = await approved(fake)
    rows = [r for r in await outbox(cid) if r["kind"] in ("stakeholder_update", "supplier_request")]
    assert {r["source"] for r in rows} == {"template"} and not any("999" in r["body"] for r in rows)
    rej = await audit_rows(cid, "gate_reject")
    assert len(rej) == 3 and {a["reason_code"] for a in rej} == {"GATE_COMMS_NUMBER"} and all(a["outcome"] == "blocked" for a in rej)


async def test_wording_that_leaks_internal_terms_is_refused_for_suppliers_but_fine_internally(fake):
    fake.comms = lambda d: {"subject": d["subject"], "body": d["body"] + " Our budget is limited."}
    _, _, cid = await approved(fake)
    rows = await outbox(cid)
    assert {r["source"] for r in rows if r["kind"] == "stakeholder_update"} == {"llm"}
    (draft,) = [r for r in rows if r["kind"] == "supplier_request"]
    assert draft["source"] == "template" and "budget" not in draft["body"].lower()
    (rej,) = await audit_rows(cid, "gate_reject")
    assert rej["reason_code"] == "GATE_COMMS_LEAK" and rej["refs"]["audience"] == "external"


async def test_good_polish_is_accepted_and_recorded_as_model_wording(fake):
    fake.comms = lambda d: {"subject": d["subject"], "body": d["body"].replace("Hello", "Dear", 1)}
    _, _, cid = await approved(fake)
    rows = [r for r in await outbox(cid) if r["kind"] in ("stakeholder_update", "supplier_request")]
    assert {r["source"] for r in rows} == {"llm"} and all(r["body"].startswith("Dear") for r in rows)
    assert not await audit_rows(cid, "gate_reject")


# ───────────── human review of supplier mail ─────────────
async def test_a_manager_releases_the_supplier_draft_and_only_then_the_supplier_receives_it(fake):
    _, _, cid = await approved(fake)
    _, sup, _ = await option_of(cid)
    (draft,) = await outbox(cid, "supplier_request")
    mgr = await client_for("rahul.manager@demo.test")
    assert (await mgr.post(f"/api/outbox/{draft['id']}/release")).status_code == 200
    await sender.run_once(cid)
    got = [m for m in await mailbox(cid) if m["recipient"] == sup["email"]]
    assert len(got) == 1 and got[0]["kind"] == "supplier_request" and got[0]["body"] == draft["body"]
    assert (await outbox(cid, "supplier_request"))[0]["status"] == "sent"


# ───────────── change after approval ─────────────
async def test_a_revision_after_approval_voids_unsent_messages(fake):
    req, _, cid = await approved(fake)
    fake.intake = out(issue(delay_days=fv("20", "20 days")))
    r = await post_msg(req, cid, "revision", "Supplier update: delay is now 20 days.")
    assert r.json()["changed"]
    rows = [r for r in await outbox(cid) if r["kind"] in ("stakeholder_update", "supplier_request")]
    assert len(rows) == 3 and {r["status"] for r in rows} == {"cancelled"}
    assert (await sender.run_once(cid))["sent"] >= 0 and not [m for m in await mailbox(cid) if m["kind"] in ("stakeholder_update", "supplier_request")]
    assert await audit_rows(cid, "comms_voided")
    assert await comms.draft_for_approval(cid) == {"queued": 0, "drafts": 0, "skipped": 0}      # the case is not approved any more


async def test_a_message_already_sent_is_left_alone_by_a_later_revision(fake):
    req, _, cid = await approved(fake)
    await sender.run_once(cid)                                                                   # internal updates are out
    fake.intake = out(issue(delay_days=fv("20", "20 days")))
    await post_msg(req, cid, "revision", "Supplier update: delay is now 20 days.")
    by_kind = {(r["kind"], r["status"]) for r in await outbox(cid)}
    assert ("stakeholder_update", "sent") in by_kind and ("supplier_request", "cancelled") in by_kind


async def test_cancelling_after_approval_voids_the_supplier_draft_and_pending_updates(fake):
    _, head, cid = await approved(fake)
    r = await post_msg(head, cid, "cancel", "Confirmed: cancel.")
    assert r.json()["outcome"] == "cancelled"
    rows = [r for r in await outbox(cid) if r["kind"] in ("stakeholder_update", "supplier_request")]
    assert len(rows) == 3 and {r["status"] for r in rows} == {"cancelled"}
    assert await outbox(cid, "cancel_notice") or await outbox(cid, "finance_task")               # people are still told about the cancel
    await sender.run_once(cid)
    assert not [m for m in await mailbox(cid) if m["kind"] == "supplier_request"]


# ───────────── the trail ─────────────
async def test_the_whole_comms_flow_leaves_a_valid_contiguous_audit_chain(fake):
    fake.comms = lambda d: {"subject": d["subject"], "body": d["body"] + " Quantity 999."}
    req, _, cid = await approved(fake)
    await sender.run_once(cid)
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert a["chain"]["ok"] is True and [e["seq"] for e in a["events"]] == list(range(1, len(a["events"]) + 1))
    assert {"comms_drafted", "gate_reject", "message_sent"} <= {e["action"] for e in a["events"]}
    assert not any("228000" in str(e["summary"]) and e["action"] == "comms_drafted" for e in a["events"])      # no message text in the log
    adm = await client_for("admin@demo.test")
    v = (await adm.get(f"/api/admin/audit/verify?chain={cid}")).json()
    assert v["ok"] is True
