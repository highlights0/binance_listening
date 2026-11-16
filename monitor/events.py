from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def order_fingerprint(row: dict[str, Any]) -> str:
    """
    HAR 中的公开订单对象没有独立 orderId/tradeId。
    因此以完整订单对象的规范化 JSON 计算指纹。
    """
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_direction(row: dict[str, Any]) -> str:
    side = str(row.get("side", "")).upper()
    position_side = str(row.get("positionSide", "")).upper()

    mapping = {
        ("BUY", "LONG"): "开多 / 加多方向",
        ("SELL", "LONG"): "减多 / 平多方向",
        ("SELL", "SHORT"): "开空 / 加空方向",
        ("BUY", "SHORT"): "减空 / 平空方向",
    }
    return mapping.get(
        (side, position_side),
        f"方向待识别（{side}/{position_side}）",
    )


def format_timestamp(
    value: Any,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    try:
        dt = datetime.fromtimestamp(
            float(value) / 1000,
            tz=timezone.utc,
        )
        return dt.astimezone(ZoneInfo(timezone_name)).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except (TypeError, ValueError, OSError, KeyError):
        return "未知"


def normalize_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "position_side": row.get("positionSide"),
        "direction": classify_direction(row),
        "order_type": row.get("type"),
        "executed_qty": row.get("executedQty"),
        "avg_price": row.get("avgPrice"),
        "total_pnl": row.get("totalPnl"),
        "order_time": row.get("orderTime"),
        "order_update_time": row.get("orderUpdateTime"),
        "fingerprint": order_fingerprint(row),
        "raw": row,
    }
