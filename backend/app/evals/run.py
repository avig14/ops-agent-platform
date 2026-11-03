"""Eval runner.  python -m app.evals.run [--live] [--refresh] [--rpm N]

replay (default)  re-scores the recorded model output. Zero API calls. Safe to run anywhere, any time.
--live            calls Gemini ONLY for prompts that are not yet in the recording, saves each answer at once (resumable).
--refresh         ignore the recording and call the API for everything (burns quota: avoid).

Writes results/latest.json. The Admin page reads that file; nothing is computed in the browser.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import extract
from . import datasets as ds
from . import suites
from .gateway import EvalGateway

HERE = Path(__file__).parent
RESULTS = HERE / "results"
RECORDING = RESULTS / "recording.json"
LATEST = RESULTS / "latest.json"

LIMITS = [
    "Small, synthetic, hand-written test sets (26 emails, 112 injection attacks built from 14 payloads x 8 wrappers). Intervals are wide on purpose.",
    "The injection payloads in the hold-out split were written before any result was seen and never tuned against. The scanner catches 56% of them; the rest rely on the other layers.",
    "The scripted injection test assumes the worst case: a model that obeys the attacker. The real-model probe is only 6 payloads, so it shows direction, not a rate.",
    "Confidence scores are heuristics (agreement of independent signals), not probabilities. Calibration is reported, not claimed.",
    "Gold labels were written by the same team that built the system. No outside annotator, no inter-rater check.",
    "Recommendation and risk wording quality is not measured here. Only the deterministic risk rules and the policy ladder are tested.",
    "Hinglish and other non-English payloads are not detected by the scanner.",
    "The numbers/quote gate checks that a quote exists in the email, not which issue it belongs to (known limitation, test G14).",
    "Latency and token counts come from the free tier on one day. They are indicative, not an SLA.",
]


def prompt_hash() -> str:
    return hashlib.sha256(extract.INTAKE_SYSTEM.encode("utf-8")).hexdigest()[:12]


def _gateway(live: bool, refresh: bool, rpm: int) -> EvalGateway:
    return EvalGateway(mode="live" if live else "replay", store=RECORDING, rpm=rpm if live else None, refresh=refresh and live)


async def run(live: bool = False, refresh: bool = False, rpm: int = 5) -> dict:
    if live and not os.environ.get("GEMINI_API_KEY"):
        from ..config import settings
        if not settings.gemini_api_key:
            raise SystemExit("live run needs GEMINI_API_KEY in the environment or .env")
    gw = _gateway(live, refresh, rpm)
    models_in_recording = sorted({v.get("model", "?") for v in gw.entries.values()})
    out: dict = {"scripted": {}, "real_model": {}}
    try:
        print("injection (scripted worst case) ...", flush=True)
        out["scripted"]["injection"] = await suites.injection_suite()
        out["scripted"]["false_positives"] = await suites.false_positive_suite()
        print("output gates ...", flush=True)
        out["scripted"]["gates"] = await suites.gate_suite()
        print("policy / risk / metamorphic ...", flush=True)
        out["scripted"]["policy"] = suites.policy_suite()
        out["scripted"]["risk"] = suites.risk_suite()
        out["scripted"]["metamorphic"] = await asyncio.to_thread(suites.metamorphic_suite)
        print("extraction (rules, llm, llm+second) ...", flush=True)
        out["real_model"]["extraction"] = await suites.extraction_suite(gw)
        print("real-model injection probe ...", flush=True)
        out["real_model"]["injection_probe"] = await suites.llm_probe_suite(gw)
    finally:
        gw.save()                                                  # a crash or Ctrl-C keeps every answer already paid for
    out["real_model"]["cost_latency"] = suites.cost_latency(gw)
    _, dataset_hash = ds.load_extraction()
    out["provenance"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_mode": "live" if live else "replay",
        "dataset_hash": dataset_hash,
        "prompt_hash": prompt_hash(),
        "models_in_recording": sorted({v.get("model", "?") for v in gw.entries.values()}) or models_in_recording,
        "recorded_responses": len(gw.entries),
        "api_calls_this_run": len(gw.calls) if live else 0,
        "git_sha": None,                                           # the project is not a git repo yet; filled once it is
        "python": platform.python_version(),
    }
    out["limits"] = LIMITS
    return out


def headline(r: dict) -> list[str]:
    s, m = r["scripted"], r["real_model"]
    inj = s["injection"]
    lines = [f"policy {s['policy']['passed']}/{s['policy']['checks']}  risk {s['risk']['passed']}/{s['risk']['checks']}",
             f"injection dev detect {inj['dev']['detected']['rate']:.0%}  holdout detect {inj['holdout']['detected']['rate']:.0%}",
             f"benign false positives {s['false_positives']['false_positives']['k']}/{s['false_positives']['false_positives']['n']}"]
    for mode, v in m["extraction"]["modes"].items():
        o = v["overall"]
        lines.append(f"extraction {mode}: scored {v['scored']} not_scored {len(v['not_scored'])} P {o['precision']} R {o['recall']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evals.run")
    ap.add_argument("--live", action="store_true", help="call Gemini for prompts not yet recorded")
    ap.add_argument("--refresh", action="store_true", help="with --live: ignore the recording")
    ap.add_argument("--rpm", type=int, default=5, help="live requests per minute (free tier)")
    a = ap.parse_args(argv)
    if sys.platform == "win32":
        from ..loop import selector_loop
        loop = selector_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run(a.live, a.refresh, a.rpm))
    else:
        result = asyncio.run(run(a.live, a.refresh, a.rpm))
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = LATEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, LATEST)
    for ln in headline(result):
        print(ln)
    print(f"wrote {LATEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
