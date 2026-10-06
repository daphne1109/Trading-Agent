"""Postgres fixtures. Integration DB tests are skipped when no database is reachable."""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://agent:agent@127.0.0.1:5432/agent")
TEST_DB = "agent_test"


def _test_url() -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(TEST_DB)))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc.__class__.__name__}); run docker compose up -d")
    return _test_url()


@pytest.fixture
async def pool(test_db_url):
    async with await psycopg.AsyncConnection.connect(
        test_db_url, autocommit=True, connect_timeout=5
    ) as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    p = AsyncConnectionPool(
        test_db_url, min_size=1, max_size=4, open=False, kwargs={"connect_timeout": 5}
    )
    await p.open(wait=True, timeout=10)
    try:
        yield p
    finally:
        await p.close()
