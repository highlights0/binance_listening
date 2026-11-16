from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .market_data import SymbolRules


@dataclass(frozen=True)
class OpenPlan:
    quantity: Decimal
    margin_usdt: Decimal
    notional_usdt: Decimal
    leverage: Decimal
    mark_price: Decimal
    price_deviation_bps: Decimal


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0 or step <= 0:
        return Decimal("0")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def build_open_plan(
    *,
    margin_usdt: Decimal,
    leverage: Decimal,
    mark_price: Decimal,
    reference_price: Decimal,
    rules: SymbolRules,
) -> OpenPlan:
    if margin_usdt <= 0 or leverage <= 0 or mark_price <= 0 or reference_price <= 0:
        raise ValueError("保证金、杠杆和价格必须大于 0")

    notional = margin_usdt * leverage
    raw_quantity = notional / mark_price
    quantity = floor_to_step(raw_quantity, rules.step_size)

    if quantity < rules.min_qty:
        raise ValueError(
            f"下单数量 {quantity} 小于交易对最小数量 {rules.min_qty}"
        )
    if quantity > rules.max_qty:
        raise ValueError(
            f"下单数量 {quantity} 超过交易对最大数量 {rules.max_qty}"
        )
    if quantity * mark_price < rules.min_notional:
        raise ValueError(
            f"名义价值 {quantity * mark_price} 小于最小名义价值 {rules.min_notional}"
        )

    deviation = abs(mark_price - reference_price) / reference_price * Decimal("10000")
    return OpenPlan(
        quantity=quantity,
        margin_usdt=margin_usdt,
        notional_usdt=quantity * mark_price,
        leverage=leverage,
        mark_price=mark_price,
        price_deviation_bps=deviation,
    )
