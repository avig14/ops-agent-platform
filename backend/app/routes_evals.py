"""Serves the last evaluation snapshot (app/evals/results/latest.json) to the Admin page.

Read-only. The snapshot is produced offline by `python -m app.evals.run`; nothing is computed per request, and nothing here
can reach the LLM. `stale` tells the viewer when the prompt or dataset changed after the snapshot was taken.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends

from . import auth
from .schemas import Role, UserOut

router = APIRouter(prefix="/api/admin", tags=["evals"])
admin = Depends(auth.require_roles(Role.admin))
SNAPSHOT = Path(__file__).parent / "evals" / "results" / "latest.json"


def _slim(report: dict) -> dict:
    """Drop the per-email rows (large, only useful in the file); keep every number the page shows."""
    for mode in (report.get("real_model", {}).get("extraction", {}).get("modes") or {}).values():
        mode.pop("cases", None)
    return report


@router.get("/evals")
async def evals(user: UserOut = admin):
    if not SNAPSHOT.is_file():
        return {"available": False, "hint": "run: python -m app.evals.run"}
    try:
        report = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "hint": "snapshot unreadable; re-run: python -m app.evals.run"}
    from .evals import datasets, run          # imported here so the app boots without loading the eval code
    prov = report.get("provenance", {})
    now = {"dataset_hash": datasets.load_extraction()[1], "prompt_hash": run.prompt_hash()}
    stale = [k for k, v in now.items() if prov.get(k) != v]
    return {"available": True, "stale": bool(stale), "stale_because": stale, "current": now, **_slim(report)}
