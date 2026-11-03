"""FastAPI app. Foundation routes: health, auth, admin (llm status, chaos, audit verify).

Every 4xx/5xx under /api is written to the audit log (independent connection) with the request_id that is also
returned in the error body, so "where did it fail" is answerable from either side.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import activity, approvals, audit, auth, clock, db, llm, reminders, routes_cases, routes_closure, routes_comms, routes_evals, routes_reports, sender
from .codes import Reason
from .config import settings
from .schemas import LoginIn, Role, UserOut
from .seed import DEMO_PASSWORD, USERS


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.env == "prod" and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("JWT_SECRET must be set in prod")
    await db.open_pools()
    every = settings.loop_interval_s
    loops = [asyncio.create_task(approvals.tick_loop(every)), asyncio.create_task(sender.tick_loop(every))]
    try:
        yield
    finally:
        for t in loops:
            t.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await db.close_pools()


app = FastAPI(title="Ops Agent Platform", version="0.1.0", lifespan=lifespan)
app.include_router(routes_cases.router)
app.include_router(routes_comms.router)
app.include_router(routes_closure.router)
app.include_router(routes_reports.router)
app.include_router(routes_evals.router)


def _rid() -> str:
    return audit.trace_id_var.get() or audit.new_id()


@app.middleware("http")
async def trace_and_audit_errors(request: Request, call_next):
    tid = audit.start_trace()
    if request.url.path.startswith("/api"):
        activity.touch()                                   # someone is using the app: background loops run at full pace
    response = await call_next(request)
    response.headers["X-Request-ID"] = tid
    if request.url.path.startswith("/api") and response.status_code >= 400:
        uid = getattr(request.state, "user_id", None)
        await audit.write(None, "human" if uid else "system", uid or "anonymous", "api_error", "failure",
                          f"{request.method} {request.url.path} -> {response.status_code}",
                          severity="error" if response.status_code >= 500 else "warn",
                          error=audit.error_obj(None, "api", response.status_code, tid, None),
                          refs={"path": request.url.path, "method": request.method})
    return response


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else "error", "request_id": _rid()},
                        status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    errs = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()][:5]
    return JSONResponse({"error": "validation failed", "detail": errs, "request_id": _rid()}, status_code=422)


@app.exception_handler(Exception)
async def crash(request: Request, exc: Exception):
    rid = _rid()
    await audit.write(None, "system", "api", "unhandled_exception", "failure",
                      f"{request.method} {request.url.path}: {type(exc).__name__}", severity="critical",
                      error=audit.error_obj(type(exc).__name__, "api", 500, rid, str(exc)))
    return JSONResponse({"error": "internal error", "request_id": rid}, status_code=500)


# ───────────── health ─────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    try:
        async with db.pool.connection() as c:
            await c.execute("SELECT 1")
        return {"status": "ready", "db": "ok"}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "not_ready", "db": "down", "detail": audit.scrub(repr(exc), 200)}, status_code=503)


# ───────────── auth ─────────────
@app.get("/api/auth/demo-users")
async def demo_users():
    """Public list for one-click demo login on the login page (synthetic accounts only)."""
    return {"password": DEMO_PASSWORD,
            "users": [{"email": e, "name": n, "role": r, "region": g} for _, e, n, r, g in USERS]}


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}|{body.email.lower()}"
    if auth.login_blocked(key):
        await audit.write(None, "system", "auth", "login", "blocked", f"rate limited: {body.email.lower()}",
                          reason=Reason.POLICY_UNAUTHORIZED)
        raise HTTPException(429, "too many attempts; wait a few minutes")
    async with db.pool.connection() as c:
        row = await (await c.execute("SELECT * FROM users WHERE lower(email)=%s AND active", (body.email.lower(),))).fetchone()
    if not row or not auth.verify_password(body.password, row["password_hash"]):
        auth.login_failed(key)
        await audit.write(None, "system", "auth", "login", "failure", f"bad credentials for {body.email.lower()}",
                          reason=Reason.POLICY_UNAUTHORIZED)
        raise HTTPException(401, "invalid email or password")
    auth.login_ok(key)
    response.set_cookie(auth.COOKIE, auth.make_token(row), httponly=True, samesite="lax",
                        secure=settings.env == "prod", max_age=int(auth.TOKEN_TTL.total_seconds()), path="/")
    await audit.write(None, "human", row["id"], "login", "success", f"{row['role']} logged in")
    return UserOut(id=row["id"], email=row["email"], name=row["name"], role=row["role"], region=row["region"])


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me", response_model=UserOut)
async def me(user: UserOut = Depends(auth.current_user)):
    return user


# ───────────── admin ─────────────
admin = Depends(auth.require_roles(Role.admin))


@app.get("/api/admin/llm")
async def llm_status(user: UserOut = admin):
    return llm.gateway.status()


@app.post("/api/admin/chaos")
async def set_chaos(body: dict, user: UserOut = admin):
    if not settings.enable_chaos:
        raise HTTPException(403, "chaos disabled in this environment")
    mode = body.get("mode")
    if mode not in (None, "402", "429", "503", "401", "timeout", "garbage"):
        raise HTTPException(422, "unknown chaos mode")
    llm.chaos.mode = mode
    await audit.write(None, "human", user.id, "chaos_toggle", "success", f"chaos mode set to {mode}")
    return llm.gateway.status()


@app.get("/api/admin/audit/verify")
async def verify_audit(chain: str | None = None, user: UserOut = admin):
    async with db.pool.connection() as c:
        rows = await (await c.execute("SELECT * FROM verify_audit_chain(%s)", (chain,))).fetchall()
    return {"ok": all(r["ok"] for r in rows), "chains": rows}


@app.get("/api/admin/clock")
async def get_clock(user: UserOut = admin):
    return {"enabled": settings.enable_clock_offset, "offset_seconds": clock.offset_s, "now": clock.now()}


@app.post("/api/admin/clock")
async def set_clock(body: dict, user: UserOut = admin):
    """Demo time travel for deadline logic only. Audit `ts` stays real time; `sim_ts` shows the simulated time."""
    if not settings.enable_clock_offset:
        raise HTTPException(403, "clock offset disabled in this environment")
    sec = body.get("offset_seconds")
    if not isinstance(sec, int) or isinstance(sec, bool) or not 0 <= sec <= 30 * 86400:
        raise HTTPException(422, "offset_seconds must be an integer between 0 and 2592000")
    clock.set_offset(sec)
    await audit.write(None, "human", user.id, "clock_offset", "success", f"business clock moved forward {sec}s (demo)")
    return {"enabled": True, "offset_seconds": clock.offset_s, "now": clock.now()}


@app.post("/api/admin/sweep")
async def run_sweep(user: UserOut = admin):
    return await approvals.sweep_deadlines()


@app.post("/api/admin/reminders")
async def run_reminders(user: UserOut = admin):
    """Run the NEEDS_INFO reminder sweep now (the background loop does this every few seconds)."""
    return await reminders.sweep()


@app.post("/api/admin/outbox/run")
async def run_outbox(user: UserOut = admin):
    return await sender.run_once()


@app.get("/api/admin/outbox/stats")
async def outbox_stats(user: UserOut = admin):
    return {**await sender.stats(), "fault": {"mode": sender.faults.mode, "remaining": sender.faults.remaining}}


@app.post("/api/admin/comms-chaos")
async def set_comms_chaos(body: dict, user: UserOut = admin):
    """Make the sandbox mail provider fail: fail (503, retried), bounce (permanent), timeout (delivered but no acknowledgement)."""
    if not settings.enable_chaos:
        raise HTTPException(403, "chaos disabled in this environment")
    mode, count = body.get("mode"), body.get("count")
    if mode not in (None, "fail", "bounce", "timeout"):
        raise HTTPException(422, "unknown comms chaos mode")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100):
        raise HTTPException(422, "count must be an integer between 1 and 100")
    sender.faults.set(mode, count)
    await audit.write(None, "human", user.id, "chaos_toggle", "success", f"mail provider fault set to {mode} (count {count})")
    return {"mode": sender.faults.mode, "remaining": sender.faults.remaining}


# ───────────── built frontend (production: one container serves the API and the UI) ─────────────
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST") or Path(__file__).resolve().parents[2] / "frontend" / "dist")


def mount_frontend(target: FastAPI, dist: Path) -> bool:
    """Serve the built React app: hashed files under /assets, any other non-API path gets index.html (client-side routes).
    Registered last so every API route wins. Returns False when there is no build (dev uses the Vite server instead)."""
    index = dist / "index.html"
    if not index.is_file():
        return False
    root = dist.resolve()
    if (dist / "assets").is_dir():
        target.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @target.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"error": "not found", "request_id": _rid()}, status_code=404)
        f = (root / path).resolve()
        if path and f.is_file() and root in f.parents:
            return FileResponse(f)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return True


mount_frontend(app, FRONTEND_DIST)
