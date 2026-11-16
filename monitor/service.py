from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .binance_client import BinanceAPIError, BinanceCopyTradingClient
from .config import Settings
from .database import Database
from .email_notifier import EmailNotifier
from .events import order_fingerprint


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        binance: BinanceCopyTradingClient,
        notifier: EmailNotifier,
    ) -> None:
        self.settings = settings
        self.database = database
        self.binance = binance
        self.notifier = notifier
        self.log = logging.getLogger("monitor.service")
        self._stop = asyncio.Event()
        self._runtime: dict[str, Any] = {
            "running": False,
            "last_poll_duration_ms": None,
            "last_fetch_pages": 0,
            "last_reported_total": None,
            "last_new_order_count": 0,
        }

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "trader_name": self.settings.trader_name,
            "portfolio_id": self.settings.portfolio_id,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "page_size": self.settings.page_size,
            "lookback_days": self.settings.lookback_days,
            "email_enabled": self.settings.email_enabled,
            **self._runtime,
            **self.database.status_snapshot(),
        }

    async def run(self) -> None:
        self._runtime["running"] = True
        self.log.info(
            "开始监听：%s (%s)，轮询间隔 %.1f 秒，最新页 %s 条",
            self.settings.trader_name,
            self.settings.portfolio_id,
            self.settings.poll_interval_seconds,
            self.settings.page_size,
        )
        if not self.settings.email_enabled:
            self.log.warning(
                "SMTP 尚未配置：新订单会保存到数据库，但不会发送邮件。"
            )

        failure_count = 0
        try:
            while not self._stop.is_set():
                started = asyncio.get_running_loop().time()
                try:
                    await self._poll_once()
                    failure_count = 0
                except Exception as exc:
                    failure_count += 1
                    message = f"{type(exc).__name__}: {exc}"
                    self.database.set_state("last_error", message)
                    self.log.exception("本轮监听失败：%s", message)

                duration_ms = int(
                    (asyncio.get_running_loop().time() - started) * 1000
                )
                self._runtime["last_poll_duration_ms"] = duration_ms

                if failure_count:
                    sleep_seconds = min(
                        self.settings.poll_interval_seconds * (2 ** min(failure_count - 1, 5)),
                        120.0,
                    )
                else:
                    sleep_seconds = self.settings.poll_interval_seconds

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=sleep_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._runtime["running"] = False
            await self.binance.close()
            self.log.info("监听服务已停止")

    async def _poll_once(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.database.set_state("last_poll_at", now)

        if not self.database.is_bootstrapped():
            data = await self.binance.fetch_order_page(
                start_ms=int(
                    (datetime.now(timezone.utc).timestamp()
                     - self.settings.lookback_days * 86400) * 1000
                ),
                end_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                page_size=self.settings.page_size,
            )
            rows = data.get("list") or []
            inserted = self.database.bootstrap(rows)
            self.database.set_state("last_success_at", now)
            self.database.set_state("last_error", "")
            self._runtime["last_fetch_pages"] = 1
            self._runtime["last_reported_total"] = int(
                data.get("total") or 0
            )
            self._runtime["last_new_order_count"] = 0
            self.log.info(
                "首次启动完成基线：读取最新 %s 条，写入 %s 条；不发送历史邮件",
                len(rows),
                inserted,
            )
            return

        known = self.database.get_known_fingerprints()
        fresh, pages, reported_total = (
            await self.binance.fetch_latest_and_unseen(
                known_fingerprints=known,
                lookback_days=self.settings.lookback_days,
                page_size=self.settings.page_size,
                max_catchup_pages=self.settings.max_catchup_pages,
                fingerprint_fn=order_fingerprint,
            )
        )

        inserted_fingerprints = self.database.insert_new_orders(fresh)
        self._runtime["last_fetch_pages"] = pages
        self._runtime["last_reported_total"] = reported_total
        self._runtime["last_new_order_count"] = len(inserted_fingerprints)

        self.database.set_state("last_success_at", now)
        self.database.set_state("last_error", "")

        if inserted_fingerprints:
            self.log.info(
                "检测到 %s 条新订单，已写入 SQLite",
                len(inserted_fingerprints),
            )

        await self._send_pending_email()

    async def _send_pending_email(self) -> None:
        if not self.settings.email_enabled:
            return

        pending = self.database.get_pending_notifications()
        if pending:
            fingerprints = [row["fingerprint"] for row in pending]
            try:
                await asyncio.to_thread(self.notifier.send_orders, pending)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.database.mark_notification_attempt(fingerprints, error)
                self.database.set_state("last_email_error", error)
                self.log.exception("邮件发送失败，订单保留待重试：%s", error)
            else:
                self.database.mark_notification_sent(fingerprints)
                self.database.set_state("last_email_at", datetime.now(timezone.utc).isoformat())
                self.database.set_state("last_email_error", "")
                self.log.info("邮件发送成功：%s 条新订单", len(pending))

        for email, rows in self.database.pending_subscriber_notifications().items():
            token = self.database.subscriber_token(email)
            if not token:
                continue
            fingerprints = [row["fingerprint"] for row in rows]
            try:
                await asyncio.to_thread(
                    self.notifier.send_orders, rows, email,
                    f"{self.settings.public_base_url}/?unsubscribe={token}",
                )
            except Exception as exc:
                self.database.mark_subscriber_delivery(email, fingerprints, False)
                self.log.exception("订阅者邮件发送失败：%s", exc)
            else:
                self.database.mark_subscriber_delivery(email, fingerprints, True)
                self.log.info("订阅者邮件发送成功：%s 条新订单", len(rows))
