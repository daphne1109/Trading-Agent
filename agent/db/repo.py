"""Small data-access helpers for the runtime: batched tick writes, events, key/value state."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.deriv.schemas import Tick

log = logging.getLogger(__name__)


class TickWriter:
    """Buffers ticks and writes them in batches (one statement per flush) to spare a small VM."""

    def __init__(
        self, pool: AsyncConnectionPool, *, batch_size: int = 50, flush_every_s: float = 2.0
    ) -> None:
        self._pool = pool
        self._batch_size = batch_size
        self._flush_every_s = flush_every_s
        self._buffer: list[tuple[str, int, float, datetime]] = []
        self._lock = asyncio.Lock()
        self.flushes = 0
        self.written = 0

    async def add(self, tick: Tick, received_at: datetime) -> None:
        self._buffer.append((tick.symbol, tick.epoch, tick.quote, received_at))
        if len(self._buffer) >= self._batch_size:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            rows, self._buffer = self._buffer, []
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO ticks (symbol, epoch, quote, received_at) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (symbol, epoch) DO NOTHING",
                    rows,
                )
            self.flushes += 1
            self.written += len(rows)

    async def run_periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self._flush_every_s)
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 - keep streaming; the next flush retries new rows
                log.exception("tick flush failed")


class EventLog:
    """Writes operational events (reconnects, stale skips, fallbacks, kills) to agent_events."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record(
        self, kind: str, message: str, data: dict[str, Any] | None = None, level: str = "info"
    ) -> None:
        log.log(logging.getLevelName(level.upper()), "%s: %s", kind, message)
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO agent_events (level, kind, message, data) VALUES (%s, %s, %s, %s)",
                    (level, kind, message, Jsonb(data or {})),
                )
        except Exception:  # noqa: BLE001 - logging must never take the agent down
            log.exception("could not record event %s", kind)


class StateStore:
    """Key/value runtime state (heartbeat, paused, killed) in agent_state."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def set(self, key: str, value: Any) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_state (key, value, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, Jsonb(value)),
            )

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT value FROM agent_state WHERE key = %s", (key,))
            row = await cur.fetchone()
        return row[0] if row else default
