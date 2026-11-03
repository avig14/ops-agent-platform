"""Policy engine: code only, no LLM. Decides when a human must approve and who has authority.

Every evaluation is stored as a `policy_decisions` row so the UI can answer "Why human approval?".
Authority ladder (assumption, documented in the PRD): <=INR 50,000 manager; <=5,00,000 regional head;
above that regional head AND finance director. Confidence only ever ADDS review, never removes it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .codes import Reason
from .schemas import RISK_ORDER, Role, UserOut

TIER1_MAX = 50_000
TIER2_MAX = 500_000
CRITICAL_FIELDS = ("quantity_affected", "location", "approval_deadline", "additional_cost", "delay_days")
THRESH_HIGH_STAKE, THRESH_LOW_STAKE = 0.80, 0.55


@dataclass
class PolicyInput:
    amount: float = 0.0
    risk_level: str = "Low"
    injection_flag: bool = False
    degraded: bool = False
    agent_conflict: bool = False
    unresolved_conflicts: int = 0
    missing_fields: int = 0
    abstained: bool = False
    field_confidence: dict[str, float] = field(default_factory=dict)   # usable critical fields only
    cancellation_after_approval: bool = False
    revision_after_approval: bool = False
    prior_rejection: bool = False
    possible_duplicates: list[str] = field(default_factory=list)     # ids of cases this one may repeat
    field_disagreements: list[str] = field(default_factory=list)     # critical fields where two readers disagreed (AI-read only)
    shortfall_unverified: bool = False      # a shortage/delay case whose shortfall could not be computed
    shortfall_uncovered: bool = False       # a real shortfall that no option closes in time
    escalation_chosen: bool = False         # the plan itself is "ask management"
    date_unreadable: bool = False           # a need date was stated but code could not read it
    substitute_chosen: bool = False         # the plan swaps the requested product for a different one


@dataclass
class PolicyDecision:
    requires_human: bool
    rules: list[dict]
    tiers: list[dict]                  # [{"tier":1,"role":"manager"}, ...] all must approve
    auto_clear: bool

    def as_json(self) -> dict:
        return {"requires_human": self.requires_human, "rules": self.rules, "tiers": self.tiers, "auto_clear": self.auto_clear}


def situation_flags(facts: dict, chosen: dict | None) -> dict:
    """Code-derived facts about the plan itself. They only ever ADD review: a free-looking plan that does not
    actually solve the problem must not auto-clear."""
    intent = facts.get("intent")
    short = facts.get("shortfall")
    needs_qty = intent in ("inventory_shortage", "delivery_delay")
    uncovered = False
    if short and short > 0:
        ok = chosen is not None and bool(chosen.get("covers_shortfall") and chosen.get("feasible_by_need"))             and chosen.get("action") != "escalate_to_management"
        uncovered = not ok
    return {
        "shortfall_unverified": bool(needs_qty and short is None),
        "shortfall_uncovered": uncovered,
        "escalation_chosen": bool(chosen and chosen.get("action") == "escalate_to_management"),
        "date_unreadable": bool(facts.get("needed_by_unreadable")),
        "substitute_chosen": bool(chosen and chosen.get("substitute")),
    }


def required_tiers(amount: float, risk_level: str) -> list[dict]:
    if amount > TIER2_MAX:
        roles = [Role.regional_head.value, Role.finance_director.value]
    elif amount > TIER1_MAX:
        roles = [Role.regional_head.value]
    else:
        roles = [Role.manager.value]
    # risk floor
    if risk_level == "Critical" and roles == [Role.manager.value]:
        roles = [Role.regional_head.value]
    return [{"tier": i + 1, "role": r} for i, r in enumerate(roles)]


def evaluate(p: PolicyInput) -> PolicyDecision:
    fired: list[dict] = []

    def rule(rid: str, why: str) -> None:
        fired.append({"id": rid, "why": why})

    if p.amount > 0:
        rule("SPEND_GT_ZERO", f"additional spend INR {int(p.amount)} requires approval")
    if RISK_ORDER.get(p.risk_level, 0) >= RISK_ORDER["High"]:
        rule("RISK_HIGH_OR_CRITICAL", f"risk is {p.risk_level}")
    if p.injection_flag:
        rule("INJECTION_FLAG", "possible embedded instructions in the request; human triage required")
    if p.degraded:
        rule("DEGRADED_MODE", "a dependency was unavailable; analysis is incomplete")
    if p.agent_conflict:
        rule("AGENT_CONFLICT", "risk agents disagree; human must resolve")
    if p.unresolved_conflicts:
        rule("DATA_CONFLICT_UNRESOLVED", f"{p.unresolved_conflicts} conflicting data point(s) unresolved")
    if p.missing_fields:
        rule("MISSING_REQUIRED_INFO", f"{p.missing_fields} required field(s) missing")
    if p.abstained:
        rule("AI_ABSTAINED", "AI abstained (insufficient evidence)")
    high_stake = p.amount > 0 or RISK_ORDER.get(p.risk_level, 0) >= RISK_ORDER["High"]
    thr = THRESH_HIGH_STAKE if high_stake else THRESH_LOW_STAKE
    low = {k: v for k, v in p.field_confidence.items() if k in CRITICAL_FIELDS and v < thr}
    if low:
        rule("LOW_CONFIDENCE", f"fields below {thr:.2f} for this stake level: " + ", ".join(f"{k}={v:.2f}" for k, v in low.items()))
    if p.cancellation_after_approval:
        rule("CANCEL_AFTER_APPROVAL", "cancellation after approvals started")
    if p.revision_after_approval:
        rule("REVISION_AFTER_APPROVAL", "supplier revision after approval; prior approvals invalid")

    if p.possible_duplicates:
        rule("POSSIBLE_DUPLICATE", "may repeat " + ", ".join(p.possible_duplicates) + "; a human must confirm it is a separate request")
    if p.prior_rejection:
        rule("PRIOR_REJECTION", "an approver rejected an earlier plan; the revised plan needs a human decision")
    if p.field_disagreements:
        rule("FIELD_DISAGREEMENT", "two readers of the request disagree on " + ", ".join(p.field_disagreements) + "; a human must confirm the value")
    if p.shortfall_unverified:
        rule("SHORTFALL_UNVERIFIED", "the shortfall could not be computed from the request and the systems of record")
    if p.shortfall_uncovered:
        rule("SHORTFALL_NOT_COVERED", "no option closes the shortfall before the need date")
    if p.escalation_chosen:
        rule("ESCALATION_RECOMMENDED", "the plan is to escalate; management must decide")
    if p.date_unreadable:
        rule("DATE_UNREADABLE", "a need date was stated but could not be read; timing is unverified")
    if p.substitute_chosen:
        rule("SUBSTITUTE_PRODUCT", "the plan swaps the requested product for a different one; the requester's agreement is a human decision")

    requires = bool(fired)
    tiers = required_tiers(p.amount, p.risk_level) if requires else []
    return PolicyDecision(requires, fired, tiers, auto_clear=not requires)


def check_approver(approver: UserOut, required_role: str, requester_id: str | None, case_region: str | None,
                   already_approved_by: list[str] | None = None) -> tuple[bool, Reason | None, str]:
    """Authorization for a single approval action. Returns (ok, reason_code, message)."""
    if approver.role == Role.service:
        return False, Reason.POLICY_UNAUTHORIZED, "AI service identity cannot approve"
    if requester_id and approver.id == requester_id:
        return False, Reason.POLICY_SOD, "requester cannot approve their own request (separation of duties)"
    if already_approved_by and approver.id in already_approved_by:
        return False, Reason.POLICY_SOD, "same person cannot satisfy two approval tiers"
    if approver.role == Role.admin:
        return False, Reason.POLICY_UNAUTHORIZED, "admin does not hold approval authority"
    ok_roles = {required_role}
    if required_role == Role.manager.value:
        ok_roles |= {Role.regional_head.value, Role.finance_director.value}      # higher authority may approve a lower tier
    if approver.role.value not in ok_roles:
        return False, Reason.POLICY_UNAUTHORIZED, f"role {approver.role.value} cannot approve a {required_role}-tier decision"
    if approver.role in (Role.manager, Role.regional_head) and case_region and approver.region != case_region:
        return False, Reason.POLICY_UNAUTHORIZED, "approver is outside the case region"
    return True, None, "ok"
