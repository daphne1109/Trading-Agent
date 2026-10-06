from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent.config import Settings


def make(**overrides):
    return Settings(_env_file=None, **overrides)


def test_config_defaults_are_consistent():
    s = make()
    assert s.stake_usd <= s.max_stake_usd
    assert s.min_confidence <= s.approval_band_high
    assert not s.kill_switch


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_confidence": 0.3},
        {"min_confidence": 1.0},
        {"stake_usd": Decimal("5"), "max_stake_usd": Decimal("2")},
        {"approval_band_high": 0.52, "min_confidence": 0.6},
        {"stale_tick_s": 0},
        {"min_history_ticks": 500, "buffer_ticks": 200},
    ],
)
def test_config_rejects_bad_limits(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_secrets_are_not_printed():
    s = make(deriv_pat="super-secret-token")
    assert "super-secret-token" not in repr(s)
    assert "super-secret-token" not in str(s.model_dump())
