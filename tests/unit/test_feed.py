import pytest

from agent.clock import FakeClock
from agent.deriv.schemas import Tick
from agent.market.feed import MarketFeed, StaleData

SYMBOL = "1HZ100V"


def make_feed(clock, *, min_history=120, maxlen=600):
    return MarketFeed(clock, stale_after_s=5, min_history=min_history, maxlen=maxlen)


def push(feed, clock, n, start_epoch=1_000, step_s=1.0):
    for i in range(n):
        feed.on_tick(Tick(symbol=SYMBOL, epoch=start_epoch + i, quote=1000 + i * 0.1))
        clock.advance(step_s)


def test_feed_snapshot_fresh():
    clock = FakeClock()
    feed = make_feed(clock)
    push(feed, clock, 150)
    snap = feed.snapshot(SYMBOL)
    assert snap.age_s == pytest.approx(1.0)
    assert len(snap.quotes) == 150
    assert snap.last_epoch == 1_149
    assert "volatility_30" in snap.indicators


def test_feed_snapshot_stale():
    clock = FakeClock()
    feed = make_feed(clock)
    push(feed, clock, 150)
    clock.advance(5)  # last tick now 6 s old
    with pytest.raises(StaleData, match="old"):
        feed.snapshot(SYMBOL)


def test_feed_snapshot_insufficient_history():
    clock = FakeClock()
    feed = make_feed(clock)
    push(feed, clock, 119)
    with pytest.raises(StaleData, match="history"):
        feed.snapshot(SYMBOL)


def test_feed_unknown_symbol():
    with pytest.raises(StaleData, match="no ticks"):
        make_feed(FakeClock()).snapshot("R_75")


def test_feed_ring_buffer_caps():
    clock = FakeClock()
    feed = make_feed(clock, maxlen=600)
    push(feed, clock, 1_000, step_s=0.001)
    assert feed.size(SYMBOL) == 600
    assert feed.snapshot(SYMBOL).last_epoch == 1_999


def test_feed_ignores_out_of_order_tick():
    clock = FakeClock()
    feed = make_feed(clock, min_history=2)
    push(feed, clock, 5)
    feed.on_tick(Tick(symbol=SYMBOL, epoch=1_001, quote=1.0))
    assert feed.size(SYMBOL) == 5


def test_tick_parses_new_api_field_name():
    t = Tick.from_message({"tick": {"underlying_symbol": SYMBOL, "epoch": 1, "quote": 2.5}})
    assert t.symbol == SYMBOL


def test_snapshot_to_state_is_compact():
    clock = FakeClock()
    feed = make_feed(clock)
    push(feed, clock, 200)
    state = feed.snapshot(SYMBOL).to_state(recent=60)
    assert len(state["last_ticks"]) == 60
