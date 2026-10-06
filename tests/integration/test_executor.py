import asyncio
import contextlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from agent.clock import SystemClock
from agent.config import Settings
from agent.db.migrate import migrate
from agent.db.repo import EventLog
from agent.db.trades import DecisionRecord, TradeRepo
from agent.deriv.ws import DerivWS
from agent.execution.executor import Executor, Outcome
from agent.loop import limits_from
from agent.risk.rules import RiskContext
from tests.fakes.fake_deriv_server import FakeDerivServer

pytestmark = pytest.mark.integration

SETTINGS = Settings(_env_file=None, deriv_ws_allowed_hosts=frozenset({"127.0.0.1"}))
LIMITS = limits_from(SETTINGS)


class Harness:
    def __init__(self, pool, server, ws, repo, executor, overrides, control):
        self.pool, self.server, self.ws, self.repo = pool, server, ws, repo
        self.executor, self.overrides, self.control = executor, overrides, control

    async def decision(self, action="CALL", confidence=0.8, primary=True, age_s=0) -> int:
        decision_id = await self.repo.insert_decision(
            {
                "symbol": SETTINGS.symbol,
                "action": action,
                "confidence": confidence,
                "provider_is_primary": primary,
            }
        )
        if age_s:
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE decisions SET created_at = now() - %s WHERE id = %s",
                    (timedelta(seconds=age_s), decision_id),
                )
        return decision_id

    async def trade_status(self, trade_id):
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT status, profit FROM trades WHERE id = %s", (trade_id,))
            return await cur.fetchone()

    async def wait_settled(self, trade_id, wait_s=3.0):
        async with asyncio.timeout(wait_s):
            while True:
                status, profit = await self.trade_status(trade_id)
                if status not in ("pending", "open"):
                    return status, profit
                await asyncio.sleep(0.05)


