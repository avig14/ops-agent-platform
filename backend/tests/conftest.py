"""Test DB: a throwaway database `ops_test` on the dev Postgres, migrated fresh per session."""
import os

import psycopg
import pytest
import pytest_asyncio

ADMIN_URL = os.environ.get("TEST_ADMIN_URL", "postgresql://ops:ops@localhost:5433/postgres")
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://ops:ops@localhost:5433/ops_test")
os.environ["DATABASE_URL"] = TEST_URL  # must be set before app.config is imported


@pytest.fixture(scope="session", autouse=True)
def _create_test_db():
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS ops_test WITH (FORCE)")
        c.execute("CREATE DATABASE ops_test")
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _pools(_create_test_db):
    from app import db
    from app.migrate import migrate

    await migrate(TEST_URL)
    await db.open_pools()
    yield
    await db.close_pools()
