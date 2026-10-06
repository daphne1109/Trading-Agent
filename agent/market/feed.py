"""In-memory market feed: a ring buffer of recent ticks per symbol, with a stale-data guard."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from agent.clock import Clock
from agent.deriv.schemas import Tick
from agent.market import indicators


class StaleData(Exception):
    """The feed can't support a decision right now (old, missing or too little data)."""


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    quotes: tuple[float, ...]
    last_epoch: int
    age_s: float
    indicators: dict[str, float] = field(default_factory=dict)

    def to_state(self, recent: int = 60) -> dict[str, object]:
        """Compact form for the decision model."""
        return {
            "symbol": self.symbol,
            "last_ticks": [round(q, 3) for q in self.quotes[-recent:]],
            "indicators": self.indicators,
        }


@dataclass(frozen=True)
class _Point:
    tick: Tick
    received_at: datetime


class MarketFeed:
    def __init__(
        self, clock: Clock, *, stale_after_s: float, min_history: int, maxlen: int
    ) -> None:
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._min_history = min_history
        self._maxlen = maxlen
        self._buffers: dict[str, deque[_Point]] = {}

    def on_tick(self, tick: Tick) -> None:
        buf = self._buffers.setdefault(tick.symbol, deque(maxlen=self._maxlen))
        if buf and tick.epoch < buf[-1].tick.epoch:
            return  # out-of-order tick after a reconnect: ignore it
        buf.append(_Point(tick, self._clock.now()))

    def size(self, symbol: str) -> int:
        return len(self._buffers.get(symbol, ()))

    def snapshot(self, symbol: str) -> Snapshot:
        buf = self._buffers.get(symbol)
        if not buf:
            raise StaleData(f"no ticks for {symbol}")
        age = (self._clock.now() - buf[-1].received_at).total_seconds()
        if age > self._stale_after_s:
            raise StaleData(f"last tick for {symbol} is {age:.1f}s old")
        if len(buf) < self._min_history:
            raise StaleData(f"only {len(buf)}/{self._min_history} ticks of history for {symbol}")
        quotes = tuple(p.tick.quote for p in buf)
        return Snapshot(
            symbol=symbol,
            quotes=quotes,
            last_epoch=buf[-1].tick.epoch,
            age_s=age,
            indicators=indicators.compute(quotes),
        )
