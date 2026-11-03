"""Event-loop factory for uvicorn: `uvicorn app.main:app --loop app.loop:selector_loop`.

Why: psycopg's async mode cannot run on Windows' ProactorEventLoop. Since uvicorn 0.36 the loop comes from
its own factory (not the asyncio policy), so on Windows the policy set in app/__init__.py is not enough.
On Linux (Docker, Render) this is the same SelectorEventLoop that uvicorn would pick anyway.
"""

import asyncio


def selector_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()
