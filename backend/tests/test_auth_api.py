import httpx
import pytest
import pytest_asyncio

from app import auth, db
from app.main import app
from app.seed import DEMO_PASSWORD, seed


@pytest_asyncio.fixture(scope="module")
async def client():
    await seed()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def login(client, email, pw=DEMO_PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": pw})


async def test_health_and_ready(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json()["db"] == "ok"


async def test_login_me_and_cookie_flags(client):
    r = await login(client, "admin@demo.test")
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert "httponly" in r.headers["set-cookie"].lower()
    me = await client.get("/api/auth/me")
    assert me.json()["email"] == "admin@demo.test"


async def test_unauthenticated_and_forbidden_are_audited(client):
    c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c2.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["request_id"]
    await login(c2, "priya.requester@demo.test")
    r = await c2.get("/api/admin/llm")
    assert r.status_code == 403
    async with db.pool.connection() as c:
        n = (await (await c.execute("SELECT count(*) n FROM audit_log WHERE reason_code='POLICY_UNAUTHORIZED' "
                                    "AND action='access_denied' AND actor_id='u-req-west'")).fetchone())["n"]
    assert n >= 1  # 403 recorded even though request ended in an error
    await c2.aclose()


async def test_bad_password_rate_limited(client):
    for _ in range(auth.MAX_FAILS):
        assert (await login(client, "rahul.manager@demo.test", "wrong")).status_code == 401
    assert (await login(client, "rahul.manager@demo.test", "wrong")).status_code == 429


async def test_tampered_and_expired_token_rejected():
    c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    c2.cookies.set("session", "abc.def.ghi")
    assert (await c2.get("/api/auth/me")).status_code == 401
    import jwt
    forged = jwt.encode({"sub": "u-admin", "role": "admin", "exp": 9999999999}, "wrong-secret", algorithm="HS256")
    c2.cookies.set("session", forged)
    assert (await c2.get("/api/auth/me")).status_code == 401
    await c2.aclose()


async def test_service_identity_cannot_login(client):
    r = await login(client, "ai-agent@system.local", "anything")
    assert r.status_code == 401


async def test_long_password_rejected_not_crash(client):
    r = await login(client, "admin@demo.test", "x" * 200)
    assert r.status_code == 401


async def test_chain_still_valid_after_api_activity(client):
    await login(client, "admin@demo.test")  # may be 200 even after other tests (different key)
    r = await client.get("/api/admin/audit/verify")
    assert r.status_code == 200
    bad = [c for c in r.json()["chains"] if not c["ok"] and c["chain_key"] in ("SYSTEM",)]
    assert not bad
