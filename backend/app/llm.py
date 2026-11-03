"""LLM gateway: Gemini REST with classified errors, retry, model fallback chain, circuit breaker, replay cache,
rate limiting, chaos injection. Every attempt is recorded in `external_calls`; failures and failovers are audited.

Auth: key goes in the `x-goog-api-key` header only (never in the URL). Structured output: `responseJsonSchema`
(verified against the live API; `responseSchema` rejects type arrays such as ["string","null"]).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from . import audit, db
from .codes import Reason
from .config import settings

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
Transport = Callable[[str, dict], Awaitable[tuple[int, dict, dict]]]  # (model, body) -> (status, json, headers)


class LLMUnavailable(Exception):
    """Whole chain failed. `reason` is the terminal Reason code; `attempts` lists what was tried."""

    def __init__(self, reason: Reason, message: str, attempts: list[dict] | None = None):
        super().__init__(message)
        self.reason, self.attempts = reason, attempts or []


@dataclass
class LLMResult:
    data: BaseModel
    model: str
    cache_hit: bool = False
    attempts: int = 1
    tokens_in: int = 0
    tokens_out: int = 0
    failovers: list[str] = field(default_factory=list)


def inline_schema(schema: dict) -> dict:
    """Resolve $ref/$defs and drop noise keys (title/default) so the schema is self-contained for Gemini."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


# ───────────────────────── chaos (demo/testing only) ─────────────────────────
class Chaos:
    mode: str | None = None  # None | 402 | 429 | 503 | timeout | garbage | 401


chaos = Chaos()

# ───────────────────────── error classification ─────────────────────────
RETRYABLE = {Reason.LLM_RATE_LIMIT, Reason.LLM_OVERLOADED, Reason.LLM_TIMEOUT}
STOP_CHAIN = {Reason.LLM_BILLING, Reason.LLM_AUTH}  # project-wide: every model would fail the same way


def classify(status: int, body: dict) -> tuple[Reason, bool]:
    """-> (reason, daily_quota_exhausted). Daily quota is per model, so fall back instead of retrying."""
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    msg = str(err.get("message", "")).lower()
    st = str(err.get("status", "")).upper()
    if status == 402 or "billing" in msg and status in (400, 403, 429):
        return Reason.LLM_BILLING, False
    if status in (401, 403) or st in ("PERMISSION_DENIED", "UNAUTHENTICATED") or "api key not valid" in msg or "api_key_invalid" in msg:
        return Reason.LLM_AUTH, False
    if status == 429 or st == "RESOURCE_EXHAUSTED":
        daily = "perday" in msg.replace(" ", "").replace("_", "") or "per day" in msg
        return Reason.LLM_RATE_LIMIT, daily
    if status in (500, 502, 503, 504, 529) or st in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
        return Reason.LLM_OVERLOADED, False
    if status == 400:
        return (Reason.LLM_CONTEXT_TOO_LONG if "token" in msg and "exceed" in msg else Reason.LLM_BAD_REQUEST), False
    return Reason.LLM_UNKNOWN, False


# ───────────────────────── circuit breaker + limiter ─────────────────────────
@dataclass
class Breaker:
    failures: int = 0
    open_until: float = 0.0
    threshold: int = 3
    cooldown_s: float = 30.0

    def allow(self) -> bool:  # closed, or half-open after cooldown (one probe allowed)
        return time.monotonic() >= self.open_until

    def ok(self) -> None:
        self.failures, self.open_until = 0, 0.0

    def fail(self) -> bool:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.cooldown_s
            return True
        return False

    @property
    def state(self) -> str:
        if self.open_until == 0.0:
            return "closed"
        return "open" if time.monotonic() < self.open_until else "half_open"


class RateLimiter:
    def __init__(self, rpm: int):
        self.interval, self._next, self._lock = 60.0 / max(rpm, 1), 0.0, asyncio.Lock()

    async def acquire(self, max_wait: float = 25.0) -> bool:
        async with self._lock:
            wait = self._next - time.monotonic()
            if wait > max_wait:
                return False
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(time.monotonic(), self._next) + self.interval
            return True


