import math

import pytest

from agent.market import indicators as ind


def test_log_returns_known_values():
    assert ind.log_returns([100, 110, 99]) == pytest.approx([math.log(1.1), math.log(0.9)])


def test_total_return_uses_last_n_ticks():
    quotes = [50, 100, 101, 102, 110]
    assert ind.total_return(quotes, 2) == pytest.approx(math.log(110 / 101))


def test_volatility_flat_series_is_zero():
    assert ind.volatility([100.0] * 50, 30) == 0.0


def test_volatility_hand_computed():
    # returns +ln(1.01), -ln(1.01) alternating -> population std = ln(1.01)
    quotes = [100.0, 101.0, 100.0, 101.0, 100.0]
    assert ind.volatility(quotes, 4) == pytest.approx(math.log(1.01))


@pytest.mark.parametrize(
    ("quotes", "expected"),
    [
        ([1, 2, 3, 4], 3),
        ([4, 3, 2, 1], -3),
        ([1, 3, 2, 3, 4], 2),
        ([1, 2, 2], 0),
        ([5], 0),
        ([], 0),
    ],
)
def test_streak(quotes, expected):
    assert ind.streak(quotes) == expected


def test_zscore_last_above_mean_is_positive():
    assert ind.zscore_from_mean([1, 1, 1, 1, 5], 5) > 1.5


def test_compute_returns_all_keys_and_short_series_safe():
    out = ind.compute([100.0, 100.5])
    assert set(out) == {
        "return_10",
        "return_30",
        "volatility_30",
        "volatility_120",
        "streak",
        "zscore_60",
    }
