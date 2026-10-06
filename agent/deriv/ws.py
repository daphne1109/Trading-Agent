"""Resilient Deriv WebSocket connection.

- Every request carries a `req_id`; replies are routed back to the awaiting caller.
- Subscriptions are remembered and re-sent after every reconnect, feeding the same queue.
- Any disconnect, error or silence longer than `silence_timeout_s` triggers a reconnect with
  exponential backoff + jitter, using a fresh OTP URL from `url_provider`.
- The demo-only guard runs on every URL before connecting. A refusal is never retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import random
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import websockets

from agent.deriv.guard import RealAccountRefused, assert_demo_ws_url

UrlProvider = Callable[[], Awaitable[str]]
EventSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
Message = dict[str, Any]

SUBSCRIPTION_QUEUE_SIZE = 2_000


class DerivError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ConnectionLost(Exception):
    """The connection dropped while a request was waiting for its reply."""


class ConnectionSilent(Exception):
    """No message arrived within the silence timeout."""


_OTP = re.compile(r"(otp=)[^&\s'\"]+")


def redact(text: str) -> str:
    """Strip one-time passwords from anything that may be logged."""
    return _OTP.sub(r"\1***", text)


def backoff_delay(attempt: int, *, base_s: float, max_s: float, jitter: float) -> float:
    """Exponential backoff: base * 2^attempt, capped at max_s, plus up to one base of jitter."""
    return min(max_s, base_s * float(2**attempt)) + jitter * base_s


@dataclass
class _Subscription:
    request: Message
    queue: asyncio.Queue[Message] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIPTION_QUEUE_SIZE)
    )
    server_id: str | None = None  # Deriv's subscription.id, needed to `forget` it


async def _no_events(kind: str, message: str, data: dict[str, Any]) -> None:
    return None


class DerivWS:
    def __init__(
        self,
        url_provider: UrlProvider,
        allowed_hosts: Collection[str],
        *,
        ping_interval_s: float = 30.0,
        silence_timeout_s: float = 15.0,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 30.0,
        on_event: EventSink = _no_events,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._url_provider = url_provider
        self._allowed_hosts = allowed_hosts
        self._ping_interval_s = ping_interval_s
        self._silence_timeout_s = silence_timeout_s
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._emit = on_event
        self._rng = rng

        self._req_ids = itertools.count(1)
        self._sub_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._subs: dict[int, _Subscription] = {}
        self._route: dict[int, int] = {}  # live req_id -> subscription id
        self._ws: Any = None
        self._url: str | None = None
        self._stopping = False

        self.connected = asyncio.Event()
        self.reconnects = 0

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Connect and keep the connection alive until close() is called."""
        attempt = 0
        while not self._stopping:
            try:
                url = await self._url_provider()
                assert_demo_ws_url(url, self._allowed_hosts)
                async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
                    self._ws = ws
                    self._url = url
                    attempt = 0
                    await self._resubscribe()
                    self.connected.set()
                    await self._emit("ws_connected", "connected", {"reconnects": self.reconnects})
                    await self._serve(ws)
            except RealAccountRefused:
                await self._emit("guard_refused", "refused a non-demo WebSocket URL", {})
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every other failure means "reconnect"
                if not self._stopping:
                    await self._emit("ws_disconnected", redact(f"{type(exc).__name__}: {exc}"), {})
            finally:
                self.connected.clear()
                self._ws = None
                self._url = None
                self._fail_pending()

            if self._stopping:
                break
            self.reconnects += 1
            delay = backoff_delay(
                attempt, base_s=self._backoff_base_s, max_s=self._backoff_max_s, jitter=self._rng()
            )
            attempt += 1
            await asyncio.sleep(delay)

    async def close(self) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()

    # ------------------------------------------------------------------ public API

    async def request(self, payload: Message, timeout_s: float = 10.0) -> Message:
        """Send one request and return its reply. Raises DerivError on an API error."""
        await asyncio.wait_for(self.connected.wait(), timeout_s)
        ws = self._ws
        if ws is None:
            raise ConnectionLost("connection dropped before sending")
        req_id = next(self._req_ids)
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await ws.send(json.dumps({**payload, "req_id": req_id}))
            msg = await asyncio.wait_for(future, timeout_s)
        finally:
            self._pending.pop(req_id, None)
        if "error" in msg:
            err = msg["error"]
            raise DerivError(str(err.get("code", "Unknown")), str(err.get("message", "")))
        return msg

    async def subscribe(self, payload: Message) -> asyncio.Queue[Message]:
        """Start a subscription that survives reconnects. Messages (and errors) go to the queue."""
        sub_id = next(self._sub_ids)
        self._subs[sub_id] = _Subscription(request=dict(payload))
        if self.connected.is_set():
            await self._send_subscription(sub_id)
        return self._subs[sub_id].queue

    async def unsubscribe(self, queue: asyncio.Queue[Message]) -> None:
        """Stop a subscription: never re-sent after reconnects; Deriv is told to forget it."""
        for sub_id, sub in list(self._subs.items()):
            if sub.queue is queue:
                del self._subs[sub_id]
                for live_req, sid in list(self._route.items()):
                    if sid == sub_id:
                        del self._route[live_req]
                if sub.server_id and self.connected.is_set():
                    with contextlib.suppress(Exception):
                        await self.request({"forget": sub.server_id}, timeout_s=5)
                return

    def connection_is_demo(self) -> bool:
        """Re-run the demo guard on the live connection's URL (called right before every buy)."""
        if not self.connected.is_set() or self._url is None:
            return False
        try:
            assert_demo_ws_url(self._url, self._allowed_hosts)
        except RealAccountRefused:
            return False
        return True

    # ------------------------------------------------------------------ internals

    async def _serve(self, ws: Any) -> None:
        keepalive = asyncio.create_task(self._keepalive(ws))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), self._silence_timeout_s)
                except TimeoutError:
                    raise ConnectionSilent(f"no message for {self._silence_timeout_s}s") from None
                self._dispatch(json.loads(raw))
        finally:
            keepalive.cancel()

    async def _keepalive(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._ping_interval_s)
            await ws.send(json.dumps({"ping": 1, "req_id": next(self._req_ids)}))

    def _dispatch(self, msg: Message) -> None:
        req_id = msg.get("req_id")
        sub_id = self._route.get(req_id) if isinstance(req_id, int) else None
        if sub_id is not None:
            sub = self._subs[sub_id]
            server_id = (msg.get("subscription") or {}).get("id")
            if server_id:
                sub.server_id = str(server_id)
            queue = sub.queue
            if queue.full():
                queue.get_nowait()  # drop the oldest; a slow consumer must not stall the socket
            queue.put_nowait(msg)
            return
        future = self._pending.get(req_id) if isinstance(req_id, int) else None
        if future is not None and not future.done():
            future.set_result(msg)

    async def _send_subscription(self, sub_id: int) -> None:
        for live_req, sid in list(self._route.items()):
            if sid == sub_id:
                del self._route[live_req]
        req_id = next(self._req_ids)
        self._route[req_id] = sub_id
        request = {**self._subs[sub_id].request, "subscribe": 1, "req_id": req_id}
        await self._ws.send(json.dumps(request))

    async def _resubscribe(self) -> None:
        self._route.clear()
        for sub_id in self._subs:
            await self._send_subscription(sub_id)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionLost("connection dropped"))
        self._pending.clear()
