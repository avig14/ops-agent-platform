"""Outbox sender: delivers queued messages to the sandbox mail provider.

Guarantees (each one has a test):
  * A row is claimed with `FOR UPDATE SKIP LOCKED` plus a lease, so several workers never send the same row twice at once
    and a crashed worker's rows are picked up again when its lease runs out.
  * `attempts` is the fencing token: a worker that lost its lease cannot overwrite the result of the worker that took over.
  * Sending is at-least-once, the receiver is idempotent (`mailbox.message_id` is UNIQUE), so retries, lost acknowledgements
    and crashed workers never show a reader the same message twice.
  * A timeout is not a failure: the message may have arrived. The row becomes `delivery_unknown`; the next attempt first
    looks the message up in the provider and marks it sent without sending it again.
  * Transient errors retry with capped exponential backoff and full jitter; a permanent error (bounce) or an empty attempt
    budget moves the row to `dead` and tells the requester in-app. A manager can retry a dead row.
  * An external message cannot be sent without a human reviewer (CHECK constraint) and not after its plan was cancelled.
  * Every attempt is written to `external_calls` before and after it runs, and every outcome to the case audit chain.
"""
from __future__ import annotations

import asyncio
import random
import time

from . import activity, audit, db, reminders
from .codes import Reason
from .config import settings

TICK_S = 5.0
LEASE_S = 60            # a worker must finish one message within this time
BATCH = 20
BASE_S = 5.0            # backoff: random(0, min(CAP_S, BASE_S * 2^(attempt-1))), floor 1 s
CAP_S = 300.0
FLOOR_S = 1.0
SENDABLE_CASE_STATES = ("APPROVED", "EXECUTING", "MONITORING", "CLOSED")     # external mail only while its plan is live
OUTCOMES = ("claimed", "sent", "retry", "unknown", "dead", "cancelled", "lost_lease")


def backoff_s(attempt: int, rng=random.random) -> float:
    """Full jitter (AWS Architecture Blog, 'Exponential Backoff And Jitter'): uniform in [0, min(cap, base * 2^(n-1))]."""
    ceiling = min(CAP_S, BASE_S * (2 ** max(attempt - 1, 0)))
    return max(FLOOR_S, rng() * ceiling)


# ───────────── fault injection (demo / chaos tests; only when chaos is enabled) ─────────────
class Faults:
    mode: str | None = None          # fail (provider 503) | bounce (permanent) | timeout (delivered, ack lost)
    remaining: int | None = None     # None = until switched off

    def set(self, mode: str | None, count: int | None = None) -> None:
        self.mode, self.remaining = mode, (count if mode else None)

    def take(self) -> str | None:
        if not settings.enable_chaos or not self.mode:
            return None
        m = self.mode
        if self.remaining is not None:
            self.remaining -= 1
            if self.remaining <= 0:
                self.mode = self.remaining = None
        return m


faults = Faults()


class SendError(Exception):
    def __init__(self, message: str, reason: Reason, *, permanent: bool = False, ack_lost: bool = False):
        super().__init__(message)
        self.reason, self.permanent, self.ack_lost = reason, permanent, ack_lost


