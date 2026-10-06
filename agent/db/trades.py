"""Data access for decisions, verdicts, approvals, trades and the risk statistics derived
from them. The DB is the source of truth the executor trusts, not its caller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.decision.base import Action

DECISION_COLUMNS = frozenset(
    {
        "symbol", "provider", "model", "prompt_hash", "playbook_rev", "snapshot", "action",
        "probabilities", "confidence", "latency_ms", "input_tokens", "output_tokens", "cost_usd",
        "skipped_reason", "error", "provider_is_primary",
    }
)  # fmt: skip

# How long an `unknown` buy is assumed to possibly still be open. Contracts last ~5 ticks,
# so after this it can't be open any more, but its result stays a worst-case loss.
UNKNOWN_OPEN_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class RiskStats:
    open_positions: int
    open_exposure_usd: Decimal
    daily_pnl_usd: Decimal
    consecutive_losses: int
    last_loss_at: datetime | None


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: int
    created_at: datetime
    action: Action
    confidence: float
    provider_is_primary: bool


@dataclass(frozen=True)
class ApprovalRecord:
    status: str
    reasons: tuple[str, ...]
    expires_at: datetime | None
    responded_at: datetime | None


class TradeRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ decisions

    async def insert_decision(self, row: dict[str, Any]) -> int:
        unknown = set(row) - DECISION_COLUMNS
        if unknown:
            raise ValueError(f"unknown decision columns: {sorted(unknown)}")
        cols = list(row)
        values = [Jsonb(v) if isinstance(v, dict | list) else v for v in row.values()]
        sql = (
            f"INSERT INTO decisions ({', '.join(cols)}) "  # noqa: S608 - whitelisted columns
            f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
        )
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, values)
            row_out = await cur.fetchone()
        assert row_out is not None
        return int(row_out[0])

    async def get_decision(self, decision_id: int) -> DecisionRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT created_at, action, confidence, provider_is_primary "
                "FROM decisions WHERE id = %s",
                (decision_id,),
            )
            row = await cur.fetchone()
        if row is None or row[1] is None or row[2] is None:
            return None
        try:
            action = Action(row[1])
        except ValueError:  # corrupted row: never tradeable
            return None
        return DecisionRecord(decision_id, row[0], action, float(row[2]), row[3] is True)

    async def insert_verdict(
        self, decision_id: int, verdict: str, reasons: list[str], rule_results: list[dict[str, Any]]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO risk_verdicts (decision_id, verdict, reasons, rule_results) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (decision_id) DO UPDATE SET "
                "verdict = EXCLUDED.verdict, reasons = EXCLUDED.reasons, "
                "rule_results = EXCLUDED.rule_results, checked_at = now()",
                (decision_id, verdict, reasons, Jsonb(rule_results)),
            )

    # ------------------------------------------------------------------ approvals

    async def create_approval(
        self, decision_id: int, reasons: list[str], expires_at: datetime
    ) -> int | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO approvals (decision_id, trigger, reasons, expires_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (decision_id) DO NOTHING RETURNING id",
                (decision_id, "; ".join(reasons), reasons, expires_at),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def respond_approval(self, decision_id: int, status: str, at: datetime) -> bool:
        """Set approved/rejected/expired once. False if it was no longer pending."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE approvals SET status = %s, responded_at = %s "
                "WHERE decision_id = %s AND status = 'pending' RETURNING id",
                (status, at, decision_id),
            )
            return await cur.fetchone() is not None

    async def get_approval(self, decision_id: int) -> ApprovalRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, reasons, expires_at, responded_at FROM approvals "
                "WHERE decision_id = %s",
                (decision_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ApprovalRecord(row[0], tuple(row[1] or ()), row[2], row[3])

    # ------------------------------------------------------------------ trades

    async def create_pending(
        self, decision_id: int, contract_type: str, symbol: str, stake: Decimal, duration: int
    ) -> int | None:
        """Claim the right to buy for this decision. None means it was already claimed."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO trades (decision_id, contract_type, symbol, stake, duration_ticks) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (decision_id) DO NOTHING RETURNING id",
                (decision_id, contract_type, symbol, stake, duration),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def mark_open(
        self,
        trade_id: int,
        contract_id: str,
        buy_price: Decimal,
        payout: Decimal | None,
        bought_at: datetime,
        raw: dict[str, Any],
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE trades SET status = 'open', contract_id = %s, buy_price = %s, "
                "payout = %s, bought_at = %s, raw = %s, error = NULL WHERE id = %s",
                (contract_id, buy_price, payout, bought_at, Jsonb(raw), trade_id),
            )

    async def mark_error(self, trade_id: int, error: str) -> None:
        """Definitely not bought (Deriv rejected it, or we never sent it)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE trades SET status = 'error', error = %s, settled_at = now() WHERE id = %s",
                (error[:500], trade_id),
            )

    async def mark_unknown(self, trade_id: int, error: str) -> None:
        """Maybe bought: counted as open (for a while) and as a full-stake loss until resolved."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE trades SET status = 'unknown', error = %s WHERE id = %s",
                (error[:500], trade_id),
            )

    async def settle(
        self, trade_id: int, status: str, profit: Decimal, settled_at: datetime, raw: dict[str, Any]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE trades SET status = %s, profit = %s, settled_at = %s, raw = %s "
                "WHERE id = %s AND status IN ('pending', 'open', 'unknown')",
                (status, profit, settled_at, Jsonb(raw), trade_id),
            )

    async def open_trades(self) -> list[tuple[int, str | None, str]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, contract_id, status FROM trades WHERE status IN ('pending', 'open')"
            )
            return [(int(r[0]), r[1], str(r[2])) for r in await cur.fetchall()]

    async def spend_today(self, now: datetime) -> dict[str, Decimal]:
        """Model spend per provider since 00:00 UTC, so spend caps survive restarts."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT provider, coalesce(sum(cost_usd), 0) FROM decisions "
                "WHERE created_at >= %s AND provider IS NOT NULL GROUP BY provider",
                (day_start,),
            )
            return {str(p): Decimal(c) for p, c in await cur.fetchall()}

    # ------------------------------------------------------------------ risk stats

    async def risk_stats(self, now: datetime) -> RiskStats:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*), coalesce(sum(stake), 0) FROM trades "
                "WHERE status IN ('pending', 'open') "
                "   OR (status = 'unknown' AND created_at >= %s)",
                (now - UNKNOWN_OPEN_WINDOW,),
            )
            open_row = await cur.fetchone()
            cur = await conn.execute(
                "SELECT coalesce(sum(CASE WHEN status = 'unknown' THEN -stake ELSE profit END), 0) "
                "FROM trades WHERE (status = 'unknown' AND created_at >= %s) "
                "   OR (settled_at >= %s AND profit IS NOT NULL)",
                (day_start, day_start),
            )
            pnl_row = await cur.fetchone()
            cur = await conn.execute(
                "SELECT status, settled_at FROM trades WHERE status IN ('won', 'lost', 'sold') "
                "AND settled_at >= %s ORDER BY settled_at DESC LIMIT 50",
                (now - timedelta(days=1),),
            )
            recent = await cur.fetchall()
        assert open_row is not None and pnl_row is not None
        streak, last_loss = 0, None
        for status, settled_at in recent:
            if status != "lost":
                break
            streak += 1
            last_loss = last_loss or settled_at
        return RiskStats(
            open_positions=int(open_row[0]),
            open_exposure_usd=Decimal(open_row[1]),
            daily_pnl_usd=Decimal(pnl_row[0]),
            consecutive_losses=streak,
            last_loss_at=last_loss,
        )
