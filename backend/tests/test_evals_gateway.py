"""EvalGateway: record once, replay forever, never touch the database or the real audit chain."""
import json

import pytest
from pydantic import BaseModel

from app import audit
from app.evals.capture import capture_audit
from app.evals.gateway import EvalGateway
from app.llm import LLMUnavailable


class Out(BaseModel):
    answer: str


def ok(text="hi"):
    return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps({"answer": text})}]}, "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3}}, {}


class Counting:
    def __init__(self, *replies):
        self.replies, self.n = list(replies), 0

    async def __call__(self, model, body):
        self.n += 1
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


async def test_live_run_records_and_a_second_instance_replays_with_zero_calls(tmp_path):
    store = tmp_path / "rec.json"
    live = Counting(ok("first"))
    with capture_audit():
        g = EvalGateway(mode="live", store=store, models=("m1",), transport=live, rpm=60000)
        r = await g.generate("t", "sys", "user", Out)
        assert r.data.answer == "first" and not r.cache_hit and live.n == 1
        again = await g.generate("t", "sys", "user", Out)             # same prompt in the same run: served from the recording
        assert again.cache_hit and live.n == 1
        g.save()

        dead = Counting(ok("SHOULD NOT BE CALLED"))
        rp = EvalGateway(mode="replay", store=store, models=("m1",), transport=dead)
        r2 = await rp.generate("t", "sys", "user", Out)
    assert r2.data.answer == "first" and r2.cache_hit and dead.n == 0


async def test_replay_miss_raises_instead_of_calling_the_api(tmp_path):
    dead = Counting(ok())
    with capture_audit() as rec:
        g = EvalGateway(mode="replay", store=tmp_path / "none.json", models=("m1",), transport=dead)
        with pytest.raises(LLMUnavailable):
            await g.generate("t", "sys", "never recorded", Out)
    assert dead.n == 0
    assert rec.count("llm_call") == 1                                  # the miss is recorded as a failure, in memory only


async def test_recording_file_has_tokens_and_latency_and_no_secret(tmp_path):
    store = tmp_path / "rec.json"
    with capture_audit():
        g = EvalGateway(mode="live", store=store, models=("m1",), transport=Counting(ok()), rpm=60000)
        await g.generate("t", "sys", "user", Out)
        g.save()
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["entries"]) == 1
    (call,) = data["calls"]
    assert call["tokens_in"] == 11 and call["tokens_out"] == 3 and call["status"] == 200 and call["ms"] >= 0
    blob = store.read_text(encoding="utf-8").lower()
    assert "api-key" not in blob and "x-goog" not in blob


async def test_scripted_mode_never_caches_so_two_cases_with_one_email_get_their_own_output():
    with capture_audit():
        g = EvalGateway(mode="scripted", models=("m1",), transport=Counting(ok("A")), rpm=60000)
        a = await g.generate("t", "sys", "same email", Out)
        g2 = EvalGateway(mode="scripted", models=("m1",), transport=Counting(ok("B")), rpm=60000)
        b = await g2.generate("t", "sys", "same email", Out)
        again = await g.generate("t", "sys", "same email", Out)        # even on the same gateway: no cache in scripted mode
    assert (a.data.answer, b.data.answer) == ("A", "B") and not again.cache_hit


async def test_refresh_ignores_an_existing_recording(tmp_path):
    store = tmp_path / "rec.json"
    with capture_audit():
        g = EvalGateway(mode="live", store=store, models=("m1",), transport=Counting(ok("old")), rpm=60000)
        await g.generate("t", "sys", "user", Out)
        g2 = EvalGateway(mode="live", store=store, models=("m1",), transport=Counting(ok("new")), rpm=60000, refresh=True)
        r = await g2.generate("t", "sys", "user", Out)
    assert r.data.answer == "new" and not r.cache_hit


def test_a_recording_with_the_wrong_format_version_is_refused(tmp_path):
    store = tmp_path / "rec.json"
    store.write_text(json.dumps({"version": 99, "entries": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        EvalGateway(mode="replay", store=store, models=("m1",))


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        EvalGateway(mode="turbo")


async def test_capture_audit_restores_the_real_writer_even_when_the_block_raises():
    real = audit.write
    with pytest.raises(RuntimeError):
        with capture_audit():
            raise RuntimeError("boom")
    assert audit.write is real
