from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent.decision.base import Action
from agent.risk import rules as R
from agent.risk.gate import Verdict, evaluate
from agent.risk.rules import RULES, RiskContext, RiskLimits

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LIMITS = RiskLimits(
    max_stake_usd=Decimal("2.00"),
    max_open_positions=1,
    daily_loss_cap_usd=Decimal("20.00"),
    cooldown_after_losses=3,
    cooldown_minutes=10,
    min_confidence=0.55,
    approval_band_high=0.65,
    stale_tick_s=5.0,
    default_stake_usd=Decimal("1.00"),
)
SAFE = RiskContext(
    now=NOW,
    action=Action.CALL,
    confidence=0.80,
    stake_usd=Decimal("1.00"),
    provider_is_primary=True,
    kill_switch=False,
    paused=False,
    ws_url_is_demo=True,
    open_positions=0,
    open_exposure_usd=Decimal("0"),
    daily_pnl_usd=Decimal("0"),
    consecutive_losses=0,
    last_loss_at=None,
    data_age_s=0.8,
    first_trade_after_resume=False,
    limits=LIMITS,
)


def ctx(**changes):
    return replace(SAFE, **changes)


def test_safe_context_is_approved():
    result = evaluate(SAFE)
    assert result.verdict is Verdict.APPROVE
    assert result.reasons == []


def test_rule_set_is_complete():
    names = {r.__name__ for r in RULES}
    assert names == {
        "context_valid",
        "kill_switch",
        "paused",
        "demo_only",
        "confidence_floor",
        "stake_within_max",
        "max_open_positions",
        "daily_loss_cap",
        "cooldown_after_losses",
        "data_fresh",
        "approval_triggers",
    }


@pytest.mark.parametrize("rules", [(), (R.approval_triggers,), RULES[1:]])
def test_gate_refuses_incomplete_rule_set(rules):
    result = evaluate(ctx(kill_switch=True), rules=rules)
    assert result.verdict is Verdict.REJECT
    assert result.failed_rules == ["rule_set"]


def test_gate_rejects_spoofed_rule_with_same_name():
    def kill_switch(_ctx):  # look-alike that always passes
        return R.RuleResult("kill_switch", True)

    spoofed = tuple(kill_switch if r is R.kill_switch else r for r in RULES)
    result = evaluate(ctx(kill_switch=True), rules=spoofed)
    assert result.verdict is Verdict.REJECT
    assert result.failed_rules == ["rule_set"]


def test_gate_treats_crashing_rule_as_failure():
    def exploding(_ctx):
        raise RuntimeError("boom")

    result = evaluate(SAFE, rules=(*RULES, exploding))
    assert result.verdict is Verdict.REJECT
    assert "exploding" in result.failed_rules


def test_gate_rejects_unknown_action():
    assert evaluate(ctx(action="BUY")).verdict is Verdict.REJECT


@pytest.mark.parametrize("action", [Action.CALL, Action.PUT])
def test_rule_kill_switch_blocks_everything(action):
    result = evaluate(ctx(kill_switch=True, action=action))
    assert result.verdict is Verdict.REJECT
    assert "kill_switch" in result.failed_rules


def test_rule_paused_blocks():
    assert "paused" in evaluate(ctx(paused=True)).failed_rules


def test_rule_demo_only_blocks_non_demo_connection():
    assert "demo_only" in evaluate(ctx(ws_url_is_demo=False)).failed_rules


@pytest.mark.parametrize(
    ("confidence", "blocked"), [(0.54, True), (0.5499, True), (0.55, False), (0.56, False)]
)
def test_rule_confidence_floor(confidence, blocked):
    result = evaluate(ctx(confidence=confidence))
    assert ("confidence_floor" in result.failed_rules) is blocked


@pytest.mark.parametrize(
    "confidence", [-0.1, 1.01, float("nan"), float("inf"), True, "0.9", None, Decimal("NaN")]
)
def test_rule_confidence_rejects_invalid_values(confidence):
    assert evaluate(ctx(confidence=confidence)).verdict is Verdict.REJECT


