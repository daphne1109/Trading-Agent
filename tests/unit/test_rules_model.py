import pytest

from agent.decision.base import Action
from agent.decision.rules_model import RuleBasedModel
from agent.decision.validate import validate_answer


@pytest.mark.parametrize(
    ("z", "streak", "expected"),
    [
        (2.0, 3, Action.CALL),
        (-2.0, -3, Action.PUT),
        (2.0, -1, Action.HOLD),  # move not confirmed by the streak
        (0.3, 4, Action.HOLD),
        (0.0, 0, Action.HOLD),
    ],
)
async def test_rule_model_directions(z, streak, expected):
    d = await RuleBasedModel().decide({"indicators": {"zscore_60": z, "streak": streak}}, {})
    assert d.action is expected
    assert d.provider == "rules"
    # its answers must pass the same validation as a real model's
    validate_answer(d.action.value, d.probabilities, d.confidence)


async def test_rule_model_confidence_is_capped():
    d = await RuleBasedModel().decide({"indicators": {"zscore_60": 9, "streak": 9}}, {})
    assert d.confidence <= 0.72
