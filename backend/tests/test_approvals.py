"""Approvals: who may decide, rejection re-plan, stale/duplicate decisions, deadline reminders and escalation, time travel."""
import asyncio

import pytest
import pytest_asyncio

from app import approvals, clock, db
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401  (fixtures)


@pytest_asyncio.fixture(autouse=True)
async def _reset_clock():
    clock.set_offset(0)
    yield
    clock.set_offset(0)


async def pending(cid):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM approvals WHERE case_id=%s AND status='pending' ORDER BY id", (cid,))).fetchall()


async def all_approvals(cid):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM approvals WHERE case_id=%s ORDER BY id", (cid,))).fetchall()


async def status(cid):
    async with db.pool.connection() as c:
        return (await (await c.execute("SELECT status FROM cases WHERE id=%s", (cid,))).fetchone())["status"]


async def audit_rows(cid, action):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM audit_log WHERE chain_key=%s AND action=%s ORDER BY seq", (cid, action))).fetchall()


async def decide(c, cid, aid, decision="approve", reason=None, wait=True):
    return await c.post(f"/api/cases/{cid}/approvals/{aid}/decision?wait={'true' if wait else 'false'}",
                        json={"decision": decision, **({"reason": reason} if reason else {})})


async def new_case(fake):
    req = await client_for("priya.requester@demo.test")
    cid, d = await submit(req)
    assert d["case"]["status"] == "AWAITING_APPROVAL"
    return req, cid, d["approvals"][0]["id"]


# ───────────── authorization ─────────────
async def test_only_the_right_human_can_approve_and_every_refusal_is_audited(fake):
    req, cid, aid = await new_case(fake)
    # requester approving their own request: separation of duties
    r = await decide(req, cid, aid)
    assert r.status_code == 403 and "separation" in r.json()["error"]
    # a manager is below the regional-head tier
    mgr = await client_for("rahul.manager@demo.test")
    r = await decide(mgr, cid, aid)
    assert r.status_code == 403
    # admin holds no approval authority
    adm = await client_for("admin@demo.test")
    assert (await decide(adm, cid, aid)).status_code == 403
    # another region cannot even see the case
    south = await client_for("sneha.manager@demo.test")
    assert (await decide(south, cid, aid)).status_code == 404
    assert await status(cid) == "AWAITING_APPROVAL" and len(await pending(cid)) == 1       # nothing changed
    blocked = await audit_rows(cid, "approval_decision")
    assert [b["reason_code"] for b in blocked] == ["POLICY_SOD", "POLICY_UNAUTHORIZED", "POLICY_UNAUTHORIZED"]
    assert all(b["outcome"] == "blocked" for b in blocked)


async def test_regional_head_approves_and_second_decision_is_409(fake):
    req, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    r = await decide(head, cid, aid, reason="ok")
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    d = (await req.get(f"/api/cases/{cid}")).json()
    assert d["case"]["needs_human"] is False and d["approvals"][0]["approver_id"] == "u-rh-west"
    assert [x["status"] for x in d["recommendations"] if x["status"] != "superseded"] == ["approved"]
    assert (await decide(head, cid, aid)).status_code == 409                                  # already decided
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert a["chain"]["ok"] and "approval_decision" in {e["action"] for e in a["events"]}


async def test_concurrent_decisions_exactly_one_wins(fake):
    _, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    r1, r2 = await asyncio.gather(decide(head, cid, aid, "approve"), decide(head, cid, aid, "reject", "not now"))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert await status(cid) in ("APPROVED", "AWAITING_APPROVAL", "REPLANNING", "REJECTED", "ESCALATED")
    assert len([a for a in await all_approvals(cid) if a["approver_id"]]) == 1


# ───────────── rejection ─────────────
async def test_rejection_needs_a_reason(fake):
    _, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    assert (await decide(head, cid, aid, "reject")).status_code == 422
    assert len(await pending(cid)) == 1


async def test_rejection_replans_without_the_rejected_option_and_old_approval_is_stale(fake):
    req, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    r = await decide(head, cid, aid, "reject", "too expensive, find another way")
    assert r.status_code == 200
    d = (await req.get(f"/api/cases/{cid}")).json()
    assert d["case"]["status"] == "AWAITING_APPROVAL"
    rejected = [x for x in d["recommendations"] if x["status"] == "rejected"]
    live = [x for x in d["recommendations"] if x["status"] in ("proposed", "abstained")]
    assert rejected[0]["chosen_option"] == "expedite_alternate_supplier"
    assert live and live[0]["round"] == 2 and live[0]["chosen_option"] != "expedite_alternate_supplier"   # rejected option not re-proposed
    assert "PRIOR_REJECTION" in {x["id"] for x in d["policy"]["rules_fired"]}                       # a human must look at round 2 too
    assert {a["round"] for a in d["approvals"] if a["status"] == "pending"} == {2}
    assert (await decide(head, cid, aid, "approve")).status_code == 409                       # round-1 approval is dead
    assert any(m["kind"] == "event" and m["payload"].get("event") == "rejected" for m in d["agent_messages"])