@pytest.mark.parametrize(
    ("stake", "blocked"),
    [("2.00", False), ("2.01", True), ("0", True), ("-1", True), ("1.00", False), ("0.01", False)],
)
def test_rule_stake_cap(stake, blocked):
    result = evaluate(ctx(stake_usd=Decimal(stake)))
    assert ("stake_within_max" in result.failed_rules) is blocked


@pytest.mark.parametrize("stake", [Decimal("NaN"), Decimal("Infinity"), 1.0, "1.00", None])
def test_rule_stake_rejects_invalid_values(stake):
    assert evaluate(ctx(stake_usd=stake)).verdict is Verdict.REJECT


@pytest.mark.parametrize(("open_positions", "blocked"), [(0, False), (1, True), (5, True)])
def test_rule_max_open_positions(open_positions, blocked):
    result = evaluate(ctx(open_positions=open_positions))
    assert ("max_open_positions" in result.failed_rules) is blocked


@pytest.mark.parametrize(
    "changes",
    [
        {"open_positions": -1},
        {"consecutive_losses": -5},
        {"open_positions": True},
        {"open_exposure_usd": Decimal("-1")},
        {"daily_pnl_usd": Decimal("NaN")},
        {"daily_pnl_usd": 0.0},
        {"now": datetime(2026, 10, 7, 12, 0)},  # naive
        {"last_loss_at": datetime(2026, 10, 7, 11, 0)},  # naive
        {"kill_switch": 0},
        {"ws_url_is_demo": 1},
    ],
)
def test_context_validation_rejects_malformed_fields(changes):
    result = evaluate(ctx(**changes))
    assert result.verdict is Verdict.REJECT
    assert "context_valid" in result.failed_rules


@pytest.mark.parametrize(
    ("pnl", "blocked"),
    [("-19.00", False), ("-19.01", True), ("-20.00", True), ("5.00", False)],
)
def test_rule_daily_loss_cap_counts_the_stake_at_risk(pnl, blocked):
    # stake 1.00: a -19.00 day can still lose 1.00 and land exactly on the -20 cap.
    result = evaluate(ctx(daily_pnl_usd=Decimal(pnl)))
    assert ("daily_loss_cap" in result.failed_rules) is blocked


def test_rule_daily_loss_cap_counts_open_exposure():
    result = evaluate(ctx(daily_pnl_usd=Decimal("-18.00"), open_exposure_usd=Decimal("1.50")))
    assert "daily_loss_cap" in result.failed_rules


@pytest.mark.parametrize(
    ("minutes_ago", "blocked"), [(5, True), (9.99, True), (10, False), (11, False)]
)
def test_rule_cooldown(minutes_ago, blocked):
    result = evaluate(ctx(consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=minutes_ago)))
    assert ("cooldown_after_losses" in result.failed_rules) is blocked


def test_rule_cooldown_fails_closed_without_timestamp():
    result = evaluate(ctx(consecutive_losses=4, last_loss_at=None))
    assert "cooldown_after_losses" in result.failed_rules


def test_rule_cooldown_ignores_short_streak():
    assert evaluate(ctx(consecutive_losses=2, last_loss_at=NOW)).verdict is Verdict.APPROVE


@pytest.mark.parametrize(
    ("age", "blocked"),
    [
        (6.0, True),
        (5.0001, True),
        (None, True),
        (float("nan"), True),
        (float("inf"), True),
        (-100.0, True),
        (5.0, False),
        (0.0, False),
    ],
)
def test_rule_stale_data(age, blocked):
    assert ("data_fresh" in evaluate(ctx(data_age_s=age)).failed_rules) is blocked


def test_gate_hold_is_noop():
    result = evaluate(ctx(action=Action.HOLD, kill_switch=True))
    assert result.verdict is Verdict.NO_TRADE
    assert result.results == ()


