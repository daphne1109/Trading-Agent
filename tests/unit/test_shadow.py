from decimal import Decimal

import pytest

from agent.decision.base import Action
from agent.shadow import baseline_actions, coin_flip, momentum, settle

PAYOUT = Decimal("1.95")


def test_coin_flip_is_deterministic_per_decision():
    assert coin_flip(42) == coin_flip(42)
    picks = {coin_flip(i) for i in range(50)}
    assert picks == {Action.CALL, Action.PUT}


@pytest.mark.parametrize(
    ("quotes", "expected"),
    [
        ([1.0] * 5 + [2.0] * 7, Action.CALL),
        ([2.0] * 5 + [1.0] * 7, Action.PUT),
        ([1.0] * 12, Action.HOLD),
        ([1.0, 2.0], Action.HOLD),  # not enough history
    ],
)
def test_momentum(quotes, expected):
    assert momentum(quotes) is expected


def test_baseline_set():
    assert set(baseline_actions(1, [1.0] * 20)) == {"coin_flip", "always_hold", "momentum_10"}


@pytest.mark.parametrize(
    ("action", "entry", "exit_", "profit"),
    [
        (Action.CALL, 100.0, 100.5, Decimal("0.95")),
        (Action.CALL, 100.0, 99.5, Decimal("-1")),
        (Action.CALL, 100.0, 100.0, Decimal("-1")),  # a tie loses
        (Action.PUT, 100.0, 99.5, Decimal("0.95")),
        (Action.PUT, 100.0, 100.5, Decimal("-1")),
        (Action.PUT, 100.0, 100.0, Decimal("-1")),
        (Action.HOLD, 100.0, 120.0, Decimal("0")),
    ],
)
def test_settle_rise_fall_rules(action, entry, exit_, profit):
    assert settle(action, entry, exit_, PAYOUT) == profit
