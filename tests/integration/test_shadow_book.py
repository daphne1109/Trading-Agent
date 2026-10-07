from decimal import Decimal

import pytest

from agent.db.migrate import migrate
from agent.db.trades import TradeRepo
from agent.decision.base import Action
from agent.deriv.schemas import Tick
from agent.shadow import ShadowBook

pytestmark = pytest.mark.integration


async def test_shadow_book_opens_and_settles_on_exit_tick(pool):
    async with pool.connection() as conn:
        await migrate(conn)
    decision_id = await TradeRepo(pool).insert_decision({"symbol": "1HZ100V", "action": "CALL"})
    book = ShadowBook(pool, duration_ticks=5, payout_ratio=Decimal("1.95"))
    quotes = [100.0 + i * 0.1 for i in range(20)]  # rising: momentum says CALL
    await book.open(decision_id, entry_epoch=1_000, quotes=quotes, model_action=Action.CALL,
                    p_call=0.7)  # fmt: skip

    # Deriv timing: entry = first tick after the decision (1001), exit = 5 ticks later (1006).
    # The decision-time quote (101.9) is irrelevant; entry is 50.0, so a rise to 60 is a win.
    assert await book.on_tick(Tick(symbol="1HZ100V", epoch=1_001, quote=50.0)) == 0  # entry
    for epoch in range(1_002, 1_006):
        assert await book.on_tick(Tick(symbol="1HZ100V", epoch=epoch, quote=1.0)) == 0
    assert await book.on_tick(Tick(symbol="1HZ100V", epoch=1_006, quote=60.0)) == 1  # exit

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT strategy, action, settled_up, paper_profit, p_call FROM shadow_decisions "
            "WHERE decision_id = %s ORDER BY strategy",
            (decision_id,),
        )
        rows = {r[0]: r[1:] for r in await cur.fetchall()}
    assert rows["model"] == ("CALL", True, Decimal("0.9500"), 0.7)
    assert rows["momentum_10"][:3] == ("CALL", True, Decimal("0.9500"))
    assert rows["always_hold"][:3] == ("HOLD", True, Decimal("0.0000"))
    assert rows["coin_flip"][1] is True


async def test_shadow_matches_paper_trade_timing_when_price_falls_after_decision(pool):
    """Regression: the shadow 'model' row must use the same entry tick as a real contract."""
    async with pool.connection() as conn:
        await migrate(conn)
    decision_id = await TradeRepo(pool).insert_decision({"symbol": "1HZ100V", "action": "PUT"})
    book = ShadowBook(pool, duration_ticks=5, payout_ratio=Decimal("1.95"))
    await book.open(decision_id, entry_epoch=1_000, quotes=[100.0] * 20,
                    model_action=Action.PUT, p_call=0.2)  # fmt: skip
    # decision-time quote 100; entry tick 90; exit 95. Measured from the decision a PUT
    # would "win" (95 < 100), but by Deriv's rule it loses (95 > 90 entry).
    await book.on_tick(Tick(symbol="1HZ100V", epoch=1_001, quote=90.0))
    for epoch in range(1_002, 1_006):
        await book.on_tick(Tick(symbol="1HZ100V", epoch=epoch, quote=92.0))
    await book.on_tick(Tick(symbol="1HZ100V", epoch=1_006, quote=95.0))
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT paper_profit FROM shadow_decisions WHERE decision_id = %s "
            "AND strategy = 'model'",
            (decision_id,),
        )
        assert (await cur.fetchone())[0] == Decimal("-1.0000")
