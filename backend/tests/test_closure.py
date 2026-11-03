"""Step 4: execution tracking and closure. Who may act, what the checklist counts, what a refused close leaves behind, reopen."""
import asyncio

from app import db, reporting, sender
from app.codes import Reason
from tests.test_approvals import _reset_clock  # noqa: F401  (autouse fixture)
from tests.test_approvals import audit_rows, new_case, status  # noqa: F401
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_changes import outbox, post_msg
from tests.test_comms import _no_faults, approved  # noqa: F401  (autouse fixture)


async def ready(fake):
    """An approved sample case whose messages are all delivered. Returns (requester, head, manager, finance, case id)."""
    req, head, cid = await approved(fake)
    mgr = await client_for("rahul.manager@demo.test")
    fin = await client_for("vikram.finance@demo.test")
    (draft,) = await outbox(cid, "supplier_request")
    assert (await mgr.post(f"/api/outbox/{draft['id']}/release")).status_code == 200
    await sender.run_once(cid)
    return req, head, mgr, fin, cid


async def tick_all(mgr, fin, cid):
    assert (await mgr.post(f"/api/cases/{cid}/closure/items/resolution_confirmed", json={"note": "goods received"})).status_code == 200
    assert (await fin.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 200


def by_key(state):
    return {i["key"]: i for i in state["items"]}


async def closure(c, cid):
    r = await c.get(f"/api/cases/{cid}/closure")
    assert r.status_code == 200, r.text
    return r.json()


# ───────────── who may act ─────────────
async def test_requester_and_admin_cannot_move_or_close_a_case_and_every_refusal_is_audited(fake):
    req, head, mgr, fin, cid = await ready(fake)
    adm = await client_for("admin@demo.test")
    for who, name in ((req, "requester"), (adm, "admin")):
        for step in ("start", "monitor", "close"):
            r = await who.post(f"/api/cases/{cid}/closure/{step}")
            assert r.status_code == 403, (name, step, r.text)
        assert (await who.post(f"/api/cases/{cid}/closure/items/resolution_confirmed")).status_code == 403
        assert (await who.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "not fixed"})).status_code == 403
        assert (await who.post(f"/api/cases/{cid}/closure/summary")).status_code == 403
    assert await status(cid) == "APPROVED"
    rows = [r for a in ("closure_start", "closure_monitor", "closure_close", "closure_confirm_item", "closure_reopen", "closure_summary")
            for r in await audit_rows(cid, a)]
    assert len(rows) == 12 and {r["reason_code"] for r in rows} == {Reason.POLICY_UNAUTHORIZED.value} and {r["outcome"] for r in rows} == {"blocked"}
    assert {r["refs"]["role"] for r in rows} == {"requester", "admin"}
    assert not await db_items(cid)


async def db_items(cid):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM closure_items WHERE case_id=%s", (cid,))).fetchall()


async def test_a_manager_of_another_region_cannot_even_see_the_case(fake):
    _, _, _, _, cid = await ready(fake)
    south = await client_for("sneha.manager@demo.test")
    for path in ("closure", "summary"):
        assert (await south.get(f"/api/cases/{cid}/{path}")).status_code == 404
    assert (await south.post(f"/api/cases/{cid}/closure/start")).status_code == 404
    assert await status(cid) == "APPROVED"
    assert any(r["reason_code"] == Reason.POLICY_UNAUTHORIZED.value for r in await audit_rows(cid, "access_denied"))


async def test_the_requester_can_read_the_checklist_of_their_own_case(fake):
    req, _, _, _, cid = await ready(fake)
    s = await closure(req, cid)
    assert s["status"] == "APPROVED" and s["can_close"] is False and s["epoch"] == 0
    other = await client_for("karthik.requester@demo.test")
    assert (await other.get(f"/api/cases/{cid}/closure")).status_code == 404


