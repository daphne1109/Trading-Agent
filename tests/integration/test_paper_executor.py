import asyncio
import contextlib
from dataclasses import replace
from decimal import Decimal

import pytest

from agent.clock import SystemClock
from agent.config import Settings
from agent.db.migrate import migrate
from agent.db.repo import EventLog
from agent.db.trades import DecisionRecord, TradeRepo
from agent.deriv.guard import assert_public_ws_url
from agent.deriv.schemas import Tick
from agent.deriv.ws import DerivWS
from agent.execution.executor import Outcome
from agent.execution.paper import PaperExecutor
from agent.loop import limits_from
from agent.market.feed import MarketFeed
from agent.risk.rules import RiskContext
from tests.fakes.fake_deriv_server import FakeDerivServer

pytestmark = pytest.mark.integration

SETTINGS = Settings(_env_file=None, deriv_ws_allowed_hosts=frozenset({"127.0.0.1"}))


@pytest.fixture
async def ph(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    repo, events, clock = TradeRepo(pool), EventLog(pool), SystemClock()
    overrides: dict = {}

    async with FakeDerivServer(tick_interval_s=0.02, public=True) as server:

        async def url():
            return server.issue_url()

        ws = DerivWS(
            url, {"127.0.0.1"}, backoff_base_s=0.01, backoff_max_s=0.05,
            url_guard=assert_public_ws_url,
        )  # fmt: skip
        feed = MarketFeed(clock, stale_after_s=5, min_history=10, maxlen=600)

        async def pump():
            queue = await ws.subscribe({"ticks": SETTINGS.symbol})
            while True:
                msg = await queue.get()
                if msg.get("msg_type") == "tick":
                    feed.on_tick(Tick.from_message(msg))

        async def build_context(record: DecisionRecord, stake: Decimal) -> RiskContext:
            stats = await repo.risk_stats(clock.now())
            ctx = RiskContext(
                now=clock.now(), action=record.action, confidence=record.confidence,
                stake_usd=stake, provider_is_primary=record.provider_is_primary,
                kill_switch=False, paused=False, ws_url_is_demo=ws.connection_is_demo(),
                open_positions=stats.open_positions, open_exposure_usd=stats.open_exposure_usd,
                daily_pnl_usd=stats.daily_pnl_usd, consecutive_losses=stats.consecutive_losses,
                last_loss_at=stats.last_loss_at, data_age_s=0.1, first_trade_after_resume=False,
                limits=limits_from(SETTINGS),
            )  # fmt: skip
            return replace(ctx, **overrides)

        async def is_halted():
            return False

        executor = PaperExecutor(
            ws, repo, events, SETTINGS, clock, build_context, is_halted, feed,
            request_timeout_s=1,
        )  # fmt: skip
        tasks = [asyncio.create_task(ws.run()), asyncio.create_task(pump())]
        await asyncio.wait_for(ws.connected.wait(), 3)
        async with asyncio.timeout(3):
            while feed.size(SETTINGS.symbol) < 10:  # noqa: ASYNC110 - test warm-up poll
                await asyncio.sleep(0.02)
        try:
            yield {"pool": pool, "server": server, "executor": executor, "repo": repo,
                   "overrides": overrides}  # fmt: skip
        finally:
            await executor.close()
            await ws.close()
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t


async def new_decision(repo, action="CALL"):
    return await repo.insert_decision(
        {
            "symbol": SETTINGS.symbol,
            "action": action,
            "confidence": 0.8,
            "provider_is_primary": True,
        }  # fmt: skip
    )


async def settled(pool, trade_id, wait_s=3.0):
    async with asyncio.timeout(wait_s):
        while True:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT status, profit, raw, buy_price, payout FROM trades WHERE id = %s",
                    (trade_id,),
                )
                row = await cur.fetchone()
            if row[0] not in ("pending", "open"):
                return row
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("action", ["CALL", "PUT"])
async def test_paper_fill_settles_on_real_ticks_by_rise_fall_rules(ph, action):
    result = await ph["executor"].execute(await new_decision(ph["repo"], action))
    assert result.outcome is Outcome.BOUGHT
    status, profit, raw, buy_price, payout = await settled(ph["pool"], result.trade_id)
    entry, exit_ = raw["entry"]["quote"], raw["exit"]["quote"]
    assert raw["exit"]["epoch"] - raw["entry"]["epoch"] == SETTINGS.duration_ticks
    won = exit_ > entry if action == "CALL" else exit_ < entry
    assert status == ("won" if won else "lost")
    assert profit == (payout - buy_price if won else -buy_price)
    assert buy_price == Decimal("1.00") and payout == Decimal("1.95")  # the real quote's numbers


async def test_paper_executor_never_sends_buy(ph):
    for _ in range(2):
        result = await ph["executor"].execute(await new_decision(ph["repo"]))
        await settled(ph["pool"], result.trade_id)
    assert ph["server"].buy_attempts_on_public == 0
    assert ph["server"].buys == []


async def test_paper_mode_keeps_every_risk_check(ph):
    ph["overrides"]["kill_switch"] = True
    result = await ph["executor"].execute(await new_decision(ph["repo"]))
    assert result.outcome is Outcome.BLOCKED
    assert "kill switch" in result.detail


async def test_paper_trades_interrupted_by_restart_become_unknown(ph):
    trade_id = await ph["repo"].create_pending(
        await new_decision(ph["repo"]), "CALL", SETTINGS.symbol, Decimal(1), 5
    )
    assert await ph["executor"].resume_open_trades() == 0
    assert (await settled(ph["pool"], trade_id))[0] == "unknown"