@pytest.mark.parametrize(("confidence", "escalated"), [(0.60, True), (0.6499, True), (0.65, False)])
def test_gate_grey_zone_needs_approval(confidence, escalated):
    result = evaluate(ctx(confidence=confidence))
    assert (result.verdict is Verdict.NEEDS_APPROVAL) is escalated


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"provider_is_primary": False}, "fallback provider"),
        ({"first_trade_after_resume": True}, "after resume"),
        ({"daily_pnl_usd": Decimal("-10.00")}, "half the cap"),
        ({"stake_usd": Decimal("1.50")}, "non-default stake"),
    ],
)
def test_gate_approval_triggers(changes, reason):
    result = evaluate(ctx(**changes))
    assert result.verdict is Verdict.NEEDS_APPROVAL
    assert any(reason in r for r in result.reasons)


def test_half_cap_trigger_boundary():
    assert evaluate(ctx(daily_pnl_usd=Decimal("-9.99"))).verdict is Verdict.APPROVE


def test_gate_reject_beats_needs_approval():
    result = evaluate(ctx(confidence=0.60, open_positions=1))
    assert result.verdict is Verdict.REJECT


@pytest.mark.parametrize(
    "changes",
    [
        {"max_stake_usd": Decimal("0")},
        {"daily_loss_cap_usd": Decimal("NaN")},
        {"default_stake_usd": Decimal("3")},
        {"max_open_positions": 0},
        {"min_confidence": 0.3},
        {"min_confidence": float("nan")},
        {"approval_band_high": 0.5},
        {"stale_tick_s": 0},
    ],
)
def test_risk_limits_validate_themselves(changes):
    with pytest.raises(ValueError):
        replace(LIMITS, **changes)


contexts = st.builds(
    RiskContext,
    now=st.just(NOW),
    action=st.sampled_from(list(Action)),
    confidence=st.floats(allow_nan=True, allow_infinity=True),
    stake_usd=st.decimals(min_value=-10, max_value=10, places=2, allow_nan=False),
    provider_is_primary=st.booleans(),
    kill_switch=st.booleans(),
    paused=st.booleans(),
    ws_url_is_demo=st.booleans(),
    open_positions=st.integers(min_value=-2, max_value=5),
    open_exposure_usd=st.decimals(min_value=-2, max_value=10, places=2, allow_nan=False),
    daily_pnl_usd=st.decimals(min_value=-100, max_value=100, places=2, allow_nan=False),
    consecutive_losses=st.integers(min_value=-2, max_value=10),
    last_loss_at=st.one_of(
        st.none(), st.integers(0, 120).map(lambda m: NOW - timedelta(minutes=m))
    ),
    data_age_s=st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    first_trade_after_resume=st.booleans(),
    limits=st.just(LIMITS),
)


@given(contexts)
def test_gate_property_never_approves_when_unsafe(c):
    """Whatever the model says, no trade is approved while killed, paused or off-demo."""
    verdict = evaluate(c).verdict
    if c.kill_switch or c.paused or not c.ws_url_is_demo:
        assert verdict in (Verdict.REJECT, Verdict.NO_TRADE)


@given(contexts)
def test_gate_property_approve_implies_every_limit_holds(c):
    if evaluate(c).verdict is not Verdict.APPROVE:
        return
    assert c.action in (Action.CALL, Action.PUT)
    assert not c.kill_switch and not c.paused and c.ws_url_is_demo
    assert c.provider_is_primary and not c.first_trade_after_resume
    assert LIMITS.approval_band_high <= c.confidence <= 1
    assert Decimal("0") < c.stake_usd <= LIMITS.max_stake_usd
    assert 0 <= c.open_positions < LIMITS.max_open_positions
    assert c.daily_pnl_usd - c.open_exposure_usd - c.stake_usd >= -LIMITS.daily_loss_cap_usd
    assert c.data_age_s is not None and 0 <= c.data_age_s <= LIMITS.stale_tick_s
    if c.consecutive_losses >= LIMITS.cooldown_after_losses:
        assert c.last_loss_at is not None
        assert c.now >= c.last_loss_at + timedelta(minutes=LIMITS.cooldown_minutes)


@given(contexts)
def test_gate_property_never_raises(c):
    evaluate(c)  # every input yields a verdict, never an exception
