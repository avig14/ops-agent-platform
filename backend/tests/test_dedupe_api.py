"""A4 duplicate handling through the real pipeline and HTTP API (the pure scoring tests are in test_dedupe.py)."""
import asyncio

from app import db
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_changes import detail, outbox, post_msg
from tests.test_extract import fv, good_issue, out
from tests.test_safety import SAMPLE



async def chain_ok(c, cid):
    a = (await c.get(f"/api/cases/{cid}/audit")).json()
    assert a["chain"]["ok"] and [e["seq"] for e in a["events"]] == list(range(1, len(a["events"]) + 1))
    return a["events"]


async def test_same_issue_twice_is_one_workflow(fake):
    req = await client_for("priya.requester@demo.test")
    c1, d1 = await submit(req)
    assert d1["case"]["status"] == "AWAITING_APPROVAL"
    calls_before = len(fake.calls)
    c2, d2 = await submit(req)
    assert d2["case"]["status"] == "DUPLICATE" and d2["case"]["duplicate_of"] == c1
    assert d2["related"]["duplicate_of"]["id"] == c1
    assert d2["recommendations"] == [] and d2["approvals"] == []                    # no second cost, no second approval
    assert not any(k in ("risk", "recommend") for k in fake.calls[calls_before:])  # the duplicate is not analysed
    d1b = await detail(req, c1)
    assert len(d1b["approvals"]) == len(d1["approvals"]) and d1b["case"]["status"] == "AWAITING_APPROVAL"
    assert {s["role"] for s in d1b["sources"]} == {"origin", "duplicate"}           # the extra report is attached to the original
    (n,) = await outbox(c2, "duplicate_notice")
    assert c1 in n["body"] and "nothing is approved twice" in n["body"]
    ev1, ev2 = await chain_ok(req, c1), await chain_ok(req, c2)
    assert any(e["action"] == "duplicate_linked" for e in ev1)
    dup = [e for e in ev2 if e["action"] == "status_change" and e["reason_code"] == "WORKFLOW_DUPLICATE"]
    assert dup and dup[0]["actor_id"] == "dedupe" and dup[0]["refs"]["kind"] == "duplicate"


async def test_a_duplicate_cannot_be_revised_or_cancelled(fake):
    req = await client_for("priya.requester@demo.test")
    await submit(req)
    c2, _ = await submit(req)
    for kind in ("revision", "reply", "cancel"):
        r = await post_msg(req, c2, kind, "x")
        assert r.status_code == 409, (kind, r.text)
    assert "duplicate" in (await post_msg(req, c2, "revision", "x")).json()["error"].lower()


async def test_concurrent_identical_ingest_yields_exactly_one_original(fake):
    req = await client_for("priya.requester@demo.test")
    rs = await asyncio.gather(*[req.post("/api/cases?wait=true", json=mail()) for _ in range(3)])
    ids = [r.json()["case_id"] for r in rs]
    ds = {i: await detail(req, i) for i in ids}
    assert sorted(d["case"]["status"] for d in ds.values()) == ["AWAITING_APPROVAL", "DUPLICATE", "DUPLICATE"]
    (orig,) = [i for i, d in ds.items() if d["case"]["status"] != "DUPLICATE"]
    for i, d in ds.items():                                                          # duplicates point at the one original, never at each other
        assert d["case"]["duplicate_of"] in (None, orig)
        await chain_ok(req, i)


async def test_same_problem_with_a_different_number_is_only_a_possible_duplicate(fake):
    req = await client_for("priya.requester@demo.test")
    c1, _ = await submit(req)
    g = good_issue()
    g["delay_days"] = fv("12", "12 days")
    fake.intake = out(g)
    c2, d2 = await submit(req, body=SAMPLE + "\nCorrection: the delay is now 12 days.")
    assert d2["case"]["status"] == "AWAITING_APPROVAL"                               # not merged: a human decides
    assert "POSSIBLE_DUPLICATE" in {r["id"] for r in d2["policy"]["rules_fired"]} and d2["case"]["needs_human"]
    (rel,) = d2["related"]["possible_duplicates"]
    assert rel["id"] == c1 and rel["kind"] == "possible"
    assert (await detail(req, c1))["case"]["duplicate_of"] is None
    assert any(e["action"] == "duplicate_check" for e in await chain_ok(req, c2))


async def test_possible_duplicate_is_hidden_from_a_user_who_cannot_see_it(fake):
    west = await client_for("priya.requester@demo.test")
    c1, _ = await submit(west)
    g = good_issue()
    g["delay_days"] = fv("12", "12 days")
    fake.intake = out(g)
    mgr = await client_for("rahul.manager@demo.test")
    c2, d2 = await submit(mgr, body=SAMPLE + "\nCorrection: the delay is now 12 days.")
    assert [r["id"] for r in d2["related"]["possible_duplicates"]] == [c1]       # the manager (West) sees it
    # the original belongs to Priya; a requester who is not Priya sees neither case
    south = await client_for("karthik.requester@demo.test")
    assert (await south.get(f"/api/cases/{c2}")).status_code == 404


async def test_a_cancellation_is_never_swallowed_as_a_duplicate(fake):
    req = await client_for("priya.requester@demo.test")
    await submit(req)
    x = out(good_issue())
    x["is_cancellation"] = True
    fake.intake = x
    _, d = await submit(req)
    assert d["case"]["status"] != "DUPLICATE"


async def test_a_closed_original_cannot_absorb_a_new_report(fake):
    req = await client_for("priya.requester@demo.test")
    c1, _ = await submit(req)
    async with db.pool.connection() as c:
        await c.execute("UPDATE cases SET status='CLOSED' WHERE id=%s", (c1,))
    _, d2 = await submit(req)
    assert d2["case"]["status"] == "AWAITING_APPROVAL"


