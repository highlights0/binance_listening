from __future__ import annotations

import time
from typing import Any

import httpx


class BinanceAPIError(RuntimeError):
    pass


class BinanceCopyTradingClient:
    """
    访问 Binance 网页公开使用的 Copy Trading BAPI。
    只读，不使用 API Key，不访问用户私人账户。
    """

    ORDER_HISTORY_PATH = "/lead-portfolio/order-history"

    def __init__(
        self,
        base_url: str,
        referer: str,
        portfolio_id: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.portfolio_id = portfolio_id
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://www.binance.com",
                "Referer": referer,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0"
                ),
                "clienttype": "web",
                "lang": "zh-CN",
                "bnc-time-zone": "Asia/Shanghai",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _time_window(self, lookback_days: int) -> tuple[int, int]:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback_days * 24 * 60 * 60 * 1000
        return start_ms, end_ms

    async def fetch_order_page(
        self,
        *,
        start_ms: int,
        end_ms: int,
        page_size: int,
        index_value: str | int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "portfolioId": self.portfolio_id,
            "startTime": start_ms,
            "endTime": end_ms,
            "pageSize": page_size,
        }
        if index_value is not None:
            body["indexValue"] = index_value

        try:
            response = await self.client.post(
                self.ORDER_HISTORY_PATH,
                json=body,
            )
        except httpx.HTTPError as exc:
            raise BinanceAPIError(
                f"请求币安订单接口失败：{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise BinanceAPIError(
                f"币安 HTTP {response.status_code}："
                f"{response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceAPIError(
                "币安响应不是 JSON：" + response.text[:300]
            ) from exc

        if payload.get("success") is False:
            raise BinanceAPIError(
                f"币安业务错误 code={payload.get('code')}："
                f"{payload.get('message') or payload.get('msg') or '未知错误'}"
            )

        code = payload.get("code")
        if code not in (None, "000000", 0, "0"):
            raise BinanceAPIError(
                f"币安业务错误 code={code}："
                f"{payload.get('message') or payload.get('msg') or '未知错误'}"
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise BinanceAPIError("币安响应缺少 data 对象")

        if not isinstance(data.get("list"), list):
            raise BinanceAPIError("币安响应缺少 data.list 数组")

        return data

    async def fetch_latest_and_unseen(
        self,
        *,
        known_fingerprints: set[str],
        lookback_days: int,
        page_size: int,
        max_catchup_pages: int,
        fingerprint_fn,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """
        只取最新页。若最新页全部是未知记录，才用网页的 indexValue
        游标继续向前补页；遇到已知记录立即停止，不扫描旧历史。
        """
        start_ms, end_ms = self._time_window(lookback_days)
        cursor: str | int | None = None
        seen_cursors: set[str] = set()
        seen_rows: set[str] = set()
        fresh: list[dict[str, Any]] = []
        pages = 0
        reported_total = 0

        for page_index in range(max_catchup_pages + 1):
            data = await self.fetch_order_page(
                start_ms=start_ms,
                end_ms=end_ms,
                page_size=page_size,
                index_value=cursor,
            )
            pages += 1
            rows = data.get("list") or []
            reported_total = int(data.get("total") or 0)

            if not rows:
                break

            found_known = False
            for row in rows:
                fp = fingerprint_fn(row)
                if fp in known_fingerprints:
                    found_known = True
                    break
                if fp not in seen_rows:
                    seen_rows.add(fp)
                    fresh.append(row)

            # 订单历史按时间倒序；遇到已知记录，后面的记录更旧。
            if found_known:
                break

            # 只有一页全是新记录时才考虑向前补页。
            if len(rows) < page_size:
                break

            if page_index >= max_catchup_pages:
                break

            next_cursor = data.get("indexValue")
            if next_cursor is None or str(next_cursor) in ("", "-1", "0"):
                break

            cursor_key = str(next_cursor)
            if cursor_key in seen_cursors:
                break

            seen_cursors.add(cursor_key)
            cursor = next_cursor

        # 邮件按旧到新排列，阅读顺序更自然。
        fresh.sort(
            key=lambda row: int(
                row.get("orderTime")
                or row.get("orderUpdateTime")
                or 0
            )
        )
        return fresh, pages, reported_total
