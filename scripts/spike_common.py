"""Shared helpers for the P0 spike scripts (throwaway exploration code)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "spike_output"  # git-ignored: raw responses may contain account ids

load_dotenv(ROOT / ".env")


def require_env(*names: str) -> dict[str, str]:
    """Return the requested env vars, or exit with a clear message if any are missing."""
    values = {n: os.getenv(n, "").strip() for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}  (see .env.example)")
    return values


def save(group: str, name: str, payload: Any) -> Path:
    """Write a raw response to spike_output/<group>/<name>.json for later inspection."""
    path = OUT_DIR / group / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def show(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, default=str)[:3000])


def sample_state() -> dict[str, Any]:
    """A fake market snapshot shaped like the one the agent will send to the decision model."""
    ticks = [1000.00 + 0.15 * i + (0.4 if i % 3 == 0 else -0.2) for i in range(60)]
    return {
        "symbol": "1HZ100V",
        "last_60_ticks": [round(t, 2) for t in ticks],
        "indicators": {
            "return_30": 0.0021,
            "volatility_30": 0.00041,
            "volatility_120": 0.00038,
            "up_streak": 2,
            "distance_from_mean_60": 0.8,
        },
        "open_position": None,
        "today": {"trades": 0, "pnl_usd": 0.0},
        "playbook": "Market is random by design. Prefer HOLD when the signal is unclear.",
    }


DECISION_QUESTION = {
    "action": {
        "type": "choice",
        "instructions": (
            "You are the decision step of a trading agent on a demo account. Given the market "
            "state, choose whether the price of the symbol will be higher (CALL) or lower (PUT) "
            "5 ticks from now, or HOLD if there is no clear read. Follow the playbook."
        ),
        "criteria": {
            "CALL": "Price is likely to be higher 5 ticks from now",
            "PUT": "Price is likely to be lower 5 ticks from now",
            "HOLD": "No clear read, or conditions look unsafe",
        },
    }
}
