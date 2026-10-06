"""Tries decision providers in order (Jev -> Clef -> Claude) with timeouts, circuit breakers
and spend caps. It never raises: if nothing answers validly, the result is HOLD."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent.clock import Clock
from agent.decision.base import Decision, DecisionModel
from agent.spend import SpendTracker


@dataclass(frozen=True)
class RoutedDecision:
    decision: Decision
    is_primary: bool
    attempts: tuple[str, ...] = field(default_factory=tuple)  # "jev: TimeoutError", ...


@dataclass
class _Breaker:
    failures: int = 0
    open_until: datetime | None = None


class DecisionRouter:
    def __init__(
        self,
        providers: list[DecisionModel],
        spend: SpendTracker,
        clock: Clock,
        *,
        timeout_s: float = 3.0,
        breaker_threshold: int = 3,
        breaker_cooldown_s: float = 300.0,
    ) -> None:
        if not providers:
            raise ValueError("at least one decision provider is required")
        self._providers = providers
        self._spend = spend
        self._clock = clock
        self._timeout_s = timeout_s
        self._threshold = breaker_threshold
        self._cooldown = timedelta(seconds=breaker_cooldown_s)
        self._breakers = {p.name: _Breaker() for p in providers}

    def breaker_open(self, name: str) -> bool:
        b = self._breakers[name]
        if b.open_until is None:
            return False
        if self._clock.now() >= b.open_until:
            b.open_until, b.failures = None, 0  # half-open: allow a fresh attempt
            return False
        return True

    def _failed(self, name: str) -> None:
        b = self._breakers[name]
        b.failures += 1
        if b.failures >= self._threshold:
            b.open_until = self._clock.now() + self._cooldown

    async def decide(self, state: dict[str, Any], question: dict[str, Any]) -> RoutedDecision:
        attempts: list[str] = []
        for index, provider in enumerate(self._providers):
            name = provider.name
            if self.breaker_open(name):
                attempts.append(f"{name}: circuit open")
                continue
            if not self._spend.allowed(name):
                attempts.append(f"{name}: daily spend cap reached")
                continue
            try:
                decision = await asyncio.wait_for(provider.decide(state, question), self._timeout_s)
            except Exception as exc:  # noqa: BLE001 - any provider failure means "try the next"
                self._failed(name)
                attempts.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
                continue
            self._breakers[name].failures = 0
            self._spend.add(name, decision.cost_usd)
            return RoutedDecision(decision, is_primary=index == 0, attempts=tuple(attempts))

        hold = Decision.hold("none", "none", error="; ".join(attempts) or "no provider")
        return RoutedDecision(hold, is_primary=False, attempts=tuple(attempts))
