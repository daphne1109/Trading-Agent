import asyncio
import contextlib
from decimal import Decimal
from pathlib import Path

import pytest

from agent.clock import SystemClock
from agent.config import Settings
from agent.db.migrate import migrate
from agent.db.repo import EventLog, StateStore
from agent.db.trades import TradeRepo
from agent.decision.base import Action, Decision
from agent.decision.prompt import load_prompt
from agent.decision.router import DecisionRouter
from agent.deriv.schemas import Tick
from agent.deriv.ws import DerivWS
from agent.execution.executor import Executor
from agent.loop import DecisionLoop
from agent.market.feed import MarketFeed
from agent.shadow import ShadowBook
from agent.spend import SpendTracker
from tests.fakes.fake_deriv_server import FakeDerivServer

pytestmark = pytest.mark.integration

SETTINGS = Settings(
    _env_file=None,
    deriv_ws_allowed_hosts=frozenset({"127.0.0.1"}),
    min_history_ticks=10,
)


class StubModel:
    name = "jev"

    def __init__(self):
        self.action, self.confidence, self.calls = Action.CALL, 0.8, 0

    async def decide(self, state, question):
        self.calls += 1
        probs = {"CALL": 0.1, "PUT": 0.1, "HOLD": 0.1}
        probs[self.action.value] = 0.8
        probs = {k: v / sum(probs.values()) for k, v in probs.items()}
        return Decision(
            action=self.action,
            probabilities=probs,
            confidence=self.confidence,
            provider="jev",
            model="stub",
            cost_usd=Decimal("0.0001"),
        )


class LoopHarness:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def fill_feed(self, n=20):
        for _ in range(n):
            self.feed.on_tick(Tick(symbol=SETTINGS.symbol, epoch=self.next_epoch(), quote=1000.0))

    def next_epoch(self):
        self.epoch += 1
        return self.epoch

    async def scalar(self, sql, *args):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return (await cur.fetchone())[0]


@pytest.fixture
async def lh(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    clock = SystemClock()
    repo, events, state = TradeRepo(pool), EventLog(pool), StateStore(pool)
    lock = {"held": True}
    model = StubModel()

    async with FakeDerivServer(tick_interval_s=0.05) as server:

        async def url():
            return server.issue_url()

        ws = DerivWS(url, {"127.0.0.1"}, backoff_base_s=0.01, backoff_max_s=0.05)
        feed = MarketFeed(clock, stale_after_s=5, min_history=10, maxlen=600)
        shadow = ShadowBook(pool, duration_ticks=5, payout_ratio=Decimal("1.95"))
        router = DecisionRouter(
            [model], SpendTracker(clock, {"jev": Decimal("1")}), clock, timeout_s=1
        )
        holder = {}

        async def build_context(record, stake):
            return await holder["loop"].build_context(record, stake)

        async def is_halted():
            return await holder["loop"].is_halted()

        async def lock_held():
            return lock["held"]

        executor = Executor(
            ws, repo, events, SETTINGS, clock, build_context, is_halted, request_timeout_s=1
        )
        loop = DecisionLoop(
            settings=SETTINGS,
            clock=clock,
            feed=feed,
            router=router,
            prompt=load_prompt(),
            repo=repo,
            state=state,
            events=events,
            executor=executor,
            ws=ws,
            shadow=shadow,
            playbook_path=Path("playbook.md"),
            lock_held=lock_held,
        )
        holder["loop"] = loop
        task = asyncio.create_task(ws.run())
        await asyncio.wait_for(ws.connected.wait(), 3)
        try:
            yield LoopHarness(
                pool=pool, server=server, loop=loop, model=model, feed=feed, state=state,
                lock=lock, epoch=1_000,
            )  # fmt: skip
        finally:
            await executor.close()
            await ws.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def test_full_round_trades_and_logs_everything(lh):
    await lh.fill_feed()
    result = await lh.loop.run_once()
    assert result.outcome == "executed:bought"
    assert len(lh.server.buys) == 1
    d = result.decision_id
    assert await lh.scalar("SELECT verdict FROM risk_verdicts WHERE decision_id = %s", d) == (
        "APPROVE"
    )
    assert await lh.scalar("SELECT provider_is_primary FROM decisions WHERE id = %s", d) is True
    assert await lh.scalar("SELECT count(*) FROM shadow_decisions WHERE decision_id = %s", d) == 4
    assert await lh.scalar("SELECT count(*) FROM trades WHERE decision_id = %s", d) == 1
    snapshot = await lh.scalar("SELECT snapshot FROM decisions WHERE id = %s", d)
    assert "playbook" not in snapshot and len(snapshot["last_ticks"]) == 20


async def test_hold_is_logged_without_trading(lh):
    await lh.fill_feed()
    lh.model.action = Action.HOLD
    result = await lh.loop.run_once()
    assert result.outcome == "hold"
    assert lh.server.buys == []
    verdict = await lh.scalar(
        "SELECT verdict FROM risk_verdicts WHERE decision_id = %s", result.decision_id
    )
    assert verdict == "NO_TRADE"


async def test_loop_skips_on_stale_data_without_calling_model(lh):
    result = await lh.loop.run_once()  # feed is empty
    assert result.outcome == "skipped"
    assert lh.model.calls == 0
    reason = await lh.scalar(
        "SELECT skipped_reason FROM decisions WHERE id = %s", result.decision_id
    )
    assert reason.startswith("stale")


@pytest.mark.parametrize("flag", ["paused", "killed"])
async def test_loop_skips_when_paused_or_killed(lh, flag):
    await lh.fill_feed()
    await lh.state.set(flag, True)
    result = await lh.loop.run_once()
    assert result.outcome == "skipped"
    assert lh.model.calls == 0 and lh.server.buys == []


async def test_needs_approval_without_channel_does_not_trade(lh):
    await lh.fill_feed()
    lh.model.confidence = 0.60
    result = await lh.loop.run_once()
    assert result.outcome == "rejected"
    assert lh.server.buys == []


async def test_low_confidence_is_rejected_by_gate(lh):
    await lh.fill_feed()
    lh.model.confidence = 0.52
    result = await lh.loop.run_once()
    assert result.outcome == "rejected"
    reasons = await lh.scalar(
        "SELECT reasons FROM risk_verdicts WHERE decision_id = %s", result.decision_id
    )
    assert any("confidence" in r for r in reasons)


async def test_lost_instance_lock_halts_trading(lh):
    await lh.fill_feed()
    lh.lock["held"] = False
    result = await lh.loop.run_once()
    assert result.outcome == "skipped"
    assert lh.server.buys == []
