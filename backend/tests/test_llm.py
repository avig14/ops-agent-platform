import asyncio
import json
import uuid

import pytest
from pydantic import BaseModel

from app import db, llm
from app.codes import Reason
from app.llm import Gateway, LLMUnavailable, RateLimiter, chaos

_real_sleep = asyncio.sleep


class Out(BaseModel):
    qty: int
    note: str | None = None


def ok_body(obj: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}


def err_body(code: int, msg: str, status: str = "") -> dict:
    return {"error": {"code": code, "message": msg, "status": status}}


class Fake:
    """Scripted transport: list of (status, body) per call; records (model) of each call."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def __call__(self, model, body):
        self.calls.append(model)
        s = self.script.pop(0) if self.script else self.script_last
        self.script_last = s
        return s[0], s[1], {}


def make(script, models=("m1", "m2")):
    f = Fake(script)
    g = Gateway(models=models, transport=f)
    g.limiter = RateLimiter(60000)
    return g, f


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def quick(_s):
        await _real_sleep(0)
    monkeypatch.setattr(llm.asyncio, "sleep", quick)
    chaos.mode = None
    yield
    chaos.mode = None


def uniq():
    return "task-" + uuid.uuid4().hex[:8]


async def run(g, task=None, **kw):
    return await g.generate(task or uniq(), "sys", "user " + uuid.uuid4().hex, Out, **kw)


async def audit_reasons(model_or_none=None):
    async with db.audit_pool.connection() as c:
        rows = await (await c.execute("SELECT reason_code, action, outcome FROM audit_log WHERE action LIKE 'llm%%' OR action='circuit_open'")).fetchall()
    return rows


async def test_success_and_cache_replay():
    g, f = make([(200, ok_body({"qty": 5}))])
    r = await g.generate("t-cache", "s", "u-cache-1", Out)
    assert r.data.qty == 5 and not r.cache_hit and f.calls == ["m1"]
    r2 = await g.generate("t-cache", "s", "u-cache-1", Out)
    assert r2.cache_hit and r2.data.qty == 5 and f.calls == ["m1"]  # no 2nd call


async def test_retry_503_then_success_same_model():
    g, f = make([(503, err_body(503, "overloaded", "UNAVAILABLE")), (200, ok_body({"qty": 1}))])
    r = await run(g)
    assert r.model == "m1" and r.attempts == 2 and f.calls == ["m1", "m1"]


async def test_daily_quota_429_fails_over_without_retry():
    g, f = make([(429, err_body(429, "Quota exceeded for metric ... PerDay", "RESOURCE_EXHAUSTED")),
                 (200, ok_body({"qty": 2}))])
    r = await run(g)
    assert r.model == "m2" and f.calls == ["m1", "m2"] and r.failovers == ["m1->m2"]


async def test_billing_error_stops_chain_and_is_audited():
    g, f = make([(402, err_body(402, "billing: credit balance too low"))])
    with pytest.raises(LLMUnavailable) as ei:
        await run(g, case_id="CASE-LLM-BILL")
    assert ei.value.reason == Reason.LLM_BILLING
    assert f.calls == ["m1"]  # no pointless fallback / retry
    async with db.audit_pool.connection() as c:
        rows = await (await c.execute("SELECT reason_code, not_fetched FROM audit_log WHERE chain_key='CASE-LLM-BILL' "
                                      "AND action='llm_chain_exhausted'")).fetchall()
    assert rows and rows[0]["reason_code"] == "LLM_BILLING"
    assert rows[0]["not_fetched"][0]["source"] == "llm"


async def test_auth_error_not_retried():
    g, f = make([(401, err_body(401, "API key not valid", "UNAUTHENTICATED"))])
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.LLM_AUTH and f.calls == ["m1"]


async def test_garbage_json_repair_retry_then_ok():
    bad = {"candidates": [{"content": {"parts": [{"text": "{nope"}]}, "finishReason": "STOP"}]}
    g, f = make([(200, bad), (200, ok_body({"qty": 9}))])
    r = await run(g)
    assert r.data.qty == 9 and r.attempts == 2


async def test_schema_invalid_twice_falls_to_next_model():
    wrong = ok_body({"qty": "not-a-number"})
    g, f = make([(200, wrong), (200, wrong), (200, wrong), (200, ok_body({"qty": 4}))])
    r = await run(g)
    assert r.model == "m2" and r.data.qty == 4


async def test_all_models_down_raises_with_attempts_log():
    g, f = make([(503, err_body(503, "overloaded", "UNAVAILABLE"))])
    with pytest.raises(LLMUnavailable) as ei:
        await run(g, case_id="CASE-LLM-DOWN")
    assert ei.value.reason == Reason.LLM_OVERLOADED
    assert len(ei.value.attempts) >= 2  # both models tried
    async with db.pool.connection() as c:
        n = (await (await c.execute("SELECT count(*) n FROM external_calls WHERE case_id='CASE-LLM-DOWN' AND status='error'")).fetchone())["n"]
    assert n >= 2  # per-attempt rows


async def test_circuit_opens_then_skips_model():
    g, f = make([(503, err_body(503, "overloaded", "UNAVAILABLE"))], models=("m1",))
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await run(g)
    assert g.breakers["m1"].state == "open"
    n = len(f.calls)
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.INFRA_CIRCUIT_OPEN and len(f.calls) == n  # no new call while open


async def test_safety_block_classified():
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    g, f = make([(200, blocked)], models=("m1",))
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.LLM_SAFETY_BLOCK


async def test_truncated_output_classified():
    t = {"candidates": [{"content": {"parts": [{"text": '{"qty": 1'}]}, "finishReason": "MAX_TOKENS"}]}
    g, f = make([(200, t)], models=("m1",))
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.LLM_TRUNCATED


async def test_chaos_injection_and_replay_mode():
    g, f = make([(200, ok_body({"qty": 1}))], models=("m1",))
    chaos.mode = "402"
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.LLM_BILLING and f.calls == []  # injected before transport
    chaos.mode = None
    g.mode = "replay"
    with pytest.raises(LLMUnavailable):  # replay mode never calls the provider
        await run(g)
    assert f.calls == []


async def test_daily_cap_blocks():
    g, f = make([(200, ok_body({"qty": 1}))])
    g.calls_today = 10**6
    with pytest.raises(LLMUnavailable) as ei:
        await run(g)
    assert ei.value.reason == Reason.INFRA_BUDGET_EXHAUSTED and f.calls == []


def test_classify_table():
    c = llm.classify
    assert c(429, err_body(429, "Quota exceeded ... GenerateRequestsPerDayPerProjectPerModel", "RESOURCE_EXHAUSTED")) == (Reason.LLM_RATE_LIMIT, True)
    assert c(429, err_body(429, "too many requests", "RESOURCE_EXHAUSTED")) == (Reason.LLM_RATE_LIMIT, False)
    assert c(503, err_body(503, "x", "UNAVAILABLE"))[0] == Reason.LLM_OVERLOADED
    assert c(400, err_body(400, "API key not valid. Please pass a valid API key."))[0] == Reason.LLM_AUTH
    assert c(400, err_body(400, "bad field"))[0] == Reason.LLM_BAD_REQUEST
    assert c(403, err_body(403, "denied", "PERMISSION_DENIED"))[0] == Reason.LLM_AUTH
