"""Communications API: the sandbox mailbox (what people received), a case's message log, and the human actions on messages."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from . import approvals, auth, db, messages
from .routes_cases import get_visible
from .schemas import DiscardIn, ReleaseIn, Role, UserOut

router = APIRouter(prefix="/api", tags=["communications"])
anyone = Depends(auth.current_user)

_MAIL = "id,message_id,case_id,kind,recipient,subject,body,channel,delivered_at,read_at"
_LOG = ("o.id,o.kind,o.recipient,o.audience,o.subject,o.body,o.status,o.attempts,o.max_attempts,o.next_attempt_at,o.last_error,o.reason_code,"
        "o.source,o.edited,o.reviewed_by,o.reviewed_at,o.created_at,o.sent_at,m.delivered_at")


@router.get("/mailbox")
async def mailbox(scope: str = Query("mine", pattern="^(mine|all)$"), unread: bool = False, user: UserOut = anyone):
    """The messages this person received. `scope=all` (admin only) shows the whole sandbox provider."""
    if scope == "all" and user.role != Role.admin:
        raise HTTPException(403, "only an admin can view every mailbox")
    where, params = [], []
    if scope == "mine":
        where.append("lower(recipient)=lower(%s)")
        params.append(user.email)
    if unread:
        where.append("read_at IS NULL")
    sql = f"SELECT {_MAIL} FROM mailbox" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 200"
    async with db.pool.connection() as c:
        rows = await (await c.execute(sql, params)).fetchall()
        mine = await (await c.execute("SELECT count(*) n FROM mailbox WHERE lower(recipient)=lower(%s) AND read_at IS NULL", (user.email,))).fetchone()
    return {"messages": rows, "unread": mine["n"]}


@router.post("/mailbox/{msg_id}/read")
async def mark_read(msg_id: int, user: UserOut = anyone):
    async with db.pool.connection() as c:
        r = await c.execute("UPDATE mailbox SET read_at=COALESCE(read_at, now()) WHERE id=%s AND lower(recipient)=lower(%s)", (msg_id, user.email))
    if r.rowcount != 1:
        raise HTTPException(404, "message not found")
    return {"id": msg_id, "read": True}


@router.get("/cases/{case_id}/communications")
async def communications(case_id: str, user: UserOut = anyone):
    """Every message about a case with its delivery state. A requester sees only what was addressed to them."""
    await get_visible(case_id, user)
    async with db.pool.connection() as c:
        rows = await (await c.execute(
            f"SELECT {_LOG} FROM outbox o LEFT JOIN mailbox m ON m.message_id=o.message_id WHERE o.case_id=%s ORDER BY o.id", (case_id,))).fetchall()
        inapp = await (await c.execute("SELECT id,kind,recipient,subject,body,channel,delivered_at FROM mailbox WHERE case_id=%s AND channel='in_app' ORDER BY id",
                                       (case_id,))).fetchall()
    if user.role == Role.requester:
        mine = user.email.lower()
        rows = [{k: r[k] for k in ("id", "kind", "recipient", "subject", "body", "status", "sent_at", "delivered_at")}
                for r in rows if r["recipient"].lower() == mine]
        inapp = [r for r in inapp if r["recipient"].lower() == mine]
    return {"messages": rows, "notices": inapp}


async def _row(msg_id: int, user: UserOut) -> dict:
    row = await messages.load(msg_id)
    if row is None or (row["case_id"] is None and user.role != Role.admin):
        raise HTTPException(404, "message not found")
    if row["case_id"]:
        await get_visible(row["case_id"], user)
    return row


async def _run(fn, *args):
    try:
        return await fn(*args)
    except approvals.Denied as d:
        raise HTTPException(d.status, d.detail)


@router.post("/outbox/{msg_id}/retry")
async def retry(msg_id: int, user: UserOut = anyone):
    return await _run(messages.retry, user, await _row(msg_id, user))


@router.post("/outbox/{msg_id}/release")
async def release(msg_id: int, body: ReleaseIn | None = None, user: UserOut = anyone):
    body = body or ReleaseIn()
    return await _run(messages.release, user, await _row(msg_id, user), body.subject, body.body)


@router.post("/outbox/{msg_id}/discard")
async def discard(msg_id: int, body: DiscardIn | None = None, user: UserOut = anyone):
    return await _run(messages.discard, user, await _row(msg_id, user), (body or DiscardIn()).reason)
