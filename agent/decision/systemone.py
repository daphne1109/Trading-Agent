"""Client for SystemOne-compatible decision models: TypeSafe Jev and Cloudflare Clef.

Request:  {"model", "state", "questions": {"action": {"type": "choice", ...}}}
Response: {"answers": {"action": {"choice", "probabilities", "confidence"}}, "usage": {...}}
Cloudflare Workers AI wraps the response as {"result": {...}, "success": true}.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from agent.decision.base import Decision
from agent.decision.validate import InvalidModelOutput, validate_answer

MILLION = Decimal(1_000_000)


class SystemOneModel:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        api_key: str,
        model: str,
        http: httpx.AsyncClient,
        usd_per_m_input: Decimal,
        send_model_in_body: bool = True,
    ) -> None:
        self.name = name
        self._url = url
        self._api_key = api_key
        self._model = model
        self._http = http
        self._price = usd_per_m_input
        self._send_model = send_model_in_body

    async def decide(self, state: dict[str, Any], question: dict[str, Any]) -> Decision:
        body: dict[str, Any] = {"state": state, "questions": {"action": question}}
        if self._send_model:
            body["model"] = self._model
        started = time.perf_counter()
        response = await self._http.post(
            self._url, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        return self.parse(response.json(), latency_ms)

    def parse(self, data: Any, latency_ms: int) -> Decision:
        wrapped = isinstance(data, dict) and "answers" not in data
        if wrapped and isinstance(data.get("result"), dict):
            data = data["result"]
        try:
            answer = data["answers"]["action"]
            usage = data.get("usage") or {}
        except (KeyError, TypeError, AttributeError):
            raise InvalidModelOutput("response has no answers.action") from None
        if not isinstance(answer, dict) or not isinstance(usage, dict):
            raise InvalidModelOutput("answers.action / usage must be objects")
        action, probs, confidence = validate_answer(
            answer.get("choice"), answer.get("probabilities"), answer.get("confidence")
        )
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        return Decision(
            action=action,
            probabilities=probs,
            confidence=confidence,
            provider=self.name,
            model=self._model,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cost_usd=Decimal(input_tokens) * self._price / MILLION,  # output tokens are free
            raw=answer,
        )
