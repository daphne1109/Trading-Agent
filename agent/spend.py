"""Per-provider daily spend caps (UTC day). Checked BEFORE each model call."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from agent.clock import Clock


class SpendTracker:
    def __init__(
        self,
        clock: Clock,
        caps_usd: dict[str, Decimal],
        spent_today_usd: dict[str, Decimal] | None = None,
    ) -> None:
        """`spent_today_usd` seeds totals from the decision log so a restart can't reset them."""
        self._clock = clock
        self._caps = caps_usd
        self._day: date = clock.now().date()
        self._spent: defaultdict[str, Decimal] = defaultdict(Decimal, spent_today_usd or {})

    def _roll(self) -> None:
        today = self._clock.now().date()
        if today != self._day:
            self._day = today
            self._spent.clear()

    def allowed(self, provider: str) -> bool:
        """False once today's spend reached the cap. Providers without a cap are refused."""
        self._roll()
        cap = self._caps.get(provider)
        return cap is not None and self._spent[provider] < cap

    def add(self, provider: str, usd: Decimal) -> None:
        self._roll()
        self._spent[provider] += usd

    def spent(self, provider: str) -> Decimal:
        self._roll()
        return self._spent[provider]
