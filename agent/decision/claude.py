"""Last-resort decision fallback: Claude Haiku 4.5 with structured output.

Its probabilities are self-reported, not trained for calibration, so decisions are marked
calibrated=False and (being a fallback) always need human approval via the risk gate.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.decision.base import Decision
from agent.decision.validate import InvalidModelOutput, validate_answer

MODEL = "claude-haiku-4-5"
USD_PER_M_INPUT = Decimal("1")
USD_PER_M_OUTPUT = Decimal("5")
MILLION = Decimal(1_000_000)


class _Answer(BaseModel):
    action: Literal["CALL", "PUT", "HOLD"]
    p_call: float = Field(ge=0, le=1)
    p_put: float = Field(ge=0, le=1)
    p_hold: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


class ClaudeDecisionModel:
    name = "claude"

    def __init__(self, client: Any, model: str = MODEL) -> None:
        self._client = client  # anthropic.AsyncAnthropic (injected so tests can fake it)
        self._model = model

    async def decide(self, state: dict[str, Any], question: dict[str, Any]) -> Decision:
        prompt = (
            f"{question['instructions']}\n\n"
            f"Options:\n{json.dumps(question['criteria'], indent=1)}\n\n"
            f"State:\n{json.dumps(state)}\n\n"
            "Give a probability for each option (they must sum to 1), the chosen action, and "
            "your confidence in it."
        )
        started = time.perf_counter()
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=_Answer,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        answer = response.parsed_output
        if answer is None:
            raise InvalidModelOutput(f"no parsed output (stop_reason={response.stop_reason})")
        action, probs, confidence = validate_answer(
            answer.action,
            {"CALL": answer.p_call, "PUT": answer.p_put, "HOLD": answer.p_hold},
            answer.confidence,
        )
        usage = response.usage
        cost = (
            Decimal(usage.input_tokens) * USD_PER_M_INPUT
            + Decimal(usage.output_tokens) * USD_PER_M_OUTPUT
        ) / MILLION
        return Decision(
            action=action,
            probabilities=probs,
            confidence=confidence,
            provider=self.name,
            model=self._model,
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            calibrated=False,
            raw=answer.model_dump(),
        )
