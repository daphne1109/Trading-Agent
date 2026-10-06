"""Simple, explainable market indicators. Pure functions over a sequence of quotes."""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import fmean, pstdev


def log_returns(quotes: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(quotes, quotes[1:], strict=False)]


def total_return(quotes: Sequence[float], n: int) -> float:
    """Log return over the last n ticks."""
    window = quotes[-(n + 1) :]
    return math.log(window[-1] / window[0]) if len(window) >= 2 else 0.0


def volatility(quotes: Sequence[float], n: int) -> float:
    """Population std-dev of the last n tick log returns."""
    rets = log_returns(quotes[-(n + 1) :])
    return pstdev(rets) if len(rets) >= 2 else 0.0


def streak(quotes: Sequence[float]) -> int:
    """Consecutive moves in the same direction at the end: +3 = three up-ticks, -2 = two down."""
    count = 0
    direction = 0
    for a, b in zip(reversed(quotes[:-1]), reversed(quotes[1:]), strict=False):
        move = (b > a) - (b < a)
        if move == 0:
            break
        if direction == 0:
            direction = move
        elif move != direction:
            break
        count += 1
    return count * direction


def zscore_from_mean(quotes: Sequence[float], n: int) -> float:
    """How many std-devs the last quote is from the mean of the last n quotes."""
    window = quotes[-n:]
    if len(window) < 2:
        return 0.0
    sd = pstdev(window)
    return (window[-1] - fmean(window)) / sd if sd > 0 else 0.0


def compute(quotes: Sequence[float]) -> dict[str, float]:
    """The indicator set sent to the decision model (rounded to keep prompts small)."""
    return {
        "return_10": round(total_return(quotes, 10), 6),
        "return_30": round(total_return(quotes, 30), 6),
        "volatility_30": round(volatility(quotes, 30), 7),
        "volatility_120": round(volatility(quotes, 120), 7),
        "streak": float(streak(quotes)),
        "zscore_60": round(zscore_from_mean(quotes, 60), 3),
    }
