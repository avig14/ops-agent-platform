"""Risk agent: a transparent rule scorer (code) and an LLM assessor run independently, then are reconciled.

Disagreement of >=1 level => CONFLICT_AGENTS: the higher level wins provisionally and a human must resolve.
The LLM sees validated facts only (never the raw email).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import audit
from .codes import Reason
from .llm import LLMUnavailable, gateway
from .schemas import RISK_ORDER, RiskLLMOut

LEVELS = ["Low", "Medium", "High", "Critical"]

RISK_SYSTEM = (
    "You are an operations risk analyst for a retail company. You receive VALIDATED case facts as JSON (not the raw "
    "email). Assess urgency, business impact and overall risk as Low, Medium, High or Critical. Give short reasons that "
    "cite the facts provided. Do not invent facts or numbers. List information that would change your assessment."
)


@dataclass
class RiskResult:
    level: str
    urgency: str
    impact: str
    rule: dict
    llm: dict | None
    conflict: bool = False
    reasons: list[str] = field(default_factory=list)
    llm_failed: bool = False


def _max(*lv: str) -> str:
    return LEVELS[max(RISK_ORDER[x] for x in lv)]


def rule_score(facts: dict) -> dict:
    """Deterministic, monotone in shortfall and in lateness. Every contribution is listed so it can be shown in the UI."""
    why: list[str] = []
    ordered, short = facts.get("qty_ordered"), facts.get("shortfall")
    pct = (short / ordered) if ordered and short is not None else None
    gap = facts.get("launch_gap_days")
    days = facts.get("days_to_need")

    impact = "Low"
    if short and short > 0:
        impact = "Medium"
        why.append(f"shortfall of {short} units ({round(pct * 100)}% of {ordered})")
        if pct is not None and pct >= 0.25:
            impact = "High"
        if gap is not None and gap > 0:
            impact = _max(impact, "High")
            why.append(f"delivery lands {gap} day(s) after the need date")
            if pct is not None and pct >= 0.5:
                impact = "Critical"
    elif gap is not None and gap > 0:
        impact = "Medium"
        why.append(f"delivery lands {gap} day(s) after the need date")

    urgency = "Low"
    if days is not None:
        urgency = "Critical" if days <= 3 else "High" if days <= 7 else "Medium" if days <= 14 else "Low"
        why.append(f"{days} day(s) until the need date")
    if facts.get("deadline_passed_at_ingest"):
        urgency = _max(urgency, "High")
        why.append("stated approval deadline already passed at ingest")
    return {"impact": impact, "urgency": urgency, "level": _max(impact, urgency), "why": why}


def _facts_for_llm(facts: dict) -> dict:
    keep = ("intent", "store", "item", "supplier", "qty_ordered", "qty_available", "shortfall", "delay_days", "needed_by",
            "days_to_need", "launch_gap_days", "approval_deadline", "deadline_passed_at_ingest", "unresolved_conflicts", "missing", "data_notes")
    return {k: facts.get(k) for k in keep}


async def assess(facts: dict, case_id: str | None = None, use_llm: bool = True) -> RiskResult:
    rule = rule_score(facts)
    res = RiskResult(rule["level"], rule["urgency"], rule["impact"], rule, None, reasons=list(rule["why"]))
    if not use_llm:
        res.llm_failed = True
        return res
    try:
        r = await gateway.generate("risk", RISK_SYSTEM, "<facts>\n" + json.dumps(_facts_for_llm(facts), default=str) + "\n</facts>",
                                   RiskLLMOut, case_id=case_id, actor="risk")
    except LLMUnavailable as exc:
        res.llm_failed = True
        await audit.write(case_id, "agent", "risk", "llm_risk", "degraded", "LLM risk assessment unavailable; rule score only",
                          reason=exc.reason, nf=[audit.not_fetched("llm", exc.reason, "risk", None, len(exc.attempts), "rule_score_only",
                                                                   affects=["risk cross-check"])])
        return res
    res.llm = r.data.model_dump()
    gapl = abs(RISK_ORDER[r.data.risk] - RISK_ORDER[rule["level"]])
    res.level = _max(rule["level"], r.data.risk)           # higher wins provisionally
    res.urgency = _max(rule["urgency"], r.data.urgency)
    res.impact = _max(rule["impact"], r.data.impact)
    res.reasons += [f"LLM: {x}" for x in r.data.reasons[:4]]
    if gapl >= 1:
        res.conflict = True
        await audit.write(case_id, "agent", "risk", "agent_conflict", "blocked",
                          f"rule scorer={rule['level']} vs LLM={r.data.risk}; using higher ({res.level}); human must resolve",
                          reason=Reason.CONFLICT_AGENTS, refs={"rule": rule["level"], "llm": r.data.risk})
    return res
