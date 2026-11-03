"""B12 communication failure: the outbox sender (claim, lease, fencing, retry, dead letter, receipt lookup) and the human actions on messages."""
import asyncio
import uuid

import psycopg
import pytest
import pytest_asyncio

from app import db, outbox, sender
from tests.test_approvals import audit_rows
from tests.test_cases_api import _clean, client_for, fake, submit  # noqa: F401


@pytest_asyncio.fixture(autouse=True)
async def _faults():
    sender.faults.set(None)
    yield
    sender.faults.set(None)


async def case_with_empty_outbox(fake, status=None):
    """A real case whose own queued notices are cleared, so a test sees only the rows it adds."""
    req = await client_for("priya.requester@demo.test")
    cid, _ = await submit(req)
    async with db.pool.connection() as c:
        await c.execute("UPDATE outbox SET status='cancelled' WHERE case_id=%s", (cid,))
        if status:
            await c.execute("UPDATE cases SET status=%s WHERE id=%s", (status, cid))
    return req, cid


async def put(cid, to="rahul.manager@demo.test", kind="stakeholder_update", **kw):
    async with db.pool.connection() as c:
        await outbox.queue(c, cid, kind, to, "Subject " + kind, "Body text", f"t-{uuid.uuid4().hex}", **kw)
        return (await (await c.execute("SELECT id FROM outbox WHERE case_id=%s ORDER BY id DESC LIMIT 1", (cid,))).fetchone())["id"]


async def row(mid):
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT * FROM outbox WHERE id=%s", (mid,))).fetchone()


async def sql(q, *p):
    async with db.pool.connection() as c:
        r = await c.execute(q, p)
        return await r.fetchall() if r.description else None


async def due_now(mid):
    await sql("UPDATE outbox SET next_attempt_at=now() - interval '1 second' WHERE id=%s", mid)


async def received(message_id):
    return await sql("SELECT * FROM mailbox WHERE message_id=%s", message_id)


# ───────────── pure ─────────────
def test_backoff_is_full_jitter_capped_and_never_below_the_floor():
    assert sender.backoff_s(1, rng=lambda: 1.0) == 5.0
    assert sender.backoff_s(2, rng=lambda: 1.0) == 10.0
    assert sender.backoff_s(3, rng=lambda: 0.5) == 10.0
    assert sender.backoff_s(30, rng=lambda: 1.0) == sender.CAP_S                  # capped
    assert sender.backoff_s(5, rng=lambda: 0.0) == sender.FLOOR_S                 # never zero: no hot loop
    assert sender.backoff_s(0, rng=lambda: 1.0) == 5.0                            # a bad attempt number does not crash or go negative


