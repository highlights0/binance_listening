from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import MirrorConfig
from .events import SourceOrder, parse_source_order
from .market_data import FuturesMarketData, MarketDataError, MarketSnapshot
from .planner import build_open_plan, floor_to_step
from .store import MirrorStore


ZERO = Decimal("0")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _utc_day_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class MirrorEngine:
    """
    Phase 1 virtual mirror engine.

    This class has no exchange trading client and cannot submit real orders.
    It only maintains source/mirror ledgers and writes simulation intents.
    """

    def __init__(
        self,
        store: MirrorStore,
        market_data: FuturesMarketData,
        config: MirrorConfig,
    ) -> None:
        config.validate()
        self.store = store
        self.market_data = market_data
        self.config = config
        self.log = logging.getLogger("mirror.engine")

    async def close(self) -> None:
        await self.market_data.close()

    def status(self) -> dict[str, Any]:
        return {
            **self.store.status(),
            "enabled": self.config.enabled,
            "fixed_margin_usdt": str(self.config.fixed_margin_usdt),
            "leverage": str(self.config.leverage),
            "max_order_margin_usdt": str(self.config.max_order_margin_usdt),
            "max_total_margin_usdt": str(self.config.max_total_margin_usdt),
            "max_symbol_margin_usdt": str(self.config.max_symbol_margin_usdt),
            "max_price_deviation_bps": str(self.config.max_price_deviation_bps),
            "max_signal_age_seconds": self.config.max_signal_age_seconds,
            "max_daily_loss_usdt": str(self.config.max_daily_loss_usdt),
            "allowed_symbols": sorted(self.config.allowed_symbols),
        }

    async def process_pending_source_orders(self) -> int:
        if not self.config.enabled:
            return 0
        pending = self.store.pending_source_orders()
        processed = 0
        for record in pending:
            try:
                raw = json.loads(record["raw_json"])
                await self.process_source_snapshot(raw, record["fingerprint"])
                processed += 1
            except Exception:
                self.log.exception(
                    "镜像模拟处理源订单失败 fingerprint=%s",
                    record.get("fingerprint"),
                )
        return processed

    async def process_source_snapshot(
        self,
        raw: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = fingerprint or self._fingerprint(raw)
        try:
            source = parse_source_order(raw)
        except Exception as exc:
            return self._record_unsupported(raw, fingerprint, str(exc))

        with self.store.connect() as conn:
            already = conn.execute(
                "SELECT 1 FROM mirror_processed_fingerprints WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if already:
                return {"status": "DUPLICATE", "fingerprint": fingerprint}
            existing = conn.execute(
                "SELECT * FROM mirror_source_orders WHERE stable_key=?",
                (source.stable_key,),
            ).fetchone()

        if existing:
            return self._record_ambiguous_update(source)

        snapshot: MarketSnapshot | None = None
        market_error: str | None = None
        try:
            snapshot = await self.market_data.get_snapshot(source.symbol)
        except MarketDataError as exc:
            market_error = str(exc)
            self.log.warning("无法获取 %s 行情规则：%s", source.symbol, exc)

        return self._apply_new_source_order(source, snapshot, market_error)

    @staticmethod
    def _fingerprint(raw: dict[str, Any]) -> str:
        canonical = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _record_unsupported(
        self,
        raw: dict[str, Any],
        fingerprint: str,
        reason: str,
    ) -> dict[str, Any]:
        symbol = str(raw.get("symbol") or "")
        stable_key = hashlib.sha256(
            (fingerprint + ":unsupported").encode("utf-8")
        ).hexdigest()
        event_id = hashlib.sha256((fingerprint + ":phase1").encode()).hexdigest()
        now = self.store.now()
        with self.store.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM mirror_processed_fingerprints WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if exists:
                return {"status": "DUPLICATE", "fingerprint": fingerprint}
            conn.execute(
                "INSERT INTO mirror_processed_fingerprints VALUES (?, ?)",
                (fingerprint, now),
            )
            MirrorStore._set_meta(conn, "source_sync_status", "UNSYNCED")
            MirrorStore._set_meta(conn, "source_sync_note", reason)
            MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
            MirrorStore._set_meta(conn, "mirror_sync_note", reason)
            conn.execute(
                """INSERT INTO mirror_events
                   (event_id, source_fingerprint, stable_key, symbol, side,
                    position_side, action, source_quantity, reference_price,
                    status, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, fingerprint, stable_key, symbol,
                    str(raw.get("side") or ""),
                    str(raw.get("positionSide") or ""),
                    "UNSUPPORTED", str(raw.get("executedQty") or ""),
                    str(raw.get("avgPrice") or ""),
                    "BLOCKED_UNSUPPORTED_SOURCE_EVENT", reason, now,
                ),
            )
            conn.execute(
                """INSERT INTO mirror_intents
                   (intent_id, event_id, symbol, action, side, quantity,
                    margin_usdt, leverage, reference_price, mark_price,
                    price_deviation_bps, status, reason, realized_pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, event_id, symbol, "UNSUPPORTED",
                    str(raw.get("positionSide") or ""), "0", "0", "0",
                    str(raw.get("avgPrice") or ""), None, None,
                    "BLOCKED_UNSUPPORTED_SOURCE_EVENT", reason, "0", now,
                ),
            )
            conn.commit()
        return {"status": "BLOCKED_UNSUPPORTED_SOURCE_EVENT", "reason": reason}

    def _record_ambiguous_update(self, source: SourceOrder) -> dict[str, Any]:
        now = self.store.now()
        event_id = hashlib.sha256(
            (source.fingerprint + ":ambiguous").encode("utf-8")
        ).hexdigest()
        reason = (
            "相同 symbol/side/positionSide/type/orderTime 出现不同订单快照；"
            "公开接口没有 orderId/tradeId，无法确认是部分成交更新还是另一笔订单"
        )
        with self.store.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM mirror_processed_fingerprints WHERE fingerprint=?",
                (source.fingerprint,),
            ).fetchone()
            if exists:
                return {"status": "DUPLICATE", "fingerprint": source.fingerprint}
            conn.execute(
                "INSERT INTO mirror_processed_fingerprints VALUES (?, ?)",
                (source.fingerprint, now),
            )
            conn.execute(
                """UPDATE mirror_source_orders
                   SET identity_status='AMBIGUOUS', updated_at=?
                   WHERE stable_key=?""",
                (now, source.stable_key),
            )
            MirrorStore._set_meta(conn, "source_sync_status", "UNSYNCED")
            MirrorStore._set_meta(conn, "source_sync_note", reason)
            MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
            MirrorStore._set_meta(conn, "mirror_sync_note", reason)
            conn.execute(
                """INSERT INTO mirror_events
                   (event_id, source_fingerprint, stable_key, symbol, side,
                    position_side, action, source_quantity, reference_price,
                    status, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, source.fingerprint, source.stable_key,
                    source.symbol, source.side, source.position_side,
                    source.action, str(source.quantity),
                    str(source.reference_price),
                    "BLOCKED_IDENTITY_AMBIGUOUS", reason, now,
                ),
            )
            conn.execute(
                """INSERT INTO mirror_intents
                   (intent_id, event_id, symbol, action, side, quantity,
                    margin_usdt, leverage, reference_price, mark_price,
                    price_deviation_bps, status, reason, realized_pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, event_id, source.symbol, source.action,
                    source.position_side, "0", "0", str(self.config.leverage),
                    str(source.reference_price), None, None,
                    "BLOCKED_IDENTITY_AMBIGUOUS", reason, "0", now,
                ),
            )
            conn.commit()
        return {"status": "BLOCKED_IDENTITY_AMBIGUOUS", "reason": reason}

    def _apply_new_source_order(
        self,
        source: SourceOrder,
        snapshot: MarketSnapshot | None,
        market_error: str | None,
    ) -> dict[str, Any]:
        now = self.store.now()
        event_id = hashlib.sha256(
            (source.fingerprint + ":phase1").encode("utf-8")
        ).hexdigest()
        with self.store.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM mirror_processed_fingerprints WHERE fingerprint=?",
                (source.fingerprint,),
            ).fetchone()
            if exists:
                return {"status": "DUPLICATE", "fingerprint": source.fingerprint}

            conn.execute(
                """INSERT INTO mirror_source_orders
                   (stable_key, fingerprint, symbol, side, position_side,
                    order_type, order_time_ms, order_update_time_ms,
                    executed_qty, avg_price, identity_quality, identity_status,
                    raw_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OK', ?, ?)""",
                (
                    source.stable_key, source.fingerprint, source.symbol,
                    source.side, source.position_side,
                    str(source.raw.get("type") or ""),
                    source.order_time_ms, source.update_time_ms,
                    str(source.quantity), str(source.reference_price),
                    source.identity_quality,
                    json.dumps(source.raw, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO mirror_processed_fingerprints VALUES (?, ?)",
                (source.fingerprint, now),
            )

            sync_status = MirrorStore._get_meta(conn, "source_sync_status") or "UNSYNCED"
            mirror_sync_status = MirrorStore._get_meta(conn, "mirror_sync_status") or "SYNCED"
            intent_status = "SIMULATED"
            reason = ""
            intent_qty = ZERO
            intent_margin = ZERO
            mark_price = snapshot.mark_price if snapshot else None
            deviation_bps: Decimal | None = None
            realized_pnl = ZERO

            source_position = conn.execute(
                """SELECT * FROM mirror_source_positions
                   WHERE symbol=? AND side=?""",
                (source.symbol, source.position_side),
            ).fetchone()
            source_qty_before = _d(source_position["quantity"]) if source_position else ZERO
            source_avg_before = _d(source_position["avg_entry_price"]) if source_position else ZERO

            mirror_position = conn.execute(
                """SELECT * FROM mirror_positions
                   WHERE symbol=? AND side=?""",
                (source.symbol, source.position_side),
            ).fetchone()
            mirror_qty_before = _d(mirror_position["quantity"]) if mirror_position else ZERO
            mirror_avg_before = _d(mirror_position["avg_entry_price"]) if mirror_position else ZERO
            mirror_margin_before = _d(mirror_position["margin_used"]) if mirror_position else ZERO

            if source.identity_quality == "MISSING_TIME":
                intent_status = "BLOCKED_MISSING_ORDER_TIME"
                reason = "订单缺少稳定 orderTime，无法构造可靠的合成订单标识"
                MirrorStore._set_meta(conn, "source_sync_status", "UNSYNCED")
                MirrorStore._set_meta(conn, "source_sync_note", reason)
                MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                MirrorStore._set_meta(conn, "mirror_sync_note", reason)
            elif sync_status != "SYNCED":
                intent_status = "BLOCKED_SOURCE_UNSYNCED"
                reason = "熬鹰源仓位没有可信基准；当前事件只记录，不更新镜像仓位"
                if source.action == "INCREASE" and snapshot:
                    try:
                        preview = build_open_plan(
                            margin_usdt=self.config.fixed_margin_usdt,
                            leverage=self.config.leverage,
                            mark_price=snapshot.mark_price,
                            reference_price=source.reference_price,
                            rules=snapshot.rules,
                        )
                        intent_qty = preview.quantity
                        intent_margin = preview.margin_usdt
                        deviation_bps = preview.price_deviation_bps
                    except ValueError:
                        pass
            elif source.action == "INCREASE":
                source_qty_after = source_qty_before + source.quantity
                source_avg_after = (
                    (source_qty_before * source_avg_before)
                    + (source.quantity * source.reference_price)
                ) / source_qty_after
                self._write_source_position(
                    conn, source.symbol, source.position_side,
                    source_qty_after, source_avg_after, now,
                )

                if mirror_sync_status != "SYNCED":
                    intent_status = "BLOCKED_MIRROR_UNSYNCED"
                    reason = MirrorStore._get_meta(conn, "mirror_sync_note") or "镜像账本状态不一致"
                elif snapshot is None:
                    intent_status = "BLOCKED_MARKET_DATA_UNAVAILABLE"
                    reason = market_error or "无法获取当前标记价格和交易规则"
                else:
                    plan, block_reason = self._build_and_check_open_plan(
                        conn, source, snapshot
                    )
                    if plan is None:
                        intent_status = block_reason or "BLOCKED_RISK"
                        reason = block_reason or "风险检查未通过"
                        if plan is None and snapshot:
                            try:
                                preview = build_open_plan(
                                    margin_usdt=self.config.fixed_margin_usdt,
                                    leverage=self.config.leverage,
                                    mark_price=snapshot.mark_price,
                                    reference_price=source.reference_price,
                                    rules=snapshot.rules,
                                )
                                intent_qty = preview.quantity
                                intent_margin = preview.margin_usdt
                                deviation_bps = preview.price_deviation_bps
                            except ValueError:
                                pass
                    else:
                        intent_qty = plan.quantity
                        intent_margin = plan.margin_usdt
                        deviation_bps = plan.price_deviation_bps
                        self._apply_mirror_open(
                            conn, source, plan.quantity, plan.mark_price,
                            plan.margin_usdt, now,
                        )
                        intent_status = "SIMULATED"
            else:
                if source_qty_before <= 0:
                    intent_status = "BLOCKED_SOURCE_POSITION_UNKNOWN"
                    reason = "收到减仓信号，但源账本没有对应的已知持仓"
                    MirrorStore._set_meta(conn, "source_sync_status", "UNSYNCED")
                    MirrorStore._set_meta(conn, "source_sync_note", reason)
                    MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                    MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                elif source.quantity > source_qty_before:
                    intent_status = "BLOCKED_SOURCE_QTY_UNDERFLOW"
                    reason = (
                        f"减仓数量 {source.quantity} 大于已知源仓位 {source_qty_before}；"
                        "源数据可能缺失或重复"
                    )
                    MirrorStore._set_meta(conn, "source_sync_status", "UNSYNCED")
                    MirrorStore._set_meta(conn, "source_sync_note", reason)
                    MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                    MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                else:
                    ratio = source.quantity / source_qty_before
                    source_qty_after = max(ZERO, source_qty_before - source.quantity)
                    self._write_source_position(
                        conn, source.symbol, source.position_side,
                        source_qty_after,
                        source_avg_before if source_qty_after > 0 else ZERO,
                        now,
                    )
                    signal_time = source.update_time_ms or source.order_time_ms
                    signal_age = max(0, (_now_ms() - signal_time) // 1000)
                    if mirror_sync_status != "SYNCED":
                        intent_status = "BLOCKED_MIRROR_UNSYNCED"
                        reason = MirrorStore._get_meta(conn, "mirror_sync_note") or "镜像账本状态不一致"
                    elif signal_time > _now_ms() + 30_000:
                        intent_status = "BLOCKED_FUTURE_SIGNAL"
                        reason = "减仓信号时间超前于服务器时间"
                        MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                        MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                    elif signal_age > self.config.max_signal_age_seconds:
                        intent_status = "BLOCKED_STALE_SIGNAL"
                        reason = f"减仓信号已延迟 {signal_age} 秒，超过限制"
                        MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                        MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                    elif snapshot is None:
                        intent_status = "BLOCKED_MARKET_DATA_UNAVAILABLE"
                        reason = market_error or "无法获取交易规则"
                        MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                        MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                    elif mirror_qty_before <= 0:
                        intent_status = "BLOCKED_NO_MIRROR_POSITION"
                        reason = "源仓位有减仓，但当前没有对应的模拟镜像仓位"
                    else:
                        if ratio >= 1:
                            reduce_qty = mirror_qty_before
                        else:
                            reduce_qty = floor_to_step(
                                mirror_qty_before * ratio,
                                snapshot.rules.step_size,
                            )
                        if reduce_qty <= 0:
                            intent_status = "BLOCKED_REDUCE_DUST"
                            reason = "按源仓位减仓比例计算后，模拟减仓数量小于数量步进"
                            MirrorStore._set_meta(conn, "mirror_sync_status", "UNSYNCED")
                            MirrorStore._set_meta(conn, "mirror_sync_note", reason)
                        else:
                            exit_price = source.reference_price
                            if source.position_side == "LONG":
                                realized_pnl = (exit_price - mirror_avg_before) * reduce_qty
                            else:
                                realized_pnl = (mirror_avg_before - exit_price) * reduce_qty
                            remaining = max(ZERO, mirror_qty_before - reduce_qty)
                            margin_after = (
                                ZERO if remaining == 0
                                else mirror_margin_before * (remaining / mirror_qty_before)
                            )
                            conn.execute(
                                """UPDATE mirror_positions
                                   SET quantity=?, margin_used=?, realized_pnl=?,
                                       updated_at=?
                                   WHERE symbol=? AND side=?""",
                                (
                                    str(remaining), str(margin_after),
                                    str(_d(mirror_position["realized_pnl"]) + realized_pnl),
                                    now, source.symbol, source.position_side,
                                ),
                            )
                            intent_qty = reduce_qty
                            intent_status = "SIMULATED"
                            reason = "按源仓位减仓比例模拟减仓"

            self._write_event_and_intent(
                conn, source, event_id, intent_status, reason,
                intent_qty, intent_margin,
                self.config.leverage,
                source.reference_price,
                mark_price,
                deviation_bps,
                realized_pnl,
                now,
            )
            conn.commit()

        self.log.info(
            "镜像模拟事件：%s %s %s qty=%s status=%s reason=%s",
            source.symbol, source.position_side, source.action,
            intent_qty, intent_status, reason,
        )
        return {
            "status": intent_status,
            "symbol": source.symbol,
            "action": source.action,
            "quantity": str(intent_qty),
            "reason": reason,
        }

    def _build_and_check_open_plan(
        self,
        conn,
        source: SourceOrder,
        snapshot: MarketSnapshot,
    ):
        cfg = self.config
        now_ms = _now_ms()
        signal_time = source.update_time_ms or source.order_time_ms
        if signal_time > now_ms + 30_000:
            return None, "BLOCKED_FUTURE_SIGNAL"
        age_seconds = max(0, (now_ms - signal_time) // 1000)
        if age_seconds > cfg.max_signal_age_seconds:
            return None, "BLOCKED_STALE_SIGNAL"
        if cfg.allowed_symbols and source.symbol not in cfg.allowed_symbols:
            return None, "BLOCKED_SYMBOL_NOT_ALLOWED"
        if cfg.fixed_margin_usdt > cfg.max_order_margin_usdt:
            return None, "BLOCKED_MAX_ORDER_MARGIN"
        try:
            plan = build_open_plan(
                margin_usdt=cfg.fixed_margin_usdt,
                leverage=cfg.leverage,
                mark_price=snapshot.mark_price,
                reference_price=source.reference_price,
                rules=snapshot.rules,
            )
        except ValueError as exc:
            return None, f"BLOCKED_SYMBOL_FILTER: {exc}"

        if plan.price_deviation_bps > cfg.max_price_deviation_bps:
            return None, "BLOCKED_PRICE_DEVIATION"

        total_margin = sum(
            (_d(row[0]) for row in conn.execute(
                "SELECT margin_used FROM mirror_positions"
            ).fetchall()),
            ZERO,
        )
        symbol_margin = sum(
            (_d(row[0]) for row in conn.execute(
                "SELECT margin_used FROM mirror_positions WHERE symbol=?",
                (source.symbol,),
            ).fetchall()),
            ZERO,
        )
        if total_margin + cfg.fixed_margin_usdt > cfg.max_total_margin_usdt:
            return None, "BLOCKED_MAX_TOTAL_MARGIN"
        if symbol_margin + cfg.fixed_margin_usdt > cfg.max_symbol_margin_usdt:
            return None, "BLOCKED_MAX_SYMBOL_MARGIN"

        daily_pnl = sum(
            (_d(row[0]) for row in conn.execute(
                """SELECT realized_pnl FROM mirror_intents
                   WHERE status='SIMULATED' AND created_at>=?""",
                (_utc_day_start(),),
            ).fetchall()),
            ZERO,
        )
        if daily_pnl <= -cfg.max_daily_loss_usdt:
            return None, "BLOCKED_DAILY_LOSS"

        return plan, None

    @staticmethod
    def _write_source_position(
        conn,
        symbol: str,
        side: str,
        quantity: Decimal,
        avg_entry_price: Decimal,
        now: str,
    ) -> None:
        conn.execute(
            """INSERT INTO mirror_source_positions
               (symbol, side, quantity, avg_entry_price, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol, side) DO UPDATE SET
                 quantity=excluded.quantity,
                 avg_entry_price=excluded.avg_entry_price,
                 updated_at=excluded.updated_at""",
            (symbol, side, str(quantity), str(avg_entry_price), now),
        )

    @staticmethod
    def _apply_mirror_open(
        conn,
        source: SourceOrder,
        quantity: Decimal,
        mark_price: Decimal,
        margin: Decimal,
        now: str,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM mirror_positions WHERE symbol=? AND side=?",
            (source.symbol, source.position_side),
        ).fetchone()
        old_qty = _d(row["quantity"]) if row else ZERO
        old_avg = _d(row["avg_entry_price"]) if row else ZERO
        old_margin = _d(row["margin_used"]) if row else ZERO
        new_qty = old_qty + quantity
        new_avg = (
            (old_qty * old_avg + quantity * mark_price) / new_qty
            if new_qty > 0 else ZERO
        )
        old_pnl = _d(row["realized_pnl"]) if row else ZERO
        conn.execute(
            """INSERT INTO mirror_positions
               (symbol, side, quantity, avg_entry_price, margin_used,
                realized_pnl, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, side) DO UPDATE SET
                 quantity=excluded.quantity,
                 avg_entry_price=excluded.avg_entry_price,
                 margin_used=excluded.margin_used,
                 updated_at=excluded.updated_at""",
            (
                source.symbol, source.position_side, str(new_qty),
                str(new_avg), str(old_margin + margin), str(old_pnl), now,
            ),
        )

    @staticmethod
    def _write_event_and_intent(
        conn,
        source: SourceOrder,
        event_id: str,
        status: str,
        reason: str,
        quantity: Decimal,
        margin: Decimal,
        leverage: Decimal,
        reference_price: Decimal,
        mark_price: Decimal | None,
        deviation_bps: Decimal | None,
        realized_pnl: Decimal,
        now: str,
    ) -> None:
        action = "OPEN" if source.action == "INCREASE" else "REDUCE"
        if source.action == "INCREASE" and status == "SIMULATED":
            action = "OPEN_OR_ADD"
        elif source.action == "REDUCE" and status == "SIMULATED":
            action = "REDUCE_OR_CLOSE"

        conn.execute(
            """INSERT INTO mirror_events
               (event_id, source_fingerprint, stable_key, symbol, side,
                position_side, action, source_quantity, reference_price,
                status, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, source.fingerprint, source.stable_key,
                source.symbol, source.side, source.position_side,
                action, str(source.quantity), str(reference_price),
                status, reason, now,
            ),
        )
        conn.execute(
            """INSERT INTO mirror_intents
               (intent_id, event_id, symbol, action, side, quantity,
                margin_usdt, leverage, reference_price, mark_price,
                price_deviation_bps, status, reason, realized_pnl, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, event_id, source.symbol, action,
                source.position_side, str(quantity), str(margin),
                str(leverage), str(reference_price),
                str(mark_price) if mark_price is not None else None,
                str(deviation_bps) if deviation_bps is not None else None,
                status, reason, str(realized_pnl), now,
            ),
        )
