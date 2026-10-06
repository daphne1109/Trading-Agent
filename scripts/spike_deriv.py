"""P0 spike: prove the new Deriv API works end to end on the DEMO account.

Usage (run from the repo root, in order):
    python scripts/spike_deriv.py accounts   # list accounts (read-only)
    python scripts/spike_deriv.py ticks      # OTP URL -> connect -> 10 ticks, ping, OTP reuse
    python scripts/spike_deriv.py proposal   # + price a 1 USD Rise/Fall contract (no purchase)
    python scripts/spike_deriv.py buy        # + buy it (virtual money) and follow until settled

Every raw message is saved under spike_output/deriv/ so we can write tests from real shapes.
This script refuses to continue unless the WebSocket URL is the /ws/demo endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

from spike_common import require_env, save, show

REST = "https://api.derivws.com"
SYMBOL = "1HZ100V"
STAKE = 1
DURATION_TICKS = 5

env = require_env("DERIV_APP_ID", "DERIV_PAT")
ACCOUNT_ID_OVERRIDE = os.getenv("DERIV_ACCOUNT_ID", "").strip()


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env['DERIV_PAT']}",
        "Deriv-App-ID": env["DERIV_APP_ID"],
        "Content-Type": "application/json",
    }


def assert_demo(url: str) -> None:
    path = urlparse(url).path.rstrip("/")
    if not path.endswith("/ws/demo"):
        sys.exit(f"REFUSING: WebSocket path is {path!r}, not /ws/demo. Nothing was sent.")


# --------------------------------------------------------------------------- REST


async def list_accounts(client: httpx.AsyncClient) -> Any:
    r = await client.get(f"{REST}/trading/v1/options/accounts", headers=headers())
    print(f"GET /accounts -> HTTP {r.status_code}")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    save("deriv", "accounts", {"status": r.status_code, "body": body})
    show("accounts", body)
    return body


def pick_demo_account(body: Any) -> str:
    """Find the demo account id. Field names are not documented yet, so this is a heuristic."""
    if ACCOUNT_ID_OVERRIDE:
        return ACCOUNT_ID_OVERRIDE
    items = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(items, dict):
        items = items.get("accounts", [items])
    candidates = []
    for acc in items if isinstance(items, list) else []:
        blob = json.dumps(acc).lower()
        if "demo" in blob or '"is_virtual": 1' in blob or '"is_virtual": true' in blob:
            acc_id = acc.get("account_id") or acc.get("id") or acc.get("loginid")
            if acc_id:
                candidates.append(str(acc_id))
    if len(candidates) != 1:
        sys.exit(
            f"Could not pick exactly one demo account (found {candidates}). "
            "Look at spike_output/deriv/accounts.json and set DERIV_ACCOUNT_ID in .env."
        )
    return candidates[0]


async def get_ws_url(client: httpx.AsyncClient, account_id: str) -> str:
    r = await client.post(f"{REST}/trading/v1/options/accounts/{account_id}/otp", headers=headers())
    print(f"POST /accounts/<id>/otp -> HTTP {r.status_code}")
    body = r.json()
    save("deriv", "otp", {"status": r.status_code, "body": body})
    url = body["data"]["url"]
    assert_demo(url)
    print("Got a /ws/demo URL (OTP hidden).")
    return url


# --------------------------------------------------------------------------- WebSocket


async def recv_until(
    ws: Any, done: Callable[[dict[str, Any]], bool], wait_s: float = 20
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no matching message in time")
        msg = json.loads(await asyncio.wait_for(ws.recv(), remaining))
        save("deriv", f"msg_{msg.get('msg_type', 'unknown')}", msg)
        if "error" in msg:
            show("ERROR from Deriv", msg)
        if done(msg):
            return msg


async def stream_ticks(ws: Any) -> None:
    await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1, "req_id": 1}))
    for i in range(10):
        msg = await recv_until(ws, lambda m: m.get("msg_type") == "tick" or "error" in m)
        if "error" in msg:
            return
        tick = msg["tick"]
        lag = time.time() - float(tick["epoch"])
        print(f"tick {i + 1:2d}: {tick['quote']}  epoch={tick['epoch']}  lag={lag:.2f}s")
    await ws.send(json.dumps({"forget_all": "ticks", "req_id": 2}))


async def ping(ws: Any) -> None:
    await ws.send(json.dumps({"ping": 1, "req_id": 3}))
    msg = await recv_until(ws, lambda m: m.get("req_id") == 3)
    print(f"ping -> {msg.get('ping', msg)}")


async def get_proposal(ws: Any) -> dict[str, Any] | None:
    req = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": "CALL",
        "currency": "USD",
        "duration": DURATION_TICKS,
        "duration_unit": "t",
        "underlying_symbol": SYMBOL,
        "req_id": 4,
    }
    await ws.send(json.dumps(req))
    msg = await recv_until(ws, lambda m: m.get("req_id") == 4)
    show("proposal", msg)
    if "error" in msg:
        print("\nRise/Fall proposal rejected. Asking which contracts exist for this symbol...")
        await ws.send(json.dumps({"contracts_for": SYMBOL, "req_id": 5}))
        cf = await recv_until(ws, lambda m: m.get("req_id") == 5)
        show("contracts_for (first 3000 chars)", cf)
        return None
    return msg["proposal"]


async def buy_and_follow(ws: Any, proposal: dict[str, Any]) -> None:
    await ws.send(json.dumps({"buy": proposal["id"], "price": proposal["ask_price"], "req_id": 6}))
    bought = await recv_until(ws, lambda m: m.get("req_id") == 6)
    show("buy", bought)
    if "error" in bought:
        return
    contract_id = bought["buy"]["contract_id"]
    await ws.send(
        json.dumps(
            {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1, "req_id": 7}
        )
    )
    while True:
        msg = await recv_until(ws, lambda m: m.get("msg_type") == "proposal_open_contract", 60)
        poc = msg.get("proposal_open_contract", {})
        print(f"contract {contract_id}: status={poc.get('status')} profit={poc.get('profit')}")
        if poc.get("is_sold") or poc.get("status") in {"won", "lost", "sold"}:
            show("settled contract", poc)
            return


async def main(step: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        accounts = await list_accounts(client)
        if step == "accounts":
            return
        account_id = pick_demo_account(accounts)
        url = await get_ws_url(client, account_id)

    async with websockets.connect(url) as ws:
        await stream_ticks(ws)
        await ping(ws)
        proposal = await get_proposal(ws) if step in {"proposal", "buy"} else None
        if step == "buy" and proposal:
            await buy_and_follow(ws, proposal)

    if step == "ticks":
        print("\nTesting whether the OTP URL can be reused after disconnecting...")
        try:
            async with websockets.connect(url) as ws2:
                await ws2.send(json.dumps({"ping": 1, "req_id": 9}))
                print("OTP reuse: ACCEPTED ->", await asyncio.wait_for(ws2.recv(), 10))
        except Exception as exc:  # noqa: BLE001 - spike: we want to see any failure
            print(f"OTP reuse: REJECTED ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "accounts"
    if step not in {"accounts", "ticks", "proposal", "buy"}:
        sys.exit(__doc__)
    asyncio.run(main(step))
