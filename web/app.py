"""Sentinel dashboard: a read-only window onto the agent's decision log.

Run locally:  uvicorn web.app:app --port 8080     (or the `web` service in docker compose)
No write routes exist; the dashboard can't change anything the agent does.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool

from web import queries

HERE = Path(__file__).parent
MYT = ZoneInfo("Asia/Kuala_Lumpur")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://agent:agent@127.0.0.1:5432/agent")

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = AsyncConnectionPool(
        DATABASE_URL, min_size=1, max_size=4, open=False, kwargs={"connect_timeout": 5}
    )
    await pool.open(wait=False)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Sentinel", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ------------------------------------------------------------------ template helpers


def myt(value: datetime | None, fmt: str = "%H:%M:%S") -> str:
    return value.astimezone(MYT).strftime(fmt) if value else "—"


def money(value: Any, signed: bool = False) -> str:
    if value is None:
        return "—"
    d = Decimal(str(value))
    sign = "+" if signed and d > 0 else ("−" if d < 0 else "")
    return f"{sign}${abs(d):,.2f}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 1:
        return "just now"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    return f"{seconds / 60:.0f} min ago"


templates.env.filters.update(myt=myt, money=money, pct=pct, ago=ago)


async def _with_conn(request: Request, fn: Any, *args: Any) -> Any:
    try:
        async with request.app.state.pool.connection(timeout=5) as conn:
            return await fn(conn, *args)
    except Exception as exc:  # noqa: BLE001 - show a friendly state instead of a 500
        raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from exc


# ------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/decisions/{decision_id}", response_class=HTMLResponse)
async def decision_page(request: Request, decision_id: int) -> HTMLResponse:
    d = await _with_conn(request, queries.decision, decision_id)
    if d is None:
        raise HTTPException(404, "no such decision")
    return templates.TemplateResponse(request, "decision.html", {"d": d})


# ------------------------------------------------------------------ live fragments


@app.get("/fragments/status", response_class=HTMLResponse)
async def frag_status(request: Request) -> HTMLResponse:
    s = await _with_conn(request, queries.status)
    return templates.TemplateResponse(request, "fragments/status.html", {"s": s})


@app.get("/fragments/kpis", response_class=HTMLResponse)
async def frag_kpis(request: Request) -> HTMLResponse:
    k = await _with_conn(request, queries.kpis)
    return templates.TemplateResponse(request, "fragments/kpis.html", {"k": k})


@app.get("/fragments/feed", response_class=HTMLResponse)
async def frag_feed(request: Request) -> HTMLResponse:
    rows = await _with_conn(request, queries.feed)
    return templates.TemplateResponse(request, "fragments/feed.html", {"rows": rows})


@app.get("/fragments/shadow", response_class=HTMLResponse)
async def frag_shadow(request: Request) -> HTMLResponse:
    rows = await _with_conn(request, queries.shadow)
    return templates.TemplateResponse(request, "fragments/shadow.html", {"rows": rows})


@app.get("/api/chart")
async def api_chart(request: Request) -> JSONResponse:
    return JSONResponse(await _with_conn(request, queries.chart))


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    s = await _with_conn(request, queries.status)
    return JSONResponse(
        {
            "status": s["light"],
            "heartbeat_age_s": s["heartbeat_age_s"],
            "last_tick_age_s": s["last_tick_age_s"],
        }
    )
