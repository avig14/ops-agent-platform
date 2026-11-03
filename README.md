# Ops Agent Platform

An agentic operations assistant for retail ops. It reads an email or a manual request, extracts the facts, assesses
urgency and risk, recommends an action, routes it for approval, tracks it to closure, drafts stakeholder messages and keeps
an audit trail of every AI and human action.

**Principle: the LLM interprets, code decides.** The model never approves, never sends, and never changes policy.
Approval authority, thresholds, separation of duties and state transitions are plain, tested code.

All data in this repo is synthetic. Do not paste real customer or supplier data: the free Gemini tier may use inputs for
training.

## Demo

- Live app: _added at deploy time_
- Demo login: any account below, password `Demo@2026`. The login page also has one-click buttons.

| Account | Role |
| --- | --- |
| priya.requester@demo.test, karthik.requester@demo.test | Requester (store ops) |
| rahul.manager@demo.test, sneha.manager@demo.test | Ops manager (approves up to INR 50,000) |
| anita.regional@demo.test | Regional head (up to INR 5,00,000) |
| vikram.finance@demo.test | Finance director (above INR 5,00,000, together with the regional head) |
| admin@demo.test | Platform admin (system health, evaluations, chaos and clock controls) |

Quickest tour: sign in as Priya, open **New request**, load the sample email (or any of the 16 scenarios), then sign in as
the manager or regional head to approve. Sign in as admin to see audit verification, failures and the evaluation panel.

## Run it locally

Needs Python 3.11, Node 24, Docker.

```bash
docker compose up -d db                      # Postgres 16 on port 5433
cp .env.example .env                         # add GEMINI_API_KEY (optional, see "No API key")
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m app.migrate && .venv/Scripts/python -m app.seed
.venv/Scripts/uvicorn app.main:app --port 8000 --loop app.loop:selector_loop
cd ../frontend && npm ci && npm run dev      # http://localhost:5173
```

`--loop app.loop:selector_loop` is needed on Windows (psycopg async). It is harmless elsewhere.

### One container (what production runs)

```bash
docker build -t ops-agent-platform .
docker run -p 8080:8000 -e DATABASE_URL=postgresql://... -e JWT_SECRET=<32+ random bytes> ops-agent-platform
python backend/scripts/smoke.py http://localhost:8080 --case
```

The image serves the API and the built UI. On start it applies migrations and seeds the demo users. `smoke.py` is a
deployment check: health, SPA deep links, login, RBAC, audit-chain verification, evaluation data, and (with `--case`) a full
case from email to audit trail.

### No API key

The app still works without `GEMINI_API_KEY`. Extraction falls back to a rule-based extractor, the case is marked
**degraded**, no spend is recommended, and a person must review. This is the same path used when Gemini is down.

### Tests

```bash
cd backend && .venv/Scripts/python -m pytest        # 392 tests, needs the dev Postgres
cd frontend && npm run typecheck && npm run lint && npm test && npm run build
```

## How it works

```
React (Vite, TypeScript)  ->  FastAPI  ->  Postgres
                              |- Orchestrator: explicit state machine (cases.status is the only source of truth)
                              |- Intake (LLM, quarantined) -> Validator (code) -> Risk (rules + LLM) -> Recommend (LLM + tools)
                              |- Policy engine (code only) -> Approvals / escalation ladder -> Outbox sender -> Closure
                              |- LLM gateway: retry, model fallback chain, circuit breaker, rate and daily budget, cache, chaos
                              `- Audit log: per-case SHA-256 hash chain written by a database trigger