# ───────────── progress ─────────────
async def test_start_then_monitor_and_every_wrong_order_is_refused(fake):
    _, head, mgr, _, cid = await ready(fake)
    assert (await mgr.post(f"/api/cases/{cid}/closure/monitor")).status_code == 409          # not started yet
    assert (await mgr.post(f"/api/cases/{cid}/closure/start")).json() == {"status": "EXECUTING"}
    assert (await head.post(f"/api/cases/{cid}/closure/start")).status_code == 409           # already started
    assert (await mgr.post(f"/api/cases/{cid}/closure/monitor")).json() == {"status": "MONITORING"}
    assert (await mgr.post(f"/api/cases/{cid}/closure/monitor")).status_code == 409
    assert await status(cid) == "MONITORING"
    steps = [r["summary"].split(":")[0] for r in await audit_rows(cid, "status_change")]
    assert steps[-2:] == ["APPROVED -> EXECUTING", "EXECUTING -> MONITORING"]
    refused = await audit_rows(cid, "closure_start")
    assert refused and refused[-1]["outcome"] == "blocked" and refused[-1]["reason_code"] == Reason.WORKFLOW_CONCURRENCY_CONFLICT.value


async def test_a_case_that_is_not_approved_cannot_be_started(fake):
    req, cid, _ = await new_case(fake)
    mgr = await client_for("rahul.manager@demo.test")
    for step in ("start", "monitor", "close"):
        assert (await mgr.post(f"/api/cases/{cid}/closure/{step}")).status_code == 409
    assert (await mgr.post(f"/api/cases/{cid}/closure/items/resolution_confirmed")).status_code == 409
    assert await status(cid) == "AWAITING_APPROVAL"
    s = await closure(mgr, cid)
    assert s["can_close"] is False and by_key(s)["plan_approved"]["done"] is False


async def test_a_cancelled_case_cannot_be_carried_on(fake):
    _, head, mgr, _, cid = await ready(fake)
    assert (await post_msg(head, cid, "cancel", "Confirmed: cancel.")).json()["outcome"] == "cancelled"
    r = await mgr.post(f"/api/cases/{cid}/closure/start")
    assert r.status_code == 409 and "CANCELLED" in r.json()["error"]
    assert (await audit_rows(cid, "closure_start"))[-1]["reason_code"] == Reason.WORKFLOW_CANCELLED.value


# ───────────── the checklist ─────────────
async def test_the_checklist_counts_only_what_really_happened(fake):
    _, head, cid = await approved(fake)
    mgr = await client_for("rahul.manager@demo.test")
    s = await closure(mgr, cid)
    k = by_key(s)
    assert k["plan_approved"]["done"] and not k["stakeholders_informed"]["done"] and not k["supplier_contacted"]["done"]
    assert not k["resolution_confirmed"]["done"] and not k["spend_recorded"]["done"] and s["can_close"] is False
    await sender.run_once(cid)                                                               # internal updates go out, supplier draft waits for review
    k = by_key(await closure(mgr, cid))
    assert k["stakeholders_informed"]["done"] and not k["supplier_contacted"]["done"]
    (draft,) = await outbox(cid, "supplier_request")
    await mgr.post(f"/api/outbox/{draft['id']}/release")
    await sender.run_once(cid)
    assert by_key(await closure(mgr, cid))["supplier_contacted"]["done"]


async def test_a_message_that_failed_for_good_blocks_closure_until_it_is_handled(fake):
    _, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    assert (await closure(mgr, cid))["can_close"] is True
    # a new stakeholder message that dies makes the checklist incomplete again
    async with db.pool.connection() as c:
        row = (await outbox(cid, "stakeholder_update"))[0]
        await c.execute("INSERT INTO outbox(case_id,kind,recipient,subject,body,message_id,status,audience,source) "
                        "VALUES (%s,'stakeholder_update','x@demo.test','s','b',%s,'dead','internal','template')",
                        (cid, row["message_id"].rsplit("-", 1)[0] + "-deadone"))
    s = await closure(mgr, cid)
    assert by_key(s)["stakeholders_informed"]["done"] is False and s["can_close"] is False
    r = await head.post(f"/api/cases/{cid}/closure/close")
    assert r.status_code == 409 and "Stakeholders informed" in r.json()["error"] and await status(cid) == "APPROVED"


