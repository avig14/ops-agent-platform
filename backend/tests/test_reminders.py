"""A2/A7: a case waiting for the requester is chased at 24 h and 48 h and handed to a manager at 72 h. Silence never becomes an answer."""
from app import clock, db, outbox as outbox_mod, reminders, sender
from tests.test_approvals import audit_rows, status  # noqa: F401
from tests.test_approvals import _reset_clock  # noqa: F401  (autouse fixture)
from tests.test_cases_api import _clean, client_for, fake, mail, submit  # noqa: F401
from tests.test_changes import needs_info_case, outbox, post_msg
from tests.test_extract import fv, issue, out

H = 3600


async def sql(q, *p):
    async with db.pool.connection() as c:
        r = await c.execute(q, p)
        return await r.fetchall() if r.description else None


async def sweep(cid, hours):
    clock.set_offset(int(hours * H))
    return await reminders.sweep(cid)


async def test_nothing_happens_before_24_hours(fake):
    _, cid = await needs_info_case(fake)
    assert await sweep(cid, 23.9) == {"reminded": 0, "escalated": 0, "errors": 0}
    assert not await outbox(cid, "reminder") and not await outbox(cid, "needs_info_escalation")


async def test_reminders_at_24_and_48_hours_then_a_manager_at_72(fake):
    _, cid = await needs_info_case(fake)
    assert await sweep(cid, 25) == {"reminded": 1, "escalated": 0, "errors": 0}
    (r1,) = await outbox(cid, "reminder")
    assert r1["recipient"] == "priya.requester@demo.test" and r1["audience"] == "internal" and "Reminder 1" in r1["body"]
    assert "which store or site" in r1["body"].lower()                                             # it repeats the open question
    assert await sweep(cid, 49) == {"reminded": 1, "escalated": 0, "errors": 0}
    assert len(await outbox(cid, "reminder")) == 2
    await sql("UPDATE cases SET needs_human=false WHERE id=%s", cid)
    assert await sweep(cid, 73) == {"reminded": 0, "escalated": 1, "errors": 0}
    (note,) = await outbox(cid, "needs_info_escalation")
    assert note["recipient"] == "rahul.manager@demo.test" and "72 hours" in note["body"]
    assert (await sql("SELECT needs_human, status FROM cases WHERE id=%s", cid))[0] == {"needs_human": True, "status": "NEEDS_INFO"}   # flagged, never moved on
    (a,) = await audit_rows(cid, "needs_info_escalated")
    assert a["reason_code"] == "WORKFLOW_DEADLINE_MISSED" and a["actor_type"] == "system"
    assert [x["refs"]["reminder"] for x in await audit_rows(cid, "reminder_sent")] == [1, 2]


async def test_repeated_sweeps_never_send_the_same_reminder_twice(fake):
    _, cid = await needs_info_case(fake)
    assert (await sweep(cid, 73))["reminded"] == 2                                       # a cold start after a long gap catches up once
    for _ in range(3):
        assert await sweep(cid, 73) == {"reminded": 0, "escalated": 0, "errors": 0}
    assert len(await outbox(cid, "reminder")) == 2 and len(await outbox(cid, "needs_info_escalation")) == 1


async def test_reminders_are_delivered_to_the_requesters_mailbox(fake):
    _, cid = await needs_info_case(fake)
    await sweep(cid, 49)
    await sender.run_once(cid)
    got = await sql("SELECT recipient, kind FROM mailbox WHERE case_id=%s AND kind='reminder'", cid)
    assert len(got) == 2 and {g["recipient"] for g in got} == {"priya.requester@demo.test"}


async def test_an_answered_case_gets_no_more_reminders(fake):
    req, cid = await needs_info_case(fake)
    await sweep(cid, 25)
    fake.intake = out(issue(location=fv("Pune")))
    assert (await post_msg(req, cid, "reply", "The store is Pune.")).json()["changed"] is True
    assert await status(cid) == "AWAITING_APPROVAL"
    assert await sweep(cid, 100) == {"reminded": 0, "escalated": 0, "errors": 0}
    assert len(await outbox(cid, "reminder")) == 1 and not await outbox(cid, "needs_info_escalation")


async def test_a_fresh_clarification_starts_a_fresh_clock(fake):
    _, cid = await needs_info_case(fake)
    await sweep(cid, 49)
    async with db.pool.connection() as c:                                                 # the system asks again, 48 h from "now"
        await outbox_mod.queue(c, cid, "clarification", "priya.requester@demo.test", "Still need the store", "Which store?", "clar-again-" + cid)
        await c.execute("UPDATE outbox SET created_at = now() + interval '48 hours' WHERE message_id=%s", ("clar-again-" + cid,))
    assert await sweep(cid, 49) == {"reminded": 0, "escalated": 0, "errors": 0}           # only one hour old in business time
    assert await sweep(cid, 73) == {"reminded": 1, "escalated": 0, "errors": 0}           # 25 h old: reminder 1 of the new question
    keys = [r["message_id"] for r in await outbox(cid, "reminder")]
    assert len(keys) == 3 and len(set(keys)) == 3                                         # the new question has its own reminder keys


async def test_cases_that_are_not_waiting_are_ignored_and_one_failure_does_not_stop_the_sweep(fake, monkeypatch):
    _, cid = await needs_info_case(fake)
    other_req = await client_for("karthik.requester@demo.test")
    ocid, d = await submit(other_req)
    assert d["case"]["status"] != "NEEDS_INFO"                                            # (it is a duplicate of the waiting case)
    clock.set_offset(100 * H)
    assert (await reminders.sweep(ocid)) == {"reminded": 0, "escalated": 0, "errors": 0}
    real = reminders._one

    async def boom(r, age, res):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(reminders, "_one", boom)
    res = await reminders.sweep(cid)
    assert res["errors"] == 1 and res["reminded"] == 0
    (a,) = await audit_rows(cid, "reminder_sweep")
    assert a["outcome"] == "failure" and a["severity"] == "error"
    monkeypatch.setattr(reminders, "_one", real)
    assert (await reminders.sweep(cid))["reminded"] == 2                                  # the next sweep simply does the work


async def test_the_admin_can_run_the_sweep_and_nobody_else(fake):
    _, cid = await needs_info_case(fake)
    adm, mgr = await client_for("admin@demo.test"), await client_for("rahul.manager@demo.test")
    assert (await mgr.post("/api/admin/reminders")).status_code == 403
    clock.set_offset(25 * H)
    r = await adm.post("/api/admin/reminders")
    assert r.status_code == 200 and r.json()["reminded"] >= 1 and set(r.json()) == {"reminded", "escalated", "errors"}
    assert await outbox(cid, "reminder")
