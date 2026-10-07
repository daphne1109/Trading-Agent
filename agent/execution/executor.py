"""Executor: the only code that sends `buy`.

Every trade passes these steps, and any of them can stop it:
  1. load the decision from the DB (never trust the caller) and refuse stale ones;
  2. the live connection must still be the demo endpoint;
  3. re-quote at the configured stake; the quote must equal that stake exactly;
  4. re-run the RiskGate on fresh state. Buy on APPROVE; on NEEDS_APPROVAL only if the DB holds
     an unexpired human approval that covered every current trigger. REJECT always wins;
  5. claim the decision in the DB (UNIQUE decision_id) so it can never buy twice;
  6. re-check kill/pause and the demo guard, then `buy` at the quoted price;
  7. an uncertain buy (timeout, dropped socket) is `unknown`: counted as open and as a full
     loss until resolved. Only a definite rejection by Deriv is `error`;
  8. follow the contract until it settles.
Buys are serialised in-process with a lock; the agent's single-instance DB lock covers the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from agent.clock import Clock
from agent.config import Settings
from agent.db.repo import EventLog
from agent.db.trades import DecisionRecord, TradeRepo
from agent.decision.base import Action
from agent.deriv.ws import DerivError, DerivWS
from agent.risk.gate import GateResult, Verdict, evaluate
from agent.risk.rules import RiskContext

CONTRACT_TYPES = {Action.CALL: "CALL", Action.PUT: "PUT"}
SETTLE_GRACE_S = 120.0


class Outcome(StrEnum):
    BOUGHT = "bought"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutionResult:
    outcome: Outcome
    detail: str = ""
    trade_id: int | None = None


ContextBuilder = Callable[[DecisionRecord, Decimal], Awaitable[RiskContext]]
HaltCheck = Callable[[], Awaitable[bool]]


def _money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a money amount: {value!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return d


def _triggers(reasons: list[str] | tuple[str, ...]) -> set[str]:
    return {part.strip() for r in reasons for part in r.split(";") if part.strip()}


class Executor:
    def __init__(
        self,
        ws: DerivWS,
        repo: TradeRepo,
        events: EventLog,
        settings: Settings,
        clock: Clock,
        build_context: ContextBuilder,
        is_halted: HaltCheck,
        *,
        request_timeout_s: float = 10.0,
    ) -> None:
        self._timeout = request_timeout_s
        # 1 tick per second on 1HZ indices; generous grace for reconnects in between.
        self.settle_timeout_s = SETTLE_GRACE_S + settings.duration_ticks * 2
        self._ws = ws
        self._repo = repo
        self._events = events
        self._s = settings
        self._clock = clock
        self._build_context = build_context
        self._is_halted = is_halted
        self._lock = asyncio.Lock()
        self._watchers: set[asyncio.Task[None]] = set()
        # A decision may wait for a human for up to approval_timeout_s before it executes.
        self._max_decision_age = timedelta(seconds=settings.approval_timeout_s + 30)

    async def execute(self, decision_id: int) -> ExecutionResult:
        async with self._lock:
            result = await self._execute_locked(decision_id)
        level = {Outcome.BOUGHT: "info", Outcome.UNKNOWN: "critical"}.get(result.outcome, "warning")
        await self._events.record(
            f"execution_{result.outcome}",
            result.detail or result.outcome.value,
            {"decision_id": decision_id, "trade_id": result.trade_id},
            level,
        )
        return result

    async def _execute_locked(self, decision_id: int) -> ExecutionResult:
        record = await self._repo.get_decision(decision_id)
        if record is None or record.action not in CONTRACT_TYPES:
            return ExecutionResult(Outcome.BLOCKED, "no tradeable decision with that id")
        age = self._clock.now() - record.created_at
        if not timedelta(seconds=-5) <= age <= self._max_decision_age:
            age_s = age.total_seconds()
            return ExecutionResult(Outcome.BLOCKED, f"decision is stale ({age_s:.0f}s)")
        if not self._ws.connection_is_demo():
            return ExecutionResult(Outcome.BLOCKED, "connection is not the demo endpoint")

        stake = self._s.stake_usd
        contract_type = CONTRACT_TYPES[record.action]
        try:
            reply = await self._ws.request(
                self._proposal_request(contract_type, stake), self._timeout
            )
            proposal = reply["proposal"]
            ask_price = _money(proposal["ask_price"])
            payout = _money(proposal["payout"]) if "payout" in proposal else None
        except Exception as exc:  # noqa: BLE001 - no quote, no trade
            return ExecutionResult(Outcome.ERROR, f"proposal failed: {type(exc).__name__}: {exc}")
        if ask_price != stake:
            return ExecutionResult(Outcome.BLOCKED, f"quoted {ask_price}, expected stake {stake}")

        try:
            gate = evaluate(await self._build_context(record, ask_price))
        except Exception as exc:  # noqa: BLE001 - can't assess risk, can't trade
            return ExecutionResult(Outcome.ERROR, f"risk context failed: {type(exc).__name__}")
        blocked = await self._blocked_by_gate(decision_id, gate)
        if blocked:
            return ExecutionResult(Outcome.BLOCKED, blocked)

        trade_id = await self._repo.create_pending(
            decision_id, contract_type, self._s.symbol, ask_price, self._s.duration_ticks
        )
        if trade_id is None:
            return ExecutionResult(Outcome.DUPLICATE, "decision already executed")

        if await self._halted() or not self._ws.connection_is_demo():
            await self._repo.mark_error(trade_id, "halted or not demo just before buy")
            return ExecutionResult(Outcome.BLOCKED, "halted or not demo just before buy", trade_id)
        return await self._place(trade_id, contract_type, proposal, ask_price, payout)

    async def _place(
        self,
        trade_id: int,
        contract_type: str,
        proposal: dict[str, Any],
        ask_price: Decimal,
        payout: Decimal | None,
    ) -> ExecutionResult:
        """Send `buy` for the claimed trade. Every check has already passed."""
        try:
            reply = await self._ws.request(
                {"buy": proposal["id"], "price": float(ask_price)}, self._timeout
            )
        except DerivError as exc:  # Deriv definitely refused: nothing was bought
            await self._repo.mark_error(trade_id, f"buy rejected: {exc}")
            return ExecutionResult(Outcome.ERROR, f"buy rejected: {exc}", trade_id)
        except Exception as exc:  # noqa: BLE001 - sent, but we don't know if it happened
            await self._repo.mark_unknown(trade_id, f"buy outcome unknown: {type(exc).__name__}")
            return ExecutionResult(Outcome.UNKNOWN, "buy sent but outcome unknown", trade_id)
        try:
            bought = reply["buy"]
            contract_id = str(bought["contract_id"])
            buy_price = _money(bought.get("buy_price", ask_price))
        except Exception as exc:  # noqa: BLE001 - malformed confirmation: treat as maybe-bought
            await self._repo.mark_unknown(trade_id, f"bad buy confirmation: {exc}")
            return ExecutionResult(Outcome.UNKNOWN, "unreadable buy confirmation", trade_id)

        await self._repo.mark_open(
            trade_id, contract_id, buy_price, payout, self._clock.now(), dict(bought)
        )
        self.watch(trade_id, contract_id)
        return ExecutionResult(Outcome.BOUGHT, f"{contract_type} {contract_id}", trade_id)

    async def _blocked_by_gate(self, decision_id: int, gate: GateResult) -> str | None:
        """None if the trade may proceed, else the reason it may not."""
        if gate.verdict is Verdict.APPROVE:
            return None
        if gate.verdict is not Verdict.NEEDS_APPROVAL:
            return f"re-check {gate.verdict}: {'; '.join(gate.reasons)}"
        approval = await self._repo.get_approval(decision_id)
        if approval is None or approval.status != "approved" or approval.responded_at is None:
            return "needs human approval"
        if approval.expires_at is None or approval.responded_at > approval.expires_at:
            return "approval arrived after it expired"
        if self._clock.now() > approval.expires_at:
            return "approval expired before execution"
        new = _triggers(gate.reasons) - _triggers(approval.reasons)
        if new:
            return f"new approval triggers since the human approved: {sorted(new)}"
        return None

    async def _halted(self) -> bool:
        try:
            return await self._is_halted()
        except Exception:  # noqa: BLE001 - unreadable control state counts as halted
            return True

    def _proposal_request(self, contract_type: str, stake: Decimal) -> dict[str, Any]:
        return {
            "proposal": 1,
            "amount": float(stake),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": self._s.currency,
            "duration": self._s.duration_ticks,
            "duration_unit": "t",
            "underlying_symbol": self._s.symbol,
        }

    # ------------------------------------------------------------------ settlement

    def watch(self, trade_id: int, contract_id: str) -> None:
        task = asyncio.create_task(self._watch(trade_id, contract_id), name=f"watch-{trade_id}")
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def resume_open_trades(self) -> int:
        """After a restart, keep following contracts that were open when the agent stopped."""
        resumed = 0
        for trade_id, contract_id, _status in await self._repo.open_trades():
            if contract_id is None:  # stopped between claim and buy confirmation
                await self._repo.mark_unknown(trade_id, "interrupted before buy confirmation")
                await self._events.record(
                    "trade_unknown",
                    "interrupted before buy confirmation",
                    {"trade_id": trade_id},
                    "critical",
                )
                continue
            self.watch(trade_id, contract_id)
            resumed += 1
        return resumed

    async def _watch(self, trade_id: int, contract_id: str) -> None:
        cid: int | str = int(contract_id) if contract_id.isdigit() else contract_id
        queue = await self._ws.subscribe({"proposal_open_contract": 1, "contract_id": cid})
        deadline = self.settle_timeout_s
        try:
            async with asyncio.timeout(deadline):
                while True:
                    msg = await queue.get()
                    poc = msg.get("proposal_open_contract") or {}
                    if not (poc.get("is_sold") or poc.get("status") in ("won", "lost", "sold")):
                        continue
                    profit = _money(poc.get("profit", 0))
                    status = poc.get("status")
                    if status not in ("won", "lost", "sold"):
                        status = "won" if profit > 0 else "lost"
                    await self._repo.settle(trade_id, status, profit, self._clock.now(), poc)
                    await self._events.record(
                        "trade_settled",
                        f"{status} {profit}",
                        {"trade_id": trade_id, "contract_id": contract_id},
                    )
                    return
        except TimeoutError:
            await self._repo.mark_unknown(trade_id, f"not settled after {deadline:.0f}s")
            await self._events.record(
                "settlement_timeout",
                f"contract {contract_id} not settled after {deadline:.0f}s; marked unknown",
                {"trade_id": trade_id},
                "critical",
            )
        finally:
            with contextlib.suppress(Exception):
                await self._ws.unsubscribe(queue)

    async def close(self) -> None:
        for task in list(self._watchers):
            task.cancel()
        await asyncio.gather(*self._watchers, return_exceptions=True)
