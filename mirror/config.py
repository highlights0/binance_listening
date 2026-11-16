from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _decimal(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
        if not value.is_finite():
            raise InvalidOperation
        return value
    except (InvalidOperation, AttributeError):
        return Decimal(default)


def _bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _symbols() -> frozenset[str]:
    return frozenset(
        item.strip().upper()
        for item in os.getenv("MIRROR_ALLOWED_SYMBOLS", "").split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class MirrorConfig:
    enabled: bool
    fixed_margin_usdt: Decimal
    leverage: Decimal
    max_order_margin_usdt: Decimal
    max_total_margin_usdt: Decimal
    max_symbol_margin_usdt: Decimal
    max_price_deviation_bps: Decimal
    max_signal_age_seconds: int
    max_daily_loss_usdt: Decimal
    allowed_symbols: frozenset[str]
    market_data_base_url: str
    market_data_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "MirrorConfig":
        return cls(
            enabled=_bool("MIRROR_ENABLED", True),
            fixed_margin_usdt=_decimal("MIRROR_FIXED_MARGIN_USDT", "10"),
            leverage=_decimal("MIRROR_LEVERAGE", "3"),
            max_order_margin_usdt=_decimal("MIRROR_MAX_ORDER_MARGIN_USDT", "10"),
            max_total_margin_usdt=_decimal("MIRROR_MAX_TOTAL_MARGIN_USDT", "50"),
            max_symbol_margin_usdt=_decimal("MIRROR_MAX_SYMBOL_MARGIN_USDT", "20"),
            max_price_deviation_bps=_decimal("MIRROR_MAX_PRICE_DEVIATION_BPS", "50"),
            max_signal_age_seconds=max(
                1, int(os.getenv("MIRROR_MAX_SIGNAL_AGE_SECONDS", "120"))
            ),
            max_daily_loss_usdt=_decimal("MIRROR_MAX_DAILY_LOSS_USDT", "10"),
            allowed_symbols=_symbols(),
            market_data_base_url=os.getenv(
                "MIRROR_MARKET_DATA_BASE_URL", "https://fapi.binance.com"
            ).rstrip("/"),
            market_data_timeout_seconds=max(
                3.0, float(os.getenv("MIRROR_MARKET_DATA_TIMEOUT_SECONDS", "10"))
            ),
        )

    def validate(self) -> None:
        positive = {
            "MIRROR_FIXED_MARGIN_USDT": self.fixed_margin_usdt,
            "MIRROR_LEVERAGE": self.leverage,
            "MIRROR_MAX_ORDER_MARGIN_USDT": self.max_order_margin_usdt,
            "MIRROR_MAX_TOTAL_MARGIN_USDT": self.max_total_margin_usdt,
            "MIRROR_MAX_SYMBOL_MARGIN_USDT": self.max_symbol_margin_usdt,
            "MIRROR_MAX_PRICE_DEVIATION_BPS": self.max_price_deviation_bps,
            "MIRROR_MAX_DAILY_LOSS_USDT": self.max_daily_loss_usdt,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError("镜像模拟参数必须大于 0：" + ", ".join(invalid))
        if self.fixed_margin_usdt > self.max_order_margin_usdt:
            raise ValueError("每笔固定保证金不能大于单笔最大保证金")
        if self.max_symbol_margin_usdt > self.max_total_margin_usdt:
            raise ValueError("单币种保证金上限不能大于总保证金上限")
