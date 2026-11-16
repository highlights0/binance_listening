from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from mirror.config import MirrorConfig
from mirror.engine import MirrorEngine
from mirror.market_data import MarketSnapshot, SymbolRules
from mirror.planner import build_open_plan
from mirror.store import MirrorStore
from monitor.database import Database


class FakeMarketData:
    def __init__(self, mark_price: str = "100") -> None:
        self.snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            mark_price=Decimal(mark_price),
            rules=SymbolRules(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                max_qty=Decimal("100000"),
                min_notional=Decimal("5"),
            ),
        )

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            mark_price=self.snapshot.mark_price,
            rules=self.snapshot.rules,
        )

    async def close(self) -> None:
        return None


def make_config(**overrides) -> MirrorConfig:
    values = {
        "enabled": True,
        "fixed_margin_usdt": Decimal("10"),
        "leverage": Decimal("3"),
        "max_order_margin_usdt": Decimal("10"),
        "max_total_margin_usdt": Decimal("50"),
        "max_symbol_margin_usdt": Decimal("20"),
        "max_price_deviation_bps": Decimal("50"),
        "max_signal_age_seconds": 300,
        "max_daily_loss_usdt": Decimal("10"),
        "allowed_symbols": frozenset(),
        "market_data_base_url": "https://example.invalid",
        "market_data_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return MirrorConfig(**values)


def order(
    *,
    side: str = "BUY",
    position_side: str = "LONG",
    quantity: str = "1",
    price: str = "100",
    order_time: int | None = None,
    symbol: str = "BTCUSDT",
    order_type: str = "MARKET",
) -> dict:
    now_ms = int(time.time() * 1000)
    timestamp = order_time if order_time is not None else now_ms
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "side": side,
        "type": order_type,
        "positionSide": position_side,
        "executedQty": quantity,
        "avgPrice": price,
        "totalPnl": "0",
        "orderUpdateTime": timestamp,
        "orderTime": timestamp,
    }


class MirrorEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "monitor.sqlite3")
        # Creates the existing monitor orders table used by the mirror queue.
        self.monitor_db = Database(self.db_path)
        self.store = MirrorStore(self.db_path)
        self.market = FakeMarketData()
        self.engine = MirrorEngine(
            self.store, self.market, make_config()
        )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self.temp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_default_source_state_is_unsynced_and_blocks_open(self):
        result = self.run_async(
            self.engine.process_source_snapshot(order())
        )
        self.assertEqual(result["status"], "BLOCKED_SOURCE_UNSYNCED")
        self.assertEqual(self.store.mirror_positions(), [])
        self.assertEqual(
            self.store.get_meta("source_sync_status"), "UNSYNCED"
        )

    def test_fixed_margin_open_and_proportional_reduce(self):
        self.store.assume_source_flat("测试：显式初始化为空仓")
        opened = self.run_async(
            self.engine.process_source_snapshot(order(quantity="1"))
        )
        self.assertEqual(opened["status"], "SIMULATED")
        mirror = self.store.mirror_positions()[0]
        self.assertEqual(Decimal(mirror["quantity"]), Decimal("0.3"))
        self.assertEqual(Decimal(mirror["margin_used"]), Decimal("10"))

        closed = self.run_async(
            self.engine.process_source_snapshot(
                order(
                    side="SELL",
                    position_side="LONG",
                    quantity="0.5",
                    price="110",
                    order_time=int(time.time() * 1000) + 1,
                )
            )
        )
        self.assertEqual(closed["status"], "SIMULATED")
        mirror = self.store.mirror_positions()[0]
        self.assertEqual(Decimal(mirror["quantity"]), Decimal("0.15"))
        self.assertEqual(Decimal(mirror["margin_used"]), Decimal("5"))
        intent = self.store.recent_intents(1)[0]
        self.assertEqual(Decimal(intent["realized_pnl"]), Decimal("1.5"))

    def test_exact_duplicate_is_idempotent(self):
        self.store.assume_source_flat("测试：显式初始化为空仓")
        row = order()
        first = self.run_async(self.engine.process_source_snapshot(row))
        second = self.run_async(self.engine.process_source_snapshot(row))
        self.assertEqual(first["status"], "SIMULATED")
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(len(self.store.recent_events(10)), 1)
        self.assertEqual(len(self.store.recent_intents(10)), 1)

    def test_changed_snapshot_with_same_synthetic_key_fails_closed(self):
        self.store.assume_source_flat("测试：显式初始化为空仓")
        timestamp = int(time.time() * 1000)
        first = order(quantity="1", order_time=timestamp)
        second = order(quantity="1.5", order_time=timestamp)
        self.run_async(self.engine.process_source_snapshot(first))
        result = self.run_async(self.engine.process_source_snapshot(second))
        self.assertEqual(result["status"], "BLOCKED_IDENTITY_AMBIGUOUS")
        self.assertEqual(
            self.store.get_meta("source_sync_status"), "UNSYNCED"
        )
        self.assertEqual(
            self.store.get_meta("mirror_sync_status"), "UNSYNCED"
        )

    def test_stale_open_updates_source_but_blocks_mirror(self):
        self.engine.config = make_config(max_signal_age_seconds=1)
        self.store.assume_source_flat("测试：显式初始化为空仓")
        old_time = int(time.time() * 1000) - 10_000
        result = self.run_async(
            self.engine.process_source_snapshot(order(order_time=old_time))
        )
        self.assertEqual(result["status"], "BLOCKED_STALE_SIGNAL")
        self.assertEqual(self.store.mirror_positions(), [])
        source = self.store.source_positions()[0]
        self.assertEqual(Decimal(source["quantity"]), Decimal("1"))

    def test_price_deviation_blocks_open(self):
        self.store.assume_source_flat("测试：显式初始化为空仓")
        self.engine.market_data = FakeMarketData(mark_price="101")
        result = self.run_async(
            self.engine.process_source_snapshot(order(price="100"))
        )
        self.assertEqual(result["status"], "BLOCKED_PRICE_DEVIATION")
        self.assertEqual(self.store.mirror_positions(), [])

    def test_both_position_side_is_unsupported_and_unsyncs(self):
        result = self.run_async(
            self.engine.process_source_snapshot(
                order(position_side="BOTH")
            )
        )
        self.assertEqual(result["status"], "BLOCKED_UNSUPPORTED_SOURCE_EVENT")
        self.assertEqual(
            self.store.get_meta("source_sync_status"), "UNSYNCED"
        )

    def test_missing_order_time_fails_closed(self):
        row = order()
        row["orderTime"] = 0
        row["orderUpdateTime"] = int(time.time() * 1000)
        result = self.run_async(self.engine.process_source_snapshot(row))
        self.assertEqual(result["status"], "BLOCKED_MISSING_ORDER_TIME")
        self.assertEqual(
            self.store.get_meta("source_sync_status"), "UNSYNCED"
        )

    def test_store_keeps_sync_state_across_reinitialization(self):
        self.store.assume_source_flat("测试：持久化状态")
        reopened = MirrorStore(self.db_path)
        self.assertEqual(reopened.get_meta("source_sync_status"), "SYNCED")
        self.assertEqual(reopened.get_meta("source_sync_note"), "测试：持久化状态")

    def test_market_lot_step_rounding(self):
        plan = build_open_plan(
            margin_usdt=Decimal("10"),
            leverage=Decimal("3"),
            mark_price=Decimal("101"),
            reference_price=Decimal("101"),
            rules=SymbolRules(
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                max_qty=Decimal("1000"),
                min_notional=Decimal("5"),
            ),
        )
        self.assertEqual(plan.quantity, Decimal("0.29"))
        self.assertLessEqual(
            plan.quantity * plan.mark_price,
            Decimal("30"),
        )


if __name__ == "__main__":
    unittest.main()
