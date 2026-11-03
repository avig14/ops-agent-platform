"""Auth: bcrypt passwords, JWT in httpOnly cookie, RBAC dependencies, region scoping, login rate limit.

Every denied request is audited through the independent audit connection (POLICY_UNAUTHORIZED), so a 401/403
survives even if the surrounding request transaction rolls back.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from . import audit, db
from .codes import Reason
from .config import settings
from .schemas import Role, UserOut

COOKIE = "session"
TOKEN_TTL = timedelta(hours=8)
ALGO = "HS256"
MAX_PW_BYTES = 72  # bcrypt 5.x raises ValueError above this instead of truncating
BCRYPT_ROUNDS = 10


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8")[:MAX_PW_BYTES], bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    raw = pw.encode("utf-8")
    if len(raw) > MAX_PW_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode())
    except ValueError:
        return False


def make_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user["id"], "role": user["role"], "iat": now, "exp": now + TOKEN_TTL,
                       "jti": uuid.uuid4().hex}, settings.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGO], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None


# ── login rate limit (in-memory, per ip+email; enough for a single-instance demo) ──
_FAILS: dict[str, deque[float]] = defaultdict(deque)
WINDOW_S, MAX_FAILS = 300, 5


def login_blocked(key: str) -> bool:
    q = _FAILS[key]
    now = time.monotonic()
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


def login_failed(key: str) -> None:
    _FAILS[key].append(time.monotonic())


def login_ok(key: str) -> None:
    _FAILS.pop(key, None)


async def get_user_row(user_id: str) -> dict | None:
    async with db.pool.connection() as c:
        return await (await c.execute("SELECT id,email,name,role,region,active FROM users WHERE id=%s", (user_id,))).fetchone()


async def current_user(request: Request) -> UserOut:
    token = request.cookies.get(COOKIE)
    claims = decode_token(token) if token else None
    row = await get_user_row(claims["sub"]) if claims else None
    if not row or not row["active"]:
        raise HTTPException(401, "not authenticated")
    request.state.user_id = row["id"]
    return UserOut(id=row["id"], email=row["email"], name=row["name"], role=row["role"], region=row["region"])


def require_roles(*roles: Role):
    allowed = {r.value for r in roles}

    async def dep(request: Request, user: UserOut = Depends(current_user)) -> UserOut:
        if user.role.value not in allowed:
            await audit.write(None, "human", user.id, "access_denied", "blocked",
                              f"{user.role.value} tried {request.method} {request.url.path}; needs one of {sorted(allowed)}",
                              reason=Reason.POLICY_UNAUTHORIZED, refs={"path": request.url.path})
            raise HTTPException(403, "role not permitted for this action")
        return user

    return dep


def can_see_region(user: UserOut, region: str | None) -> bool:
    """Admins, finance and the service identity see everything; others only their own region."""
    if user.role in (Role.admin, Role.finance_director, Role.service):
        return True
    return region is None or user.region == region
