import pytest

from agent.db.instance_lock import AnotherInstanceRunning, InstanceLock

pytestmark = pytest.mark.integration


async def test_second_instance_is_refused_and_lock_is_released(test_db_url):
    first, second = InstanceLock(test_db_url), InstanceLock(test_db_url)
    await first.acquire()
    try:
        assert await first.held()
        with pytest.raises(AnotherInstanceRunning):
            await second.acquire()
    finally:
        await first.release()
    assert not await first.held()
    await second.acquire()  # free again once the first instance is gone
    await second.release()
