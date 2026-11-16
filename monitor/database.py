from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(
        self,
        database_path: str,
        keep_event_days: int = 30,
        keep_event_count: int = 500,
    ) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.keep_event_days = keep_event_days
        self.keep_event_count = keep_event_count
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

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    fingerprint TEXT PRIMARY KEY,
                    symbol TEXT,
                    side TEXT,
                    position_side TEXT,
                    order_type TEXT,
                    executed_qty TEXT,
                    avg_price TEXT,
                    total_pnl TEXT,
                    order_time_ms INTEGER,
                    order_update_time_ms INTEGER,
                    raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    notification_status TEXT NOT NULL DEFAULT 'pending',
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_last_attempt_at TEXT,
                    notification_sent_at TEXT,
                    notification_last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_orders_time
                ON orders(order_time_ms DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS subscribers (
                email TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                confirmed INTEGER NOT NULL DEFAULT 0,
                requested_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                email TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                attempt_at TEXT,
                sent_at TEXT,
                PRIMARY KEY (email, fingerprint)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS unsubscribe_requests (
                email TEXT PRIMARY KEY,
                requested_at TEXT NOT NULL
            )""")
            conn.commit()

    def request_subscription(self, email: str) -> str | None:
        now = self._now()
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
        ).isoformat()
        with self.connect() as conn:
            recent = conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE requested_at > ?", (cutoff,)
            ).fetchone()[0]
            row = conn.execute(
                "SELECT confirmed, requested_at FROM subscribers WHERE email=?", (email,)
            ).fetchone()
            if row and (row["confirmed"] or row["requested_at"] > cutoff):
                return None
            if recent >= 20:
                return None
            token = secrets.token_urlsafe(32)
            conn.execute("""INSERT INTO subscribers(email, token, requested_at)
                VALUES(?, ?, ?) ON CONFLICT(email) DO UPDATE SET
                token=excluded.token, requested_at=excluded.requested_at""",
                (email, token, now))
            conn.commit()
            return token

    def confirm_subscription(self, token: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE subscribers SET confirmed=1 WHERE token=? AND confirmed=0",
                (token,))
            conn.commit()
            return cursor.rowcount == 1

    def unsubscribe(self, token: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT email FROM subscribers WHERE token=?", (token,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM deliveries WHERE email=?", (row["email"],))
            conn.execute("DELETE FROM unsubscribe_requests WHERE email=?", (row["email"],))
            conn.execute("DELETE FROM subscribers WHERE email=?", (row["email"],))
            conn.commit()
            return True

    def request_unsubscribe(self, email: str) -> str | None:
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
        ).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT token FROM subscribers WHERE email=? AND confirmed=1", (email,)
            ).fetchone()
            recent = conn.execute(
                "SELECT COUNT(*) FROM unsubscribe_requests WHERE requested_at > ?", (cutoff,)
            ).fetchone()[0]
            previous = conn.execute(
                "SELECT requested_at FROM unsubscribe_requests WHERE email=?", (email,)
            ).fetchone()
            if not row or recent >= 20 or (previous and previous["requested_at"] > cutoff):
                return None
            conn.execute("""INSERT INTO unsubscribe_requests(email, requested_at)
                VALUES(?, ?) ON CONFLICT(email) DO UPDATE SET requested_at=excluded.requested_at""",
                (email, self._now()))
            conn.commit()
            return row["token"]

    def cancel_unsubscribe_request(self, email: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM unsubscribe_requests WHERE email=?", (email,))
            conn.commit()

    def subscriber_token(self, email: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT token FROM subscribers WHERE email=? AND confirmed=1", (email,)
            ).fetchone()
            return row["token"] if row else None

    def pending_subscriber_notifications(self) -> dict[str, list[dict[str, Any]]]:
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 60, tz=timezone.utc
        ).isoformat()
        with self.connect() as conn:
            rows = conn.execute("""SELECT d.email, o.* FROM deliveries d
                JOIN orders o ON o.fingerprint=d.fingerprint
                WHERE d.sent_at IS NULL AND (d.attempt_at IS NULL OR d.attempt_at < ?)
                ORDER BY o.order_time_ms ASC LIMIT 200""", (cutoff,)).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(item.pop("email"), []).append(item)
        return grouped

    def mark_subscriber_delivery(self, email: str, fingerprints: list[str], sent: bool) -> None:
        now = self._now()
        with self.connect() as conn:
            conn.executemany(
                "UPDATE deliveries SET attempt_at=?, sent_at=? WHERE email=? AND fingerprint=?",
                [(now, now if sent else None, email, fp) for fp in fingerprints],
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM service_state WHERE key = ?",
                (key,),
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO service_state(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def is_bootstrapped(self) -> bool:
        return self.get_state("bootstrapped", "0") == "1"

    def get_known_fingerprints(self, limit: int = 1000) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT fingerprint FROM orders
                ORDER BY order_time_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {row["fingerprint"] for row in rows}

    def _insert_order(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
        notification_status: str,
    ) -> bool:
        from .events import normalize_order

        normalized = normalize_order(row)
        now = self._now()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO orders (
                fingerprint, symbol, side, position_side, order_type,
                executed_qty, avg_price, total_pnl,
                order_time_ms, order_update_time_ms, raw_json,
                first_seen_at, notification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["fingerprint"],
                normalized["symbol"],
                normalized["side"],
                normalized["position_side"],
                normalized["order_type"],
                str(normalized["executed_qty"]),
                str(normalized["avg_price"]),
                str(normalized["total_pnl"]),
                int(normalized["order_time"] or 0),
                int(normalized["order_update_time"] or 0),
                json.dumps(row, ensure_ascii=False, sort_keys=True),
                now,
                notification_status,
            ),
        )
        return cursor.rowcount == 1

    def bootstrap(self, rows: list[dict[str, Any]]) -> int:
        """首次启动只建立基线，不发送旧订单通知。"""
        inserted = 0
        with self.connect() as conn:
            for row in rows:
                inserted += int(self._insert_order(conn, row, "baseline"))
            conn.execute(
                """
                INSERT INTO service_state(key, value)
                VALUES('bootstrapped', '1')
                ON CONFLICT(key) DO UPDATE SET value='1'
                """
            )
            conn.commit()
        self.prune()
        return inserted

    def insert_new_orders(self, rows: list[dict[str, Any]]) -> list[str]:
        inserted_fingerprints: list[str] = []
        with self.connect() as conn:
            for row in rows:
                from .events import order_fingerprint
                fp = order_fingerprint(row)
                if self._insert_order(conn, row, "pending"):
                    inserted_fingerprints.append(fp)
                    conn.execute("""INSERT INTO deliveries(email, fingerprint)
                        SELECT email, ? FROM subscribers WHERE confirmed=1""", (fp,))
            conn.commit()
        self.prune()
        return inserted_fingerprints

    def get_pending_notifications(
        self,
        retry_after_seconds: int = 60,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - retry_after_seconds,
            tz=timezone.utc,
        ).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE notification_status='pending'
                  AND (
                    notification_last_attempt_at IS NULL
                    OR notification_last_attempt_at < ?
                  )
                ORDER BY order_time_ms ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_attempt(
        self,
        fingerprints: list[str],
        error: str | None = None,
    ) -> None:
        now = self._now()
        with self.connect() as conn:
            for fp in fingerprints:
                conn.execute(
                    """
                    UPDATE orders
                    SET notification_attempts = notification_attempts + 1,
                        notification_last_attempt_at = ?,
                        notification_last_error = ?
                    WHERE fingerprint = ? AND notification_status='pending'
                    """,
                    (now, error, fp),
                )
            conn.commit()

    def mark_notification_sent(self, fingerprints: list[str]) -> None:
        now = self._now()
        with self.connect() as conn:
            for fp in fingerprints:
                conn.execute(
                    """
                    UPDATE orders
                    SET notification_status='sent',
                        notification_sent_at=?,
                        notification_last_attempt_at=?,
                        notification_last_error=NULL,
                        notification_attempts=notification_attempts + 1
                    WHERE fingerprint = ?
                    """,
                    (now, now, fp),
                )
            conn.commit()

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = min(100, max(1, limit))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM orders
                ORDER BY order_time_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        result = []
        from .events import classify_direction, format_timestamp
        for row in rows:
            item = dict(row)
            try:
                raw = json.loads(item["raw_json"])
            except (TypeError, json.JSONDecodeError):
                raw = {}
            item["direction"] = classify_direction(raw)
            item["order_time_text"] = format_timestamp(
                item.get("order_time_ms")
            )
            item.pop("raw_json", None)
            result.append(item)
        return result

    def prune(self) -> None:
        cutoff_ms = int(
            (datetime.now(timezone.utc).timestamp()
             - self.keep_event_days * 86400) * 1000
        )
        with self.connect() as conn:
            conn.execute(
                """DELETE FROM orders
                   WHERE order_time_ms < ?
                     AND notification_status != 'pending'
                     AND fingerprint NOT IN (
                       SELECT fingerprint FROM deliveries WHERE sent_at IS NULL
                     )""",
                (cutoff_ms,),
            )
            conn.execute(
                """
                DELETE FROM orders
                WHERE notification_status != 'pending'
                  AND fingerprint NOT IN (
                    SELECT fingerprint FROM deliveries WHERE sent_at IS NULL
                  )
                  AND fingerprint NOT IN (
                    SELECT fingerprint FROM orders
                    ORDER BY order_time_ms DESC
                    LIMIT ?
                  )
                """,
                (self.keep_event_count,),
            )
            conn.execute("DELETE FROM deliveries WHERE fingerprint NOT IN (SELECT fingerprint FROM orders)")
            conn.commit()

    def status_snapshot(self) -> dict[str, Any]:
        with self.connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders"
            ).fetchone()
            pending_row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM orders
                WHERE notification_status='pending'
                """
            ).fetchone()
            latest_row = conn.execute(
                """
                SELECT symbol, side, position_side, order_time_ms,
                       notification_status
                FROM orders ORDER BY order_time_ms DESC LIMIT 1
                """
            ).fetchone()

        return {
            "bootstrapped": self.is_bootstrapped(),
            "stored_order_count": count_row["n"] if count_row else 0,
            "pending_email_count": pending_row["n"] if pending_row else 0,
            "latest_order": dict(latest_row) if latest_row else None,
            "last_poll_at": self.get_state("last_poll_at"),
            "last_success_at": self.get_state("last_success_at"),
            "last_error": self.get_state("last_error"),
            "last_email_at": self.get_state("last_email_at"),
            "last_email_error": self.get_state("last_email_error"),
        }