@pytest.fixture
async def h(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    repo, events, clock = TradeRepo(pool), EventLog(pool), SystemClock()
    overrides: dict = {}
    control = {"halted_after_checks": None, "checks": 0, "context_raises": False}

    async with FakeDerivServer(tick_interval_s=0.05) as server:

        async def url():
            return server.issue_url()

        ws = DerivWS(url, {"127.0.0.1"}, backoff_base_s=0.01, backoff_max_s=0.05)

        async def build_context(record: DecisionRecord, stake: Decimal) -> RiskContext:
            if control["context_raises"]:
                raise RuntimeError("db down")
            stats = await repo.risk_stats(clock.now())
            ctx = RiskContext(
                now=clock.now(),
                action=record.action,
                confidence=record.confidence,
                stake_usd=stake,
                provider_is_primary=record.provider_is_primary,
                kill_switch=False,
                paused=False,
                ws_url_is_demo=ws.connection_is_demo(),
                open_positions=stats.open_positions,
                open_exposure_usd=stats.open_exposure_usd,
                daily_pnl_usd=stats.daily_pnl_usd,
                consecutive_losses=stats.consecutive_losses,
                last_loss_at=stats.last_loss_at,
                data_age_s=0.5,
                first_trade_after_resume=False,
                limits=LIMITS,
            )
            return replace(ctx, **overrides)

        async def is_halted():
            return control["halted_after_checks"] is not None

        executor = Executor(
            ws, repo, events, SETTINGS, clock, build_context, is_halted, request_timeout_s=0.5
        )
        task = asyncio.create_task(ws.run())
        await asyncio.wait_for(ws.connected.wait(), 3)
        try:
            yield Harness(pool, server, ws, repo, executor, overrides, control)
        finally:
            await executor.close()
            await ws.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def test_executor_buys_and_settles(h):
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BOUGHT
    assert await h.wait_settled(result.trade_id) == ("won", Decimal("0.95"))
    buy = h.server.buys[0]
    assert buy["amount"] == 1.0 and buy["duration"] == 5 and buy["contract_type"] == "CALL"


async def test_executor_put_maps_to_put_contract(h):
    result = await h.executor.execute(await h.decision(action="PUT"))
    assert result.outcome is Outcome.BOUGHT
    assert h.server.buys[0]["contract_type"] == "PUT"


async def test_executor_records_losses_and_streak(h):
    h.server.outcomes = ["lost", "lost", "lost"]
    for _ in range(3):
        result = await h.executor.execute(await h.decision())
        assert await h.wait_settled(result.trade_id) == ("lost", Decimal("-1.00"))
    stats = await h.repo.risk_stats(SystemClock().now())
    assert stats.consecutive_losses == 3
    assert stats.daily_pnl_usd == Decimal("-3.00")
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BLOCKED
    assert "cooling down" in result.detail


async def test_executor_reads_decision_from_db_not_caller(h):
    hold = await h.decision(action="HOLD")
    assert (await h.executor.execute(hold)).outcome is Outcome.BLOCKED
    assert (await h.executor.execute(999_999)).outcome is Outcome.BLOCKED
    assert h.server.buys == []


async def test_executor_refuses_stale_decision(h):
    result = await h.executor.execute(await h.decision(age_s=600))
    assert result.outcome is Outcome.BLOCKED
    assert "stale" in result.detail


async def test_executor_rechecks_risk_after_requote(h):
    h.overrides["kill_switch"] = True
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BLOCKED
    assert "kill switch" in result.detail
    assert h.server.buys == []


async def test_executor_halt_between_gate_and_buy(h):
    h.control["halted_after_checks"] = 0  # the gate passes; the pre-buy halt check fails
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BLOCKED
    assert (await h.trade_status(result.trade_id))[0] == "error"
    assert h.server.buys == []


async def test_executor_refuses_when_connection_not_demo(h, monkeypatch):
    monkeypatch.setattr(h.ws, "connection_is_demo", lambda: False)
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BLOCKED
    assert h.server.buys == []


async def test_executor_context_failure_is_error_not_trade(h):
    h.control["context_raises"] = True
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.ERROR
    assert h.server.buys == []


async def test_executor_idempotent(h):
    decision_id = await h.decision()
    first = await h.executor.execute(decision_id)
    assert first.outcome is Outcome.BOUGHT
    await h.wait_settled(first.trade_id)  # position closed, so only idempotency can stop it
    second = await h.executor.execute(decision_id)
    assert second.outcome is Outcome.DUPLICATE
    assert len(h.server.buys) == 1


async def test_executor_serialises_concurrent_buys(h):
    ids = [await h.decision() for _ in range(3)]
    results = await asyncio.gather(*(h.executor.execute(i) for i in ids))
    assert [r.outcome for r in results].count(Outcome.BOUGHT) == 1  # max_open_positions = 1
    assert len(h.server.buys) == 1


async def test_executor_definite_buy_rejection_is_error(h):
    h.server.fail_next_buy = True
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.ERROR
    assert (await h.trade_status(result.trade_id))[0] == "error"


async def test_buy_timeout_is_unknown_and_counts_as_open_and_lost(h):
    h.server.swallow_next_buy = True
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.UNKNOWN
    assert (await h.trade_status(result.trade_id))[0] == "unknown"
    stats = await h.repo.risk_stats(SystemClock().now())
    assert stats.open_positions == 1
    assert stats.open_exposure_usd == Decimal("1.00")
    assert stats.daily_pnl_usd == Decimal("-1.00")
    blocked = await h.executor.execute(await h.decision())
    assert blocked.outcome is Outcome.BLOCKED  # max positions holds while the outcome is unknown


@pytest.mark.parametrize("ask", [0.99, 1.01, 5.0])
async def test_executor_blocks_quote_different_from_stake(h, ask):
    h.server.ask_price_override = ask
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BLOCKED
    assert h.server.buys == []


async def test_needs_approval_without_db_approval_is_blocked(h):
    result = await h.executor.execute(await h.decision(confidence=0.60))
    assert result.outcome is Outcome.BLOCKED
    assert "needs human approval" in result.detail


async def test_valid_db_approval_executes(h):
    now = SystemClock().now()
    decision_id = await h.decision(confidence=0.60)
    await h.repo.create_approval(
        decision_id, ["confidence in grey zone"], now + timedelta(seconds=90)
    )
    await h.repo.respond_approval(decision_id, "approved", now)
    assert (await h.executor.execute(decision_id)).outcome is Outcome.BOUGHT


async def test_late_approval_is_blocked(h):
    now = SystemClock().now()
    decision_id = await h.decision(confidence=0.60)
    await h.repo.create_approval(
        decision_id, ["confidence in grey zone"], now - timedelta(seconds=1)
    )
    await h.repo.respond_approval(decision_id, "approved", now)
    result = await h.executor.execute(decision_id)
    assert result.outcome is Outcome.BLOCKED and "expired" in result.detail


async def test_approval_without_expiry_is_blocked(h):
    decision_id = await h.decision(confidence=0.60)
    async with h.pool.connection() as conn:  # no expires_at: the column defaults to the epoch
        await conn.execute(
            "INSERT INTO approvals (decision_id, trigger, reasons, status, responded_at) "
            "VALUES (%s, 'x', %s, 'approved', now())",
            (decision_id, ["confidence in grey zone"]),
        )
    result = await h.executor.execute(decision_id)
    assert result.outcome is Outcome.BLOCKED and "expired" in result.detail


async def test_corrupted_decision_action_is_not_tradeable(h):
    decision_id = await h.decision()
    async with h.pool.connection() as conn:
        await conn.execute("ALTER TABLE decisions DROP CONSTRAINT decisions_action_check")
        await conn.execute("UPDATE decisions SET action = 'MOON' WHERE id = %s", (decision_id,))
    assert (await h.executor.execute(decision_id)).outcome is Outcome.BLOCKED
    assert h.server.buys == []


async def test_approval_does_not_cover_new_triggers(h):
    now = SystemClock().now()
    decision_id = await h.decision(confidence=0.60, primary=False)  # two triggers now
    await h.repo.create_approval(
        decision_id, ["confidence in grey zone"], now + timedelta(seconds=90)
    )
    await h.repo.respond_approval(decision_id, "approved", now)
    result = await h.executor.execute(decision_id)
    assert result.outcome is Outcome.BLOCKED
    assert "fallback provider answered" in result.detail


async def test_human_approval_cannot_override_reject(h):
    now = SystemClock().now()
    decision_id = await h.decision(confidence=0.60)
    await h.repo.create_approval(
        decision_id, ["confidence in grey zone"], now + timedelta(seconds=90)
    )
    await h.repo.respond_approval(decision_id, "approved", now)
    h.overrides["paused"] = True
    result = await h.executor.execute(decision_id)
    assert result.outcome is Outcome.BLOCKED
    assert h.server.buys == []


async def test_resume_open_trades_after_restart(h):
    result = await h.executor.execute(await h.decision())
    await h.executor.close()  # simulate a crash before settlement was recorded
    async with h.pool.connection() as conn:
        await conn.execute("UPDATE trades SET status = 'open' WHERE id = %s", (result.trade_id,))
    assert await h.executor.resume_open_trades() == 1
    assert (await h.wait_settled(result.trade_id))[0] == "won"


async def test_crash_between_claim_and_confirmation_becomes_unknown(h):
    trade_id = await h.repo.create_pending(await h.decision(), "CALL", "1HZ100V", Decimal(1), 5)
    assert await h.executor.resume_open_trades() == 0
    assert (await h.trade_status(trade_id))[0] == "unknown"


async def test_settlement_timeout_marks_unknown(h):
    h.executor.settle_timeout_s = 0.3
    h.server.settle_after_s = 10  # Deriv never reports a result in time
    result = await h.executor.execute(await h.decision())
    assert result.outcome is Outcome.BOUGHT
    assert (await h.wait_settled(result.trade_id))[0] == "unknown"
    stats = await h.repo.risk_stats(SystemClock().now())
    assert stats.open_positions == 1 and stats.daily_pnl_usd == Decimal("-1.00")
