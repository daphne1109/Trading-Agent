"""Read-only SQL for the dashboard. Every function takes a connection and returns plain data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

LIVE_HEARTBEAT_S = 30


def _day_start() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _rows(conn: AsyncConnection, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)  # type: ignore[arg-type]
        return await cur.fetchall()


async def _one(conn: AsyncConnection, sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    rows = await _rows(conn, sql, params)
    return rows[0] if rows else None


async def status(conn: AsyncConnection) -> dict[str, Any]:
    rows = await _rows(conn, "SELECT key, value, updated_at FROM agent_state")
    state = {r["key"]: r for r in rows}
    hb = state.get("heartbeat")
    now = datetime.now(UTC)
    age = (now - hb["updated_at"]).total_seconds() if hb else None
    killed = bool(state.get("killed", {}).get("value"))
    paused = bool(state.get("paused", {}).get("value"))
    connected = bool(hb and hb["value"].get("connected"))
    if killed:
        light = "KILLED"
    elif paused:
        light = "PAUSED"
    elif age is not None and age <= LIVE_HEARTBEAT_S and connected:
        light = "LIVE"
    elif age is not None and age <= LIVE_HEARTBEAT_S:
        light = "RECONNECTING"
    else:
        light = "OFFLINE"
    last_tick = await _one(
        conn, "SELECT quote, epoch, received_at FROM ticks ORDER BY id DESC LIMIT 1"
    )
    started = state.get("started_at", {}).get("value")
    return {
        "light": light,
        "heartbeat_age_s": age,
        "reconnects": hb["value"].get("reconnects") if hb else None,
        "config": state.get("config", {}).get("value") or {},
        "started_at": datetime.fromisoformat(started) if started else None,
        "last_tick": last_tick,
        "last_tick_age_s": (now - last_tick["received_at"]).total_seconds() if last_tick else None,
    }


async def kpis(conn: AsyncConnection) -> dict[str, Any]:
    day = _day_start()
    d = await _one(
        conn,
        """SELECT count(*) FILTER (WHERE skipped_reason IS NULL) AS decisions,
                  count(*) FILTER (WHERE skipped_reason IS NOT NULL) AS skipped,
                  count(*) FILTER (WHERE action = 'HOLD') AS holds,
                  coalesce(sum(cost_usd), 0) AS spend,
                  avg(latency_ms) FILTER (WHERE latency_ms > 0) AS latency
           FROM decisions WHERE created_at >= %s""",
        (day,),
    )
    v = {
        r["verdict"]: r["n"]
        for r in await _rows(
            conn,
            "SELECT verdict, count(*) AS n FROM risk_verdicts WHERE checked_at >= %s GROUP BY 1",
            (day,),
        )
    }
    t = await _one(
        conn,
        """SELECT count(*) AS trades,
                  count(*) FILTER (WHERE status = 'won') AS won,
                  count(*) FILTER (WHERE status = 'lost') AS lost,
                  count(*) FILTER (WHERE status IN ('pending', 'open')) AS open,
                  count(*) FILTER (WHERE status = 'unknown') AS unknown,
                  coalesce(sum(profit), 0) AS pnl
           FROM trades WHERE created_at >= %s""",
        (day,),
    )
    assert d is not None and t is not None
    return {**d, **t, "verdicts": v, "blocked": v.get("REJECT", 0)}


async def feed(conn: AsyncConnection, limit: int = 25) -> list[dict]:
    return await _rows(
        conn,
        """SELECT d.id, d.created_at, d.action, d.confidence, d.provider, d.skipped_reason,
                  d.error, v.verdict, v.reasons, t.status AS trade_status, t.profit
           FROM decisions d
           LEFT JOIN risk_verdicts v ON v.decision_id = d.id
           LEFT JOIN trades t ON t.decision_id = d.id
           ORDER BY d.id DESC LIMIT %s""",
        (limit,),
    )


async def shadow(conn: AsyncConnection) -> list[dict]:
    rows = await _rows(
        conn,
        """SELECT strategy,
                  count(*) FILTER (WHERE action <> 'HOLD' AND settled_up IS NOT NULL) AS trades,
                  count(*) FILTER (WHERE paper_profit > 0) AS wins,
                  coalesce(sum(paper_profit), 0) AS pnl
           FROM shadow_decisions GROUP BY strategy""",
    )
    order = {"model": 0, "momentum_10": 1, "coin_flip": 2, "always_hold": 3}
    for r in rows:
        r["win_rate"] = (r["wins"] / r["trades"]) if r["trades"] else None
    return sorted(rows, key=lambda r: order.get(r["strategy"], 9))


async def chart(conn: AsyncConnection, minutes: int = 10) -> dict[str, Any]:
    ticks = await _rows(
        conn,
        """SELECT epoch, quote FROM ticks
           WHERE received_at >= now() - make_interval(mins => %s) ORDER BY epoch""",
        (minutes,),
    )
    trades = await _rows(
        conn,
        """SELECT t.id, t.contract_type, t.status, t.profit,
                  extract(epoch FROM t.bought_at)::bigint AS epoch,
                  (SELECT quote FROM ticks k WHERE k.received_at <= t.bought_at
                   ORDER BY k.received_at DESC LIMIT 1) AS quote, t.decision_id
           FROM trades t
           WHERE t.bought_at >= now() - make_interval(mins => %s)""",
        (minutes,),
    )
    blocked = await _rows(
        conn,
        """SELECT d.id AS decision_id, d.action, v.verdict,
                  extract(epoch FROM d.created_at)::bigint AS epoch,
                  (SELECT quote FROM ticks k WHERE k.received_at <= d.created_at
                   ORDER BY k.received_at DESC LIMIT 1) AS quote
           FROM decisions d JOIN risk_verdicts v ON v.decision_id = d.id
           WHERE v.verdict IN ('REJECT', 'NEEDS_APPROVAL')
             AND d.created_at >= now() - make_interval(mins => %s)""",
        (minutes,),
    )
    step = max(1, len(ticks) // 600)
    return {
        "ticks": [[r["epoch"], r["quote"]] for r in ticks[::step]],
        "trades": [_jsonable(r) for r in trades if r["quote"] is not None],
        "blocked": [_jsonable(r) for r in blocked if r["quote"] is not None],
    }


async def decision(conn: AsyncConnection, decision_id: int) -> dict[str, Any] | None:
    d = await _one(conn, "SELECT * FROM decisions WHERE id = %s", (decision_id,))
    if d is None:
        return None
    d["verdict"] = await _one(
        conn, "SELECT * FROM risk_verdicts WHERE decision_id = %s", (decision_id,)
    )
    d["trade"] = await _one(conn, "SELECT * FROM trades WHERE decision_id = %s", (decision_id,))
    d["approval"] = await _one(
        conn, "SELECT * FROM approvals WHERE decision_id = %s", (decision_id,)
    )
    d["shadow"] = await _rows(
        conn,
        "SELECT * FROM shadow_decisions WHERE decision_id = %s ORDER BY strategy",
        (decision_id,),
    )
    return d


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
