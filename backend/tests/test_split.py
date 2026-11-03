"""B15: one message with several problems. Part 1: consolidate() as a pure function. Part 2: through the real pipeline."""
import pytest

from app import db
from app.extract import FIELDS, Field, IntakeIssueResult
from app.split import MAX_ISSUES, conflicts, consolidate
from tests.test_cases_api import _clean, client_for, fake, mail  # noqa: F401
from tests.test_changes import detail, outbox, post_msg
from tests.test_extract import fv, good_issue, issue, out


# ───────────── pure ─────────────
def f(name, value):
    return Field(name, value, value, "ai", confidence=0.9)


def blank(name):
    return Field(name, None, None, "missing")


def iss(intent="delivery_delay", summary="s", **vals):
    return IntakeIssueResult(intent, summary, {n: f(n, vals[n]) if n in vals else blank(n) for n in FIELDS})


def test_single_issue_unchanged():
    a = iss(location="Pune", quantity_affected="500")
    assert consolidate([a]) == [a]


def test_empty_is_an_error_not_a_silent_pass():
    with pytest.raises(ValueError):
        consolidate([])


def test_noise_is_dropped_but_never_the_last_issue():
    real = iss(location="Pune", quantity_affected="500")
    assert consolidate([iss("other"), real, iss("unclear")]) == [real]
    only_noise = iss("other")
    assert consolidate([only_noise]) == [only_noise]          # something must survive: the case then asks for clarification


def test_fragments_of_one_problem_merge_and_fill_gaps():
    a = iss(location="Pune", quantity_affected="500")
    b = iss(location="pune store", delay_days="10")
    (m,) = consolidate([a, b])
    assert m.fields["quantity_affected"].value == "500" and m.fields["delay_days"].value == "10" and m.fields["location"].value == "Pune"


def test_merge_never_overwrites_the_first_issues_value():
    a = iss(location="Pune", quantity_affected="500")
    b = iss(location="Pune", item="display units")
    (m,) = consolidate([a, b])
    assert m.fields["quantity_affected"].value == "500" and m.fields["item"].value == "display units"


def test_conflicting_values_stay_separate_so_nothing_leaks_between_problems():
    a = iss(location="Pune", quantity_affected="500")
    b = iss(location="Pune", quantity_affected="200")
    assert conflicts(a, b) == ["quantity_affected"]
    assert len(consolidate([a, b])) == 2


def test_different_locations_stay_separate():
    assert len(consolidate([iss(location="Pune", delay_days="10"), iss(location="Delhi", delay_days="10")])) == 2


def test_without_a_shared_location_there_is_no_basis_to_merge():
    assert len(consolidate([iss(quantity_affected="500"), iss(delay_days="10")])) == 2


def test_numbers_compare_as_numbers():
    assert conflicts(iss(quantity_affected="500"), iss(quantity_affected="500.0")) == []


def test_concrete_intent_wins_over_a_vague_one_when_merging():
    (m,) = consolidate([iss("other", location="Pune", quantity_affected="5"), iss("delivery_delay", location="Pune", delay_days="3")])
    assert m.intent == "delivery_delay"


# ───────────── through the pipeline ─────────────
TWO = ("Hi team. Two separate things.\nPune store: 500 display units are delayed by 10 days and only 120 units are in the warehouse.\n"
       "Separately, 200 shelving bays are short and we need them by Friday.")


def two_issue_extraction():
    a = issue(item=fv("display units"), quantity_affected=fv("500", "500 display units"), quantity_available=fv("120", "only 120 units"),
              delay_days=fv("10", "10 days"), location=fv("Pune"), summary="Pune display delay")
    b = issue(intent="inventory_shortage", quantity_affected=fv("200", "200 shelving bays"), needed_by=fv("Friday"),
              summary="Shelving shortage")
    return out(a, b)


async def split_case(fake, body=TWO, extraction=None):
    fake.intake = extraction or two_issue_extraction()
    req = await client_for("priya.requester@demo.test")
    r = await req.post("/api/cases?wait=true", json=mail(body=body))
    assert r.status_code == 202, r.text
    return req, r.json()["case_id"], await detail(req, r.json()["case_id"])


