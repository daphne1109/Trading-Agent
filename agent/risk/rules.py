"""Risk rules: pure functions of a RiskContext. No I/O, no clock reads, no randomness.

Each rule answers one question and returns a RuleResult. The gate (gate.py) runs them all.
A rule must fail closed: when it can't tell (missing, malformed, NaN, wrong type), it blocks.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from agent.decision.base import Action


def _finite_number(value: object) -> bool:
    """A real int/float (not bool), finite."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _utc(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() == timedelta(0)


@dataclass(frozen=True)
class RiskLimits:
    max_stake_usd: Decimal
    max_open_positions: int
    daily_loss_cap_usd: Decimal
    cooldown_after_losses: int
    cooldown_minutes: int
    min_confidence: float
    approval_band_high: float
    stale_tick_s: float
    default_stake_usd: Decimal

    def __post_init__(self) -> None:
        for name in ("max_stake_usd", "daily_loss_cap_usd", "default_stake_usd"):
            value = getattr(self, name)
            if not _finite_decimal(value) or value <= 0:
                raise ValueError(f"RiskLimits.{name} must be a positive finite Decimal")
        if self.default_stake_usd > self.max_stake_usd:
            raise ValueError("default_stake_usd must be <= max_stake_usd")
        for name in ("max_open_positions", "cooldown_after_losses", "cooldown_minutes"):
            value = getattr(self, name)
            if not _count(value) or value < 1:
                raise ValueError(f"RiskLimits.{name} must be an int >= 1")
        if not (_finite_number(self.min_confidence) and 0.5 <= self.min_confidence < 1):
            raise ValueError("min_confidence must be in [0.5, 1)")
        if not (
            _finite_number(self.approval_band_high)
            and self.min_confidence <= self.approval_band_high <= 1
        ):
            raise ValueError("approval_band_high must be in [min_confidence, 1]")
        if not (_finite_number(self.stale_tick_s) and self.stale_tick_s > 0):
            raise ValueError("stale_tick_s must be > 0")


@dataclass(frozen=True)
class RiskContext:
    """Everything the rules may look at, gathered before the gate runs."""

    now: datetime
    action: Action
    confidence: float
    stake_usd: Decimal
    provider_is_primary: bool
    kill_switch: bool
    paused: bool
    ws_url_is_demo: bool
    open_positions: int
    open_exposure_usd: Decimal  # stake currently at risk in open contracts
    daily_pnl_usd: Decimal
    consecutive_losses: int
    last_loss_at: datetime | None
    data_age_s: float | None
    first_trade_after_resume: bool
    limits: RiskLimits


@dataclass(frozen=True)
class RuleResult:
    rule: str
    ok: bool
    reason: str = ""
    needs_approval: bool = False


Rule = Callable[[RiskContext], RuleResult]


def context_valid(ctx: RiskContext) -> RuleResult:
    """Reject any context whose fields are the wrong type, non-finite or out of domain."""
    problems = []
    if not _utc(ctx.now):
        problems.append("now is not timezone-aware UTC")
    if ctx.last_loss_at is not None and not _utc(ctx.last_loss_at):
        problems.append("last_loss_at is not timezone-aware UTC")
    if not _finite_number(ctx.confidence):
        problems.append(f"confidence {ctx.confidence!r} is not a finite number")
    for name in ("stake_usd", "open_exposure_usd", "daily_pnl_usd"):
        if not _finite_decimal(getattr(ctx, name)):
            problems.append(f"{name} is not a finite Decimal")
    if _finite_decimal(ctx.open_exposure_usd) and ctx.open_exposure_usd < 0:
        problems.append("open_exposure_usd is negative")
    for name in ("open_positions", "consecutive_losses"):
        if not _count(getattr(ctx, name)):
            problems.append(f"{name} must be an int >= 0")
    if ctx.data_age_s is not None and not _finite_number(ctx.data_age_s):
        problems.append(f"data_age_s {ctx.data_age_s!r} is not a finite number")
    for name in ("provider_is_primary", "kill_switch", "paused", "ws_url_is_demo"):
        if not isinstance(getattr(ctx, name), bool):
            problems.append(f"{name} must be a bool")
    if problems:
        return RuleResult("context_valid", False, "; ".join(problems))
    return RuleResult("context_valid", True)


def kill_switch(ctx: RiskContext) -> RuleResult:
    if ctx.kill_switch is not False:
        return RuleResult("kill_switch", False, "kill switch is on")
    return RuleResult("kill_switch", True)


def paused(ctx: RiskContext) -> RuleResult:
    if ctx.paused is not False:
        return RuleResult("paused", False, "agent is paused")
    return RuleResult("paused", True)


