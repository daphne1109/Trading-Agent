"""Run the real agent (agent.main.run) against the local fake Deriv server: no keys needed.

Usage (Postgres from `docker compose up -d postgres` must be running):
    python scripts/run_with_fake_server.py --seconds 45 --drop-every 15
    python scripts/run_with_fake_server.py --seconds 45 --no-trade   # market data only

With trading on, a stub decision model answers every round (random direction, confidence 0.8),
so the full pipeline runs: model -> RiskGate -> executor -> fake buy -> settlement, plus
shadow baselines. Prints what reached Postgres. Wipes the agent tables of that database first.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.decision.base import Action, Decision  # noqa: E402
from agent.main import run  # noqa: E402
from tests.fakes.fake_deriv_server import FakeDerivServer  # noqa: E402

TABLES = (
    "shadow_decisions, trades, approvals, risk_verdicts, decisions, ticks, agent_events, "
    "agent_state"
)


class StubModel:
    """Stands in for Jev: random direction at confidence 0.8. Not a strategy."""

    name = "jev"

    async def decide(self, state: dict, question: dict) -> Decision:
        action = random.choice((Action.CALL, Action.PUT))
        other = Action.PUT if action is Action.CALL else Action.CALL
        return Decision(
            action=action,
            probabilities={action.value: 0.8, other.value: 0.15, "HOLD": 0.05},
            confidence=0.8,
            provider="jev",
            model="stub",
            cost_usd=Decimal("0.00008"),
        )


async def main(seconds: float, drop_every: float, db_url: str, trade: bool) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, connect_timeout=5) as conn:
        cur = await conn.execute("SELECT to_regclass('public.ticks') IS NOT NULL")
        if (await cur.fetchone())[0]:
            await conn.execute(f"TRUNCATE {TABLES}")
            await conn.commit()

    async with FakeDerivServer(tick_interval_s=0.2) as server:
        server.settle_after_s = 1.0

        async def url_provider() -> str:
            return server.issue_url()

        settings = Settings(
            _env_file=None,
            database_url=db_url,
            deriv_ws_allowed_hosts=frozenset({"127.0.0.1"}),
            ws_silence_timeout_s=3,
            min_history_ticks=20,
            decision_interval_s=10,
        )
        agent = asyncio.create_task(
            run(settings, url_provider=url_provider, providers=[StubModel()] if trade else [])
        )

        async def chaos() -> None:
            while drop_every > 0:
                await asyncio.sleep(drop_every)
                print(">>> fake server: dropping all connections")
                await server.drop_all()

        chaos_task = asyncio.create_task(chaos())
        await asyncio.sleep(seconds)
        chaos_task.cancel()
        agent.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await agent

    async with await psycopg.AsyncConnection.connect(db_url, connect_timeout=5) as conn:
        queries = {
            "ticks stored": "SELECT count(*) FROM ticks",
            "decision rounds": "SELECT count(*) FROM decisions",
            "  skipped (warm-up/stale)": "SELECT count(*) FROM decisions WHERE skipped_reason IS NOT NULL",  # noqa: E501
            "trades": "SELECT count(*) FROM trades",
            "  settled": "SELECT count(*) FROM trades WHERE status IN ('won','lost')",
            "shadow paper decisions settled": "SELECT count(*) FROM shadow_decisions WHERE settled_up IS NOT NULL",  # noqa: E501
        }  # fmt: skip
        print()
        for label, sql in queries.items():
            cur = await conn.execute(sql)
            print(f"{label}: {(await cur.fetchone())[0]}")
        cur = await conn.execute(
            "SELECT verdict, count(*) FROM risk_verdicts GROUP BY 1 ORDER BY 1"
        )
        for verdict, n in await cur.fetchall():
            print(f"verdict {verdict}: {n}")
        cur = await conn.execute("SELECT kind, count(*) FROM agent_events GROUP BY 1 ORDER BY 1")
        for kind, n in await cur.fetchall():
            print(f"event {kind}: {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--drop-every", type=float, default=15)
    parser.add_argument("--no-trade", action="store_true")
    parser.add_argument("--db", default="postgresql://agent:agent@127.0.0.1:5432/agent")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(args.seconds, args.drop_every, args.db, not args.no_trade))
