"""Agent entry point: `python -m agent.main`.

Runs, under one TaskGroup:
  - the Deriv demo WebSocket (reconnecting, guarded);
  - the tick consumer: feed buffer, Postgres, shadow-baseline settlement;
  - a 10 s heartbeat in agent_state;
  - the decision loop (model -> RiskGate -> executor), once connected and open trades resumed.
Only one instance may run per database (advisory lock). KILL_SWITCH=1 refuses to start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from psycopg_pool import AsyncConnectionPool

from agent.clock import Clock, SystemClock
from agent.config import Settings, get_settings
from agent.db.instance_lock import InstanceLock
from agent.db.migrate import migrate
from agent.db.repo import EventLog, StateStore, TickWriter
from agent.db.trades import DecisionRecord, TradeRepo
from agent.decision.base import DecisionModel
from agent.decision.prompt import load_prompt
from agent.decision.router import DecisionRouter
from agent.decision.rules_model import RuleBasedModel
from agent.decision.systemone import SystemOneModel
from agent.deriv.guard import assert_demo_ws_url, assert_public_ws_url
from agent.deriv.rest import DerivRest
from agent.deriv.schemas import Tick
from agent.deriv.ws import DerivWS, UrlProvider
from agent.execution.executor import Executor
from agent.execution.paper import PaperExecutor
from agent.loop import DecisionLoop
from agent.market.feed import MarketFeed
from agent.risk.rules import RiskContext
from agent.shadow import ShadowBook
from agent.spend import SpendTracker

log = logging.getLogger("agent")

EVENT_LEVELS = {
    "ws_disconnected": "warning",
    "guard_refused": "critical",
    "tick_error": "error",
}
HEARTBEAT_EVERY_S = 10.0
PLAYBOOK_PATH = Path("playbook.md")
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_USD_PER_M_INPUT = "0.042"
CLEF_FLASH_USD_PER_M_INPUT = "0.09"


async def stream_ticks(
    ws: DerivWS,
    feed: MarketFeed,
    writer: TickWriter,
    shadow: ShadowBook,
    events: EventLog,
    clock: Clock,
    symbol: str,
) -> None:
    queue = await ws.subscribe({"ticks": symbol})
    while True:
        msg = await queue.get()
        if "error" in msg:
            await events.record("tick_error", str(msg["error"]), {"symbol": symbol}, "error")
            continue
        if msg.get("msg_type") != "tick":
            continue
        tick = Tick.from_message(msg)
        received_at = clock.now()
        feed.on_tick(tick)
        await writer.add(tick, received_at)
        try:
            await shadow.on_tick(tick)
        except Exception:  # noqa: BLE001 - paper bookkeeping must never stop the feed
            log.exception("shadow settlement failed")


async def heartbeat(
    state: StateStore, feed: MarketFeed, ws: DerivWS, clock: Clock, symbol: str
) -> None:
    while True:
        await state.set(
            "heartbeat",
            {
                "at": clock.now().isoformat(),
                "connected": ws.connected.is_set(),
                "reconnects": ws.reconnects,
                "ticks_buffered": feed.size(symbol),
            },
        )
        await asyncio.sleep(HEARTBEAT_EVERY_S)


async def open_pool(settings: Settings) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=6,
        open=False,
        kwargs={"connect_timeout": 10},
    )
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        applied = await migrate(conn)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    return pool


def build_providers(settings: Settings, http: httpx.AsyncClient) -> list[DecisionModel]:
    """Decision providers in priority order, from whichever credentials are configured."""
    providers: list[DecisionModel] = []
    if settings.typesafe_api_key.get_secret_value():
        providers.append(
            SystemOneModel(
                name="jev",
                url=TYPESAFE_URL,
                api_key=settings.typesafe_api_key.get_secret_value(),
                model=settings.typesafe_model,
                http=http,
                usd_per_m_input=Decimal(JEV_USD_PER_M_INPUT),
            )
        )
    if settings.cf_account_id and settings.cf_api_token.get_secret_value():
        providers.append(
            SystemOneModel(
                name="clef",
                url=(
                    "https://api.cloudflare.com/client/v4/accounts/"
                    f"{settings.cf_account_id}/ai/run/{settings.cf_model}"
                ),
                api_key=settings.cf_api_token.get_secret_value(),
                model=settings.cf_model,
                http=http,
                usd_per_m_input=Decimal(CLEF_FLASH_USD_PER_M_INPUT),
                send_model_in_body=False,
            )
        )
    if settings.anthropic_api_key.get_secret_value():
        import anthropic

        from agent.decision.claude import ClaudeDecisionModel

        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=20, max_retries=0
        )
        providers.append(ClaudeDecisionModel(client))
    return providers


async def run(
    settings: Settings,
    *,
    url_provider: UrlProvider | None = None,
    clock: Clock | None = None,
    providers: list[DecisionModel] | None = None,
) -> None:
    if settings.kill_switch:
        log.critical("KILL_SWITCH is set; refusing to start")
        return
    clock = clock or SystemClock()
    instance = InstanceLock(settings.database_url)
    await instance.acquire()
    try:
        pool = await open_pool(settings)
    except BaseException:
        await instance.release()
        raise
    events = EventLog(pool)
    state = StateStore(pool)
    repo = TradeRepo(pool)

    async def on_ws_event(kind: str, message: str, data: dict[str, Any]) -> None:
        await events.record(kind, message, data, EVENT_LEVELS.get(kind, "info"))

    paper = settings.execution_mode == "paper"

    async def public_url() -> str:
        return settings.deriv_public_ws_url

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            ws = DerivWS(
                url_provider or (public_url if paper else DerivRest(settings, http).demo_ws_url),
                settings.deriv_ws_allowed_hosts,
                ping_interval_s=settings.ws_ping_interval_s,
                silence_timeout_s=settings.ws_silence_timeout_s,
                backoff_max_s=settings.ws_backoff_max_s,
                on_event=on_ws_event,
                url_guard=assert_public_ws_url if paper else assert_demo_ws_url,
            )
            feed = MarketFeed(
                clock,
                stale_after_s=settings.stale_tick_s,
                min_history=settings.min_history_ticks,
                maxlen=settings.buffer_ticks,
            )
            writer = TickWriter(pool)
            shadow = ShadowBook(
                pool,
                duration_ticks=settings.duration_ticks,
                payout_ratio=settings.paper_payout_ratio,
            )
            models = providers if providers is not None else build_providers(settings, http)
            if not models and settings.allow_rule_model and paper:  # never in demo mode
                models = [RuleBasedModel()]
            spend = SpendTracker(
                clock,
                {
                    "jev": settings.spend_cap_jev_usd,
                    "clef": settings.spend_cap_clef_usd,
                    "claude": settings.spend_cap_anthropic_usd,
                    "rules": Decimal("1"),  # free; a cap is required for every provider
                },
                await repo.spend_today(clock.now()),
            )

            loop: DecisionLoop | None = None
            executor: Executor | None = None
            if models:
                router = DecisionRouter(models, spend, clock)

                # The executor re-runs the loop's context builder; the loop owns the executor.
                async def build_context(record: DecisionRecord, stake: Decimal) -> RiskContext:
                    assert loop is not None
                    return await loop.build_context(record, stake)

                async def is_halted() -> bool:
                    assert loop is not None
                    return await loop.is_halted()

                if paper:
                    executor = PaperExecutor(
                        ws, repo, events, settings, clock, build_context, is_halted, feed
                    )
                else:
                    executor = Executor(ws, repo, events, settings, clock, build_context, is_halted)
                loop = DecisionLoop(
                    settings=settings,
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
                    playbook_path=PLAYBOOK_PATH,
                    lock_held=instance.held,
                )
            else:
                await events.record(
                    "no_decision_provider",
                    "no model credentials configured; streaming market data only",
                    {},
                    "warning",
                )

            async def trade() -> None:
                assert loop is not None and executor is not None
                await ws.connected.wait()
                resumed = await executor.resume_open_trades()
                if resumed:
                    await events.record("trades_resumed", f"following {resumed} open trade(s)")
                await loop.run()

            await state.set("started_at", clock.now().isoformat())
            await state.set(
                "config",
                {
                    "mode": settings.execution_mode,
                    "symbol": settings.symbol,
                    "providers": [m.name for m in models],
                    "stake_usd": str(settings.stake_usd),
                    "duration_ticks": settings.duration_ticks,
                    "decision_interval_s": settings.decision_interval_s,
                    "min_confidence": settings.min_confidence,
                    "approval_band_high": settings.approval_band_high,
                    "daily_loss_cap_usd": str(settings.daily_loss_cap_usd),
                },
            )
            await events.record(
                "agent_started",
                f"{settings.execution_mode} mode; symbol {settings.symbol}; "
                f"providers: {[m.name for m in models] or 'none'}",
            )
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(ws.run(), name="ws")
                    tg.create_task(
                        stream_ticks(ws, feed, writer, shadow, events, clock, settings.symbol),
                        name="ticks",
                    )
                    tg.create_task(writer.run_periodic_flush(), name="tick-flush")
                    tg.create_task(
                        heartbeat(state, feed, ws, clock, settings.symbol), name="heartbeat"
                    )
                    if loop is not None:
                        tg.create_task(trade(), name="decision-loop")
            finally:
                if executor is not None:
                    await executor.close()
                with contextlib.suppress(Exception):
                    await writer.flush()
                await events.record("agent_stopped", "shutdown")
    finally:
        await pool.close()
        await instance.release()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _main() -> None:
        task = asyncio.create_task(run(get_settings()))
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_main())


if __name__ == "__main__":
    main()
