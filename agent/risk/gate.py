"""RiskGate: the only thing that can say yes to a trade. The model's answer is just an input."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent.decision.base import Action
from agent.risk.rules import RULES, RiskContext, Rule, RuleResult


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class GateResult:
    verdict: Verdict
    results: tuple[RuleResult, ...]

    @property
    def reasons(self) -> list[str]:
        """Why it was blocked or escalated (empty for a clean APPROVE)."""
        return [r.reason for r in self.results if not r.ok or r.needs_approval]

    @property
    def failed_rules(self) -> list[str]:
        return [r.rule for r in self.results if not r.ok]


def _run(rule: Rule, ctx: RiskContext) -> RuleResult:
    """A rule that crashes counts as a failed rule, never as a pass."""
    name = getattr(rule, "__name__", repr(rule))
    try:
        result = rule(ctx)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        return RuleResult(name, False, f"rule error: {type(exc).__name__}: {exc}")
    if not isinstance(result, RuleResult):
        return RuleResult(name, False, "rule returned a non-RuleResult")
    return result


def evaluate(ctx: RiskContext, rules: tuple[Rule, ...] = RULES) -> GateResult:
    """Run every rule. Any failure rejects; any approval trigger escalates; else approve.

    `rules` exists so tests can add extra rules; it must always contain every required rule.
    """
    if ctx.action is Action.HOLD:
        return GateResult(Verdict.NO_TRADE, ())
    if ctx.action not in (Action.CALL, Action.PUT):
        return GateResult(
            Verdict.REJECT, (RuleResult("action_valid", False, f"unknown action {ctx.action!r}"),)
        )
    # Identity, not name: a look-alike function called `kill_switch` must not count.
    missing = [rule.__name__ for rule in RULES if not any(r is rule for r in rules)]
    if missing:
        return GateResult(
            Verdict.REJECT,
            (RuleResult("rule_set", False, f"required rules missing: {missing}"),),
        )
    results = tuple(_run(rule, ctx) for rule in rules)
    if any(not r.ok for r in results):
        return GateResult(Verdict.REJECT, results)
    if any(r.needs_approval for r in results):
        return GateResult(Verdict.NEEDS_APPROVAL, results)
    return GateResult(Verdict.APPROVE, results)
