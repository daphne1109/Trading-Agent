import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agent.clock import FakeClock
from agent.config import Settings
from agent.deriv.guard import RealAccountRefused
from agent.deriv.schemas import Tick
from agent.deriv.ws import DerivWS
from agent.execution.executor import Outcome
from agent.execution.paper import PaperExecutor
from agent.market.feed import MarketFeed

SYM = "1HZ100V"
SETTINGS = Settings(_env_file=None)


class RepoStub:
    def __init__(self):
        self.calls = []

    async def mark_open(self, trade_id, *a):
        self.calls.append(("open", trade_id))

    async def mark_error(self, trade_id, msg):
        self.calls.append(("error", trade_id, msg))

    async def mark_unknown(self, trade_id, msg):
        self.calls.append(("unknown", trade_id, msg))

    async def settle(self, trade_id, status, profit, at, raw):
        self.calls.append(("settled", trade_id, status, profit, raw))

    async def open_trades(self):
        return []


class EventsStub:
    async def record(self, *a, **k):
        return None


def make(start_quotes=(100.0,) * 3, start_epoch=1000):
    clock = FakeClock()
    feed = MarketFeed(clock, stale_after_s=5, min_history=1, maxlen=600)
    for i, q in enumerate(start_quotes):
        feed.on_tick(Tick(symbol=SYM, epoch=start_epoch + i, quote=q))
    ws = SimpleNamespace(reconnects=0, connection_is_demo=lambda: True)
    repo = RepoStub()

    async def never(*_a):
        raise AssertionError("not used")

    ex = PaperExecutor(ws, repo, EventsStub(), SETTINGS, clock, never, never, feed)
    ex.settle_timeout_s = 1.0
    return ex, feed, ws, repo


def push(feed, epochs_quotes):
    for e, q in epochs_quotes:
        feed.on_tick(Tick(symbol=SYM, epoch=e, quote=q))


async def place_and_settle(ex, feed, ws, repo, contract, path, *, reconnect=False, gap=False):
    result = await ex._place(1, contract, {"id": "p"}, Decimal("1.00"), Decimal("1.95"))
    assert result.outcome is Outcome.BOUGHT
    last = feed.last_epoch(SYM)
    epochs = [last + 1 + i + (1 if gap and i >= 3 else 0) for i in range(len(path))]
    if reconnect:
        ws.reconnects += 1
    push(feed, zip(epochs, path, strict=True))
    await asyncio.gather(*ex._watchers)
    return repo.calls[-1]


@pytest.mark.parametrize(
    ("contract", "path", "status", "profit"),
    [
        ("CALL", [100, 100.5, 100.7, 100.2, 100.9, 101.0], "won", Decimal("0.95")),
        ("CALL", [100, 99.0, 99.5, 99.2, 99.9, 99.8], "lost", Decimal("-1.00")),
        ("PUT", [100, 99.0, 99.5, 99.2, 99.9, 99.8], "won", Decimal("0.95")),
        ("PUT", [100, 100.5, 100.7, 100.2, 100.9, 101.0], "lost", Decimal("-1.00")),
        ("CALL", [100, 101, 102, 99, 98, 100], "lost", Decimal("-1.00")),  # tie loses
        ("PUT", [100, 101, 102, 99, 98, 100], "lost", Decimal("-1.00")),
    ],
)
async def test_rise_fall_settlement(contract, path, status, profit):
    ex, feed, ws, repo = make()
    call = await place_and_settle(ex, feed, ws, repo, contract, path)
    assert call[0] == "settled" and call[2] == status and call[3] == profit
    raw = call[4]
    assert raw["exit"]["epoch"] - raw["entry"]["epoch"] == 5
    assert raw["entry"]["quote"] == path[0] and raw["exit"]["quote"] == path[5]


async def test_reconnect_during_contract_is_unknown():
    ex, feed, ws, repo = make()
    call = await place_and_settle(ex, feed, ws, repo, "CALL", [100, 101] * 3, reconnect=True)
    assert call[0] == "unknown" and "reconnect" in call[2]


async def test_gap_in_ticks_is_unknown():
    ex, feed, ws, repo = make()
    call = await place_and_settle(ex, feed, ws, repo, "CALL", [100, 101] * 3, gap=True)
    assert call[0] == "unknown" and "gap" in call[2]


async def test_no_exit_tick_times_out_unknown():
    ex, feed, ws, repo = make()
    ex.settle_timeout_s = 0.3
    await ex._place(1, "CALL", {"id": "p"}, Decimal("1.00"), Decimal("1.95"))
    push(feed, [(feed.last_epoch(SYM) + 1, 100.0)])  # only the entry tick ever arrives
    await asyncio.gather(*ex._watchers)
    assert repo.calls[-1][0] == "unknown"


@pytest.mark.parametrize("payout", [None, Decimal("1.00"), Decimal("0.5")])
async def test_fill_refused_without_usable_payout(payout):
    ex, _feed, _ws, repo = make()
    result = await ex._place(1, "CALL", {"id": "p"}, Decimal("1.00"), payout)
    assert result.outcome is Outcome.ERROR
    assert repo.calls[-1][0] == "error"


async def test_fill_refused_without_market_data():
    ex, _feed, _ws, repo = make(start_quotes=())
    result = await ex._place(1, "CALL", {"id": "p"}, Decimal("1.00"), Decimal("1.95"))
    assert result.outcome is Outcome.ERROR


def test_feed_drops_repeated_ticks_and_ticks_after():
    _ex, feed, _ws, _repo = make()
    push(feed, [(1002, 1.0), (1002, 9.9), (1003, 2.0)])  # 1002 re-sent after a reconnect
    assert [t.epoch for t in feed.ticks_after(SYM, 1001)] == [1002, 1003]
    assert feed.ticks_after(SYM, 1001)[0].quote == 100.0  # the original 1002 kept
    assert feed.last_epoch(SYM) == 1003
    assert feed.last_epoch("R_75") is None


async def test_ws_default_guard_refuses_public_url():
    async def public():
        return "wss://api.derivws.com/trading/v1/options/ws/public"

    ws = DerivWS(public, {"api.derivws.com"})  # no url_guard: demo-only by default
    with pytest.raises(RealAccountRefused):
        await asyncio.wait_for(ws.run(), 2)
