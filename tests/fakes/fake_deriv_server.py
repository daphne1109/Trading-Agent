"""A local stand-in for Deriv's demo WebSocket, for offline tests.

Behaves like the real thing in the ways the agent depends on:
- only `/trading/v1/options/ws/demo?otp=<valid, unused otp>` is accepted (OTPs are single-use);
- `ticks` subscriptions stream ticks, echoing `req_id` and a `subscription.id`;
- `ping`, `time` and `forget_all` are answered; anything else returns an error.

Test controls: `issue_url()`, `drop_all()`, `silent = True`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
import time
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

DEMO_PATH = "/trading/v1/options/ws/demo"


class FakeDerivServer:
    def __init__(self, *, tick_interval_s: float = 0.02, start_quote: float = 1000.0) -> None:
        self.tick_interval_s = tick_interval_s
        self.silent = False
        self.connections_total = 0
        self.rejected_total = 0
        self._quote = start_quote
        self._epoch = int(time.time())
        self._valid_otps: set[str] = set()
        self._server: Server | None = None
        self._conns: set[ServerConnection] = set()
        self._sub_ids = itertools.count(1)
        self.port = 0

    async def __aenter__(self) -> FakeDerivServer:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def issue_url(self) -> str:
        otp = secrets.token_hex(8)
        self._valid_otps.add(otp)
        return f"ws://127.0.0.1:{self.port}{DEMO_PATH}?otp={otp}"

    async def drop_all(self) -> None:
        for conn in list(self._conns):
            await conn.close(code=1011, reason="test drop")

    # ------------------------------------------------------------------ server side

    def _next_tick(self, symbol: str) -> dict[str, Any]:
        self._epoch += 1
        self._quote = round(self._quote + (0.07 if self._epoch % 3 else -0.11), 2)
        return {"symbol": symbol, "epoch": self._epoch, "quote": self._quote, "pip_size": 2}

    async def _handler(self, conn: ServerConnection) -> None:
        path, _, query = (conn.request.path if conn.request else "").partition("?")
        otp = query.removeprefix("otp=")
        if path != DEMO_PATH or otp not in self._valid_otps:
            self.rejected_total += 1
            await conn.close(code=4001, reason="invalid path or otp")
            return
        self._valid_otps.discard(otp)  # single use
        self.connections_total += 1
        self._conns.add(conn)
        streams: list[asyncio.Task[None]] = []
        try:
            async for raw in conn:
                msg = json.loads(raw)
                reply = await self._handle(conn, msg, streams)
                if reply is not None and not self.silent:
                    await conn.send(json.dumps(reply))
        except Exception:  # noqa: BLE001 - connection closed by a test
            pass
        finally:
            for task in streams:
                task.cancel()
            self._conns.discard(conn)

    async def _handle(
        self, conn: ServerConnection, msg: dict[str, Any], streams: list[asyncio.Task[None]]
    ) -> dict[str, Any] | None:
        req_id = msg.get("req_id")
        if "ping" in msg:
            return {"msg_type": "ping", "ping": "pong", "req_id": req_id}
        if "time" in msg:
            return {"msg_type": "time", "time": self._epoch, "req_id": req_id}
        if "forget_all" in msg:
            for task in streams:
                task.cancel()
            return {"msg_type": "forget_all", "forget_all": [], "req_id": req_id}
        if "ticks" in msg and msg.get("subscribe") == 1:
            sub_id = f"sub-{next(self._sub_ids)}"
            streams.append(asyncio.create_task(self._stream(conn, msg["ticks"], req_id, sub_id)))
            return None
        return {
            "msg_type": "error",
            "error": {"code": "UnrecognisedRequest", "message": "fake server"},
            "req_id": req_id,
        }

    async def _stream(self, conn: ServerConnection, symbol: str, req_id: Any, sub_id: str) -> None:
        while True:
            if not self.silent:
                await conn.send(
                    json.dumps(
                        {
                            "msg_type": "tick",
                            "tick": self._next_tick(symbol),
                            "subscription": {"id": sub_id},
                            "req_id": req_id,
                        }
                    )
                )
            await asyncio.sleep(self.tick_interval_s)
