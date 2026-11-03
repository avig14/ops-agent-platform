import pytest

from app import policy
from app.codes import Reason
from app.policy import PolicyInput, check_approver, evaluate, required_tiers
from app.schemas import Role, UserOut


def user(role, uid="u1", region="West"):
    return UserOut(id=uid, email=f"{uid}@x", name=uid, role=role, region=region)


@pytest.mark.parametrize("amount,roles", [
    (0, ["manager"]), (50_000, ["manager"]), (50_001, ["regional_head"]), (500_000, ["regional_head"]),
    (500_001, ["regional_head", "finance_director"]),
])
def test_authority_ladder_boundaries(amount, roles):
    assert [t["role"] for t in required_tiers(amount, "Low")] == roles


def test_critical_risk_raises_floor_to_regional_head():
    assert [t["role"] for t in required_tiers(0, "Critical")] == ["regional_head"]


def test_clean_zero_spend_low_risk_auto_clears():
    d = evaluate(PolicyInput(amount=0, risk_level="Low", field_confidence={"quantity_affected": 0.9}))
    assert d.auto_clear and not d.requires_human and not d.tiers


@pytest.mark.parametrize("kw,rule", [
    ({"amount": 1}, "SPEND_GT_ZERO"), ({"risk_level": "High"}, "RISK_HIGH_OR_CRITICAL"), ({"risk_level": "Critical"}, "RISK_HIGH_OR_CRITICAL"),
    ({"injection_flag": True}, "INJECTION_FLAG"), ({"degraded": True}, "DEGRADED_MODE"), ({"agent_conflict": True}, "AGENT_CONFLICT"),
    ({"unresolved_conflicts": 1}, "DATA_CONFLICT_UNRESOLVED"), ({"missing_fields": 1}, "MISSING_REQUIRED_INFO"),
    ({"abstained": True}, "AI_ABSTAINED"), ({"cancellation_after_approval": True}, "CANCEL_AFTER_APPROVAL"),
    ({"revision_after_approval": True}, "REVISION_AFTER_APPROVAL"), ({"possible_duplicates": ["CASE-0001"]}, "POSSIBLE_DUPLICATE"),
])
def test_each_mandatory_rule_fires(kw, rule):
    d = evaluate(PolicyInput(**kw))
    assert d.requires_human and rule in [r["id"] for r in d.rules] and d.tiers


@pytest.mark.parametrize("conf,fires", [(0.549, True), (0.55, False), (0.80, False)])
def test_low_stakes_threshold_boundary(conf, fires):
    d = evaluate(PolicyInput(amount=0, risk_level="Low", field_confidence={"quantity_affected": conf}))
    assert ("LOW_CONFIDENCE" in [r["id"] for r in d.rules]) is fires


@pytest.mark.parametrize("conf,fires", [(0.79, True), (0.80, False)])
def test_high_stakes_threshold_is_stricter(conf, fires):
    d = evaluate(PolicyInput(amount=10_000, risk_level="Low", field_confidence={"additional_cost": conf}))
    assert ("LOW_CONFIDENCE" in [r["id"] for r in d.rules]) is fires


def test_high_confidence_never_removes_a_required_review():
    d = evaluate(PolicyInput(amount=100, field_confidence={"additional_cost": 1.0}))
    assert d.requires_human


def test_requester_cannot_approve_own_request():
    ok, reason, _ = check_approver(user(Role.manager, "u-m"), "manager", "u-m", "West")
    assert not ok and reason == Reason.POLICY_SOD


def test_service_identity_cannot_approve():
    ok, reason, _ = check_approver(user(Role.service, "ai", None), "manager", "req", "West")
    assert not ok and reason == Reason.POLICY_UNAUTHORIZED


def test_wrong_role_and_wrong_region_blocked():
    assert not check_approver(user(Role.manager, "m"), "regional_head", "r", "West")[0]
    ok, reason, _ = check_approver(user(Role.manager, "m", "South"), "manager", "r", "West")
    assert not ok and reason == Reason.POLICY_UNAUTHORIZED
    assert check_approver(user(Role.manager, "m", "West"), "manager", "r", "West")[0]
    assert check_approver(user(Role.finance_director, "f", None), "finance_director", "r", "West")[0]


def test_higher_authority_may_cover_lower_tier_and_admin_cannot_approve():
    assert check_approver(user(Role.regional_head, "rh"), "manager", "r", "West")[0]
    assert not check_approver(user(Role.admin, "a", None), "manager", "r", "West")[0]


def test_same_person_cannot_fill_two_tiers():
    ok, reason, _ = check_approver(user(Role.finance_director, "f", None), "finance_director", "r", "West", ["f"])
    assert not ok and reason == Reason.POLICY_SOD


def test_monotonic_amount_never_lowers_required_authority():
    order = {"manager": 0, "regional_head": 1, "finance_director": 2}
    prev = -1
    for amt in (0, 1, 50_000, 50_001, 400_000, 500_000, 500_001, 5_000_000):
        top = max(order[t["role"]] for t in required_tiers(amt, "Low"))
        assert top >= prev
        prev = top