def demo_only(ctx: RiskContext) -> RuleResult:
    if ctx.ws_url_is_demo is not True:
        return RuleResult("demo_only", False, "connection is not the demo endpoint")
    return RuleResult("demo_only", True)


def confidence_floor(ctx: RiskContext) -> RuleResult:
    c = ctx.confidence
    if not _finite_number(c) or not 0.0 <= c <= 1.0:
        return RuleResult("confidence_floor", False, f"invalid confidence {c!r}")
    if c < ctx.limits.min_confidence:
        return RuleResult(
            "confidence_floor", False, f"confidence {c:.2f} < {ctx.limits.min_confidence:.2f}"
        )
    return RuleResult("confidence_floor", True)


def stake_within_max(ctx: RiskContext) -> RuleResult:
    limit = ctx.limits.max_stake_usd
    if not _finite_decimal(ctx.stake_usd) or not Decimal(0) < ctx.stake_usd <= limit:
        return RuleResult("stake_within_max", False, f"stake {ctx.stake_usd} outside (0, {limit}]")
    return RuleResult("stake_within_max", True)


def max_open_positions(ctx: RiskContext) -> RuleResult:
    n = ctx.open_positions
    if not _count(n) or n >= ctx.limits.max_open_positions:
        return RuleResult(
            "max_open_positions", False, f"{n} open >= max {ctx.limits.max_open_positions}"
        )
    return RuleResult("max_open_positions", True)


def daily_loss_cap(ctx: RiskContext) -> RuleResult:
    # Worst case: every open contract and this new one lose their whole stake.
    worst = ctx.daily_pnl_usd - ctx.open_exposure_usd - ctx.stake_usd
    if worst < -ctx.limits.daily_loss_cap_usd:
        return RuleResult(
            "daily_loss_cap",
            False,
            f"worst-case day {worst} (P&L {ctx.daily_pnl_usd}, at risk "
            f"{ctx.open_exposure_usd}+{ctx.stake_usd}) breaches -{ctx.limits.daily_loss_cap_usd}",
        )
    return RuleResult("daily_loss_cap", True)


def cooldown_after_losses(ctx: RiskContext) -> RuleResult:
    if not _count(ctx.consecutive_losses):
        return RuleResult("cooldown_after_losses", False, "invalid loss count")
    if ctx.consecutive_losses < ctx.limits.cooldown_after_losses:
        return RuleResult("cooldown_after_losses", True)
    if ctx.last_loss_at is None:
        return RuleResult("cooldown_after_losses", False, "loss streak with unknown time")
    until = ctx.last_loss_at + timedelta(minutes=ctx.limits.cooldown_minutes)
    if ctx.now < until:
        return RuleResult(
            "cooldown_after_losses",
            False,
            f"{ctx.consecutive_losses} losses in a row; cooling down until {until:%H:%M:%S}Z",
        )
    return RuleResult("cooldown_after_losses", True)


def data_fresh(ctx: RiskContext) -> RuleResult:
    age = ctx.data_age_s
    if age is None or not _finite_number(age) or not 0 <= age <= ctx.limits.stale_tick_s:
        return RuleResult("data_fresh", False, f"market data age {age!r}s is stale or invalid")
    return RuleResult("data_fresh", True)


def approval_triggers(ctx: RiskContext) -> RuleResult:
    """Never blocks on its own; flags situations where a human must approve."""
    reasons = []
    if ctx.confidence < ctx.limits.approval_band_high:
        reasons.append("confidence in grey zone")
    if ctx.provider_is_primary is not True:
        reasons.append("fallback provider answered")
    if ctx.first_trade_after_resume is not False:
        reasons.append("first trade after resume")
    if ctx.daily_pnl_usd <= -(ctx.limits.daily_loss_cap_usd / 2):
        reasons.append("daily loss past half the cap")
    if ctx.stake_usd != ctx.limits.default_stake_usd:
        reasons.append("non-default stake")
    if reasons:
        return RuleResult("approval_triggers", True, "; ".join(reasons), needs_approval=True)
    return RuleResult("approval_triggers", True)


# Every rule always runs; the gate refuses to run with any of these missing.
RULES: tuple[Rule, ...] = (
    context_valid,
    kill_switch,
    paused,
    demo_only,
    confidence_floor,
    stake_within_max,
    max_open_positions,
    daily_loss_cap,
    cooldown_after_losses,
    data_fresh,
    approval_triggers,
)
REQUIRED_RULES = frozenset(rule.__name__ for rule in RULES)
