from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolRules:
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    mark_price: Decimal
    rules: SymbolRules


class FuturesMarketData:
    """只读 USDⓈ-M 公共行情；不需要 API Key。"""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "binance-listening/phase1"},
        )
        self._exchange_info: dict[str, dict[str, Any]] | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self.client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"行情接口请求失败 {path}: {exc}") from exc

    async def _load_exchange_info(self) -> dict[str, dict[str, Any]]:
        if self._exchange_info is not None:
            return self._exchange_info
        payload = await self._get_json("/fapi/v1/exchangeInfo")
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            raise MarketDataError("exchangeInfo 缺少 symbols")
        self._exchange_info = {
            str(item.get("symbol", "")).upper(): item
            for item in symbols
            if item.get("symbol")
        }
        return self._exchange_info

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
            if not result.is_finite():
                raise InvalidOperation
            return result
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MarketDataError(f"交易规则字段无效：{field}={value!r}") from exc

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        symbol = symbol.upper()
        mark_payload = await self._get_json(
            "/fapi/v1/premiumIndex", {"symbol": symbol}
        )
        mark_price = self._decimal(mark_payload.get("markPrice"), "markPrice")
        if mark_price <= 0:
            raise MarketDataError(f"{symbol} 标记价格无效")

        exchange_info = await self._load_exchange_info()
        symbol_info = exchange_info.get(symbol)
        if not symbol_info:
            raise MarketDataError(f"exchangeInfo 不包含交易对 {symbol}")

        filters = {
            item.get("filterType"): item
            for item in symbol_info.get("filters", [])
        }
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        if not lot:
            raise MarketDataError(f"{symbol} 缺少 MARKET_LOT_SIZE/LOT_SIZE")

        step_size = self._decimal(lot.get("stepSize"), "stepSize")
        min_qty = self._decimal(lot.get("minQty"), "minQty")
        max_qty = self._decimal(lot.get("maxQty"), "maxQty")
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        min_notional = self._decimal(
            notional_filter.get("notional", notional_filter.get("minNotional", "0")),
            "minNotional",
        )

        if step_size <= 0 or min_qty < 0 or max_qty <= 0:
            raise MarketDataError(f"{symbol} 数量过滤器配置无效")

        return MarketSnapshot(
            symbol=symbol,
            mark_price=mark_price,
            rules=SymbolRules(
                step_size=step_size,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=min_notional,
            ),
        )
