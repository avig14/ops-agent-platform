"""Settings. Reads real env vars first, then a .env file (trims BOM, quotes, spaces around '=')."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for candidate in (Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip("\"'").strip()
        break
    return values


_DOTENV = _load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name) or _DOTENV.get(name) or default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _get("DATABASE_URL", "postgresql://ops:ops@localhost:5433/ops"))
    gemini_api_key: str = field(default_factory=lambda: _get("GEMINI_API_KEY"))
    gemini_models: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip()
            for m in _get("GEMINI_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest").split(",")
            if m.strip()
        )
    )
    jwt_secret: str = field(default_factory=lambda: _get("JWT_SECRET", "dev-only-secret-change-me-in-prod-0123456789"))
    env: str = field(default_factory=lambda: _get("APP_ENV", "dev"))
    # Demo-only switches. Both must be explicitly enabled.
    enable_chaos: bool = field(default_factory=lambda: _get("ENABLE_CHAOS", "1") == "1")
    enable_clock_offset: bool = field(default_factory=lambda: _get("ENABLE_CLOCK_OFFSET", "1") == "1")
    llm_daily_cap: int = field(default_factory=lambda: int(_get("LLM_DAILY_CAP", "300")))
    llm_rpm: int = field(default_factory=lambda: int(_get("LLM_RPM", "5")))
    # Seconds between background sweeps (deadlines, outbox, reminders) while the app is in use, and while it is idle.
    # Idle > active lets a hosted free Postgres sleep (see app/activity.py). Default idle = active = old behaviour.
    loop_interval_s: float = field(default_factory=lambda: max(1.0, float(_get("LOOP_INTERVAL_S", "5"))))
    loop_idle_s: float = field(default_factory=lambda: max(1.0, float(_get("LOOP_IDLE_S", "5"))))


settings = Settings()
