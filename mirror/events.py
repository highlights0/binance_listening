from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class SourceOrder:
    fingerprint: str
    stable_key: str
    identity_quality: str
    symbol: str
    side: str
    position_side: str
    action: str
    quantity: Decimal
    reference_price: Decimal
    order_time_ms: int
    update_time_ms: int
    raw: dict[str, Any]


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"订单字段无效：{field}={value!r}") from exc


def source_fingerprint(row: dict[str, Any]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_source_order(row: dict[str, Any]) -> SourceOrder:
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").upper()
    position_side = str(row.get("positionSide") or "").upper()
    order_type = str(row.get("type") or "").upper()
    if not symbol or side not in {"BUY", "SELL"}:
        raise ValueError("缺少 symbol 或 side 不受支持")
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError(
            f"positionSide={position_side or '缺失'} 暂不支持；Phase 1 只接受 LONG/SHORT"
        )

    if (position_side, side) in {("LONG", "BUY"), ("SHORT", "SELL")}:
        action = "INCREASE"
    else:
        action = "REDUCE"

    order_time = int(row.get("orderTime") or 0)
    update_time = int(row.get("orderUpdateTime") or order_time or 0)
    identity_quality = "SYNTHETIC"
    if not order_time:
        # orderUpdateTime can change as the page refreshes, so it is not a
        # sufficiently stable substitute for an absent orderTime.
        identity_quality = "MISSING_TIME"

    key_source = "|".join(
        (symbol, side, position_side, order_type, str(order_time))
    )
    stable_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()

    quantity = _decimal(row.get("executedQty"), "executedQty")
    reference_price = _decimal(row.get("avgPrice"), "avgPrice")
    if quantity <= 0 or reference_price <= 0:
        raise ValueError("executedQty 和 avgPrice 必须大于 0")

    return SourceOrder(
        fingerprint=source_fingerprint(row),
        stable_key=stable_key,
        identity_quality=identity_quality,
        symbol=symbol,
        side=side,
        position_side=position_side,
        action=action,
        quantity=quantity,
        reference_price=reference_price,
        order_time_ms=order_time,
        update_time_ms=update_time,
        raw=row,
    )