async def test_an_incomplete_close_changes_nothing_and_says_what_is_missing(fake):
    _, head, mgr, _, cid = await ready(fake)
    r = await head.post(f"/api/cases/{cid}/closure/close")
    assert r.status_code == 409
    for missing in ("Resolution confirmed", "Spend recorded"):
        assert missing in r.json()["error"]
    assert await status(cid) == "APPROVED"
    assert not await outbox(cid, "closure_notice")
    row = (await audit_rows(cid, "closure_close"))[-1]
    assert row["reason_code"] == Reason.WORKFLOW_CLOSURE_INCOMPLETE.value and row["outcome"] == "blocked"
    async with db.pool.connection() as c:
        assert (await (await c.execute("SELECT closed_at FROM cases WHERE id=%s", (cid,))).fetchone())["closed_at"] is None


async def test_confirming_an_item_twice_is_refused_once_and_the_note_stays_out_of_the_log(fake):
    _, _, mgr, fin, cid = await ready(fake)
    secret = "pallet 7731 checked by dock team"
    r1 = await mgr.post(f"/api/cases/{cid}/closure/items/resolution_confirmed", json={"note": secret})
    assert r1.status_code == 200 and r1.json() == {"key": "resolution_confirmed", "done": True}
    r2 = await mgr.post(f"/api/cases/{cid}/closure/items/resolution_confirmed", json={"note": "again"})
    assert r2.status_code == 409
    (item,) = await db_items(cid)
    assert item["note"] == secret and item["done_by"] and item["epoch"] == 0
    a = await audit_rows(cid, "closure_item")
    refused = await audit_rows(cid, "closure_confirm_item")
    assert len(a) == 1 and a[0]["outcome"] == "success" and a[0]["refs"]["note_len"] == len(secret)
    assert len(refused) == 1 and refused[0]["reason_code"] == Reason.WORKFLOW_IDEMPOTENT_REPLAY.value and refused[0]["outcome"] == "blocked"
    assert all(secret not in str(x["summary"]) + str(x["refs"]) for x in a + refused)
    s = by_key(await closure(mgr, cid))["resolution_confirmed"]
    assert s["done"] and s["note"] == secret