async def test_not_a_duplicate_recovers_the_case(fake):
    req = await client_for("priya.requester@demo.test")
    c1, _ = await submit(req)
    c2, _ = await submit(req)
    assert (await detail(req, c2))["case"]["status"] == "DUPLICATE"
    south = await client_for("karthik.requester@demo.test")
    assert (await south.post(f"/api/cases/{c2}/not-duplicate")).status_code == 404         # cannot see it, cannot touch it
    assert (await req.post(f"/api/cases/{c1}/not-duplicate")).status_code == 409           # only a DUPLICATE case can be unlinked
    r = await req.post(f"/api/cases/{c2}/not-duplicate?wait=true")
    assert r.status_code == 200 and r.json()["status"] == "AWAITING_APPROVAL" and r.json()["duplicate_of"] is None
    d2 = await detail(req, c2)
    assert d2["case"]["duplicate_of"] is None and d2["approvals"] and d2["facts"]["intake"]["dedupe_override"] is True
    assert {s["role"] for s in (await detail(req, c1))["sources"]} == {"origin"}              # the extra-report link is removed
    assert any(e["action"] == "duplicate_unlinked" for e in await chain_ok(req, c1))
    assert any(e["actor_type"] == "human" and "not a duplicate" in e["summary"] for e in await chain_ok(req, c2))
    assert (await req.post(f"/api/cases/{c2}/not-duplicate")).status_code == 409           # not repeatable


async def waiting_pair(fake, n=2):
    """n waiting cases about the same Pune problem: the screen links the repeats, a human unlinks them (this also exercises not-duplicate)."""
    req = await client_for("priya.requester@demo.test")
    ids = [(await submit(req))[0]]
    for _ in range(n - 1):
        cid, _ = await submit(req)
        assert (await req.post(f"/api/cases/{cid}/not-duplicate?wait=true")).status_code == 200
        assert (await detail(req, cid))["case"]["status"] == "AWAITING_APPROVAL"
        ids.append(cid)
    return req, ids


async def two_waiting_cases(fake):
    req, (c1, c2) = await waiting_pair(fake)
    return req, c1, c2


async def test_a_human_can_link_a_missed_duplicate(fake):
    req, c1, c2 = await two_waiting_cases(fake)
    assert any(a["status"] == "pending" for a in (await detail(req, c2))["approvals"])
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})
    assert r.status_code == 200 and r.json() == {"status": "DUPLICATE", "duplicate_of": c1}
    d2 = await detail(req, c2)
    assert d2["case"]["status"] == "DUPLICATE" and d2["case"]["duplicate_of"] == c1
    assert not any(a["status"] == "pending" for a in d2["approvals"]) and all(x["status"] != "proposed" for x in d2["recommendations"])
    assert {s["role"] for s in (await detail(req, c1))["sources"]} == {"origin", "duplicate"}
    assert len(await outbox(c2, "duplicate_notice")) == 1
    me = (await mgr.get("/api/auth/me")).json()
    assert any(e["actor_id"] == me["id"] and e["reason_code"] == "WORKFLOW_DUPLICATE" for e in await chain_ok(req, c2))
    assert any(e["action"] == "duplicate_linked" for e in await chain_ok(req, c1))
    assert (await mgr.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})).status_code == 409        # already DUPLICATE


async def test_link_duplicate_refuses_bad_targets_and_bad_callers(fake):
    req, c1, c2 = await two_waiting_cases(fake)
    assert (await req.post(f"/api/cases/{c2}/link-duplicate", json={"of": c2})).status_code == 422
    assert (await req.post(f"/api/cases/{c2}/link-duplicate", json={"of": "CASE-9999"})).status_code == 404
    assert (await req.post(f"/api/cases/{c2}/link-duplicate", json={"of": "not-a-case"})).status_code == 422
    south = await client_for("karthik.requester@demo.test")
    assert (await south.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})).status_code == 404
    admin = await client_for("admin@demo.test")
    assert (await admin.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})).status_code in (403, 404)
    assert (await detail(req, c2))["case"]["status"] == "AWAITING_APPROVAL"                  # nothing changed
    assert sum(e["outcome"] == "blocked" for e in await chain_ok(req, c2)) >= 1


async def test_link_duplicate_resolves_chains_and_refuses_loops(fake):
    req, c1, c2 = await two_waiting_cases(fake)
    assert (await req.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})).status_code == 200
    r = await req.post(f"/api/cases/{c1}/link-duplicate", json={"of": c2})                  # c2 already points at c1: a loop
    assert r.status_code == 409 and "loop" in r.json()["error"]
    assert (await detail(req, c1))["case"]["status"] == "AWAITING_APPROVAL"
    c3, _ = await submit(req)                                                                # a third report: screened as a duplicate, then unlinked
    assert (await req.post(f"/api/cases/{c3}/not-duplicate?wait=true")).status_code == 200
    r = await req.post(f"/api/cases/{c3}/link-duplicate", json={"of": c2})                  # linked to a duplicate: attaches to the ROOT
    assert r.status_code == 200 and r.json()["duplicate_of"] == c1
    assert (await detail(req, c3))["case"]["duplicate_of"] == c1


async def test_an_approved_case_cannot_be_linked_away(fake):
    req, c1, c2 = await two_waiting_cases(fake)
    async with db.pool.connection() as c:
        await c.execute("UPDATE cases SET status='APPROVED' WHERE id=%s", (c2,))
    r = await req.post(f"/api/cases/{c2}/link-duplicate", json={"of": c1})
    assert r.status_code == 409 and "waiting" in r.json()["error"]
