"""Postgres access. Two pools on purpose:
  * `pool`  - business transactions (commit/rollback together with state changes)
  * `audit_pool` - autocommit, used by the audit writer so failure rows survive a rolled-back business txn.
Use the Neon DIRECT (non-pooled) connection string; psycopg server-side prepared statements need it.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

pool: AsyncConnectionPool | None = None
audit_pool: AsyncConnectionPool | None = None


def _make(min_size: int, max_size: int, autocommit: bool) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        check=AsyncConnectionPool.check_connection,  # drops stale conns (e.g. after Neon scale-to-zero)
        kwargs={"autocommit": autocommit, "row_factory": dict_row},
    )


async def open_pools() -> None:
    global pool, audit_pool
    pool = _make(1, 6, autocommit=False)
    audit_pool = _make(1, 3, autocommit=True)
    await pool.open(wait=True, timeout=30)
    await audit_pool.open(wait=True, timeout=30)


async def close_pools() -> None:
    for p in (pool, audit_pool):
        if p is not None:
            await p.close()
