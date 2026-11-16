from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class MirrorStore:
    """镜像模拟账本，复用监控 SQLite 文件但使用独立表。"""

    def __init__(self, database_path: str) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mirror_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_processed_fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_source_orders (
                    stable_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    order_type TEXT,
                    order_time_ms INTEGER NOT NULL,
                    order_update_time_ms INTEGER NOT NULL,
                    executed_qty TEXT NOT NULL,
                    avg_price TEXT NOT NULL,
                    identity_quality TEXT NOT NULL,
                    identity_status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_source_positions (
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    avg_entry_price TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, side)
                );
                CREATE TABLE IF NOT EXISTS mirror_positions (
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    avg_entry_price TEXT NOT NULL,
                    margin_used TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, side)
                );
                CREATE TABLE IF NOT EXISTS mirror_events (
                    event_id TEXT PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL UNIQUE,
                    stable_key TEXT NOT NULL,
                    symbol TEXT,
                    side TEXT,
                    position_side TEXT,
                    action TEXT,
                    source_quantity TEXT,
                    reference_price TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_intents (
                    intent_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    symbol TEXT,
                    action TEXT,
                    side TEXT,
                    quantity TEXT,
                    margin_usdt TEXT,
                    leverage TEXT,
                    reference_price TEXT,
                    mark_price TEXT,
                    price_deviation_bps TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mirror_events_created
                    ON mirror_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mirror_intents_created
                    ON mirror_intents(created_at DESC);
            """)
            defaults = {
                "source_sync_status": "UNSYNCED",
                "source_sync_note": "尚未建立熬鹰源仓位基准",
                "mirror_sync_status": "SYNCED",
                "mirror_sync_note": "",
                "simulation_only": "1",
            }
            for key, value in defaults.items():
                if self._get_meta(conn, key) is None:
                    self._set_meta(conn, key, value)
            if self._get_meta(conn, "activation_at") is None:
                self._set_meta(conn, "activation_at", self.now())
            conn.commit()

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM mirror_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """INSERT INTO mirror_meta(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            return self._get_meta(conn, key) or default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            self._set_meta(conn, key, value)
            conn.commit()

    def pending_source_orders(self, limit: int = 200) -> list[dict[str, Any]]:
        activation = self.get_meta("activation_at", self.now())
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT o.fingerprint, o.raw_json, o.first_seen_at
                FROM orders o
                LEFT JOIN mirror_processed_fingerprints p
                  ON p.fingerprint=o.fingerprint
                WHERE p.fingerprint IS NULL
                  AND o.notification_status != 'baseline'
                  AND o.first_seen_at >= ?
                ORDER BY o.order_time_ms ASC
                LIMIT ?
                """,
                (activation, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self.connect() as conn:
            source_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_source_positions"
            ).fetchone()[0]
            mirror_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_positions WHERE CAST(quantity AS REAL) > 0"
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_events"
            ).fetchone()[0]
            intent_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_intents"
            ).fetchone()[0]
            blocked_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_intents WHERE status LIKE 'BLOCKED%'"
            ).fetchone()[0]
        return {
            "mode": "SIMULATION_ONLY",
            "real_trading_enabled": False,
            "source_sync_status": self.get_meta("source_sync_status", "UNSYNCED"),
            "source_sync_note": self.get_meta("source_sync_note", ""),
            "mirror_sync_status": self.get_meta("mirror_sync_status", "SYNCED"),
            "mirror_sync_note": self.get_meta("mirror_sync_note", ""),
            "activation_at": self.get_meta("activation_at"),
            "source_position_count": source_count,
            "mirror_position_count": mirror_count,
            "event_count": event_count,
            "intent_count": intent_count,
            "blocked_intent_count": blocked_count,
        }

    def source_positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mirror_source_positions ORDER BY symbol, side"
            ).fetchall()
        return [dict(row) for row in rows]

    def mirror_positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mirror_positions ORDER BY symbol, side"
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = min(200, max(1, limit))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mirror_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_intents(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = min(200, max(1, limit))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mirror_intents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def assume_source_flat(self, note: str) -> None:
        with self.connect() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM mirror_positions WHERE CAST(quantity AS REAL) > 0"
            ).fetchone()[0]
            if active:
                raise ValueError("已有模拟镜像仓位，不能直接重置源仓位基准")
            conn.execute("DELETE FROM mirror_source_positions")
            self._set_meta(conn, "source_sync_status", "SYNCED")
            self._set_meta(conn, "source_sync_note", note)
            self._set_meta(conn, "source_sync_at", self.now())
            self._set_meta(conn, "mirror_sync_status", "SYNCED")
            self._set_meta(conn, "mirror_sync_note", "")
            conn.commit()

    def set_source_baseline(self, positions: list[dict[str, Any]], note: str) -> None:
        with self.connect() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM mirror_positions WHERE CAST(quantity AS REAL) > 0"
            ).fetchone()[0]
            if active:
                raise ValueError("已有模拟镜像仓位，不能覆盖源仓位基准")
            conn.execute("DELETE FROM mirror_source_positions")
            seen: set[tuple[str, str]] = set()
            for item in positions:
                symbol = str(item["symbol"]).upper()
                side = str(item["side"]).upper()
                if side not in {"LONG", "SHORT"}:
                    raise ValueError(f"不支持的源仓位方向：{side}")
                key = (symbol, side)
                if key in seen:
                    raise ValueError(f"源仓位快照存在重复项：{symbol} {side}")
                seen.add(key)
                quantity = str(item["quantity"])
                avg_price = str(item["avg_entry_price"])
                if float(quantity) <= 0 or float(avg_price) <= 0:
                    raise ValueError("源仓位数量和开仓均价必须大于 0")
                conn.execute(
                    """INSERT INTO mirror_source_positions
                       (symbol, side, quantity, avg_entry_price, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (symbol, side, quantity, avg_price, self.now()),
                )
            self._set_meta(conn, "source_sync_status", "SYNCED")
            self._set_meta(conn, "source_sync_note", note)
            self._set_meta(conn, "source_sync_at", self.now())
            self._set_meta(conn, "mirror_sync_status", "SYNCED")
            self._set_meta(conn, "mirror_sync_note", "")
            conn.commit()
