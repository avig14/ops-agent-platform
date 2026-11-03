"""Multi-issue handling (edge case B15). Pure functions, no I/O.

The quarantined intake step returns `issues[]`. LLMs sometimes over-split one problem into two fragments, so issues are
consolidated before any case is created:

  * an issue with no usable fact and no concrete intent is noise and is dropped
  * two issues about the same location are merged only when neither contradicts the other (same item or unknown item,
    no differing value); the second fills gaps in the first
  * issues that disagree stay separate, so a quantity from one problem can never leak into another

Each surviving issue becomes its own child case whose fields come straight from that issue's own grounded quotes.
"""
from __future__ import annotations

from .extract import FIELDS, NUMERIC, Field, IntakeIssueResult, to_decimal

MAX_ISSUES = 5                       # more than this: ask the sender to split the message instead of opening a flood of cases
VAGUE = ("other", "unclear")


def _usable(issue: IntakeIssueResult) -> dict[str, Field]:
    return {n: f for n, f in issue.fields.items() if f.usable}


def _norm(name: str, value: str):
    if name in NUMERIC:
        d = to_decimal(value)
        return d if d is not None else value.strip().lower()
    return " ".join(value.lower().split())


def _same_value(name: str, a: str, b: str) -> bool:
    x, y = _norm(name, a), _norm(name, b)
    return x == y or (name in ("location", "item", "supplier") and (str(x) in str(y) or str(y) in str(x)))


def conflicts(a: IntakeIssueResult, b: IntakeIssueResult) -> list[str]:
    ua, ub = _usable(a), _usable(b)
    return [n for n in FIELDS if n in ua and n in ub and not _same_value(n, ua[n].value, ub[n].value)]


def _is_noise(issue: IntakeIssueResult) -> bool:
    return not _usable(issue) and issue.intent in VAGUE + ("status_update",)


def _mergeable(a: IntakeIssueResult, b: IntakeIssueResult) -> bool:
    ua, ub = _usable(a), _usable(b)
    if "location" not in ua or "location" not in ub:
        return False                              # without a shared location there is no basis to say it is one problem
    return not conflicts(a, b)


def _merge(a: IntakeIssueResult, b: IntakeIssueResult) -> IntakeIssueResult:
    fields = dict(a.fields)
    for n, f in b.fields.items():
        if f.usable and not fields[n].usable:
            fields[n] = f
    intent = a.intent if a.intent not in VAGUE else b.intent
    return IntakeIssueResult(intent, a.summary or b.summary, fields)


def consolidate(issues: list[IntakeIssueResult]) -> list[IntakeIssueResult]:
    """Drop noise, merge fragments of one problem, keep distinct problems apart. Always returns at least one issue."""
    if not issues:
        raise ValueError("no issues")
    kept = [i for i in issues if not _is_noise(i)] or [issues[0]]
    out: list[IntakeIssueResult] = []
    for issue in kept:
        for idx, existing in enumerate(out):
            if _mergeable(existing, issue):
                out[idx] = _merge(existing, issue)
                break
        else:
            out.append(issue)
    return out
