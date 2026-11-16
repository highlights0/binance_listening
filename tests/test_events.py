import unittest

from monitor.events import classify_direction, order_fingerprint


class EventTests(unittest.TestCase):
    def test_direction_mapping(self):
        self.assertIn("开多", classify_direction({
            "side": "BUY", "positionSide": "LONG"
        }))
        self.assertIn("减多", classify_direction({
            "side": "SELL", "positionSide": "LONG"
        }))
        self.assertIn("开空", classify_direction({
            "side": "SELL", "positionSide": "SHORT"
        }))
        self.assertIn("减空", classify_direction({
            "side": "BUY", "positionSide": "SHORT"
        }))

    def test_fingerprint_is_stable(self):
        a = {"symbol": "BTCUSDT", "side": "BUY", "orderTime": 123}
        b = {"orderTime": 123, "side": "BUY", "symbol": "BTCUSDT"}
        self.assertEqual(order_fingerprint(a), order_fingerprint(b))

    def test_fingerprint_changes_with_record(self):
        a = {"symbol": "BTCUSDT", "side": "BUY", "orderTime": 123}
        b = {"symbol": "BTCUSDT", "side": "BUY", "orderTime": 124}
        self.assertNotEqual(order_fingerprint(a), order_fingerprint(b))


if __name__ == "__main__":
    unittest.main()
