from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from agent.db.migrate import migrate
from agent.db.repo import StateStore
from agent.db.trades import TradeRepo
from web.app import app

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(pool):
    async with pool.connection() as conn:
        await migrate(conn)
        now = datetime.now(UTC)
        for i in range(30):
            await conn.execute(
                "INSERT INTO ticks (symbol, epoch, quote, received_at) VALUES (%s, %s, %s, %s)",
                ("1HZ100V", 1_000 + i, 1000 + i * 0.1, now),
            )
    state = StateStore(pool)
    await state.set("heartbeat", {"connected": True, "reconnects": 0})
    await state.set("config", {"mode": "paper", "providers": ["rules"], "stake_usd": "1.00"})
    await state.set("started_at", datetime.now(UTC).isoformat())
    repo = TradeRepo(pool)
    decision_id = await repo.insert_decision(
        {
            "symbol": "1HZ100V",
            "action": "CALL",
            "confidence": 0.7,
            "provider": "rules",
            "probabilities": {"CALL": 0.7, "PUT": 0.2, "HOLD": 0.1},
            "snapshot": {"last_ticks": [1.0, 1.1], "indicators": {"zscore_60": 1.5}},
            "provider_is_primary": True,
        }  # fmt: skip
    )
    await repo.insert_verdict(
        decision_id,
        "APPROVE",
        [],
        [{"rule": "kill_switch", "ok": True, "reason": "", "needs_approval": False}],  # fmt: skip
    )
    trade_id = await repo.create_pending(decision_id, "CALL", "1HZ100V", Decimal("1"), 5)
    await repo.mark_open(trade_id, "paper-1", Decimal("1"), Decimal("1.95"), now, {})
    await repo.settle(trade_id, "won", Decimal("0.95"), now, {"entry": {"quote": 1},
                                                              "exit": {"quote": 2}})  # fmt: skip
    skipped = await repo.insert_decision({"symbol": "1HZ100V", "skipped_reason": "stale: x"})

    app.state.pool = pool  # bypass lifespan: reuse the test pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.decision_id, c.skipped_id = decision_id, skipped  # type: ignore[attr-defined]
        yield c


ROUTES = [
    "/",
    "/fragments/status",
    "/fragments/kpis",
    "/fragments/feed",
    "/fragments/shadow",
    "/api/chart",
    "/health",
]


@pytest.mark.parametrize("path", ROUTES)
async def test_pages_render(client, path):
    r = await client.get(path)
    assert r.status_code == 200, r.text[:300]


async def test_status_shows_live_and_stand_in_label(client):
    html = (await client.get("/fragments/status")).text
    assert "LIVE" in html and "PAPER MODE" in html
    assert "rule-based stand-in" in html


async def test_kpis_and_feed_reflect_the_log(client):
    kpis = (await client.get("/fragments/kpis")).text
    assert "+$0.95" in kpis
    feed = (await client.get("/fragments/feed")).text
    assert "approved" in feed and "skipped: stale" in feed


async def test_decision_page_explains_the_decision(client):
    r = await client.get(f"/decisions/{client.decision_id}")
    assert r.status_code == 200
    assert "kill switch" in r.text and "APPROVE" in r.text and "+$0.95" in r.text
    assert (await client.get(f"/decisions/{client.skipped_id}")).status_code == 200


async def test_unknown_decision_is_404(client):
    assert (await client.get("/decisions/999999")).status_code == 404


async def test_chart_payload_shape(client):
    data = (await client.get("/api/chart")).json()
    assert set(data) == {"ticks", "trades", "blocked"} and len(data["ticks"]) == 30


async def test_no_secrets_in_any_response(client):
    paths = [*ROUTES, f"/decisions/{client.decision_id}"]
    for path in paths:
        body = (await client.get(path)).text
        assert "agent:agent@" not in body and "otp=" not in body and "postgresql://" not in body


async def test_dashboard_has_no_write_routes():
    methods = {m for r in app.routes for m in (getattr(r, "methods", None) or set())}
    assert methods <= {"GET", "HEAD"}
