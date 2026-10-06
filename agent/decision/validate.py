"""Strict validation of model answers. Anything odd raises InvalidModelOutput -> HOLD upstream."""

from __future__ import annotations

import math
from collections.abc import Mapping

from agent.decision.base import Action

PROB_SUM_TOLERANCE = 0.02


class InvalidModelOutput(ValueError):
    pass


def _unit_float(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidModelOutput(f"{what} is not a number: {value!r}")
    f = float(value)
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        raise InvalidModelOutput(f"{what} outside [0, 1]: {value!r}")
    return f


def validate_answer(
    choice: object, probabilities: object, confidence: object
) -> tuple[Action, dict[str, float], float]:
    """Return (action, probabilities, effective confidence).

    The effective confidence is the more cautious of the model's stated confidence and the
    probability it gave the option it chose, so a model can't claim certainty it didn't price.
    """
    try:
        action = Action(str(choice))
    except ValueError:
        raise InvalidModelOutput(f"unknown action {choice!r}") from None
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise InvalidModelOutput("probabilities missing")
    probs: dict[str, float] = {}
    for key, value in probabilities.items():
        if key not in Action.__members__:
            raise InvalidModelOutput(f"unknown probability key {key!r}")
        probs[str(key)] = _unit_float(value, f"probability[{key}]")
    if abs(sum(probs.values()) - 1.0) > PROB_SUM_TOLERANCE:
        raise InvalidModelOutput(f"probabilities sum to {sum(probs.values()):.3f}")
    if action.value not in probs:
        raise InvalidModelOutput(f"no probability for chosen action {action.value}")
    stated = _unit_float(confidence, "confidence")
    return action, probs, min(stated, probs[action.value])
