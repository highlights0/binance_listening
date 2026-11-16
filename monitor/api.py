from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .binance_client import BinanceCopyTradingClient
from .config import Settings
from .database import Database
from .email_notifier import EmailNotifier
from .service import MonitorService


settings = Settings.from_env()
database = Database(
    settings.database_path,
    keep_event_days=settings.keep_event_days,
    keep_event_count=settings.keep_event_count,
)
binance = BinanceCopyTradingClient(
    base_url=settings.binance_base_url,
    referer=settings.binance_referer,
    portfolio_id=settings.portfolio_id,
    timeout_seconds=settings.binance_timeout_seconds,
)
notifier = EmailNotifier(settings)
service = MonitorService(settings, database, binance, notifier)


async def check_status_token(
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected = settings.status_api_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = __import__("asyncio").create_task(service.run())
    app.state.monitor_task = task
    yield
    service.stop()
    await task


app = FastAPI(
    title="Binance Trader Monitor",
    description="熬鹰资本 Binance Futures Copy Trading 最新订单监听服务",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "binance-trader-monitor",
        "trader": settings.trader_name,
        "portfolio_id": settings.portfolio_id,
        "docs": "/docs",
        "health": "/healthz",
    }


@app.get("/healthz")
async def healthz():
    status = service.status()
    return {
        "ok": bool(status.get("last_success_at")) and not bool(status.get("last_error")),
        "running": status.get("running", False),
        "last_success_at": status.get("last_success_at"),
        "last_error": status.get("last_error"),
    }


@app.get("/api/status", dependencies=[Depends(check_status_token)])
async def api_status():
    return service.status()


@app.get("/api/events", dependencies=[Depends(check_status_token)])
async def api_events(limit: int = Query(default=20, ge=1, le=100)):
    items = database.recent_orders(limit)
    return {"items": items, "count": len(items)}
