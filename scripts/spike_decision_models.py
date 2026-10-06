"""P0 spike: send the same fake market snapshot to every decision-model provider.

Usage:
    python scripts/spike_decision_models.py jev      # TypeSafe Jev (primary)
    python scripts/spike_decision_models.py clef     # Cloudflare Clef-flash (fallback 1)
    python scripts/spike_decision_models.py claude   # Claude Haiku 4.5 (fallback 2)
    python scripts/spike_decision_models.py all

For each provider it prints the chosen action, the probabilities, latency and token usage,
and saves the raw response under spike_output/models/.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from spike_common import DECISION_QUESTION, require_env, sample_state, save, show

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


def systemone_body(model: str) -> dict:
    return {"model": model, "state": sample_state(), "questions": DECISION_QUESTION}


def report(provider: str, started: float, raw: dict) -> None:
    ms = (time.perf_counter() - started) * 1000
    save("models", provider, raw)
    show(f"{provider} raw response", raw)
    print(f"\n{provider}: latency {ms:.0f} ms")


def spike_jev() -> None:
    env = require_env("TYPESAFE_API_KEY")
    model = os.getenv("TYPESAFE_MODEL", "jev-1.13.0")
    started = time.perf_counter()
    r = httpx.post(
        TYPESAFE_URL,
        headers={"Authorization": f"Bearer {env['TYPESAFE_API_KEY']}"},
        json=systemone_body(model),
        timeout=15,
    )
    print(f"Jev -> HTTP {r.status_code}")
    report("jev", started, r.json() if r.content else {"status": r.status_code})


def spike_clef() -> None:
    # Cloudflare says Clef "speaks Jev's SystemOne API"; the exact Workers AI request shape is
    # one of the things this spike confirms. We try the SystemOne body on the ai/run endpoint.
    env = require_env("CF_ACCOUNT_ID", "CF_API_TOKEN")
    model = os.getenv("CF_MODEL", "@cf/cloudflare/clef-flash")
    url = f"https://api.cloudflare.com/client/v4/accounts/{env['CF_ACCOUNT_ID']}/ai/run/{model}"
    body = systemone_body(model)
    body.pop("model")
    started = time.perf_counter()
    r = httpx.post(
        url, headers={"Authorization": f"Bearer {env['CF_API_TOKEN']}"}, json=body, timeout=15
    )
    print(f"Clef -> HTTP {r.status_code}")
    report("clef", started, r.json() if r.content else {"status": r.status_code})


class ClaudeDecision(BaseModel):
    action: Literal["CALL", "PUT", "HOLD"]
    p_call: float = Field(ge=0, le=1)
    p_put: float = Field(ge=0, le=1)
    p_hold: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


def spike_claude() -> None:
    require_env("ANTHROPIC_API_KEY")
    import anthropic

    client = anthropic.Anthropic()
    q = DECISION_QUESTION["action"]
    prompt = (
        f"{q['instructions']}\n\nOptions: {json.dumps(q['criteria'])}\n\n"
        f"Market state:\n{json.dumps(sample_state())}\n\n"
        "Give a probability for each option (they must sum to 1), the chosen action, "
        "and your confidence in that action."
    )
    started = time.perf_counter()
    response = client.messages.parse(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        output_format=ClaudeDecision,
    )
    decision = response.parsed_output
    raw = {
        "parsed": decision.model_dump() if decision else None,
        "stop_reason": response.stop_reason,
        "usage": response.usage.model_dump(),
    }
    report("claude", started, raw)


PROVIDERS = {"jev": spike_jev, "clef": spike_clef, "claude": spike_claude}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = PROVIDERS if which == "all" else {which: PROVIDERS.get(which)}
    if None in targets.values():
        sys.exit(__doc__)
    for name, fn in targets.items():
        print(f"\n######## {name} ########")
        try:
            fn()
        except SystemExit as exc:
            print(exc)
        except Exception as exc:  # noqa: BLE001 - spike: report and continue to the next provider
            print(f"{name} FAILED: {type(exc).__name__}: {exc}")
