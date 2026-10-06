"""P0 spike: Telegram bot basics using the raw Bot API.

Usage:
    python scripts/spike_telegram.py chat-id   # send your bot any message first, then run this
    python scripts/spike_telegram.py buttons   # sends an approval-style card, waits for your tap

Put the printed chat id in .env as TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import sys
import time

import httpx

from spike_common import require_env, save

env = require_env("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}"


def call(method: str, **params: object) -> dict:
    r = httpx.post(f"{API}/{method}", json=params, timeout=40)
    body = r.json()
    if not body.get("ok"):
        sys.exit(f"{method} failed: {body}")
    return body["result"]


def chat_id() -> None:
    updates = call("getUpdates")
    chats = {
        u["message"]["chat"]["id"]: u["message"]["chat"].get("username")
        or u["message"]["chat"].get("title")
        for u in updates
        if "message" in u
    }
    if not chats:
        sys.exit("No messages yet. Open your bot in Telegram, press Start, send 'hi', then rerun.")
    for cid, name in chats.items():
        print(f"chat id {cid}  ({name})")


def buttons() -> None:
    cid = require_env("TELEGRAM_CHAT_ID")["TELEGRAM_CHAT_ID"]
    card = (
        "Approval needed (spike test)\n"
        "Action  CALL (Rise)   Stake $1.00   5 ticks\n"
        "Confidence 0.61\nNothing will be traded. Tap a button."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": "spike:approve"},
                {"text": "Reject", "callback_data": "spike:reject"},
            ]
        ]
    }
    msg = call("sendMessage", chat_id=cid, text=card, reply_markup=keyboard)
    print("Card sent. Waiting up to 90 s for a tap...")
    offset = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        updates = call("getUpdates", offset=offset, timeout=25, allowed_updates=["callback_query"])
        for u in updates:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq and cq["message"]["message_id"] == msg["message_id"]:
                save("telegram", "callback_query", cq)
                call("answerCallbackQuery", callback_query_id=cq["id"], text="Got it")
                call(
                    "editMessageText",
                    chat_id=cid,
                    message_id=msg["message_id"],
                    text=f"{card}\n\nYou pressed: {cq['data']}",
                )
                print(f"Received callback: {cq['data']} from user {cq['from']['id']}")
                return
    print("No tap within 90 s.")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    {"chat-id": chat_id, "buttons": buttons}.get(step, lambda: sys.exit(__doc__))()