async def test_three_rejections_stop_the_loop_and_escalate(fake):
    req, cid, aid = await new_case(fake)
    head = await client_for("anita.regional@demo.test")
    for rnd in (1, 2, 3):
        ap = await pending(cid)
        assert len(ap) == 1 and ap[0]["round"] == rnd
        r = await decide(head, cid, ap[0]["id"], "reject", f"no {rnd}")
        assert r.status_code == 200
    assert await status(cid) == "ESCALATED" and not await pending(cid)


# ───────────── deadlines ─────────────
async def test_reminders_then_escalation_then_breach_and_never_auto_approve(fake):
    req, cid, aid = await new_case(fake)
    a = (await pending(cid))[0]
    window = (a["deadline_at"] - a["created_at"]).total_seconds()

    async def sweep():
        return await approvals.sweep_deadlines(cid)

    assert (await sweep())["reminded"] == 0                                                   # nothing due yet
    clock.set_offset(int(window * 0.6))
    assert (await sweep())["reminded"] == 1 and (await sweep())["reminded"] == 0              # 50% reminder, once
    clock.set_offset(int(window * 0.95))
    assert (await sweep())["reminded"] == 1 and (await sweep())["reminded"] == 0              # 90% reminder, once
    async with db.pool.connection() as c:
        n = await (await c.execute("SELECT count(*) n FROM outbox WHERE case_id=%s AND kind='approval_notice' AND recipient='anita.regional@demo.test'", (cid,))).fetchone()
    assert n["n"] == 2

    clock.set_offset(int(window) + 60)
    assert (await sweep())["escalated"] == 1                                                  # regional head -> finance director
    rows = await all_approvals(cid)
    assert [(r["required_role"], r["status"]) for r in rows] == [("regional_head", "escalated"), ("finance_director", "pending")]
    assert await status(cid) == "AWAITING_APPROVAL" and not [r for r in rows if r["status"] == "approved"]

    new = (await pending(cid))[0]
    clock.set_offset(int(window) + 60 + 5 * 3600)
    assert (await sweep())["breached"] == 1                                                   # nobody above finance director
    assert await status(cid) == "ESCALATED"
    assert [r["status"] for r in await all_approvals(cid)].count("approved") == 0             # time alone never approves
    ev = await audit_rows(cid, "approval_escalated")
    assert len(ev) == 2 and all(e["reason_code"] == "WORKFLOW_DEADLINE_MISSED" for e in ev)
    assert (await sweep()) == {"reminded": 0, "escalated": 0, "breached": 0, "errors": 0}     # idempotent

    # the escalated owner can still decide
    fin = await client_for("vikram.finance@demo.test")
    assert (await decide(fin, cid, new["id"], "approve", "go")).json()["status"] == "APPROVED"
    # the old, escalated approval cannot be used any more
    head = await client_for("anita.regional@demo.test")
    assert (await decide(head, cid, aid)).status_code == 409


async def test_breach_when_next_role_is_already_a_required_tier(fake):
    """>5L spend needs regional head AND finance director; escalating one into the other would make one person fill both tiers."""
    req, cid, aid = await new_case(fake)
    a = (await pending(cid))[0]
    async with db.pool.connection() as c:
        await c.execute("INSERT INTO approvals(case_id,recommendation_id,round,tier,required_role,amount,deadline_at,created_at) "
                        "VALUES (%s,%s,%s,2,'finance_director',%s,%s,%s)", (cid, a["recommendation_id"], a["round"], a["amount"], a["deadline_at"], a["created_at"]))
    clock.set_offset(int((a["deadline_at"] - a["created_at"]).total_seconds()) + 60)
    res = await approvals.sweep_deadlines(cid)
    assert res["escalated"] == 0 and res["breached"] >= 1 and await status(cid) == "ESCALATED"
    assert not [r for r in await all_approvals(cid) if r["status"] == "escalated"]


async def test_lazy_sweep_on_read_catches_up_without_the_tick(fake):
    req, cid, aid = await new_case(fake)
    a = (await pending(cid))[0]
    clock.set_offset(int((a["deadline_at"] - a["created_at"]).total_seconds()) + 60)
    d = (await req.get(f"/api/cases/{cid}")).json()                                           # no tick ran; the read itself escalates
    assert [x["required_role"] for x in d["approvals"] if x["status"] == "pending"] == ["finance_director"]


# ───────────── admin clock ─────────────
async def test_time_travel_is_admin_only_and_audit_keeps_real_time(fake):
    req = await client_for("priya.requester@demo.test")
    assert (await req.post("/api/admin/clock", json={"offset_seconds": 60})).status_code == 403
    adm = await client_for("admin@demo.test")
    assert (await adm.post("/api/admin/clock", json={"offset_seconds": -5})).status_code == 422
    assert (await adm.post("/api/admin/clock", json={"offset_seconds": 3600})).json()["offset_seconds"] == 3600
    _, cid, aid = await new_case(fake)
    ev = (await audit_rows(cid, "case_created"))[0]
    assert ev["clock_offset_active"] is True and ev["sim_ts"] is not None
    assert 3500 < (ev["sim_ts"] - ev["ts"]).total_seconds() < 3700                            # real ts untouched, sim_ts shifted
