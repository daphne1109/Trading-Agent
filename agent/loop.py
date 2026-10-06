"""The decision loop: one round every `decision_interval_s`.

    skip if killed/paused -> snapshot (skip if stale) -> model via router -> log decision
    -> shadow baselines -> RiskGate -> APPROVE: execute | NEEDS_APPROVAL: ask a human | else log

Every round writes a `decisions` row, including skips and HOLDs, so the log shows what the agent
chose *not* to do as well as what it did.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.clock import Clock
from agent.config import Settings
from agent.db.repo import EventLog, StateStore
from agent.db.trades import DecisionRecord, TradeRepo
from agent.decision.prompt import DecisionPrompt
from agent.decision.router import DecisionRouter
from agent.deriv.ws import DerivWS
from agent.execution.executor import Executor
from agent.market.feed import MarketFeed, StaleData
from agent.risk.gate import GateResult, Verdict, evaluate
from agent.risk.rules import RiskContext, RiskLimits
from agent.shadow import ShadowBook

log = logging.getLogger(__name__)

# Called for NEEDS_APPROVAL. Until the Telegram bot exists (P3) there is none: treated as REJECT.
ApprovalRequester = Callable[[int, GateResult], Awaitable[None]]


def limits_from(settings: Settings) -> RiskLimits:
    return RiskLimits(
        max_stake_usd=settings.max_stake_usd,
        max_open_positions=settings.max_open_positions,
        daily_loss_cap_usd=settings.daily_loss_cap_usd,
        cooldown_after_losses=settings.cooldown_after_losses,
        cooldown_minutes=settings.cooldown_minutes,
        min_confidence=settings.min_confidence,
        approval_band_high=settings.approval_band_high,
        stale_tick_s=settings.stale_tick_s,
        default_stake_usd=settings.stake_usd,
    )


@dataclass(frozen=True)
class RoundResult:
    decision_id: int
    outcome: str  # skipped | hold | rejected | approval_requested | executed:<outcome>


class DecisionLoop:
    def __init__(
        self,
        *,
        settings: Settings,
        clock: Clock,
        feed: MarketFeed,
        router: DecisionRouter,
        prompt: DecisionPrompt,
        repo: TradeRepo,
        state: StateStore,
        events: EventLog,
        executor: Executor,
        ws: DerivWS,
        shadow: ShadowBook,
        playbook_path: Path,
        request_approval: ApprovalRequester | None = None,
        lock_held: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._lock_held = lock_held
        self._s = settings
        self._clock = clock
        self._feed = feed
        self._router = router
        self._prompt = prompt
        self._repo = repo
        self._state = state
        self._events = events
        self._executor = executor
        self._ws = ws
        self._shadow = shadow
        self._playbook_path = playbook_path
        self._request_approval = request_approval
        self._limits = limits_from(settings)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - one bad round must not stop the agent
                log.exception("decision round failed")
                msg = f"{type(exc).__name__}: {exc}"
                await self._events.record("round_error", msg, {}, "error")
            await asyncio.sleep(self._s.decision_interval_s)

    # ------------------------------------------------------------------ one round

    async def run_once(self) -> RoundResult:
        killed, paused = await self._flags()
        if killed or paused:
            reason = "killed" if killed else "paused"
            return await self._skip(reason)
        try:
            snapshot = self._feed.snapshot(self._s.symbol)
        except StaleData as exc:
            await self._events.record("stale_skip", str(exc), {}, "warning")
            return await self._skip(f"stale: {exc}")

        stats = await self._repo.risk_stats(self._clock.now())
        state: dict[str, Any] = {
            **snapshot.to_state(),
            "open_position": stats.open_positions > 0,
            "today": {
                "pnl_usd": float(stats.daily_pnl_usd),
                "consecutive_losses": stats.consecutive_losses,
            },
            "playbook": self._playbook(),
        }
        routed = await self._router.decide(state, self._prompt.question)
        d = routed.decision
        decision_id = await self._repo.insert_decision(
            {
                "symbol": self._s.symbol,
                "provider": d.provider,
                "model": d.model,
                "prompt_hash": self._prompt.hash,
                "snapshot": {k: v for k, v in state.items() if k != "playbook"},
                "action": d.action.value,
                "probabilities": d.probabilities,
                "confidence": d.confidence,
                "latency_ms": d.latency_ms,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "cost_usd": d.cost_usd,
                "error": d.error,
                "provider_is_primary": routed.is_primary,
            }
        )
        if routed.attempts:
            level = "warning" if d.provider != "none" else "error"
            await self._events.record(
                "provider_fallback", "; ".join(routed.attempts), {"decision_id": decision_id}, level
            )
        await self._shadow.open(
            decision_id,
            snapshot.last_epoch,
            snapshot.quotes,
            d.action,
            d.probabilities.get("CALL"),
        )

        if not d.is_trade:
            await self._repo.insert_verdict(decision_id, Verdict.NO_TRADE.value, [], [])
            return RoundResult(decision_id, "hold")

        record = await self._repo.get_decision(decision_id)
        if record is None:  # never trade on a decision we can't read back
            await self._repo.insert_verdict(decision_id, Verdict.REJECT.value, ["unreadable"], [])
            return RoundResult(decision_id, "rejected")
        gate = evaluate(await self.build_context(record, self._s.stake_usd))
        await self._repo.insert_verdict(
            decision_id,
            gate.verdict.value,
            gate.reasons,
            [asdict(r) for r in gate.results],
        )
        if gate.verdict is Verdict.APPROVE:
            result = await self._executor.execute(decision_id)
            return RoundResult(decision_id, f"executed:{result.outcome}")
        if gate.verdict is Verdict.NEEDS_APPROVAL:
            if self._request_approval is None:
                await self._events.record(
                    "approval_unavailable",
                    "needs human approval but no approval channel is configured; not trading",
                    {"decision_id": decision_id},
                    "warning",
                )
                return RoundResult(decision_id, "rejected")
            await self._request_approval(decision_id, gate)
            return RoundResult(decision_id, "approval_requested")
        return RoundResult(decision_id, "rejected")

    # ------------------------------------------------------------------ context

    async def build_context(self, record: DecisionRecord, stake: Decimal) -> RiskContext:
        """Gather everything the gate needs, fresh, at the moment of asking.

        Called by the loop and again by the executor after re-quoting.
        """
        now = self._clock.now()
        killed, paused = await self._flags()
        stats = await self._repo.risk_stats(now)
        try:
            data_age: float | None = self._feed.snapshot(self._s.symbol).age_s
        except StaleData:
            data_age = None
        return RiskContext(
            now=now,
            action=record.action,
            confidence=record.confidence,
            stake_usd=stake,
            provider_is_primary=record.provider_is_primary,
            kill_switch=killed,
            paused=paused,
            ws_url_is_demo=self._ws.connection_is_demo(),
            open_positions=stats.open_positions,
            open_exposure_usd=stats.open_exposure_usd,
            daily_pnl_usd=stats.daily_pnl_usd,
            consecutive_losses=stats.consecutive_losses,
            last_loss_at=stats.last_loss_at,
            data_age_s=data_age,
            first_trade_after_resume=await self._state.get("first_trade_after_resume", False)
            is not False,
            limits=self._limits,
        )

    async def is_halted(self) -> bool:
        killed, paused = await self._flags()
        return killed or paused

    async def _flags(self) -> tuple[bool, bool]:
        """(killed, paused). Unknown or unreadable state counts as stopped (fail closed)."""
        try:
            if self._lock_held is not None and not await self._lock_held():
                log.critical("single-instance lock lost; halting trading")
                return True, True
            killed = self._s.kill_switch or await self._state.get("killed", False) is not False
            paused = await self._state.get("paused", False) is not False
        except Exception:  # noqa: BLE001
            log.exception("could not read agent state; treating as killed")
            return True, True
        return killed, paused

    async def _skip(self, reason: str) -> RoundResult:
        decision_id = await self._repo.insert_decision(
            {"symbol": self._s.symbol, "skipped_reason": reason}
        )
        return RoundResult(decision_id, "skipped")

    def _playbook(self) -> str:
        try:
            return self._playbook_path.read_text(encoding="utf-8")[:6000]
        except OSError:
            return ""
