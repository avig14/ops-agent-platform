"""Tiny migration runner: applies backend/migrations/*.sql in name order, once each.
Usage: python -m app.migrate
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg

from .config import settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


async def migrate(database_url: str | None = None) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(database_url or settings.database_url, autocommit=True) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        # one migrator at a time (several app instances may start together)
        await conn.execute("SELECT pg_advisory_lock(727274)")
        try:
            done = {r[0] for r in await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8-sig"))
                    await conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727274)")
    return applied


if __name__ == "__main__":
    print("applied:", asyncio.run(migrate()) or "nothing to do")
