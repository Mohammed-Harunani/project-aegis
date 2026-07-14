from consultant.consultant import RepairPlan
from governance.policy import GovernancePolicy
from governance.selector import RepairSelector


def make_policy():
    return GovernancePolicy(auto_approve_threshold=0.92, approval_threshold=0.80)


def test_prefers_non_destructive_over_higher_confidence_destructive():
    non_destructive = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64",
        confidence=0.85,
        explanation="Type mismatch detected. Strict cast proposed.",
    )
    destructive = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64 WITH_DROP_INVALID",
        confidence=0.95,
        explanation="Type mismatch with invalid values. Drop invalid rows before cast.",
    )

    selector = RepairSelector(governance_policy=make_policy())
    best = selector.choose_best([destructive, non_destructive])

    assert best is non_destructive


def test_prefers_higher_confidence_within_same_safety_tier():
    lower = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64 WITH_DROP_INVALID",
        confidence=0.82,
        explanation="lower confidence destructive",
    )
    higher = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64 WITH_DROP_INVALID",
        confidence=0.90,
        explanation="higher confidence destructive",
    )

    selector = RepairSelector(governance_policy=make_policy())
    best = selector.choose_best([lower, higher])

    assert best is higher


def test_filters_out_plans_below_governance_floor():
    quarantined = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64 WITH_DROP_INVALID",
        confidence=0.70,
        explanation="below approval_threshold",
    )
    viable = RepairPlan(
        proposed_action="RENAME_COLUMN new_name -> old_name",
        confidence=0.85,
        explanation="viable rename",
    )

    selector = RepairSelector(governance_policy=make_policy())
    best = selector.choose_best([quarantined, viable])

    assert best is viable


def test_returns_none_when_all_plans_are_quarantined():
    quarantined = RepairPlan(
        proposed_action="CAST_COLUMN age TO int64 WITH_DROP_INVALID",
        confidence=0.50,
        explanation="well below approval_threshold",
    )

    selector = RepairSelector(governance_policy=make_policy())
    best = selector.choose_best([quarantined])

    assert best is None


def test_returns_none_for_empty_plan_list():
    selector = RepairSelector(governance_policy=make_policy())
    assert selector.choose_best([]) is None


def test_defaults_to_a_usable_governance_policy_when_none_given():
    selector = RepairSelector()  # no policy passed in — must fall back safely

    high_confidence = RepairPlan(
        proposed_action="RENAME_COLUMN new_name -> old_name",
        confidence=0.95,
        explanation="high confidence rename",
    )
    low_confidence = RepairPlan(
        proposed_action="RENAME_COLUMN foo -> bar",
        confidence=0.50,
        explanation="well below default approval_threshold",
    )

    best = selector.choose_best([high_confidence, low_confidence])

    assert best is high_confidence
