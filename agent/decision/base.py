"""The decision model contract: what every provider returns, whatever it runs on."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol


class Action(StrEnum):
    CALL = "CALL"
    PUT = "PUT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    action: Action
    probabilities: dict[str, float]
    confidence: float
    provider: str
    model: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    calibrated: bool = True
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_trade(self) -> bool:
        return self.action in (Action.CALL, Action.PUT)

    @classmethod
    def hold(cls, provider: str, model: str, error: str | None = None, **kw: Any) -> Decision:
        """A safe no-trade decision, used for every failure path."""
        return cls(
            action=Action.HOLD,
            probabilities={},
            confidence=0.0,
            provider=provider,
            model=model,
            error=error,
            **kw,
        )


class DecisionModel(Protocol):
    name: str

    async def decide(self, state: dict[str, Any], instructions: dict[str, Any]) -> Decision: ...
