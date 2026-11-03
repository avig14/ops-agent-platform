"""Data adapters over the (synthetic) inventory / PO / supplier systems, with fault injection for outage drills.

Every fetch is recorded in `external_calls` (kind='data') and failures write an audit row with a structured
`not_fetched` entry at fetch time, so "which fetch failed and what it affects" is never reconstructed later.
Faults: 'down' (unavailable), 'stale' (snapshot reported 3 days old), 'partial' (only the primary location returned).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import audit, db
from .codes import Reason
from .config import settings

STALE_AFTER_S = 24 * 3600


class Faults:
    modes: dict[str, str] = {}     # source -> down | stale | partial


faults = Faults()


@dataclass
class Fetch:
    data: Any = None
    status: str = "ok"             # ok | failed | stale | partial
    reason: Reason | None = None
    stale_age_s: int | None = None
    source: str = ""
    nf: dict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "stale", "partial") and self.data is not None


async def _record(case_id, source, op, params, status, reason, ms, stale_age=None) -> None:
    ph = audit.sha256_text(repr(params))
    try:
        async with db.audit_pool.connection() as c:
            await c.execute(
                "INSERT INTO external_calls(case_id,trace_id,span_id,kind,target,operation,attempt,status,error_class,latency_ms,"
                "params_hash,stale_age_s) VALUES (%s,%s,%s,'data',%s,%s,1,%s,%s,%s,%s,%s)",
                (case_id, audit.trace_id_var.get(), audit.span_id_var.get(), source, op, status,
                 reason.value if reason else None, ms, ph, stale_age))
    except Exception:  # noqa: BLE001
        pass


async def _fetch(case_id: str | None, source: str, op: str, params: dict, query, affects: list[str],
                 fallback: str = "none") -> Fetch:
    t0 = time.monotonic()
    mode = faults.modes.get(source) if settings.enable_chaos else None
    ph = audit.sha256_text(repr(params))
    if mode == "down":
        ms = int((time.monotonic() - t0) * 1000)
        await _record(case_id, source, op, params, "error", Reason.DATA_UNAVAILABLE, ms)
        nf = audit.not_fetched(source, Reason.DATA_UNAVAILABLE, op, ph, 1, fallback, None, affects)
        await audit.write(case_id, "system", "data_adapter", "data_fetch", "failure", f"{source}.{op} unavailable",
                          reason=Reason.DATA_UNAVAILABLE, severity="error",
                          error=audit.error_obj(Reason.DATA_UNAVAILABLE.value, source, 503, None, "source unavailable"), nf=[nf])
        return Fetch(None, "failed", Reason.DATA_UNAVAILABLE, None, source, nf)
    try:
        data = await query()
    except Exception as exc:  # noqa: BLE001
        ms = int((time.monotonic() - t0) * 1000)
        await _record(case_id, source, op, params, "error", Reason.DATA_UNAVAILABLE, ms)
        nf = audit.not_fetched(source, Reason.DATA_UNAVAILABLE, op, ph, 1, fallback, None, affects)
        await audit.write(case_id, "system", "data_adapter", "data_fetch", "failure", f"{source}.{op} error: {type(exc).__name__}",
                          reason=Reason.DATA_UNAVAILABLE, severity="error",
                          error=audit.error_obj(Reason.DATA_UNAVAILABLE.value, source, None, None, str(exc)), nf=[nf])
        return Fetch(None, "failed", Reason.DATA_UNAVAILABLE, None, source, nf)
    ms = int((time.monotonic() - t0) * 1000)
    status, reason, age = "ok", None, None
    if mode == "stale":
        status, reason, age = "stale", Reason.DATA_STALE, 3 * 86400
    elif mode == "partial":
        status, reason = "partial", Reason.DATA_PARTIAL
    f = Fetch(data, status, reason, age, source)
    if status != "ok":
        f.nf = audit.not_fetched(source, reason, op, ph, 1, "used_with_warning", age, affects)
        await audit.write(case_id, "system", "data_adapter", "data_fetch", "degraded", f"{source}.{op} returned {status} data",
                          reason=reason, nf=[f.nf])
    await _record(case_id, source, op, params, status if status != "ok" else "ok", reason, ms, age)
    return f


# ───────────── reference resolution (master data, not fault-injected) ─────────────
async def resolve_store(location: str | None) -> dict | None:
    if not location:
        return None
    loc = location.strip().lower()
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM stores")).fetchall()
    for r in rows:
        if loc == r["city"].lower() or loc == r["id"].lower() or loc in r["name"].lower() or r["city"].lower() in loc:
            return r
    return None


async def resolve_item(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip().lower()
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM items ORDER BY id")).fetchall()
    for r in rows:  # exact then keyword containment ("display units" -> Display unit (standard))
        if t == r["id"].lower() or t == r["name"].lower():
            return r
    words = [w.rstrip("s") for w in t.split() if w not in ("units", "unit", "the", "of")]
    for r in rows:
        if words and all(w in r["name"].lower() for w in words) and "express" not in r["name"].lower():
            return r
    return None


async def resolve_supplier(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip().lower()
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM suppliers")).fetchall()
    for r in rows:
        if t == r["id"].lower() or t in r["name"].lower() or r["name"].lower().split()[0] in t:
            return r
    return None


# ───────────── fault-injectable system fetches ─────────────
async def get_inventory(case_id, item_id: str, region: str | None) -> Fetch:
    async def q():
        async with db.pool.connection() as c:
            rows = await (await c.execute(
                "SELECT DISTINCT ON (location) location, on_hand, snapshot_at, "
                "EXTRACT(EPOCH FROM (now()-snapshot_at))::int AS age_s FROM inventory_snapshots WHERE item_id=%s "
                "ORDER BY location, snapshot_at DESC", (item_id,))).fetchall()
        if faults.modes.get("inventory") == "partial" and settings.enable_chaos:
            primary = f"Warehouse-{region}" if region else None
            rows = [r for r in rows if r["location"] == primary]
        return rows
    f = await _fetch(case_id, "inventory", "snapshots", {"item": item_id, "region": region}, q,
                     ["shortfall", "transfer options", "recommendation"], fallback="email_stated_quantity")
    if f.usable and f.status == "ok":
        oldest = max((r["age_s"] for r in f.data), default=0)
        if oldest > STALE_AFTER_S:
            f.status, f.reason, f.stale_age_s = "stale", Reason.DATA_STALE, oldest
            f.nf = audit.not_fetched("inventory", Reason.DATA_STALE, "snapshots", None, 1, "used_with_warning", oldest,
                                     ["shortfall"])
    return f


async def get_po(case_id, store_id: str, item_id: str) -> Fetch:
    async def q():
        async with db.pool.connection() as c:
            return await (await c.execute(
                "SELECT po.*, s.name AS supplier_name, s.lead_time_days, s.reliability FROM purchase_orders po "
                "JOIN suppliers s ON s.id=po.supplier_id WHERE po.destination_store_id=%s AND po.item_id=%s AND po.status='open' "
                "ORDER BY po.promised_date", (store_id, item_id))).fetchall()
    return await _fetch(case_id, "purchase_orders", "open_pos", {"store": store_id, "item": item_id}, q,
                        ["supplier identity", "ordered quantity"], fallback="email_stated_values")


async def get_alternatives(case_id, item_id: str, item_name: str) -> Fetch:
    """Alternate supply: items in the same family (same first word of the name) from other suppliers + their stock."""
    family = item_name.split()[0] + "%"

    async def q():
        async with db.pool.connection() as c:
            return await (await c.execute(
                "SELECT i.id, i.name, i.unit_cost, i.supplier_id, s.name AS supplier_name, s.lead_time_days, s.reliability, "
                "COALESCE((SELECT sum(on_hand) FROM inventory_snapshots x WHERE x.item_id=i.id),0) AS stock "
                "FROM items i JOIN suppliers s ON s.id=i.supplier_id WHERE i.id<>%s AND i.name LIKE %s", (item_id, family))).fetchall()
    return await _fetch(case_id, "suppliers", "alternatives", {"item": item_id}, q, ["alternate supplier option"],
                        fallback="no_alternate_option")