```

**Agent coordination and shared context.** Each case has one typed context. Agents read only the fields they declare and
write typed messages, so the Trace tab shows who passed what to whom. The orchestrator is code, not an LLM.

**When human approval is mandatory.** Any spend, high or critical risk, low confidence, a prompt-injection flag, degraded
mode, disagreeing agents, unresolved data conflicts, and any change after approval started. Every decision records which
rule fired and why. A requester cannot approve their own request, and the AI identity has no approve permission.

**How confidence is determined.** A field must first be grounded (its quote exists in the source) and valid (the supplier,
store and item exist). Then: 0.4 base, plus 0.3 if a second model agrees, plus 0.3 if the rule extractor agrees. Bands: high
at 0.80 or above, confirm at 0.55 or above, otherwise the AI abstains. Confidence only adds human review; it never removes
it. These weights are heuristics, not calibrated probabilities.

**Unsupported or incorrect AI output.** Gates run after every LLM call: schema check with one repair retry, grounding,
numeric recompute by code, referential check, action allow-list, comms number allow-list with no internal cost leakage,
and closing summaries limited to audited facts. A failed gate falls back to the next model, then to a person.

**Monitoring and audit.** Every attempt and outcome (LLM, data, mail) is written through a separate connection, so
failures survive a rolled-back transaction. Rows carry an AI, human or system badge, a trace id, a reason code and what was
not fetched. A trigger blocks UPDATE and DELETE, and `verify_audit_chain` checks hash links and sequence gaps.
This is tamper-evident, not tamper-proof.

**Prompt injection.** Sanitize, deterministic scanner, quarantined extraction with no tools, drop fields whose evidence sits
inside a flagged span, then allow-listed actions and the policy engine.

### Edge cases

The 16-scenario library (in the app) covers prompt injection, missing information, conflicting information, duplicates,
incorrect extraction, low confidence, approval delay and escalation, rejection and re-plan, changing circumstances,
LLM and data outages, unauthorized access, communication failure, contradicting agents, cancellation, and unclear or
multi-issue requests. Admin has chaos switches (LLM 402/429/503/timeout/garbage, mail faults) and a clock offset to demo
approval deadlines.

## Evaluation

`python -m app.evals.run` replays recorded model answers with zero API calls. `--live` calls Gemini and re-records.
Results are in `backend/app/evals/results/latest.json` and on the Admin page. Recorded run:

| Measure | Result |
| --- | --- |
| Field extraction, rules only | precision 0.974, recall 0.723 |
| Field extraction, LLM + second opinion | precision 0.994, recall 0.987 (26 of 26 cases scored) |
| Hallucinated fields | 1 of 70 |
| Calibration (Brier, lower is better) | 0.016 |
| Real-model injection probe | flagged 6 of 6, poison survived 0 of 6 |
| Latency per LLM call | p50 17.5 s, p95 25.5 s |

## Known limits

- Eval sets are small, synthetic and hand-written. Treat the numbers as evidence of behaviour, not as benchmarks.
- The injection scanner caught 56% of the holdout attacks. The remaining ones were stopped by quarantine and policy, not
  by the scanner. Hinglish payloads are not detected, and cross-issue quote borrowing passes.
- Under attack, extraction utility on the tuned set is 88%: dropping tainted spans costs some clean fields.
- Recorded eval answers mix `gemini-3.5-flash` and `gemini-3.5-flash-lite`: the first model often returned 429 or 503.
- Risk and recommendation quality are checked by deterministic rules, not by an LLM judge.
- The audit table is protected by a trigger. A separate INSERT-only database role is not created.
- A message already in `sending` cannot be recalled if its plan is cancelled. Closure checklist items cannot be un-ticked.
- Gemini free-tier limits change. The gateway budget (5 requests a minute, 300 a day) is conservative and configurable.
- `ENABLE_CHAOS` and `ENABLE_CLOCK_OFFSET` default to on because the demo needs them. Turn them off for real use.
- The UI is plain tokenized CSS. The original plan said Tailwind; it was not used.

## Configuration

See `.env.example`. Important: `DATABASE_URL`, `GEMINI_API_KEY`, `JWT_SECRET` (32+ bytes, required when
`APP_ENV=prod`), `LOOP_IDLE_S` (slows background sweeps while nobody uses the app, so a free hosted Postgres can sleep).
