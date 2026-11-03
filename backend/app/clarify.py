"""Clarification requests for cases that cannot proceed (A2 missing info, A3 conflicting info, B15 unclear intent).

Deterministic on purpose: no LLM writes to a requester here, so nothing can be invented or leaked. The text names only what
is missing or in conflict, and quotes only values the requester sent or the systems of record hold. It never states costs,
risk levels, approvers or any internal reasoning.
"""
from __future__ import annotations

import hashlib
import re

QUESTIONS = {
    "location": "Which store or site is this about?",
    "quantity_affected": "How many units are affected or on order?",
    "delay_days": "How many days late is the delivery expected to be?",
    "supplier": "Which supplier is this about?",
    "item": "Which item is this about?",
    "intent": "What do you need from us? For example: a delivery delay, a stock shortage, a supplier problem or an urgent approval.",
}


def _question_for_missing(m: dict) -> str:
    why = m.get("why", "")
    unknown = re.match(r"'(.*)' is not a known store", why)
    if m["field"] == "location" and unknown:
        return f"We do not recognise '{unknown.group(1)}' as a store. Which store do you mean?"
    return QUESTIONS.get(m["field"], f"Please provide: {m['field'].replace('_', ' ')}.")


def _question_for_conflict(c: dict) -> str:
    return (f"Your message gives {c['field'].replace('_', ' ')} as {c['email']}, but {c['system_source']} shows {c['system']}. "
            "Which one is correct?")


def build(case_id: str, title: str | None, missing: list[dict], conflicts: list[dict], intake_meta: dict | None = None) -> dict:
    """Returns {"subject","body","questions","key_hash"}; key_hash changes only when the set of questions changes."""
    meta = intake_meta or {}
    questions: list[str] = []
    if meta.get("too_many_issues"):
        questions.append(f"Your message describes {meta['too_many_issues']} separate problems. Please send each one as its own "
                         "request so we can handle them one by one.")
    else:
        questions += [_question_for_missing(m) for m in missing]
    questions += [_question_for_conflict(c) for c in conflicts]
    seen, uniq = set(), []
    for q in questions:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    body = (f"We received your request ({case_id}{' - ' + title if title else ''}) but need a little more before we can continue.\n\n"
            + "\n".join(f"{i}. {q}" for i, q in enumerate(uniq, 1))
            + "\n\nReply on the case with the answers. Nothing will be approved or ordered until then.")
    return {"subject": f"More information needed: {case_id}", "body": body, "questions": uniq,
            "key_hash": hashlib.sha256("|".join(uniq).encode()).hexdigest()[:10]}