# ───────────── delivery ─────────────
async def test_a_message_is_delivered_once_and_recorded(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    res = await sender.run_once(cid)
    assert res["claimed"] == 1 and res["sent"] == 1
    r = await row(mid)
    assert r["status"] == "sent" and r["attempts"] == 1 and r["sent_at"] and r["lease_until"] is None
    (m,) = await received(r["message_id"])
    assert m["recipient"] == "rahul.manager@demo.test" and m["channel"] == "sandbox" and m["case_id"] == cid
    (call,) = await sql("SELECT * FROM external_calls WHERE case_id=%s AND kind='smtp'", cid)
    assert call["status"] == "ok" and call["attempt"] == 1                       # intent row written before, outcome after
    assert [a["outcome"] for a in await audit_rows(cid, "message_sent")] == ["success"]
    assert (await sender.run_once(cid))["claimed"] == 0                           # nothing left; a sent message is never picked up again
    assert len(await received(r["message_id"])) == 1


async def test_the_receiver_is_idempotent(fake):
    _, cid = await case_with_empty_outbox(fake)
    r = await row(await put(cid))
    assert await sender.deliver(r) is True and await sender.deliver(r) is False
    assert len(await received(r["message_id"])) == 1


async def test_transient_failure_retries_with_backoff_then_succeeds(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    sender.faults.set("fail", 2)
    assert (await sender.run_once(cid))["retry"] == 1
    r = await row(mid)
    assert r["status"] == "failed" and r["attempts"] == 1 and r["reason_code"] == "COMM_SEND_FAILED" and "503" in r["last_error"]
    assert (await sql("SELECT next_attempt_at > now() AS later FROM outbox WHERE id=%s", mid))[0]["later"]     # backoff: not due yet
    assert (await sender.run_once(cid))["claimed"] == 0                                                         # so a tick does nothing
    await due_now(mid)
    assert (await sender.run_once(cid))["retry"] == 1 and (await row(mid))["attempts"] == 2
    await due_now(mid)
    assert (await sender.run_once(cid))["sent"] == 1
    r = await row(mid)
    assert r["status"] == "sent" and r["attempts"] == 3 and len(await received(r["message_id"])) == 1
    fails = [a for a in await audit_rows(cid, "message_send") if a["outcome"] == "failure"]
    assert len(fails) == 2 and fails[0]["error"]["class"] == "COMM_SEND_FAILED" and fails[0]["refs"]["retry_in_s"] > 0
    assert [c["status"] for c in await sql("SELECT status FROM external_calls WHERE case_id=%s AND kind='smtp' ORDER BY id", cid)] == ["error", "error", "ok"]


async def test_a_bounce_is_permanent_and_the_requester_is_told_in_app(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid, to="ghost@bounce.test")
    assert (await sender.run_once(cid))["dead"] == 1
    r = await row(mid)
    assert r["status"] == "dead" and r["attempts"] == 1 and r["reason_code"] == "COMM_BOUNCED"        # no pointless retries
    assert await received(r["message_id"]) == []
    (n,) = await sql("SELECT * FROM mailbox WHERE case_id=%s AND kind='delivery_failed'", cid)
    assert n["recipient"] == "priya.requester@demo.test" and n["channel"] == "in_app" and "Communications tab" in n["body"]
    (a,) = await audit_rows(cid, "message_dead")
    assert a["reason_code"] == "COMM_BOUNCED" and a["outcome"] == "failure" and a["severity"] == "error"
    assert (await sender.run_once(cid))["claimed"] == 0                                                 # the notice itself never loops


async def test_a_message_that_keeps_failing_goes_dead_when_its_budget_is_spent(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    await sql("UPDATE outbox SET max_attempts=2 WHERE id=%s", mid)
    sender.faults.set("fail")
    assert (await sender.run_once(cid))["retry"] == 1
    await due_now(mid)
    assert (await sender.run_once(cid))["dead"] == 1
    r = await row(mid)
    assert r["status"] == "dead" and r["attempts"] == 2 and r["reason_code"] == "COMM_DEAD" and "gave up" in r["last_error"]
    assert await sql("SELECT 1 FROM mailbox WHERE case_id=%s AND kind='delivery_failed'", cid)


async def test_a_lost_acknowledgement_is_not_a_failure_and_never_sends_twice(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    sender.faults.set("timeout", 1)
    assert (await sender.run_once(cid))["unknown"] == 1
    r = await row(mid)
    assert r["status"] == "delivery_unknown" and r["reason_code"] == "COMM_DELIVERY_UNKNOWN"
    assert len(await received(r["message_id"])) == 1                                  # it did arrive
    await due_now(mid)
    assert (await sender.run_once(cid))["sent"] == 1
    r = await row(mid)
    assert r["status"] == "sent" and len(await received(r["message_id"])) == 1        # confirmed, not re-sent
    assert len(await sql("SELECT 1 FROM external_calls WHERE case_id=%s AND kind='smtp'", cid)) == 1     # the provider was called once
    (a,) = await audit_rows(cid, "message_sent")
    assert a["refs"]["confirmed_by"] == "receipt"


async def test_a_message_that_never_arrived_is_resent_after_an_unknown_result(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    await sql("UPDATE outbox SET status='delivery_unknown', attempts=1, next_attempt_at=now() - interval '1 second' WHERE id=%s", mid)
    assert (await sender.run_once(cid))["sent"] == 1                                  # no receipt on file: it really sends
    assert len(await received((await row(mid))["message_id"])) == 1


# ───────────── workers, leases, fencing ─────────────
async def test_two_workers_never_send_the_same_message_twice(fake):
    _, cid = await case_with_empty_outbox(fake)
    ids = [await put(cid, to=f"u{i}@demo.test") for i in range(8)]
    a, b = await asyncio.gather(sender.run_once(cid, limit=5), sender.run_once(cid, limit=5))
    rest = await sender.run_once(cid)
    assert a["sent"] + b["sent"] + rest["sent"] == 8 and a["claimed"] + b["claimed"] + rest["claimed"] == 8
    for mid in ids:
        r = await row(mid)
        assert r["status"] == "sent" and r["attempts"] == 1
        assert len(await received(r["message_id"])) == 1


async def test_a_crashed_workers_message_is_recovered_after_its_lease_expires(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    (claimed,) = await sender.claim(cid)                       # a worker takes it ... and dies
    assert claimed["status"] == "sending" and claimed["attempts"] == 1
    assert (await sender.run_once(cid))["claimed"] == 0        # lease still valid: nobody else touches it
    await sql("UPDATE outbox SET lease_until=now() - interval '1 second' WHERE id=%s", mid)
    assert (await sender.run_once(cid))["sent"] == 1
    r = await row(mid)
    assert r["status"] == "sent" and r["attempts"] == 2 and len(await received(r["message_id"])) == 1
    (a,) = [x for x in await audit_rows(cid, "message_send") if x["outcome"] == "degraded"]
    assert a["reason_code"] == "INFRA_WORKER_LEASE_EXPIRED"


async def test_a_worker_that_lost_its_lease_cannot_overwrite_the_new_owners_result(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    (a,) = await sender.claim(cid)                                                            # worker A, attempt 1
    await sql("UPDATE outbox SET lease_until=now() - interval '1 second' WHERE id=%s", mid)
    (b,) = await sender.claim(cid)                                                            # worker B takes over, attempt 2
    assert (a["attempts"], b["attempts"]) == (1, 2)
    assert await sender._settle(a, "dead", code=sender.Reason.COMM_DEAD, err="stale") is False  # A is fenced out
    assert (await row(mid))["status"] == "sending"
    assert await sender._settle(b, "sent") is True
    assert (await row(mid))["status"] == "sent"


async def test_one_crash_does_not_stop_the_rest_of_the_batch(fake, monkeypatch):
    _, cid = await case_with_empty_outbox(fake)
    first, second = await put(cid, to="a@demo.test"), await put(cid, to="b@demo.test")
    real = sender.deliver

    async def flaky(r):
        if r["recipient"] == "a@demo.test":
            raise RuntimeError("provider library exploded")
        return await real(r)
    monkeypatch.setattr(sender, "deliver", flaky)
    res = await sender.run_once(cid)
    assert res["claimed"] == 2 and res["sent"] == 1 and res["retry"] == 1
    assert (await row(first))["status"] == "failed" and (await row(second))["status"] == "sent"      # unexpected errors are retried, not lost


# ───────────── external mail needs a human and a live plan ─────────────
async def test_the_database_refuses_external_mail_without_a_reviewer(fake):
    _, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    with pytest.raises(psycopg.errors.CheckViolation):
        await sql("UPDATE outbox SET status='pending' WHERE id=%s", mid)
    with pytest.raises(psycopg.errors.CheckViolation):
        await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="pending")
    assert (await row(mid))["status"] == "draft"
    assert (await sender.run_once(cid))["claimed"] == 0                                # a draft is never claimed


async def test_a_draft_is_released_by_a_manager_and_then_sent(fake):
    _, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/outbox/{mid}/release")
    assert r.status_code == 200 and r.json() == {"id": mid, "status": "pending", "edited": False}
    row_ = await row(mid)
    assert row_["reviewed_by"] and row_["reviewed_at"] and row_["status"] == "pending"
    assert (await sender.run_once(cid))["sent"] == 1
    assert [m["recipient"] for m in await received(row_["message_id"])] == ["orders@acme-display.test"]
    (a,) = await audit_rows(cid, "message_release")
    assert a["actor_type"] == "human" and a["outcome"] == "success" and a["diff"] is None
    assert (await mgr.post(f"/api/outbox/{mid}/release")).status_code == 409           # not repeatable


async def test_an_edit_is_recorded_without_leaking_the_text_into_the_audit_log(fake):
    _, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/outbox/{mid}/release", json={"subject": "Better subject", "body": "Secret wording by the reviewer"})
    assert r.status_code == 200 and r.json()["edited"] is True
    r_ = await row(mid)
    assert r_["edited"] and r_["subject"] == "Better subject" and r_["body"] == "Secret wording by the reviewer"
    (a,) = await audit_rows(cid, "message_release")
    assert a["diff"]["body"]["after_len"] == len("Secret wording by the reviewer") and a["diff"]["body"]["before_sha"] != a["diff"]["body"]["after_sha"]
    assert "Secret wording" not in str(a["diff"]) + str(a["summary"]) + str(a["refs"])


async def test_release_and_discard_need_the_right_role_and_every_refusal_is_audited(fake):
    req, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    adm = await client_for("admin@demo.test")
    for who in (req, adm):
        assert (await who.post(f"/api/outbox/{mid}/release")).status_code == 403
        assert (await who.post(f"/api/outbox/{mid}/discard")).status_code == 403
    south = await client_for("sneha.manager@demo.test")
    assert (await south.post(f"/api/outbox/{mid}/release")).status_code == 404         # other region: the case is invisible
    assert (await row(mid))["status"] == "draft"
    blocked = [a for a in await audit_rows(cid, "message_release") + await audit_rows(cid, "message_discard") if a["outcome"] == "blocked"]
    assert len(blocked) == 4 and {b["reason_code"] for b in blocked} == {"POLICY_UNAUTHORIZED"}
    assert (await adm.post("/api/outbox/999999/release")).status_code == 404


async def test_a_draft_can_be_discarded_and_never_leaves(fake):
    _, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/outbox/{mid}/discard", json={"reason": "wrong supplier"})
    assert r.status_code == 200 and (await row(mid))["status"] == "cancelled" and "wrong supplier" in (await row(mid))["last_error"]
    assert (await mgr.post(f"/api/outbox/{mid}/release")).status_code == 409
    assert (await mgr.post(f"/api/outbox/{mid}/discard")).status_code == 409
    assert (await sender.run_once(cid))["claimed"] == 0


async def test_a_draft_cannot_be_released_while_the_plan_is_not_live(fake):
    _, cid = await case_with_empty_outbox(fake)                                          # case is AWAITING_APPROVAL
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/outbox/{mid}/release")
    assert r.status_code == 409 and "can no longer be sent" in r.json()["error"]
    assert (await row(mid))["status"] == "draft"
    assert (await audit_rows(cid, "message_release"))[-1]["outcome"] == "blocked"
    for body in ({"subject": ""}, {"body": " "}):                                         # empty edits are refused too
        await sql("UPDATE cases SET status='APPROVED' WHERE id=%s", cid)
        assert (await mgr.post(f"/api/outbox/{mid}/release", json=body)).status_code == 422
    assert (await row(mid))["status"] == "draft"


async def test_released_external_mail_is_not_sent_if_the_plan_dies_before_it_leaves(fake):
    _, cid = await case_with_empty_outbox(fake, "APPROVED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    mgr = await client_for("rahul.manager@demo.test")
    assert (await mgr.post(f"/api/outbox/{mid}/release")).status_code == 200
    await sql("UPDATE cases SET status='CANCELLED' WHERE id=%s", cid)                    # cancelled between release and send
    assert (await sender.run_once(cid))["cancelled"] == 1
    r = await row(mid)
    assert r["status"] == "cancelled" and "CANCELLED" in r["last_error"] and await received(r["message_id"]) == []
    (a,) = await audit_rows(cid, "message_send")
    assert a["outcome"] == "blocked" and a["reason_code"] == "WORKFLOW_CANCELLED"


# ───────────── human retry ─────────────
async def test_a_manager_can_retry_a_dead_message_and_it_gets_a_fresh_budget(fake):
    _, cid = await case_with_empty_outbox(fake)
    mid = await put(cid)
    await sql("UPDATE outbox SET max_attempts=1 WHERE id=%s", mid)
    sender.faults.set("fail")
    assert (await sender.run_once(cid))["dead"] == 1
    req = await client_for("priya.requester@demo.test")
    assert (await req.post(f"/api/outbox/{mid}/retry")).status_code == 403              # the requester cannot
    mgr = await client_for("rahul.manager@demo.test")
    r = await mgr.post(f"/api/outbox/{mid}/retry")
    assert r.status_code == 200 and r.json() == {"id": mid, "status": "pending"}
    r_ = await row(mid)
    assert r_["status"] == "pending" and r_["attempts"] == 1 and r_["max_attempts"] == 6      # attempts is the fencing token: never reset
    sender.faults.set(None)
    assert (await sender.run_once(cid))["sent"] == 1
    assert (await mgr.post(f"/api/outbox/{mid}/retry")).status_code == 409                # a sent message cannot be retried
    out = await audit_rows(cid, "message_retry")
    assert [o["outcome"] for o in out] == ["blocked", "success", "blocked"] and out[1]["actor_type"] == "human"


async def test_retry_of_external_mail_is_refused_when_the_case_is_cancelled(fake):
    _, cid = await case_with_empty_outbox(fake, "CANCELLED")
    mid = await put(cid, to="orders@acme-display.test", kind="supplier_request", audience="external", status="draft")
    await sql("UPDATE outbox SET status='dead', reviewed_by=(SELECT id FROM users WHERE email='rahul.manager@demo.test') WHERE id=%s", mid)
    mgr = await client_for("rahul.manager@demo.test")
    assert (await mgr.post(f"/api/outbox/{mid}/retry")).status_code == 409
    assert (await row(mid))["status"] == "dead"


# ───────────── mailbox and message log visibility ─────────────
async def test_each_person_sees_only_their_own_mailbox(fake):
    _, cid = await case_with_empty_outbox(fake)
    await put(cid, to="priya.requester@demo.test")
    await put(cid, to="rahul.manager@demo.test")
    await sender.run_once(cid)
    priya, rahul = await client_for("priya.requester@demo.test"), await client_for("rahul.manager@demo.test")
    mine = (await priya.get("/api/mailbox")).json()
    assert mine["messages"] and {m["recipient"] for m in mine["messages"]} == {"priya.requester@demo.test"} and mine["unread"] >= 1
    assert (await priya.get("/api/mailbox?scope=all")).status_code == 403
    adm = await client_for("admin@demo.test")
    assert {"priya.requester@demo.test", "rahul.manager@demo.test"} <= {m["recipient"] for m in (await adm.get("/api/mailbox?scope=all")).json()["messages"]}
    mid = mine["messages"][0]["id"]
    assert (await rahul.post(f"/api/mailbox/{mid}/read")).status_code == 404                # not his message
    assert (await priya.post(f"/api/mailbox/{mid}/read")).json() == {"id": mid, "read": True}
    assert mid not in [m["id"] for m in (await priya.get("/api/mailbox?unread=true")).json()["messages"]]


async def test_the_message_log_shows_state_to_staff_and_only_own_mail_to_the_requester(fake):
    req, cid = await case_with_empty_outbox(fake)
    await put(cid, to="priya.requester@demo.test")
    bad = await put(cid, to="ghost@bounce.test")
    await sender.run_once(cid)
    mgr = await client_for("rahul.manager@demo.test")
    log = (await mgr.get(f"/api/cases/{cid}/communications")).json()
    by = {m["id"]: m for m in log["messages"] if m["status"] != "cancelled"}
    assert by[bad]["status"] == "dead" and by[bad]["reason_code"] == "COMM_BOUNCED" and by[bad]["attempts"] == 1
    assert any(m["status"] == "sent" and m["delivered_at"] for m in by.values())
    assert [n["kind"] for n in log["notices"]] == ["delivery_failed"]
    mine = (await req.get(f"/api/cases/{cid}/communications")).json()
    assert {m["recipient"] for m in mine["messages"]} == {"priya.requester@demo.test"}
    assert "last_error" not in mine["messages"][0] and "attempts" not in mine["messages"][0]
    south = await client_for("sneha.manager@demo.test")
    assert (await south.get(f"/api/cases/{cid}/communications")).status_code == 404


# ───────────── admin controls ─────────────
async def test_comms_chaos_and_outbox_controls_are_admin_only_and_validated(fake):
    adm, mgr = await client_for("admin@demo.test"), await client_for("rahul.manager@demo.test")
    assert (await mgr.post("/api/admin/comms-chaos", json={"mode": "fail"})).status_code == 403
    assert (await mgr.post("/api/admin/outbox/run")).status_code == 403
    assert (await adm.post("/api/admin/comms-chaos", json={"mode": "explode"})).status_code == 422
    assert (await adm.post("/api/admin/comms-chaos", json={"mode": "fail", "count": 0})).status_code == 422
    assert (await adm.post("/api/admin/comms-chaos", json={"mode": "fail", "count": True})).status_code == 422
    assert (await adm.post("/api/admin/comms-chaos", json={"mode": "fail", "count": 2})).json() == {"mode": "fail", "remaining": 2}
    assert (await adm.get("/api/admin/outbox/stats")).json()["fault"] == {"mode": "fail", "remaining": 2}
    assert (await adm.post("/api/admin/comms-chaos", json={"mode": None})).json() == {"mode": None, "remaining": None}
    assert set((await adm.post("/api/admin/outbox/run")).json()) == set(sender.OUTCOMES)
