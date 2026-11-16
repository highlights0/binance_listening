from __future__ import annotations

import hmac
import re
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .binance_client import BinanceCopyTradingClient
from .config import Settings
from .database import Database
from .email_notifier import EmailNotifier
from .service import MonitorService
from mirror.config import MirrorConfig
from mirror.engine import MirrorEngine
from mirror.market_data import FuturesMarketData
from mirror.store import MirrorStore


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

# Phase 1 is simulation-only. This market-data client has public read-only
# methods; no Futures trading client or API credentials are instantiated.
mirror_config = MirrorConfig.from_env()
mirror_store = MirrorStore(settings.database_path)
mirror_market_data = FuturesMarketData(
    mirror_config.market_data_base_url,
    mirror_config.market_data_timeout_seconds,
)
mirror_engine = MirrorEngine(mirror_store, mirror_market_data, mirror_config)
service = MonitorService(
    settings, database, binance, notifier, mirror_engine=mirror_engine
)


class EmailRequest(BaseModel):
    email: str


class TokenRequest(BaseModel):
    token: str


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="邮箱格式不正确")
    return email


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
    task = asyncio.create_task(service.run())
    app.state.monitor_task = task
    yield
    service.stop()
    await task


app = FastAPI(
    title="Binance Trader Monitor",
    description="熬鹰资本 Binance Futures Copy Trading 监控与 Phase 1 虚拟镜像模拟",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return FileResponse(Path(__file__).with_name("index.html"))


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


@app.get("/api/mirror/status", dependencies=[Depends(check_status_token)])
async def api_mirror_status():
    return mirror_engine.status()


@app.get("/api/mirror/positions", dependencies=[Depends(check_status_token)])
async def api_mirror_positions():
    return {
        "source_sync_status": mirror_store.get_meta("source_sync_status", "UNSYNCED"),
        "source_positions": mirror_store.source_positions(),
        "mirror_positions": mirror_store.mirror_positions(),
    }


@app.get("/api/mirror/events", dependencies=[Depends(check_status_token)])
async def api_mirror_events(limit: int = Query(default=50, ge=1, le=200)):
    items = mirror_store.recent_events(limit)
    return {"items": items, "count": len(items)}


@app.get("/api/mirror/intents", dependencies=[Depends(check_status_token)])
async def api_mirror_intents(limit: int = Query(default=50, ge=1, le=200)):
    items = mirror_store.recent_intents(limit)
    return {"items": items, "count": len(items)}


@app.post("/api/subscribe")
async def subscribe(body: EmailRequest):
    email = clean_email(body.email)
    if not settings.email_enabled or not settings.public_base_url:
        raise HTTPException(status_code=503, detail="邮件订阅暂不可用")
    token = database.request_subscription(email)
    if token:
        try:
            await asyncio.to_thread(
                notifier.send_confirmation, email,
                f"{settings.public_base_url}/?confirm={token}",
            )
        except Exception:
            database.unsubscribe(token)
            raise HTTPException(status_code=503, detail="确认邮件发送失败")
    return {"message": "如果尚未订阅，请检查邮箱并完成确认"}


@app.post("/api/subscribe/unsubscribe-request")
async def request_unsubscribe(body: EmailRequest):
    email = clean_email(body.email)
    if not settings.email_enabled or not settings.public_base_url:
        raise HTTPException(status_code=503, detail="邮件服务暂不可用")
    token = database.request_unsubscribe(email)
    if token:
        try:
            await asyncio.to_thread(
                notifier.send_unsubscribe, email,
                f"{settings.public_base_url}/?unsubscribe={token}",
            )
        except Exception:
            database.cancel_unsubscribe_request(email)
            raise HTTPException(status_code=503, detail="退订邮件发送失败")
    return {"message": "如果该邮箱已订阅，请检查邮箱中的退订链接"}


@app.post("/api/subscribe/confirm")
async def confirm_subscription(body: TokenRequest):
    if not database.confirm_subscription(body.token):
        raise HTTPException(status_code=400, detail="确认链接无效或已使用")
    return {"message": "订阅成功，后续新订单会发送到你的邮箱"}


@app.post("/api/subscribe/unsubscribe")
async def unsubscribe(body: TokenRequest):
    if not database.unsubscribe(body.token):
        raise HTTPException(status_code=400, detail="退订链接无效")
    return {"message": "已退订"}
