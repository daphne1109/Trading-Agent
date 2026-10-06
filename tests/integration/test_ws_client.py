import asyncio
import contextlib

import pytest

from agent.deriv.guard import RealAccountRefused
from agent.deriv.ws import DerivError, DerivWS
from tests.fakes.fake_deriv_server import FakeDerivServer

pytestmark = pytest.mark.integration
LOCAL = {"127.0.0.1"}
SYMBOL = "1HZ100V"


def make_client(server, events, **kw):
    urls = []

    async def url_provider():
        url = server.issue_url()
        urls.append(url)
        return url

    async def on_event(kind, message, data):
        events.append(kind)

    client = DerivWS(
        url_provider,
        LOCAL,
        ping_interval_s=kw.pop("ping_interval_s", 5),
        silence_timeout_s=kw.pop("silence_timeout_s", 2),
        backoff_base_s=0.01,
        backoff_max_s=0.05,
        on_event=on_event,
        rng=lambda: 0.0,
    )
    return client, urls


@contextlib.asynccontextmanager
async def running(client):
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(client.connected.wait(), 3)
        yield task
    finally:
        await client.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def next_tick(queue, wait_s=2.0):
    while True:
        msg = await asyncio.wait_for(queue.get(), wait_s)
        if msg.get("msg_type") == "tick":
            return msg


async def test_ws_connects_and_streams_ticks():
    async with FakeDerivServer() as server:
        events = []
        client, _ = make_client(server, events)
        async with running(client):
            queue = await client.subscribe({"ticks": SYMBOL})
            first = await next_tick(queue)
            second = await next_tick(queue)
            assert second["tick"]["epoch"] > first["tick"]["epoch"]
        assert "ws_connected" in events


async def test_ws_reconnect_and_resubscribe_with_fresh_otp():
    async with FakeDerivServer() as server:
        events = []
        client, urls = make_client(server, events)
        async with running(client):
            queue = await client.subscribe({"ticks": SYMBOL})
            await next_tick(queue)

            await server.drop_all()
            await asyncio.sleep(0.2)
            while not queue.empty():  # discard ticks from before the drop
                queue.get_nowait()

            await next_tick(queue)  # same queue keeps receiving after reconnect
            assert client.reconnects >= 1
            assert len(set(urls)) == len(urls) >= 2  # a new OTP for every connection
            assert server.rejected_total == 0
        assert "ws_disconnected" in events


async def test_ws_watchdog_triggers_on_silence():
    async with FakeDerivServer() as server:
        events = []
        client, _ = make_client(server, events, silence_timeout_s=0.3)
        async with running(client):
            queue = await client.subscribe({"ticks": SYMBOL})
            await next_tick(queue)
            server.silent = True
            await asyncio.sleep(0.6)
            server.silent = False
            await next_tick(queue)
            assert client.reconnects >= 1
        assert server.connections_total >= 2


async def test_req_id_routing_parallel_requests():
    async with FakeDerivServer() as server:
        client, _ = make_client(server, [])
        async with running(client):
            ping, t = await asyncio.gather(client.request({"ping": 1}), client.request({"time": 1}))
            assert ping["msg_type"] == "ping"
            assert t["msg_type"] == "time"


async def test_request_raises_deriv_error():
    async with FakeDerivServer() as server:
        client, _ = make_client(server, [])
        async with running(client):
            with pytest.raises(DerivError, match="UnrecognisedRequest"):
                await client.request({"definitely_not_real": 1})


async def test_guard_blocks_real_url_before_connecting():
    async with FakeDerivServer() as server:
        events = []

        async def real_url():
            return f"ws://127.0.0.1:{server.port}/trading/v1/options/ws/real?otp=x"

        async def on_event(kind, message, data):
            events.append(kind)

        client = DerivWS(real_url, LOCAL, on_event=on_event)
        with pytest.raises(RealAccountRefused):
            await asyncio.wait_for(client.run(), 2)
        assert server.connections_total == 0
        assert server.rejected_total == 0  # never even reached the server
        assert events == ["guard_refused"]