async def test_only_the_finance_director_records_spend_and_a_free_plan_has_nothing_to_record(fake):
    _, head, mgr, fin, cid = await ready(fake)
    assert (await mgr.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 403
    assert (await head.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 403
    assert (await fin.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 200
    assert (await mgr.post(f"/api/cases/{cid}/closure/items/made_up")).status_code == 422
    # free plan: no spend item at all
    async with db.pool.connection() as c:
        await c.execute("UPDATE recommendations SET total_cost=0 WHERE case_id=%s AND status='approved'", (cid,))
    s = await closure(mgr, cid)
    assert "spend_recorded" not in by_key(s)
    assert (await fin.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 409


async def test_a_ticked_item_shows_the_persons_name_not_just_the_id(fake):
    _, _, mgr, fin, cid = await ready(fake)
    assert (await fin.post(f"/api/cases/{cid}/closure/items/spend_recorded")).status_code == 200
    k = by_key(await closure(mgr, cid))
    assert k["spend_recorded"]["by"] == "u-fin" and k["spend_recorded"]["by_name"] == "Vikram Shah (Finance Director)"
    assert k["resolution_confirmed"]["by"] is None and k["resolution_confirmed"]["by_name"] is None   # not ticked yet
    assert k["plan_approved"]["by_name"] is None                                         # derived from the audit trail


# ───────────── closing ─────────────
async def test_a_complete_checklist_closes_the_case_writes_the_summary_and_tells_the_requester(fake):
    req, head, mgr, fin, cid = await ready(fake)
    await mgr.post(f"/api/cases/{cid}/closure/start")
    await mgr.post(f"/api/cases/{cid}/closure/monitor")
    await tick_all(mgr, fin, cid)
    assert (await closure(mgr, cid))["can_close"] is True
    r = await head.post(f"/api/cases/{cid}/closure/close")
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    summ = r.json()["summary"]
    assert summ["kind"] == "closing" and summ["source"] == "llm" and summ["content"]["headline"] == "Case closed"
    async with db.pool.connection() as c:
        case = await (await c.execute("SELECT status, closed_at, needs_human FROM cases WHERE id=%s", (cid,))).fetchone()
        n = (await (await c.execute("SELECT count(*) n FROM summaries WHERE case_id=%s AND kind='closing'", (cid,))).fetchone())["n"]
    assert case["status"] == "CLOSED" and case["closed_at"] is not None and case["needs_human"] is False and n == 1
    notes = await outbox(cid, "closure_notice")
    assert len(notes) == 1
    await sender.run_once(cid)
    assert (await outbox(cid, "closure_notice"))[0]["status"] == "sent"                      # a closed case still delivers its closing notice
    s = await closure(req, cid)
    assert s["status"] == "CLOSED" and s["latest_summary"]["source"] == "llm" and s["can_close"] is False
    # the closure screen needs the words, not only the row id (a missing `content` crashed the page)
    assert s["latest_summary"]["content"]["headline"] == "Case closed" and s["latest_summary"]["kind"] == "closing"
    assert s["latest_summary"]["metrics_hash"] and s["latest_summary"]["id"] == summ["id"]
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 409           # a second close is refused
    ev = await audit_rows(cid, "summary_generated")
    assert len(ev) == 1 and ev[0]["actor_id"] == "reporting" and ev[0]["refs"]["source"] == "llm"


async def test_closing_works_straight_from_approved_without_the_progress_clicks(fake):
    _, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 200


async def test_two_people_closing_at_the_same_moment_exactly_one_wins(fake):
    _, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    rs = await asyncio.gather(head.post(f"/api/cases/{cid}/closure/close"), mgr.post(f"/api/cases/{cid}/closure/close"),
                              fin.post(f"/api/cases/{cid}/closure/close"))
    assert sorted(r.status_code for r in rs) == [200, 409, 409]
    assert len(await outbox(cid, "closure_notice")) == 1
    assert len([r for r in await audit_rows(cid, "status_change") if r["summary"].startswith("APPROVED -> CLOSED")]) == 1


async def test_a_model_outage_at_close_time_does_not_stop_the_close_and_the_template_is_used(fake):
    _, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    fake.down = True
    r = await head.post(f"/api/cases/{cid}/closure/close")
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    summ = r.json()["summary"]
    assert summ["source"] == "template" and cid in summ["content"]["headline"] and "228000" in summ["content"]["what_happened"]
    fake.down = False
    for br in reporting.gateway.breakers.values():                                             # the model is back: close the circuit
        br.ok()
    r2 = await head.post(f"/api/cases/{cid}/closure/summary")                                  # regenerate once the model is back
    assert r2.status_code == 200 and r2.json()["source"] == "llm" and r2.json()["id"] != summ["id"]
    latest = (await head.get(f"/api/cases/{cid}/summary")).json()["summary"]
    assert latest["id"] == r2.json()["id"]


async def test_the_summary_can_only_be_rewritten_by_an_approver_for_a_closed_case(fake):
    req, head, mgr, fin, cid = await ready(fake)
    assert (await head.post(f"/api/cases/{cid}/closure/summary")).status_code == 409         # not closed yet
    assert (await head.get(f"/api/cases/{cid}/summary")).json() == {"summary": None}
    await tick_all(mgr, fin, cid)
    await head.post(f"/api/cases/{cid}/closure/close")
    assert (await req.post(f"/api/cases/{cid}/closure/summary")).status_code == 403
    assert (await mgr.post(f"/api/cases/{cid}/closure/summary")).status_code == 200


# ───────────── reopen ─────────────
async def test_reopening_needs_a_reason_and_starts_a_fresh_manual_checklist(fake):
    req, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    await head.post(f"/api/cases/{cid}/closure/close")
    assert (await mgr.post(f"/api/cases/{cid}/closure/reopen", json={"reason": " "})).status_code == 422
    assert (await mgr.post(f"/api/cases/{cid}/closure/reopen")).status_code == 422
    assert await status(cid) == "CLOSED"
    r = await mgr.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "second pallet is damaged"})
    assert r.status_code == 200 and r.json() == {"status": "MONITORING", "reopen_count": 1}
    s = await closure(mgr, cid)
    k = by_key(s)
    assert s["epoch"] == 1 and s["status"] == "MONITORING" and s["can_close"] is False
    assert k["plan_approved"]["done"] and k["stakeholders_informed"]["done"]                  # what is derived stays true
    assert not k["resolution_confirmed"]["done"] and not k["spend_recorded"]["done"]           # what a person confirms starts again
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 409
    notes = await outbox(cid, "reopen_notice")
    assert {n["recipient"] for n in notes} >= {"priya.requester@demo.test"}
    assert len(await audit_rows(cid, "status_change")) and any("reopened" in r["summary"] for r in await audit_rows(cid, "status_change"))
    await tick_all(mgr, fin, cid)
    assert (await head.post(f"/api/cases/{cid}/closure/close")).status_code == 200
    assert len(await outbox(cid, "closure_notice")) == 2                                      # one per closing, different keys
    async with db.pool.connection() as c:
        assert [r["epoch"] for r in await (await c.execute("SELECT epoch FROM closure_items WHERE case_id=%s ORDER BY id", (cid,))).fetchall()] == [0, 0, 1, 1]


async def test_only_a_closed_case_can_be_reopened_and_only_by_an_approver(fake):
    req, head, mgr, fin, cid = await ready(fake)
    assert (await mgr.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "not fixed"})).status_code == 409
    await tick_all(mgr, fin, cid)
    await head.post(f"/api/cases/{cid}/closure/close")
    assert (await req.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "not fixed"})).status_code == 403
    assert await status(cid) == "CLOSED"


