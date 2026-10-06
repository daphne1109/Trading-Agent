from datetime import UTC, datetime

import pytest

from agent.db.migrate import migrate
from agent.db.repo import EventLog, StateStore, TickWriter
from agent.deriv.schemas import Tick

pytestmark = pytest.mark.integration


async def test_migrations_idempotent(pool):
    async with pool.connection() as conn:
        first = await migrate(conn)
        second = await migrate(conn)
    assert first == ["001_init", "002_execution_safety"]
    assert second == []


async def test_tick_persist_batch(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    writer = TickWriter(pool, batch_size=50)
    now = datetime.now(UTC)
    for i in range(120):
        await writer.add(Tick(symbol="1HZ100V", epoch=1_000 + i, quote=1.0 + i), now)
    await writer.flush()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM ticks")
        (count,) = await cur.fetchone()
    assert count == 120
    assert writer.flushes <= 5  # 50 + 50 + 20, not 120 separate writes


async def test_duplicate_ticks_ignored(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    writer = TickWriter(pool)
    now = datetime.now(UTC)
    for _ in range(2):
        await writer.add(Tick(symbol="1HZ100V", epoch=42, quote=1.0), now)
        await writer.flush()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM ticks")
        assert (await cur.fetchone())[0] == 1


async def test_event_log_and_state_store(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    await EventLog(pool).record("ws_disconnected", "test", {"n": 1}, level="warning")
    state = StateStore(pool)
    await state.set("paused", True)
    await state.set("paused", False)
    assert await state.get("paused") is False
    assert await state.get("missing", "x") == "x"
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT level, kind, data FROM agent_events")
        assert await cur.fetchone() == ("warning", "ws_disconnected", {"n": 1})


async def test_trade_idempotency_constraint(pool):
    async with pool.connection() as conn:
        await migrate(conn)
        cur = await conn.execute("INSERT INTO decisions (symbol) VALUES ('1HZ100V') RETURNING id")
        (decision_id,) = await cur.fetchone()
        insert = (
            "INSERT INTO trades (decision_id, contract_type, symbol, stake, duration_ticks) "
            "VALUES (%s, 'CALL', '1HZ100V', 1, 5)"
        )
        await conn.execute(insert, (decision_id,))
        with pytest.raises(Exception, match="duplicate key"):
            await conn.execute(insert, (decision_id,))
