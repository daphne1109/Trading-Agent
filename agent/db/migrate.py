"""Minimal forward-only migration runner: applies agent/db/migrations/NNN_*.sql in order."""

from __future__ import annotations

from pathlib import Path

from psycopg import AsyncConnection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 7_311_002  # arbitrary constant for pg_advisory_xact_lock


async def migrate(conn: AsyncConnection) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the versions applied."""
    applied_now: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            cur = await conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
            )
            if await cur.fetchone():
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied_now.append(version)
    return applied_now
