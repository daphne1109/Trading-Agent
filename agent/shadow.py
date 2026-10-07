"""Shadow baselines: paper decisions by simple strategies on the same snapshot as the model.

No trades are placed. Each paper decision (and the model's own direction, whether or not the
risk gate let it trade) is settled against the real price `duration_ticks` later, so the
dashboard can answer "is the model any better than a coin flip?" with data.

Rise/Fall rules: CALL wins if the exit quote is strictly higher, PUT if strictly lower; a tie
loses. A win pays `payout_ratio - 1` per unit stake, a loss costs 1, HOLD is 0.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from psycopg_pool import AsyncConnectionPool

from agent.decision.base import Action
from agent.deriv.schemas import Tick


def coin_flip(seed: int) -> Action:
    return random.Random(seed).choice((Action.CALL, Action.PUT))


def momentum(quotes: Sequence[float], lookback: int = 10) -> Action:
    if len(quotes) <= lookback:
        return Action.HOLD
    move = quotes[-1] - quotes[-1 - lookback]
    return Action.CALL if move > 0 else Action.PUT if move < 0 else Action.HOLD


def baseline_actions(decision_id: int, quotes: Sequence[float]) -> dict[str, Action]:
    return {
        "coin_flip": coin_flip(decision_id),
        "always_hold": Action.HOLD,
        "momentum_10": momentum(quotes),
    }


def settle(action: Action, entry: float, exit_: float, payout_ratio: Decimal) -> Decimal:
    """Paper profit per 1 unit of stake."""
    if action is Action.HOLD:
        return Decimal(0)
    won = exit_ > entry if action is Action.CALL else exit_ < entry
    return payout_ratio - 1 if won else Decimal(-1)


@dataclass
class _Pending:
    decision_id: int
    decided_at_epoch: int  # last tick the decision saw
    actions: dict[str, Action]
    entry_epoch: int | None = None  # first tick after the decision (Deriv's entry spot)
    entry_quote: float | None = None


class ShadowBook:
    def __init__(
        self, pool: AsyncConnectionPool, *, duration_ticks: int, payout_ratio: Decimal
    ) -> None:
        self._pool = pool
        self._duration = duration_ticks
        self._payout = payout_ratio
        self._pending: list[_Pending] = []

    async def open(
        self,
        decision_id: int,
        entry_epoch: int,
        quotes: Sequence[float],
        model_action: Action,
        p_call: float | None,
    ) -> None:
        actions = {"model": model_action, **baseline_actions(decision_id, quotes)}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO shadow_decisions (decision_id, strategy, action, p_call) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (decision_id, name, a.value, p_call if name == "model" else None)
                    for name, a in actions.items()
                ],
            )
        self._pending.append(_Pending(decision_id, entry_epoch, actions))

    async def on_tick(self, tick: Tick) -> int:
        """Advance pending paper decisions with Deriv's Rise/Fall timing, the same as a real
        (or paper-executed) contract: entry = first tick after the decision, exit = the
        `duration_ticks`-th tick after entry. Returns how many settled on this tick."""
        due: list[tuple[_Pending, float, float]] = []
        for p in self._pending:
            if p.entry_quote is None:
                if tick.epoch > p.decided_at_epoch:
                    p.entry_epoch, p.entry_quote = tick.epoch, tick.quote
                continue
            assert p.entry_epoch is not None
            if tick.epoch >= p.entry_epoch + self._duration:
                due.append((p, p.entry_quote, tick.quote))
        if not due:
            return 0
        settled = {id(p) for p, _, _ in due}
        self._pending = [p for p in self._pending if id(p) not in settled]
        rows = []
        for p, entry, exit_ in due:
            up = exit_ > entry
            for name, action in p.actions.items():
                profit = settle(action, entry, exit_, self._payout)
                rows.append((up, profit, p.decision_id, name))
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "UPDATE shadow_decisions SET settled_up = %s, paper_profit = %s "
                "WHERE decision_id = %s AND strategy = %s",
                rows,
            )
        return len(due)
