"""Deployment smoke test. Works against any running instance (local, Docker, hosted).

    python scripts/smoke.py http://localhost:8080            # no model call (free)
    python scripts/smoke.py https://your-app.example --case  # also submits one case (uses the AI if a key is set)

Exit code 0 only if every check passes. Prints one line per check.
"""
from __future__ import annotations

import re
import sys
import time
import uuid

import httpx

PASSWORD = "Demo@2026"
SAMPLE = ("Subject: Urgent Delivery Delay – Store Launch\nHi Team,\nOur supplier has informed us that 500 display units "
          "required for the Pune store launch will be delayed by 10 days. The store is scheduled to open next week, and only "
          "120 units are currently available in the warehouse.\nPlease assess the impact, identify possible alternatives, and "
          "obtain approval for any additional cost by 4:00 PM today.\nRegards, Regional Operations Manager")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def login(c: httpx.Client, email: str) -> dict[str, str]:
    """Cookie sent by hand: the session cookie is `Secure` in prod, and an http client would refuse to replay it on http://localhost."""
    r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    sc = r.headers.get("set-cookie", "")
    m = re.match(r"session=([^;]+)", sc)
    check(r.status_code == 200 and m, f"login {email}", f"{r.status_code} {r.text[:120]}")
    check("httponly" in sc.lower(), f"session cookie is HttpOnly ({email})")
    return {"Cookie": f"session={m.group(1)}"} if m else {}


def main(base: str, with_case: bool) -> int:
    c = httpx.Client(base_url=base.rstrip("/"), timeout=60, follow_redirects=False)
    t0 = time.time()
    r = c.get("/health"); check(r.status_code == 200 and r.json() == {"status": "ok"}, "GET /health")
    r = c.get("/ready"); check(r.status_code == 200 and r.json().get("db") == "ok", "GET /ready (database reachable)", r.text[:120])

    r = c.get("/"); html = r.text
    check(r.status_code == 200 and '<div id="root"' in html, "GET / serves the React app")
    asset = re.search(r'/assets/[^"\']+\.js', html)
    check(bool(asset) and c.get(asset.group(0)).status_code == 200, "hashed JS asset is served")
    r = c.get("/admin"); check(r.status_code == 200 and '<div id="root"' in r.text, "deep link /admin falls back to index.html")
    r = c.get("/api/does-not-exist"); check(r.status_code == 404 and r.json().get("error"), "unknown /api path is a JSON 404", r.text[:80])
    check(c.get("/api/auth/me").status_code == 401, "no session -> 401")
    r = c.get("/api/auth/demo-users"); check(r.status_code == 200 and len(r.json()["users"]) >= 7, "demo users listed for one-click login")

    admin = login(c, "admin@demo.test")
    r = c.get("/api/auth/me", headers=admin); check(r.status_code == 200 and r.json()["role"] == "admin", "admin session works")

    r = c.get("/api/admin/evals", headers=admin); e = r.json() if r.status_code == 200 else {}
    check(e.get("available") is True, "evaluation snapshot is served", r.text[:120])
    check(e.get("stale") is False, "evaluation snapshot matches current prompt and dataset", str(e.get("stale_because")))
    s = e.get("scripted", {})
    check(s.get("policy", {}).get("passed") == s.get("policy", {}).get("checks") and s.get("policy", {}).get("checks", 0) > 0, "policy checks all pass",
          str(s.get("policy", {}))[:100])
    check(e.get("real_model", {}).get("extraction", {}).get("cases", 0) >= 26, "extraction results cover the 26 test emails")

    r = c.get("/api/admin/llm", headers=admin); check(r.status_code == 200, "admin AI status")
    r = c.get("/api/admin/audit/verify", headers=admin); check(r.status_code == 200 and r.json().get("ok") is True, "audit hash chains verify",
                                                                r.text[:160])

    priya = login(c, "priya.requester@demo.test")
    r = c.get("/api/admin/evals", headers=priya); check(r.status_code == 403, "requester cannot read admin evaluations (403)", str(r.status_code))
    r = c.get("/api/admin/audit/verify", headers=priya); check(r.status_code == 403, "requester cannot run the audit check (403)")

    case_id = None
    if with_case:
        body = {"channel": "email", "subject": "Urgent Delivery Delay", "body": SAMPLE + f"\nref {uuid.uuid4().hex}", "message_id": uuid.uuid4().hex}
        r = c.post("/api/cases?wait=true", json=body, headers=priya)
        check(r.status_code == 202, "submit the sample email", f"{r.status_code} {r.text[:160]}")
        case_id = r.json().get("case_id") if r.status_code == 202 else None
        if case_id:
            d = c.get(f"/api/cases/{case_id}", headers=priya).json()
            st = d["case"]["status"]
            check(st in ("AWAITING_APPROVAL", "NEEDS_INFO", "ANALYZED", "RECOMMENDED", "ESCALATED"), f"case reached a real state ({st})")
            check(d["case"].get("needs_human") or st == "AWAITING_APPROVAL", "a human is required before any action")
            a = c.get(f"/api/cases/{case_id}/audit", headers=priya)
            rows = a.json() if a.status_code == 200 else []
            rows = rows.get("events", rows) if isinstance(rows, dict) else rows
            check(len(rows) >= 3, f"case has an audit trail ({len(rows)} entries)")
            check(any(x.get("actor_type") == "agent" for x in rows) or any(x.get("actor_type") == "system" for x in rows), "audit shows AI/system actors")
            check(any(x.get("actor_type") == "human" for x in rows), "audit shows the human action")
        r = c.get("/api/admin/audit/verify", headers=admin); check(r.status_code == 200 and r.json().get("ok") is True, "audit chains still verify after the case")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed in {time.time() - t0:.1f}s" + (f"; case {case_id}" if case_id else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], "--case" in sys.argv))
