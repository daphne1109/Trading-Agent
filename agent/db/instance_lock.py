"""Single-instance guard: only one agent may trade against this database at a time.

Holds a session-level Postgres advisory lock on a dedicated connection for the whole run;
if the process dies, the connection closes and the lock is released automatically.
"""

from __future__ import annotations

import psycopg

LOCK_KEY = 7_311_001


class AnotherInstanceRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._conn: psycopg.AsyncConnection | None = None

    async def acquire(self) -> None:
        conn = await psycopg.AsyncConnection.connect(self._url, autocommit=True, connect_timeout=10)
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        row = await cur.fetchone()
        if not (row and row[0]):
            await conn.close()
            raise AnotherInstanceRunning("another agent instance holds the trading lock")
        self._conn = conn

    async def held(self) -> bool:
        """False if the lock connection is gone (the lock would have been released with it)."""
        conn = self._conn
        if conn is None or conn.closed or conn.broken:
            return False
        try:
            cur = await conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s "
                "AND pid = pg_backend_pid() AND granted",
                (LOCK_KEY,),
            )
            row = await cur.fetchone()
        except Exception:  # noqa: BLE001 - can't prove we hold it, so we don't
            return False
        return bool(row and row[0])

    async def release(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
