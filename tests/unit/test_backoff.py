import pytest

from agent.deriv.ws import backoff_delay


def test_backoff_sequence_without_jitter():
    delays = [backoff_delay(a, base_s=1, max_s=30, jitter=0) for a in range(8)]
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]


@pytest.mark.parametrize("jitter", [0.0, 0.5, 0.999])
def test_backoff_jitter_adds_at_most_one_base(jitter):
    assert 16 <= backoff_delay(4, base_s=1, max_s=30, jitter=jitter) < 17
