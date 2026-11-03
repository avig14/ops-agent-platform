"""LLM gateway for evals: same retry / fallback / schema-repair code as production (it IS the production Gateway),
but the response cache is a JSON file instead of Postgres, so evals need no database and recordings travel with the repo.

Modes
  replay    cache only. A prompt with no recording raises LLMUnavailable (the harness reports it as "not recorded").
  live      cache first, then the real API for anything not yet recorded. Every success is written to the file at once,
            so an interrupted run (rate limit, closed laptop) resumes where it stopped.
  scripted  a fake transport supplies the model output (used to feed adversarial output to the gates). Nothing is cached.

The API key never reaches this file: it is added to the request header inside Gateway._http only.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..llm import Gateway, RateLimiter

FORMAT_VERSION = 1


class EvalGateway(Gateway):
    def __init__(self, *, mode: str = "replay", store: Path | None = None, models: tuple[str, ...] | None = None,
                 transport=None, rpm: int | None = None, refresh: bool = False):
        if mode not in ("replay", "live", "scripted"):
            raise ValueError(f"unknown eval gateway mode: {mode}")
        super().__init__(models=models, transport=None)
        self.kind = mode
        self.mode = "replay" if mode == "replay" else "live"       # Gateway only knows replay | live
        self.store = store
        self.entries: dict[str, dict] = {}
        self.calls: list[dict] = []
        raw = transport or self.transport                           # Gateway._http unless a script was given
        self.transport = self._timed(raw)
        if rpm:
            self.limiter = RateLimiter(rpm)
        if store is not None and not refresh and store.is_file():
            data = json.loads(store.read_text(encoding="utf-8"))
            if data.get("version") != FORMAT_VERSION:
                raise ValueError(f"{store} has version {data.get('version')}, expected {FORMAT_VERSION}")
            self.entries, self.calls = data.get("entries", {}), data.get("calls", [])

    # -- timing / token bookkeeping around the real transport --
    def _timed(self, raw):
        async def wrapped(model: str, body: dict):
            t0 = time.monotonic()
            status, data, headers = await raw(model, body)
            usage = (data.get("usageMetadata") or {}) if isinstance(data, dict) else {}
            self.calls.append({"model": model, "status": status, "ms": int((time.monotonic() - t0) * 1000),
                               "tokens_in": usage.get("promptTokenCount", 0), "tokens_out": usage.get("candidatesTokenCount", 0),
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            return status, data, headers
        return wrapped

    # -- file-backed cache (replaces the Postgres llm_cache) --
    async def _cache_get(self, key: str) -> dict | None:
        if self.kind == "scripted":
            return None
        return self.entries.get(key)

    async def _cache_put(self, key: str, model: str, data: dict) -> None:
        if self.kind == "scripted":
            return
        self.entries[key] = {"model": model, "response": data}
        self.save()

    # -- no database bookkeeping in evals --
    async def _begin(self, *a, **k):
        return None

    async def _finish(self, *a, **k):
        return None

    def save(self) -> None:
        if self.store is None or self.kind == "replay":
            return
        self.store.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": FORMAT_VERSION, "entries": self.entries, "calls": self.calls}
        tmp = self.store.with_suffix(self.store.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.store)                                  # atomic: a crash never leaves half a file
