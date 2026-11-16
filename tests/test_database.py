import tempfile
import unittest
import time
from pathlib import Path

from monitor.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_bootstrap_does_not_queue_email(self):
        rows = [{
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "executedQty": 1,
            "avgPrice": 100,
            "totalPnl": 0,
            "orderTime": int(time.time() * 1000),
            "orderUpdateTime": int(time.time() * 1000),
        }]
        self.assertEqual(self.db.bootstrap(rows), 1)
        self.assertTrue(self.db.is_bootstrapped())
        self.assertEqual(self.db.get_pending_notifications(), [])

    def test_new_order_is_pending_and_deduplicated(self):
        row = {
            "symbol": "ETHUSDT",
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "MARKET",
            "executedQty": 2,
            "avgPrice": 200,
            "totalPnl": 0,
            "orderTime": int(time.time() * 1000),
            "orderUpdateTime": int(time.time() * 1000),
        }
        self.db.bootstrap([])
        self.assertEqual(len(self.db.insert_new_orders([row])), 1)
        self.assertEqual(len(self.db.insert_new_orders([row])), 0)
        self.assertEqual(len(self.db.get_pending_notifications()), 1)

    def test_sent_order_is_not_pending(self):
        row = {
            "symbol": "SOLUSDT",
            "side": "BUY",
            "positionSide": "SHORT",
            "type": "MARKET",
            "executedQty": 3,
            "avgPrice": 50,
            "totalPnl": 1,
            "orderTime": int(time.time() * 1000),
            "orderUpdateTime": int(time.time() * 1000),
        }
        self.db.bootstrap([])
        fps = self.db.insert_new_orders([row])
        self.db.mark_notification_sent(fps)
        self.assertEqual(self.db.get_pending_notifications(), [])


if __name__ == "__main__":
    unittest.main()
