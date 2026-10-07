"""PaperExecutor: identical checks to the live executor, but the order is filled on paper.

Used when Deriv's trading API isn't available (it isn't offered to Malaysian residents).
Everything up to the order is the same, inherited unchanged: decision from the DB, connection
guard, a *real* Deriv quote, the RiskGate re-check, the DB-verified approval and the idempotent
claim. Then, instead of sending `buy`, the trade is filled at the quoted price and settled on
Deriv's live ticks with Rise/Fall rules:

  entry spot = the first tick after the fill; exit spot = the `duration_ticks`-th tick after it;
  CALL wins if exit > entry, PUT if exit < entry; a tie loses.
  win -> profit = payout - stake; loss -> profit = -stake.

This class never sends `buy` to Deriv: there is no code path for it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from agent.clock import Clock
from agent.config import Settings
from agent.db.repo import EventLog
from agent.db.trades import TradeRepo
from agent.deriv.ws import DerivWS
from agent.execution.executor import (
    ContextBuilder,
    ExecutionResult,
    Executor,
    HaltCheck,
    Outcome,
)
from agent.market.feed import MarketFeed

POLL_S = 0.25


class PaperExecutor(Executor):
    def __init__(
        self,
        ws: DerivWS,
        repo: TradeRepo,
        events: EventLog,
        settings: Settings,
        clock: Clock,
        build_context: ContextBuilder,
        is_halted: HaltCheck,
        feed: MarketFeed,
        **kw: Any,
    ) -> None:
        super().__init__(ws, repo, events, settings, clock, build_context, is_halted, **kw)
        self._feed = feed
        # contract_id -> (contract_type, fill_epoch, stake, payout, ws reconnect count at fill)
        self._fills: dict[str, tuple[str, int, Decimal, Decimal, int]] = {}

    async def _place(
        self,
        trade_id: int,
        contract_type: str,
        proposal: dict[str, Any],
        ask_price: Decimal,
        payout: Decimal | None,
    ) -> ExecutionResult:
        if payout is None or payout <= ask_price:
            await self._repo.mark_error(trade_id, f"paper fill refused: payout {payout}")
            return ExecutionResult(Outcome.ERROR, "quote had no usable payout", trade_id)
        fill_epoch = self._feed.last_epoch(self._s.symbol)
        if fill_epoch is None:
            await self._repo.mark_error(trade_id, "paper fill refused: no market data")
            return ExecutionResult(Outcome.ERROR, "no market data to fill against", trade_id)
        contract_id = f"paper-{trade_id}"
        self._fills[contract_id] = (
            contract_type, fill_epoch, ask_price, payout, self._ws.reconnects,
        )  # fmt: skip
        raw = {
            "mode": "paper",
            "proposal_id": proposal.get("id"),
            "ask_price": str(ask_price),
            "payout": str(payout),
            "fill_epoch": fill_epoch,
        }
        await self._repo.mark_open(trade_id, contract_id, ask_price, payout, self._clock.now(), raw)
        self.watch(trade_id, contract_id)
        return ExecutionResult(Outcome.BOUGHT, f"PAPER {contract_type} {contract_id}", trade_id)

    async def resume_open_trades(self) -> int:
        """Paper fills live in memory, so after a restart their entry tick is unknown. They are
        marked `unknown` (counted against the limits as a loss), never silently dropped."""
        for trade_id, _contract_id, _status in await self._repo.open_trades():
            await self._repo.mark_unknown(trade_id, "paper trade interrupted by restart")
            await self._events.record(
                "trade_unknown", "paper trade interrupted by restart", {"trade_id": trade_id},
                "warning",
            )  # fmt: skip
        return 0  # nothing to follow

    async def _watch(self, trade_id: int, contract_id: str) -> None:
        contract_type, fill_epoch, stake, payout, reconnects_at_fill = self._fills[contract_id]
        needed = self._s.duration_ticks + 1  # entry tick + N ticks after it
        try:
            async with asyncio.timeout(self.settle_timeout_s):
                ticks = self._feed.ticks_after(self._s.symbol, fill_epoch)
                while len(ticks) < needed:
                    await asyncio.sleep(POLL_S)
                    ticks = self._feed.ticks_after(self._s.symbol, fill_epoch)
        except TimeoutError:
            await self._repo.mark_unknown(trade_id, "paper trade: exit tick never arrived")
            await self._events.record(
                "settlement_timeout", f"{contract_id} had no exit tick", {"trade_id": trade_id},
                "warning",
            )  # fmt: skip
            return
        window = ticks[:needed]
        gapless = all(b.epoch - a.epoch == 1 for a, b in zip(window, window[1:], strict=False))
        if self._ws.reconnects != reconnects_at_fill or not gapless:
            # A reconnect or a gap means we can't be sure which tick Deriv would have used.
            reason = "reconnect during contract" if gapless else "gap in ticks during contract"
            await self._repo.mark_unknown(trade_id, f"paper trade: {reason}")
            await self._events.record(
                "trade_unknown", f"{contract_id}: {reason}", {"trade_id": trade_id}, "warning"
            )
            self._fills.pop(contract_id, None)
            return
        entry, exit_ = window[0], window[self._s.duration_ticks]
        won = exit_.quote > entry.quote if contract_type == "CALL" else exit_.quote < entry.quote
        profit = payout - stake if won else -stake
        status = "won" if won else "lost"
        raw = {
            "mode": "paper",
            "entry": {"epoch": entry.epoch, "quote": entry.quote},
            "exit": {"epoch": exit_.epoch, "quote": exit_.quote},
        }
        await self._repo.settle(trade_id, status, profit, self._clock.now(), raw)
        self._fills.pop(contract_id, None)
        await self._events.record(
            "trade_settled",
            f"paper {status} {profit} ({entry.quote} -> {exit_.quote})",
            {"trade_id": trade_id, "contract_id": contract_id},
        )