async def test_two_problems_become_two_independent_child_cases(fake):
    req, pid, p = await split_case(fake)
    assert p["case"]["status"] == "SPLIT" and p["case"]["intent"] == "multi_issue"
    kids = p["related"]["children"]
    assert len(kids) == 2 and pid not in [k["id"] for k in kids]
    assert not any(a["status"] == "pending" for a in p["approvals"]) and p["recommendations"] == []     # the container is never analysed
    d = [await detail(req, k["id"]) for k in kids]
    val = lambda c, n: next((x["value"] for x in c["fields"] if x["name"] == n), None)  # noqa: E731
    (pune,) = [c for c in d if val(c, "location") == "Pune"]
    (shelf,) = [c for c in d if val(c, "location") is None]
    # no cross-contamination: each child holds only its own problem's facts
    assert val(pune, "quantity_affected") == "500" and val(pune, "delay_days") == "10"
    assert val(shelf, "quantity_affected") == "200" and val(shelf, "delay_days") is None and val(shelf, "item") is None
    for c in d:
        assert c["case"]["parent_id"] == pid and c["case"]["status"] != "DUPLICATE"
        assert c["case"]["status"] in {"AWAITING_APPROVAL", "NEEDS_INFO"}
        assert c["related"]["parent"]["id"] == pid
        assert [s["role"] for s in c["sources"]] == ["origin"] and c["sources"][0]["id"] == p["sources"][0]["id"]
        assert all(x["quote"] and x["quote"] in TWO for x in c["fields"] if x["state"] == "ai")                  # every field grounded
    assert pune["case"]["status"] == "AWAITING_APPROVAL"                                                      # the complete problem goes on
    assert shelf["case"]["status"] == "NEEDS_INFO"                                                            # the other one has no store: it asks, it does not guess
    assert any(m["kind"] == "clarification" and "Which store" in m["body"] for m in await outbox(shelf["case"]["id"]))
    assert [m["kind"] for m in await outbox(pid)] == ["split_notice"]
    for cid in [pid, *[k["id"] for k in kids]]:
        a = (await req.get(f"/api/cases/{cid}/audit")).json()
        assert a["chain"]["ok"] and [e["seq"] for e in a["events"]] == list(range(1, len(a["events"]) + 1))
    assert any(e["reason_code"] == "INGEST_MULTI_ISSUE" for e in (await req.get(f"/api/cases/{kids[0]['id']}/audit")).json()["events"])


async def test_split_parent_cannot_be_acted_on(fake):
    req, pid, _ = await split_case(fake)
    r = await post_msg(req, pid, "revision", "update")
    assert r.status_code == 409 and "split" in r.json()["error"]
    r = await req.patch(f"/api/cases/{pid}/fields?wait=true", json={"fields": {"quantity_affected": "5"}})
    assert r.status_code == 409 and "split" in r.json()["error"]
    r = await post_msg(req, pid, "cancel", "cancel it")
    assert r.status_code == 409 and "split" in r.json()["error"]
    assert (await req.post(f"/api/cases/{pid}/not-duplicate")).status_code == 409
    a = (await req.get(f"/api/cases/{pid}/audit")).json()
    assert sum(e["outcome"] == "blocked" for e in a["events"]) >= 3 and a["chain"]["ok"]                        # refusals are audited


async def test_fragments_of_one_problem_do_not_split(fake):
    # the model over-splits one Pune problem into two fragments: the platform merges them into ONE case
    a = issue(item=fv("display units"), quantity_affected=fv("500", "500 display units"), location=fv("Pune"), summary="qty")
    b = issue(delay_days=fv("10", "10 days"), location=fv("Pune"), summary="delay")
    req, cid, d = await split_case(fake, body=TWO.split("\n")[1], extraction=out(a, b))
    assert d["case"]["status"] != "SPLIT" and d["related"]["children"] == []
    assert {x["name"] for x in d["fields"]} >= {"quantity_affected", "delay_days", "location"}


async def test_too_many_problems_asks_the_sender_to_split_instead_of_opening_a_flood(fake):
    towns = ["Pune", "Mumbai", "Delhi", "Bengaluru", "Chennai", "Kolkata"]
    assert len(towns) > MAX_ISSUES
    body = " ".join(f"{t} store is short of {100 + i} units." for i, t in enumerate(towns))
    issues = [issue(intent="inventory_shortage", quantity_affected=fv(str(100 + i), f"{100 + i} units"), location=fv(t)) for i, t in enumerate(towns)]
    req, cid, d = await split_case(fake, body=body, extraction=out(*issues))
    assert d["case"]["status"] == "NEEDS_INFO" and d["related"]["children"] == []
    clar = await outbox(cid, "clarification")
    assert len(clar) == 1 and "6 separate problems" in clar[0]["body"]
    async with db.pool.connection() as c:
        n = await (await c.execute("SELECT count(*) n FROM cases WHERE parent_id=%s", (cid,))).fetchone()
    assert n["n"] == 0
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert any(e["reason_code"] == "INGEST_UNCLEAR_INTENT" for e in a["events"]) and a["chain"]["ok"]
