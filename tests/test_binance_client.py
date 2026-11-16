import asyncio
import json
import unittest

import httpx

from monitor.binance_client import BinanceCopyTradingClient
from monitor.events import order_fingerprint


def order(symbol, timestamp):
    return {
        "symbol": symbol,
        "baseAsset": symbol.replace("USDT", ""),
        "quoteAsset": "USDT",
        "side": "BUY",
        "type": "MARKET",
        "positionSide": "LONG",
        "executedQty": 1.0,
        "avgPrice": 100.0,
        "totalPnl": 0.0,
        "orderUpdateTime": timestamp,
        "orderTime": timestamp,
    }


class BinanceCursorTests(unittest.TestCase):
    def test_only_catches_up_until_known_record(self):
        known = order("KNOWNUSDT", 100)
        page1 = [order("NEW1USDT", 400), order("NEW2USDT", 300)]
        page2 = [order("NEW3USDT", 200), known]
        requests_seen = []

        async def handler(request):
            body = json.loads(request.content.decode("utf-8"))
            requests_seen.append(body)
            if "indexValue" not in body:
                payload = {
                    "code": "000000",
                    "success": True,
                    "data": {"total": 4, "indexValue": "200", "list": page1},
                }
            else:
                self.assertEqual(body["indexValue"], "200")
                payload = {
                    "code": "000000",
                    "success": True,
                    "data": {"total": 4, "indexValue": "100", "list": page2},
                }
            return httpx.Response(200, json=payload, request=request)

        async def run():
            client = BinanceCopyTradingClient(
                base_url="https://example.invalid",
                referer="https://example.invalid/trader",
                portfolio_id="123",
            )
            await client.client.aclose()
            client.client = httpx.AsyncClient(
                base_url="https://example.invalid",
                transport=httpx.MockTransport(handler),
            )
            try:
                return await client.fetch_latest_and_unseen(
                    known_fingerprints={order_fingerprint(known)},
                    lookback_days=30,
                    page_size=2,
                    max_catchup_pages=3,
                    fingerprint_fn=order_fingerprint,
                )
            finally:
                await client.close()

        fresh, pages, total = asyncio.run(run())
        self.assertEqual(len(fresh), 3)
        self.assertEqual(pages, 2)
        self.assertEqual(total, 4)
        self.assertEqual(len(requests_seen), 2)
        self.assertEqual(
            {item["symbol"] for item in fresh},
            {"NEW1USDT", "NEW2USDT", "NEW3USDT"},
        )


if __name__ == "__main__":
    unittest.main()