# ───────────────────────── gateway ─────────────────────────
class Gateway:
    def __init__(self, models: tuple[str, ...] | None = None, transport: Transport | None = None):
        self.models = models or settings.gemini_models
        self.transport = transport or self._http
        self.breakers = {m: Breaker() for m in self.models}
        self.limiter = RateLimiter(settings.llm_rpm)
        self.mode = "live"  # live | replay (cache only; used by evals/CI)
        self.calls_today = 0

    # -- transport --
    async def _http(self, model: str, body: dict) -> tuple[int, dict, dict]:
        if not settings.gemini_api_key:
            return 401, {"error": {"code": 401, "message": "API key not configured", "status": "UNAUTHENTICATED"}}, {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0)) as c:
                r = await c.post(API.format(model=model), json=body,
                                 headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"})
            try:
                data = r.json()
            except ValueError:
                data = {"error": {"message": r.text[:200]}}
            return r.status_code, data, dict(r.headers)
        except httpx.TimeoutException:
            raise
        except httpx.HTTPError as exc:  # network error: message scrubbed by caller
            return 503, {"error": {"message": f"network: {type(exc).__name__}", "status": "UNAVAILABLE"}}, {}

    async def _call(self, model: str, body: dict) -> tuple[int, dict, dict]:
        m = chaos.mode if settings.enable_chaos else None
        if m == "timeout":
            raise httpx.ReadTimeout("chaos: injected timeout")
        if m == "garbage":
            return 200, {"candidates": [{"content": {"parts": [{"text": "{not json!!"}]}, "finishReason": "STOP"}]}, {}
        if m in ("402", "429", "503", "401"):
            code = int(m)
            msgs = {402: "billing: credit balance too low", 429: "quota exceeded", 503: "model is overloaded",
                    401: "API key not valid"}
            return code, {"error": {"code": code, "message": f"chaos injected: {msgs[code]}"}}, {"retry-after": "1"}
        return await self.transport(model, body)

    # -- external_calls bookkeeping --
    async def _begin(self, case_id, kind_target: str, op: str, attempt: int, params_hash: str, failover_from: str | None) -> int | None:
        try:
            async with db.audit_pool.connection() as c:
                cur = await c.execute(
                    "INSERT INTO external_calls(case_id,trace_id,span_id,parent_span_id,kind,target,operation,attempt,status,"
                    "failover_from,params_hash) VALUES (%s,%s,%s,%s,'llm',%s,%s,%s,'attempt',%s,%s) RETURNING id",
                    (case_id, audit.trace_id_var.get(), audit.span_id_var.get(), None, kind_target, op, attempt,
                     failover_from, params_hash))
                return (await cur.fetchone())["id"]
        except Exception:  # noqa: BLE001
            return None

    async def _finish(self, call_id, status: str, *, err: Reason | None = None, http: int | None = None,
                      req_id: str | None = None, ms: int = 0, tin: int = 0, tout: int = 0, cache_hit: bool = False) -> None:
        if call_id is None:
            return
        try:
            async with db.audit_pool.connection() as c:
                await c.execute(
                    "UPDATE external_calls SET status=%s,error_class=%s,http_status=%s,request_id=%s,latency_ms=%s,"
                    "tokens_in=%s,tokens_out=%s,cache_hit=%s WHERE id=%s",
                    (status, err.value if err else None, http, req_id, ms, tin, tout, cache_hit, call_id))
        except Exception:  # noqa: BLE001
            pass

    # -- cache --
    async def _cache_get(self, key: str) -> dict | None:
        async with db.audit_pool.connection() as c:
            row = await (await c.execute("SELECT response, model FROM llm_cache WHERE prompt_hash=%s", (key,))).fetchone()
        return row

    async def _cache_put(self, key: str, model: str, data: dict) -> None:
        async with db.audit_pool.connection() as c:
            await c.execute("INSERT INTO llm_cache(prompt_hash,model,response) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (key, model, Jsonb(data)))

    # -- main entry --
    async def generate(self, task: str, system: str, user: str, out_model: type[BaseModel], *, case_id: str | None = None,
                       actor: str = "llm", temperature: float = 0.0, max_tokens: int = 2048,
                       use_cache: bool = True, models: tuple[str, ...] | None = None) -> LLMResult:
        schema = inline_schema(out_model.model_json_schema())
        chain = tuple(models) if models else self.models
        for m in chain:
            self.breakers.setdefault(m, Breaker())
        key = audit.sha256_text(json.dumps({"t": task, "s": system, "u": user, "sc": schema, "temp": temperature,
                                            "m": list(models) if models else None}, sort_keys=True))
        cached = await self._cache_get(key) if use_cache else None
        if cached:
            cid = await self._begin(case_id, cached["model"], task, 1, key, None)
            await self._finish(cid, "ok", cache_hit=True)
            return LLMResult(out_model.model_validate(cached["response"]), cached["model"], cache_hit=True)
        if self.mode == "replay":
            await audit.write(case_id, "system", "llm_gateway", "llm_call", "failure", f"replay mode: no cached response for {task}",
                              reason=Reason.LLM_UNKNOWN, refs={"prompt_hash": key})
            raise LLMUnavailable(Reason.LLM_UNKNOWN, "replay cache miss")
        if self.calls_today >= settings.llm_daily_cap:
            await audit.write(case_id, "system", "llm_gateway", "llm_call", "blocked", "daily LLM call cap reached",
                              reason=Reason.INFRA_BUDGET_EXHAUSTED)
            raise LLMUnavailable(Reason.INFRA_BUDGET_EXHAUSTED, "daily cap")

        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                                 "responseMimeType": "application/json", "responseJsonSchema": schema},
        }
        attempts_log: list[dict] = []
        failovers: list[str] = []
        prev_model: str | None = None
        last_reason = Reason.LLM_UNKNOWN
        for model in chain:
            br = self.breakers[model]
            if not br.allow():
                attempts_log.append({"model": model, "skipped": "circuit_open"})
                last_reason = Reason.INFRA_CIRCUIT_OPEN
                await audit.write(case_id, "system", "llm_gateway", "llm_call", "skipped", f"{model} skipped: circuit open",
                                  reason=Reason.INFRA_CIRCUIT_OPEN, model=model)
                continue
            if prev_model:
                failovers.append(f"{prev_model}->{model}")
                await audit.write(case_id, "system", "llm_gateway", "llm_failover", "degraded",
                                  f"failover {prev_model} -> {model}", reason=last_reason, model=model)
            result = await self._try_model(model, body, out_model, task, key, case_id, attempts_log, prev_model, actor)
            if isinstance(result, LLMResult):
                result.failovers = failovers
                await self._cache_put(key, model, result.data.model_dump(mode="json"))
                self.calls_today += 1
                return result
            last_reason = result
            prev_model = model
            if result in STOP_CHAIN:
                break
        await audit.write(case_id, "system", "llm_gateway", "llm_chain_exhausted", "failure",
                          f"all LLM models failed for {task}", severity="error", reason=last_reason,
                          error=audit.error_obj(last_reason.value, "gemini", None, None, json.dumps(attempts_log)[:400]),
                          nf=[audit.not_fetched("llm", last_reason, task, key, len(attempts_log), "rule_based_or_manual",
                                                affects=[task])])
        raise LLMUnavailable(last_reason, f"all models failed ({last_reason.value})", attempts_log)

    async def _try_model(self, model, body, out_model, task, key, case_id, attempts_log, prev_model, actor):
        """Returns LLMResult on success, else the terminal Reason for this model."""
        br = self.breakers[model]
        repaired = False
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if not await self.limiter.acquire():
                attempts_log.append({"model": model, "attempt": attempt, "reason": "local_rate_limit"})
                return Reason.LLM_RATE_LIMIT
            cid = await self._begin(case_id, model, task, attempt, key, prev_model if attempt == 1 else None)
            t0 = time.monotonic()
            reason: Reason | None = None
            status, data, headers = 0, {}, {}
            try:
                status, data, headers = await self._call(model, body)
            except httpx.TimeoutException:
                reason, status = Reason.LLM_TIMEOUT, 0
            ms = int((time.monotonic() - t0) * 1000)
            req_id = headers.get("x-goog-request-id") or headers.get("x-request-id")
            daily = False
            if reason is None and status != 200:
                reason, daily = classify(status, data)
            parsed = None
            if reason is None:
                parsed, reason = self._parse(data, out_model)
            usage = (data.get("usageMetadata") or {}) if isinstance(data, dict) else {}
            if reason is None and parsed is not None:
                await self._finish(cid, "ok", http=status, req_id=req_id, ms=ms, tin=usage.get("promptTokenCount", 0),
                                   tout=usage.get("candidatesTokenCount", 0))
                br.ok()
                if attempt > 1 or prev_model:
                    await audit.write(case_id, "agent", actor, "llm_call", "success",
                                      f"{task} ok on {model} (attempt {attempt})", model=model, refs={"prompt_hash": key})
                return LLMResult(parsed, model, attempts=attempt, tokens_in=usage.get("promptTokenCount", 0),
                                 tokens_out=usage.get("candidatesTokenCount", 0))
            # ---- failure path ----
            assert reason is not None
            msg = (data.get("error", {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else str(reason))
            await self._finish(cid, "error", err=reason, http=status or None, req_id=req_id, ms=ms)
            attempts_log.append({"model": model, "attempt": attempt, "reason": reason.value, "http": status})
            await audit.write(case_id, "agent", actor, "llm_call", "failure", f"{task} failed on {model}: {reason.value}",
                              reason=reason, model=model, refs={"prompt_hash": key},
                              error=audit.error_obj(reason.value, "gemini", status or None, req_id, msg))
            if br.fail():
                await audit.write(case_id, "system", "llm_gateway", "circuit_open", "degraded",
                                  f"circuit opened for {model} after {br.failures} failures", reason=Reason.INFRA_CIRCUIT_OPEN, model=model)
            if reason == Reason.LLM_SCHEMA_INVALID and not repaired and attempt < max_attempts:
                repaired = True  # one repair retry with error feedback
                body = {**body, "contents": body["contents"] + [{"role": "user", "parts": [{"text":
                        "Your previous reply did not match the required JSON schema. Reply again with ONLY valid JSON for the schema."}]}]}
                continue
            if reason in RETRYABLE and not daily and attempt < max_attempts:
                ra = headers.get("retry-after")
                delay = min(float(ra), 20.0) if ra and ra.replace(".", "", 1).isdigit() else min(2 ** (attempt - 1), 8.0)
                await asyncio.sleep(delay + random.uniform(0, 0.4))
                continue
            return reason
        return Reason.LLM_UNKNOWN

    @staticmethod
    def _parse(data: dict, out_model: type[BaseModel]) -> tuple[BaseModel | None, Reason | None]:
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        if fb:
            return None, Reason.LLM_SAFETY_BLOCK
        cands = data.get("candidates") or []
        if not cands:
            return None, Reason.LLM_EMPTY
        fin = cands[0].get("finishReason")
        if fin in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
            return None, Reason.LLM_SAFETY_BLOCK
        if fin == "MAX_TOKENS":
            return None, Reason.LLM_TRUNCATED
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            return None, Reason.LLM_EMPTY
        try:
            return out_model.model_validate(json.loads(text)), None
        except (ValueError, ValidationError):
            return None, Reason.LLM_SCHEMA_INVALID

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "models": [{"model": m, "circuit": self.breakers[m].state,
                                               "failures": self.breakers[m].failures} for m in self.models],
                "calls_today": self.calls_today, "chaos": chaos.mode if settings.enable_chaos else None}


gateway = Gateway()
