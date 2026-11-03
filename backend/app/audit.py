"""Audit writer.

Rules (see plan section 4):
  * Failure / attempt rows go through `audit_pool` (autocommit, own connection) so they survive a rolled-back
    business transaction. `write()` is for those.
  * A success row that must commit atomically with a state change uses `write_in_txn(conn, ...)`.
  * If the audit insert itself fails, emit an AUDIT_WRITE_FAILED JSON line to stdout and a spill file, never raise
    into the business flow.
  * Never put raw email bodies or prompts in an audit row: pass ids + hashes in `refs`.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import clock, db
from .codes import Reason, category
from .config import settings

SPILL = Path(__file__).resolve().parents[1] / "audit_spill.jsonl"

# trace context for correlation (trace_id per request/case run, span_id per step)
trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)

_SECRET_RX = re.compile(r"(key=)[^&\s\"']+|(x-goog-api-key['\":\s]+)[^\s,\"'}]+|(Bearer\s+)[A-Za-z0-9._\-]+", re.I)


def scrub(text: str | None, limit: int = 500) -> str | None:
    """Remove API keys / bearer tokens from any message before it is stored or logged."""
    if text is None:
        return None
    out = _SECRET_RX.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "[REDACTED]", str(text))
    if settings.gemini_api_key:
        out = out.replace(settings.gemini_api_key, "[REDACTED]")
    return out[:limit]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def start_trace() -> str:
    tid = new_id()
    trace_id_var.set(tid)
    span_id_var.set(None)
    return tid


def new_span() -> tuple[str, str | None]:
    """Returns (span_id, parent_span_id) and makes the new span current."""
    parent = span_id_var.get()
    sid = new_id()
    span_id_var.set(sid)
    return sid, parent


def error_obj(error_class: str | None = None, provider: str | None = None, http_status: int | None = None,
              request_id: str | None = None, message: str | None = None) -> dict[str, Any]:
    return {"class": error_class, "category": category(error_class), "provider": provider, "http_status": http_status,
            "request_id": request_id, "message": scrub(message)}


def not_fetched(source: str, reason_code: Reason | str, operation: str | None = None, params_hash: str | None = None,
                attempts: int = 1, fallback_used: str | None = None, stale_age_s: int | None = None,
                affects: list[str] | None = None) -> dict[str, Any]:
    """Structured record of an input that could not be obtained. Written at fetch time."""
    return {"source": source, "operation": operation, "params_hash": params_hash,
            "reason_code": reason_code.value if isinstance(reason_code, Reason) else reason_code,
            "attempts": attempts, "fallback_used": fallback_used, "stale_age_s": stale_age_s, "affects": affects or []}


_COLUMNS = ("chain_key", "actor_type", "actor_id", "action", "outcome", "severity", "reason_code", "summary", "diff",
            "refs", "error", "not_fetched", "model", "prompt_version", "trace_id", "span_id", "parent_span_id",
            "sim_ts", "clock_offset_active")


def _row(chain_key: str | None, actor_type: str, actor_id: str, action: str, outcome: str, summary: str,
         severity: str | None, reason: Reason | str | None, diff: Any, refs: Any, error: Any, nf: Any,
         model: str | None, prompt_version: str | None) -> tuple:
    off = clock.offset_s
    reason_s = reason.value if isinstance(reason, Reason) else reason
    sev = severity or ("info" if outcome in ("success", "attempt", "skipped") else "warn")
    j = lambda v: Jsonb(v) if v is not None else None  # noqa: E731
    sim = datetime.now(timezone.utc) + timedelta(seconds=off) if off else None
    return (chain_key or "SYSTEM", actor_type, actor_id, action, outcome, sev, reason_s, scrub(summary, 1000), j(diff),
            j(refs), j(error), j(nf), model, prompt_version, trace_id_var.get(), span_id_var.get(), None,
            sim, off != 0)


_SQL = (f"INSERT INTO audit_log ({', '.join(_COLUMNS)}) VALUES ({', '.join(['%s'] * len(_COLUMNS))}) "
        "RETURNING seq, row_hash")


def _spill(kind: str, row: tuple, err: Exception) -> None:
    line = {"event": "AUDIT_WRITE_FAILED", "reason": Reason.INFRA_AUDIT_WRITE_FAILED.value, "ts": time.time(),
            "kind": kind, "error": scrub(repr(err)), "row": [None if v is None else str(v) for v in row]}
    print(json.dumps(line), file=sys.stderr, flush=True)
    try:
        with SPILL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass


async def write(chain_key: str | None, actor_type: str, actor_id: str, action: str, outcome: str, summary: str, *,
                severity: str | None = None, reason: Reason | str | None = None, diff: Any = None, refs: Any = None,
                error: Any = None, nf: Any = None, model: str | None = None,
                prompt_version: str | None = None) -> dict | None:
    """Independent autocommit write. Never raises."""
    row = _row(chain_key, actor_type, actor_id, action, outcome, summary, severity, reason, diff, refs, error, nf,
               model, prompt_version)
    try:
        assert db.audit_pool is not None, "audit pool not open"
        async with db.audit_pool.connection() as conn:
            cur = await conn.execute(_SQL, row)
            return await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - audit must not break the business flow
        _spill("independent", row, exc)
        return None


async def write_in_txn(conn, chain_key: str | None, actor_type: str, actor_id: str, action: str, outcome: str,
                       summary: str, *, severity: str | None = None, reason: Reason | str | None = None,
                       diff: Any = None, refs: Any = None, error: Any = None, nf: Any = None,
                       model: str | None = None, prompt_version: str | None = None) -> dict:
    """Write inside the caller's transaction: commits or rolls back together with the state change."""
    row = _row(chain_key, actor_type, actor_id, action, outcome, summary, severity, reason, diff, refs, error, nf,
               model, prompt_version)
    cur = await conn.execute(_SQL, row)
    return await cur.fetchone()
