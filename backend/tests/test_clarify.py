"""A2/A3/B15: clarification requests. Part 1: text builder. Part 2: the flows through the pipeline."""
import pytest

from app import clarify
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_changes import detail, needs_info_case, outbox, post_msg
from tests.test_extract import fv, good_issue, issue, out
from tests.test_safety import SAMPLE

INTERNAL = ("INR", "cost", "risk", "margin", "regional_head", "finance", "policy", "confidence", "approver")


# ───────────── pure ─────────────
def test_missing_field_becomes_a_plain_question():
    c = clarify.build("CASE-0001", "Display delay", [{"field": "location", "why": "x"}], [])
    assert c["questions"] == ["Which store or site is this about?"]
    assert "CASE-0001" in c["subject"] and "Which store" in c["body"] and "1." in c["body"]


def test_unknown_store_is_named_back_to_the_sender():
    c = clarify.build("CASE-0001", None, [{"field": "location", "why": "'Atlantis' is not a known store"}], [])
    assert "'Atlantis'" in c["questions"][0]


def test_conflict_quotes_both_values_and_the_source():
    c = clarify.build("CASE-0001", None, [], [{"field": "quantity_affected", "email": "500", "system": "300", "system_source": "the purchase order"}])
    assert "500" in c["questions"][0] and "300" in c["questions"][0] and "purchase order" in c["questions"][0]


def test_too_many_issues_asks_to_resend_separately():
    c = clarify.build("CASE-0001", None, [{"field": "intent", "why": "x"}], [], {"too_many_issues": 7})
    assert len(c["questions"]) == 1 and "7 separate problems" in c["questions"][0]


def test_questions_are_deduplicated_and_unknown_fields_have_a_fallback():
    c = clarify.build("CASE-0001", None, [{"field": "location", "why": ""}, {"field": "location", "why": ""}, {"field": "zip_code", "why": ""}], [])
    assert len(c["questions"]) == 2 and c["questions"][1] == "Please provide: zip code."


def test_key_hash_changes_only_when_the_questions_change():
    a = clarify.build("CASE-0001", "t", [{"field": "location", "why": ""}], [])
    b = clarify.build("CASE-0001", "other title", [{"field": "location", "why": ""}], [])
    c = clarify.build("CASE-0001", "t", [{"field": "location", "why": ""}, {"field": "delay_days", "why": ""}], [])
    assert a["key_hash"] == b["key_hash"] != c["key_hash"]


@pytest.mark.parametrize("field", list(clarify.QUESTIONS))
def test_no_question_leaks_internal_reasoning(field):
    c = clarify.build("CASE-0001", None, [{"field": field, "why": ""}], [])
    assert not any(w.lower() in c["body"].lower() for w in INTERNAL), c["body"]


# ───────────── through the pipeline ─────────────
async def test_missing_info_queues_a_clarification_once(fake):
    req, cid = await needs_info_case(fake)
    mails = await outbox(cid, "clarification")
    assert len(mails) == 1 and "Which store" in mails[0]["body"] and not any(w.lower() in mails[0]["body"].lower() for w in INTERNAL)
    assert mails[0]["recipient"] == "priya.requester@demo.test"
    r = await req.post(f"/api/cases/{cid}/retry?wait=true")                      # re-analysis of the same gap must not spam the requester
    assert r.status_code in (200, 409)
    assert len(await outbox(cid, "clarification")) == 1
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert sum(e["action"] == "clarification_requested" for e in a["events"]) == 1


async def test_unclear_message_asks_what_is_needed_and_a_reply_makes_it_concrete(fake):
    fake.intake = out(issue(intent="unclear"))
    req = await client_for("priya.requester@demo.test")
    cid, d = await submit(req)
    assert d["case"]["status"] == "NEEDS_INFO" and d["case"]["intent"] == "unclear"
    assert "intent" in d["facts"]["missing"]
    assert d["recommendations"] == [] and d["approvals"] == []                    # nothing is costed or approved on a guess
    (m,) = await outbox(cid, "clarification")
    assert "What do you need from us?" in m["body"]
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert any(e["reason_code"] == "INGEST_UNCLEAR_INTENT" and e["action"] == "status_change" for e in a["events"])
    # the sender answers: the case gets a concrete intent, version 2, and moves on
    fake.intake = out(good_issue())
    r = await post_msg(req, cid, "reply", SAMPLE)
    assert r.json()["changed"] is True
    d = await detail(req, cid)
    assert d["case"]["intent"] == "delivery_delay" and d["case"]["version"] == 2 and d["case"]["status"] == "AWAITING_APPROVAL"
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    rev = [e for e in a["events"] if e["action"] == "case_revised"][0]
    assert rev["diff"]["intent"] == {"from": "unclear", "to": "delivery_delay"} and a["chain"]["ok"]


async def test_an_intent_only_reply_still_counts_as_a_change(fake):
    fake.intake = out(issue(intent="unclear"))
    req = await client_for("priya.requester@demo.test")
    cid, _ = await submit(req)
    fake.intake = out(issue(intent="supplier_issue", location=fv("Pune")))
    r = await post_msg(req, cid, "reply", "It is a supplier problem for the Pune store.")
    assert r.json()["changed"] is True
    assert (await detail(req, cid))["case"]["intent"] == "supplier_issue"


async def test_a_reply_never_rewrites_a_concrete_intent(fake):
    req, cid = await needs_info_case(fake)
    fake.intake = out(issue(intent="supplier_issue", location=fv("Pune")))
    await post_msg(req, cid, "reply", "The store is Pune.")
    assert (await detail(req, cid))["case"]["intent"] == "delivery_delay"
