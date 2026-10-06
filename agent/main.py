"""Agent entry point: `python -m agent.main`.

P1 scope: connect to the Deriv demo WebSocket, stream ticks into the feed and Postgres,
record operational events, and publish a heartbeat. Decision-making is added in P2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

import httpx
from psycopg_pool import AsyncConnectionPool

from agent.clock import Clock, SystemClock
from agent.config import Settings, get_settings
from agent.db.migrate import migrate
from agent.db.repo import EventLog, StateStore, TickWriter
from agent.deriv.rest import DerivRest
from agent.deriv.schemas import Tick
from agent.deriv.ws import DerivWS, UrlProvider
from agent.market.feed import MarketFeed

log = logging.getLogger("agent")

EVENT_LEVELS = {
    "ws_disconnected": "warning",
    "guard_refused": "critical",
    "tick_error": "error",
}

HEARTBEAT_EVERY_S = 10.0


async def stream_ticks(
    ws: DerivWS, feed: MarketFeed, writer: TickWriter, events: EventLog, clock: Clock, symbol: str
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
        max_size=5,
        open=False,
        kwargs={"connect_timeout": 10},
    )
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        applied = await migrate(conn)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    return pool


async def run(
    settings: Settings, *, url_provider: UrlProvider | None = None, clock: Clock | None = None
) -> None:
    if settings.kill_switch:
        log.critical("KILL_SWITCH is set; refusing to start")
        return
    clock = clock or SystemClock()
    pool = await open_pool(settings)
    events = EventLog(pool)
    state = StateStore(pool)

    async def on_ws_event(kind: str, message: str, data: dict[str, Any]) -> None:
        await events.record(kind, message, data, EVENT_LEVELS.get(kind, "info"))

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            ws = DerivWS(
                url_provider or DerivRest(settings, http).demo_ws_url,
                settings.deriv_ws_allowed_hosts,
                ping_interval_s=settings.ws_ping_interval_s,
                silence_timeout_s=settings.ws_silence_timeout_s,
                backoff_max_s=settings.ws_backoff_max_s,
                on_event=on_ws_event,
            )
            feed = MarketFeed(
                clock,
                stale_after_s=settings.stale_tick_s,
                min_history=settings.min_history_ticks,
                maxlen=settings.buffer_ticks,
            )
            writer = TickWriter(pool)
            await state.set("started_at", clock.now().isoformat())
            await events.record("agent_started", f"streaming {settings.symbol}")
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(ws.run(), name="ws")
                    tg.create_task(
                        stream_ticks(ws, feed, writer, events, clock, settings.symbol),
                        name="ticks",
                    )
                    tg.create_task(writer.run_periodic_flush(), name="tick-flush")
                    tg.create_task(
                        heartbeat(state, feed, ws, clock, settings.symbol), name="heartbeat"
                    )
            finally:
                with contextlib.suppress(Exception):
                    await writer.flush()
                await events.record("agent_stopped", "shutdown")
    finally:
        await pool.close()


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
