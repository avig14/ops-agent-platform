"""The Admin evals endpoint: admin-only, read-only, honest about missing or out-of-date snapshots."""
import json

import pytest

from app import routes_evals
from app.evals import datasets, run
from tests.test_cases_api import _clean, client_for  # noqa: F401  (_clean seeds the demo users)


def snapshot(**prov):
    base = {"dataset_hash": datasets.load_extraction()[1], "prompt_hash": run.prompt_hash(), "run_mode": "replay"}
    return {"provenance": {**base, **prov}, "scripted": {}, "limits": ["x"],
            "real_model": {"extraction": {"modes": {"llm": {"scored": 1, "cases": [{"id": "E01"}]}}}}}


@pytest.fixture
def snap(tmp_path, monkeypatch):
    path = tmp_path / "latest.json"
    monkeypatch.setattr(routes_evals, "SNAPSHOT", path)
    return path


async def test_non_admin_is_refused(snap):
    snap.write_text(json.dumps(snapshot()))
    c = await client_for("rahul.manager@demo.test")
    assert (await c.get("/api/admin/evals")).status_code == 403


async def test_no_snapshot_says_so_instead_of_inventing_numbers(snap):
    c = await client_for("admin@demo.test")
    r = await c.get("/api/admin/evals")
    assert r.status_code == 200 and r.json()["available"] is False


async def test_corrupt_snapshot_is_reported_not_crashed(snap):
    snap.write_text("{not json")
    c = await client_for("admin@demo.test")
    assert (await c.get("/api/admin/evals")).json()["available"] is False


async def test_current_snapshot_is_served_without_per_email_rows(snap):
    snap.write_text(json.dumps(snapshot()))
    c = await client_for("admin@demo.test")
    j = (await c.get("/api/admin/evals")).json()
    assert j["available"] and not j["stale"] and j["stale_because"] == []
    assert "cases" not in j["real_model"]["extraction"]["modes"]["llm"] and j["real_model"]["extraction"]["modes"]["llm"]["scored"] == 1


async def test_a_changed_prompt_marks_the_snapshot_stale(snap):
    snap.write_text(json.dumps(snapshot(prompt_hash="deadbeef0000")))
    c = await client_for("admin@demo.test")
    j = (await c.get("/api/admin/evals")).json()
    assert j["stale"] and j["stale_because"] == ["prompt_hash"]
