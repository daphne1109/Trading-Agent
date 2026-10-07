"""Rule-based stand-in for the decision model, used only when no AI provider key is set.

It is NOT AI and is labelled as such everywhere it appears (provider "rules"). It exists so the
full pipeline (risk gate, execution, logging, dashboard) can run before any AI key is configured.
It is only ever enabled in paper mode (agent/main.py), never with a demo account.
Logic: lean with a strong short-term move that is confirmed by a streak; otherwise HOLD.
"""

from __future__ import annotations

from typing import Any

from agent.decision.base import Action, Decision

ZSCORE_TRIGGER = 1.2
STREAK_TRIGGER = 2


class RuleBasedModel:
    name = "rules"

    async def decide(self, state: dict[str, Any], question: dict[str, Any]) -> Decision:
        ind = state.get("indicators") or {}
        z = float(ind.get("zscore_60", 0.0))
        streak = float(ind.get("streak", 0.0))
        if z >= ZSCORE_TRIGGER and streak >= STREAK_TRIGGER:
            action, lean = Action.CALL, min(0.72, 0.6 + 0.04 * (z - ZSCORE_TRIGGER) + 0.02 * streak)
        elif z <= -ZSCORE_TRIGGER and streak <= -STREAK_TRIGGER:
            action, lean = Action.PUT, min(0.72, 0.6 + 0.04 * (-z - ZSCORE_TRIGGER) - 0.02 * streak)
        else:
            return Decision(
                action=Action.HOLD,
                probabilities={"CALL": 0.25, "PUT": 0.25, "HOLD": 0.5},
                confidence=0.5,
                provider=self.name,
                model="momentum-rules-v1",
                calibrated=False,
            )
        other = Action.PUT if action is Action.CALL else Action.CALL
        rest = 1 - lean
        return Decision(
            action=action,
            probabilities={action.value: lean, other.value: rest * 0.8, "HOLD": rest * 0.2},
            confidence=lean,
            provider=self.name,
            model="momentum-rules-v1",
            calibrated=False,
        )