# ───────────── the provider ─────────────
async def deliver(row: dict) -> bool:
    """Hand one message to the sandbox provider. True = stored now, False = it was already there (idempotent receive)."""
    mode = faults.take()
    if row["recipient"].lower().endswith("@bounce.test") or mode == "bounce":
        raise SendError("recipient mailbox rejected the message (550 no such user)", Reason.COMM_BOUNCED, permanent=True)
    if mode == "fail":
        raise SendError("provider unavailable (503)", Reason.COMM_SEND_FAILED)
    async with db.pool.connection() as c:
        r = await c.execute(
            "INSERT INTO mailbox(message_id,outbox_id,case_id,kind,recipient,subject,body) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (row["message_id"], row["id"], row["case_id"], row["kind"], row["recipient"], row["subject"], row["body"]))
        stored = r.rowcount == 1
    if mode == "timeout":      # the message arrived but the acknowledgement was lost
        raise SendError("no acknowledgement from the provider (timeout)", Reason.COMM_DELIVERY_UNKNOWN, ack_lost=True)
    return stored


async def _received(message_id: str) -> bool:
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT 1 FROM mailbox WHERE message_id=%s", (message_id,))).fetchone() is not None


# ───────────── claim / settle ─────────────
_CLAIM = (
    "WITH due AS (SELECT id, status AS prev FROM outbox "
    " WHERE (%(case)s::text IS NULL OR case_id = %(case)s) "
    "   AND ((status IN ('pending','failed','delivery_unknown') AND next_attempt_at <= now()) "
    "     OR (status = 'sending' AND lease_until < now())) "
    " ORDER BY next_attempt_at, id LIMIT %(n)s FOR UPDATE SKIP LOCKED) "
    "UPDATE outbox o SET status='sending', attempts = o.attempts + 1, lease_until = now() + make_interval(secs => %(lease)s::float8) "
    "FROM due WHERE o.id = due.id RETURNING o.*, due.prev")


async def claim(case_id: str | None = None, limit: int = BATCH) -> list[dict]:
    async with db.pool.connection() as c:
        rows = await (await c.execute(_CLAIM, {"case": case_id, "n": limit, "lease": LEASE_S})).fetchall()
    return sorted(rows, key=lambda r: r["id"])


_SETTLE = ("UPDATE outbox SET status=%(status)s, lease_until=NULL, last_error=%(err)s, reason_code=%(code)s, "
           "next_attempt_at = CASE WHEN %(delay)s::float8 IS NULL THEN next_attempt_at ELSE now() + make_interval(secs => %(delay)s::float8) END, "
           "sent_at = CASE WHEN %(sent)s THEN now() ELSE sent_at END "
           "WHERE id=%(id)s AND status='sending' AND attempts=%(attempts)s")


async def _settle(row: dict, status: str, *, code: Reason | None = None, err: str | None = None, delay: float | None = None) -> bool:
    """Fenced: only the worker that still holds this attempt may write the result."""
    async with db.pool.connection() as c:
        r = await c.execute(_SETTLE, {"status": status, "err": audit.scrub(err, 300), "code": code.value if code else None,
                                      "delay": delay, "sent": status == "sent", "id": row["id"], "attempts": row["attempts"]})
        return r.rowcount == 1


async def _begin_call(row: dict) -> int | None:
    try:
        async with db.audit_pool.connection() as c:
            cur = await c.execute(
                "INSERT INTO external_calls(case_id,trace_id,span_id,kind,target,operation,attempt,status,params_hash) "
                "VALUES (%s,%s,%s,'smtp','sandbox-mail',%s,%s,'attempt',%s) RETURNING id",
                (row["case_id"], audit.trace_id_var.get(), audit.span_id_var.get(), row["kind"], row["attempts"],
                 audit.sha256_text(row["message_id"])))
            return (await cur.fetchone())["id"]
    except Exception:  # noqa: BLE001
        return None


async def _end_call(call_id: int | None, status: str, reason: Reason | None, ms: int) -> None:
    if call_id is None:
        return
    try:
        async with db.audit_pool.connection() as c:
            await c.execute("UPDATE external_calls SET status=%s, error_class=%s, latency_ms=%s WHERE id=%s",
                            (status, reason.value if reason else None, ms, call_id))
    except Exception:  # noqa: BLE001
        pass


async def _tell_requester(row: dict, why: str) -> None:
    """Dead letter -> in-app notice straight into the requester's mailbox (no outbox: it cannot fail or loop)."""
    if not row["case_id"] or row["kind"] == "delivery_failed":
        return
    async with db.pool.connection() as c:
        u = await (await c.execute("SELECT u.email FROM cases s JOIN users u ON u.id=s.requester_id WHERE s.id=%s", (row["case_id"],))).fetchone()
        if u:
            await c.execute(
                "INSERT INTO mailbox(message_id,outbox_id,case_id,kind,recipient,subject,body,channel) VALUES (%s,%s,%s,'delivery_failed',%s,%s,%s,'in_app') "
                "ON CONFLICT (message_id) DO NOTHING",
                (f"dead-{row['id']}-{row['attempts']}", row["id"], row["case_id"], u["email"],
                 f"Delivery failed: {row['subject'] or row['kind']}",
                 f"A message about {row['case_id']} could not be delivered ({why}). A manager can retry it from the case's Communications tab."))


# ───────────── one attempt ─────────────
async def _attempt(row: dict) -> str:
    audit.start_trace()
    cid, n = row["case_id"], row["attempts"]
    refs = {"outbox_id": row["id"], "kind": row["kind"], "attempt": n}

    if row["prev"] == "sending":     # the previous worker died or stalled while holding this row
        await audit.write(cid, "system", "outbox-sender", "message_send", "degraded", f"worker lease expired on a {row['kind']} message; re-checking it",
                          reason=Reason.INFRA_WORKER_LEASE_EXPIRED, refs=refs)

    if row["audience"] == "external" and cid:    # second line of defence; cancelling a case also voids its drafts
        async with db.pool.connection() as c:
            st = (await (await c.execute("SELECT status FROM cases WHERE id=%s", (cid,))).fetchone() or {}).get("status")
        if st not in SENDABLE_CASE_STATES:
            if await _settle(row, "cancelled", err=f"case is {st}; external message not sent"):
                await audit.write(cid, "system", "outbox-sender", "message_send", "blocked", f"external {row['kind']} not sent: case is {st}",
                                  reason=Reason.WORKFLOW_CANCELLED, refs=refs)
            return "cancelled"

    if row["prev"] in ("delivery_unknown", "sending") and await _received(row["message_id"]):
        if not await _settle(row, "sent"):
            return "lost_lease"
        await audit.write(cid, "system", "outbox-sender", "message_sent", "success",
                          f"{row['kind']} was already received; confirmed by receipt lookup, not sent again", refs={**refs, "confirmed_by": "receipt"})
        return "sent"

    call_id, t0 = await _begin_call(row), time.monotonic()
    try:
        stored = await deliver(row)
    except SendError as exc:
        await _end_call(call_id, "error", exc.reason, int((time.monotonic() - t0) * 1000))
        return await _failed(row, exc, refs)
    except Exception as exc:  # noqa: BLE001 - an unexpected provider error is treated as transient
        await _end_call(call_id, "error", Reason.COMM_SEND_FAILED, int((time.monotonic() - t0) * 1000))
        return await _failed(row, SendError(f"{type(exc).__name__}: {exc}", Reason.COMM_SEND_FAILED), refs)
    await _end_call(call_id, "ok", None, int((time.monotonic() - t0) * 1000))
    if not await _settle(row, "sent"):
        await audit.write(cid, "system", "outbox-sender", "message_send", "skipped", "result discarded: this worker no longer holds the message",
                          reason=Reason.INFRA_WORKER_LEASE_EXPIRED, refs=refs)
        return "lost_lease"
    await audit.write(cid, "system", "outbox-sender", "message_sent", "success",
                      f"sent {row['kind']} (attempt {n})" + ("" if stored else "; the provider already had it, no duplicate stored"), refs=refs)
    return "sent"


async def _failed(row: dict, exc: SendError, refs: dict) -> str:
    cid, n = row["case_id"], row["attempts"]
    err_obj = audit.error_obj(exc.reason.value, "sandbox-mail", None, None, str(exc))
    out_of_budget = n >= row["max_attempts"]
    if exc.permanent or out_of_budget:
        code = exc.reason if exc.permanent else Reason.COMM_DEAD
        why = str(exc) if exc.permanent else f"gave up after {n} attempt(s): {exc}"
        if not await _settle(row, "dead", code=code, err=why):
            return "lost_lease"
        await _tell_requester(row, "the address rejected it" if exc.permanent else "it kept failing")
        await audit.write(cid, "system", "outbox-sender", "message_dead", "failure", f"{row['kind']} not delivered: {why}",
                          reason=code, severity="error", refs=refs, error=err_obj)
        return "dead"
    delay = backoff_s(n)
    status = "delivery_unknown" if exc.ack_lost else "failed"
    if not await _settle(row, status, code=exc.reason, err=str(exc), delay=delay):
        return "lost_lease"
    await audit.write(cid, "system", "outbox-sender", "message_send", "degraded" if exc.ack_lost else "failure",
                      f"{row['kind']} attempt {n} {'unconfirmed' if exc.ack_lost else 'failed'}: {exc}; next try in {delay:.0f}s",
                      reason=exc.reason, refs={**refs, "retry_in_s": round(delay, 1)}, error=err_obj)
    return "unknown" if exc.ack_lost else "retry"


# ───────────── the loop ─────────────
async def run_once(case_id: str | None = None, limit: int = BATCH) -> dict:
    """Claim what is due and attempt it. Safe to call from any worker at any time."""
    res = dict.fromkeys(OUTCOMES, 0)
    rows = await claim(case_id, limit)
    res["claimed"] = len(rows)
    for row in rows:
        try:
            res[await _attempt(row)] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - leave the row 'sending': its lease expires and another pass retries it
            await audit.write(row["case_id"], "system", "outbox-sender", "message_send", "failure",
                              f"sender crashed on a {row['kind']} message: {type(exc).__name__}", severity="error", reason=Reason.COMM_SEND_FAILED,
                              refs={"outbox_id": row["id"]}, error=audit.error_obj(type(exc).__name__, "outbox-sender", None, audit.trace_id_var.get(), str(exc)))
    return res


async def stats() -> dict:
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT status, count(*) n FROM outbox GROUP BY status")).fetchall()
        old = await (await c.execute("SELECT extract(epoch FROM now() - min(next_attempt_at)) s FROM outbox WHERE status IN ('pending','failed','delivery_unknown')")).fetchone()
    return {"by_status": {r["status"]: r["n"] for r in rows}, "oldest_due_s": max(int(old["s"]), 0) if old and old["s"] is not None else None}


async def tick_loop(every: float = TICK_S) -> None:
    while True:
        try:
            await run_once()
            await reminders.sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await audit.write(None, "system", "outbox-sender", "sender_tick", "failure", f"sender loop error: {type(exc).__name__}",
                              severity="error", reason=Reason.COMM_SEND_FAILED,
                              error=audit.error_obj(type(exc).__name__, "outbox-sender", None, None, str(exc)))
        await activity.nap(every, settings.loop_idle_s)
