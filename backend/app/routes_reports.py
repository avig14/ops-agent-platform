"""Management report (numbers by SQL, wording by the reporting agent) and the scenario library."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import audit, auth, reporting, scenarios
from .approvals import Denied
from .schemas import UserOut

router = APIRouter(prefix="/api", tags=["reports"])
anyone = Depends(auth.current_user)


async def _run(fn, user: UserOut, action: str):
    try:
        return await fn(user)
    except Denied as d:
        await audit.write(None, "human", user.id, action, "blocked", f"{action.replace('_', ' ')} refused: {d.detail}", reason=d.reason,
                          refs={"role": user.role.value})
        raise HTTPException(d.status, d.detail)


@router.get("/reports/management")
async def management(user: UserOut = anyone):
    return await _run(reporting.management_metrics, user, "report_view")


@router.post("/reports/management/narrative")
async def narrative(user: UserOut = anyone):
    return await _run(reporting.management_narrative, user, "report_narrative")


@router.get("/scenarios")
async def list_scenarios(user: UserOut = anyone):
    return {"scenarios": scenarios.SCENARIOS}
