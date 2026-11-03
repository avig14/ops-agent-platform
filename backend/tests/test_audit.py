import asyncio

import pytest

from app import audit, db
from app.codes import Reason


async def verify(chain=None):
    async with db.audit_pool.connection() as c:
        cur = await c.execute("SELECT * FROM verify_audit_chain(%s)", (chain,))
        return await cur.fetchall()


async def test_chain_links_and_verifies():
    for i in range(3):
        await audit.write("CASE-A", "agent", "intake", "extract", "success", f"step {i}")
    rows = await verify("CASE-A")
    assert rows[0]["ok"] and rows[0]["checked"] == 3


async def test_concurrent_inserts_keep_chain_valid():
    await asyncio.gather(*[audit.write("CASE-C", "system", "w", "tick", "success", f"n{i}") for i in range(20)])
    rows = await verify("CASE-C")
    assert rows[0]["ok"], rows
    assert rows[0]["checked"] == 20


async def test_update_and_delete_blocked():
    await audit.write("CASE-U", "human", "u1", "approve", "success", "ok")
    async with db.audit_pool.connection() as c:
        with pytest.raises(Exception, match="append-only"):
            await c.execute("UPDATE audit_log SET summary='x' WHERE chain_key='CASE-U'")
        with pytest.raises(Exception, match="append-only"):
            await c.execute("DELETE FROM audit_log WHERE chain_key='CASE-U'")


async def test_failure_row_survives_rolled_back_business_txn():
    """Core review finding: a failure audit row must persist even when the business transaction rolls back."""
    with pytest.raises(RuntimeError):
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("INSERT INTO source_messages(id,channel,body,sha256) VALUES ('M-RB','manual','x','h')")
                await audit.write("CASE-RB", "human", "u2", "approve", "blocked", "403 not allowed",
                                  reason=Reason.POLICY_UNAUTHORIZED)
                raise RuntimeError("business txn fails")
    async with db.pool.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM source_messages WHERE id='M-RB'")).fetchone())["n"]
        a = (await (await conn.execute("SELECT count(*) AS n FROM audit_log WHERE chain_key='CASE-RB'")).fetchone())["n"]
    assert n == 0  # business change rolled back
    assert a == 1  # audit row kept


async def test_tamper_is_detected_and_scrub_hides_keys():
    await audit.write("CASE-T", "agent", "x", "a", "success", "one")
    await audit.write("CASE-T", "agent", "x", "a", "success", "two key=SECRET123 Bearer abc.def")
    async with db.audit_pool.connection() as c:
        s = (await (await c.execute("SELECT summary FROM audit_log WHERE chain_key='CASE-T' AND seq=2")).fetchone())["summary"]
        assert "SECRET123" not in s and "abc.def" not in s
        await c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        await c.execute("UPDATE audit_log SET summary='tampered' WHERE chain_key='CASE-T' AND seq=1")
        await c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")
    rows = await verify("CASE-T")
    assert not rows[0]["ok"] and rows[0]["first_bad_seq"] == 1
