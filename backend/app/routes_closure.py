"""Closure API: carry an approved plan to the end, tick the closure items, close, reopen, and read the closing summary.
Every route first checks that the caller may see the case (404 otherwise, audited). The role rules live in `closure.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import audit, auth, changes, closure, reporting
from .approvals import Denied
from .codes import Reason
from .routes_cases import get_visible
from .schemas import ItemIn, ReopenIn, UserOut

router = APIRouter(prefix="/api/cases", tags=["closure"])
anyone = Depends(auth.current_user)


async def _run(fn, *args):
    try:
        return await fn(*args)
    except Denied as d:
        raise HTTPException(d.status, d.detail)


@router.get("/{case_id}/closure")
async def get_closure(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    return await closure.state(case_id)


@router.post("/{case_id}/closure/start")
async def start(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    return await _run(closure.start, user, case_id)


@router.post("/{case_id}/closure/monitor")
async def monitor(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    return await _run(closure.monitor, user, case_id)


@router.post("/{case_id}/closure/items/{key}")
async def tick(case_id: str, key: str, body: ItemIn | None = None, user: UserOut = anyone):
    await get_visible(case_id, user)
    return await _run(closure.mark_item, user, case_id, key, (body or ItemIn()).note)


@router.post("/{case_id}/closure/close")
async def close(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    res = await _run(closure.close, user, case_id)
    summary = await reporting.closing_summary(case_id, user)         # never raises; a reporting problem must not undo a close
    return {**res, "summary": summary}


@router.post("/{case_id}/closure/reopen")
async def reopen(case_id: str, body: ReopenIn | None = None, user: UserOut = anyone):
    await get_visible(case_id, user)
    return await _run(closure.reopen, user, case_id, (body or ReopenIn()).reason)


@router.post("/{case_id}/closure/summary")
async def regenerate_summary(case_id: str, user: UserOut = anyone):
    """Write the closing summary again (for example after the model was down at close time)."""
    case = await get_visible(case_id, user)
    why = None
    if not changes.is_approver(user, case):
        why = Denied(403, f"a {user.role.value} may not write the closing summary", Reason.POLICY_UNAUTHORIZED)
    elif case["status"] != "CLOSED":
        why = Denied(409, f"case is {case['status']}; only a closed case has a closing summary", Reason.WORKFLOW_CONCURRENCY_CONFLICT)
    if why:
        await audit.write(case_id, "human", user.id, "closure_summary", "blocked", f"summary refused: {why.detail}", reason=why.reason,
                          refs={"role": user.role.value})
        raise HTTPException(why.status, why.detail)
    row = await reporting.closing_summary(case_id, user)
    if row is None:
        raise HTTPException(503, "the summary could not be written; try again")
    return row


@router.get("/{case_id}/summary")
async def get_summary(case_id: str, user: UserOut = anyone):
    await get_visible(case_id, user)
    return {"summary": await reporting.latest_closing(case_id)}
