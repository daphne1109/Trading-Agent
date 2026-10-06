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

    assert await book.on_tick(Tick(symbol="1HZ100V", epoch=1_004, quote=200.0)) == 0  # too early
    assert await book.on_tick(Tick(symbol="1HZ100V", epoch=1_005, quote=quotes[-1] + 1)) == 1

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
