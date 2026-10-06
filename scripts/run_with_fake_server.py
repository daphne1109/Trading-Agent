"""Run the real agent against the local fake Deriv server: no keys or internet needed.

Usage (Postgres from `docker compose up -d postgres` must be running):
    python scripts/run_with_fake_server.py --seconds 30 --drop-every 10

Prints how many ticks reached Postgres and which connection events were recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.main import run  # noqa: E402
from tests.fakes.fake_deriv_server import FakeDerivServer  # noqa: E402


async def main(seconds: float, drop_every: float, db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, connect_timeout=5) as conn:
        if await _tables_exist(conn):
            await conn.execute("TRUNCATE ticks, agent_events")
            await conn.commit()

    async with FakeDerivServer(tick_interval_s=0.2) as server:

        async def url_provider() -> str:
            return server.issue_url()

        settings = Settings(
            _env_file=None,
            database_url=db_url,
            deriv_ws_allowed_hosts=frozenset({"127.0.0.1"}),
            ws_silence_timeout_s=3,
        )
        agent = asyncio.create_task(run(settings, url_provider=url_provider))

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
        cur = await conn.execute("SELECT count(*), min(epoch), max(epoch) FROM ticks")
        count, lo, hi = await cur.fetchone()
        print(f"\nticks stored: {count} (epochs {lo}..{hi})")
        cur = await conn.execute("SELECT kind, count(*) FROM agent_events GROUP BY kind ORDER BY 1")
        for kind, n in await cur.fetchall():
            print(f"event {kind}: {n}")


async def _tables_exist(conn: psycopg.AsyncConnection) -> bool:
    cur = await conn.execute("SELECT to_regclass('public.ticks') IS NOT NULL")
    return bool((await cur.fetchone())[0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--drop-every", type=float, default=10)
    parser.add_argument("--db", default="postgresql://agent:agent@127.0.0.1:5432/agent")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(args.seconds, args.drop_every, args.db))