async def test_a_closed_case_without_an_approved_plan_cannot_be_reopened(fake):
    _, head, mgr, fin, cid = await ready(fake)
    await tick_all(mgr, fin, cid)
    await head.post(f"/api/cases/{cid}/closure/close")
    async with db.pool.connection() as c:
        await c.execute("UPDATE recommendations SET status='superseded' WHERE case_id=%s", (cid,))
    r = await mgr.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "not fixed"})
    assert r.status_code == 409 and await status(cid) == "CLOSED"


# ───────────── the trail ─────────────
async def test_messages_from_an_older_plan_do_not_count_for_the_current_plan(fake):
    _, head, mgr, fin, cid = await ready(fake)
    async with db.pool.connection() as c:
        rec = await (await c.execute("SELECT id FROM recommendations WHERE case_id=%s AND status='approved'", (cid,))).fetchone()
        await c.execute("UPDATE outbox SET message_id=replace(message_id, %s, 'comm-' || %s || '-0-') WHERE case_id=%s AND kind IN ('stakeholder_update','supplier_request')",
                        (f"comm-{cid}-{rec['id']}-", cid, cid))
    s = by_key(await closure(mgr, cid))
    assert not s["stakeholders_informed"]["done"] and "supplier_contacted" not in s


async def test_the_whole_closure_flow_leaves_a_valid_contiguous_audit_chain(fake):
    req, head, mgr, fin, cid = await ready(fake)
    await mgr.post(f"/api/cases/{cid}/closure/start")
    await mgr.post(f"/api/cases/{cid}/closure/monitor")
    await head.post(f"/api/cases/{cid}/closure/close")                                         # refused: incomplete
    await tick_all(mgr, fin, cid)
    await head.post(f"/api/cases/{cid}/closure/close")
    await mgr.post(f"/api/cases/{cid}/closure/reopen", json={"reason": "problem is back"})
    a = (await req.get(f"/api/cases/{cid}/audit")).json()
    assert a["chain"]["ok"] is True and [e["seq"] for e in a["events"]] == list(range(1, len(a["events"]) + 1))
    assert {"closure_item", "closure_close", "summary_generated", "status_change"} <= {e["action"] for e in a["events"]}
    assert all("pallet" not in str(e["summary"]) for e in a["events"])
