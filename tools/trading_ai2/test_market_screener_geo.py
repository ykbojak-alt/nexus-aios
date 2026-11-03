"""Focused tests for Binance spot feed fallback on geo-blocked hosts."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import ccxt


class _Response:
    def __init__(self, payload: object):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class BinanceGeoFallbackTests(unittest.TestCase):
    def test_market_data_uses_vision_klines_after_451(self):
        from tools.trading_ai2.market_data import MarketData

        rows = [[i, 100, 102, 98, 100, 10] for i in range(3)]
        md = MarketData(symbol="BTC/USDT")
        with patch.object(md.ex, "fetch_ohlcv",
                          side_effect=ccxt.ExchangeNotAvailable(
                              "451 restricted location")), \
                patch("tools.trading_ai2.market_data.urlopen",
                      return_value=_Response(rows)) as request:
            frame = md.fetch_ohlcv("15m", limit=3)

        self.assertEqual(len(frame), 3)
        self.assertEqual(float(frame.iloc[0]["close"]), 100.0)
        self.assertIn("data-api.binance.vision/api/v3/klines",
                      request.call_args.args[0].full_url)
        self.assertIn("symbol=BTCUSDT", request.call_args.args[0].full_url)

    def test_screener_uses_vision_exchange_info_after_451(self):
        from tools.trading_ai2.swarm.portfolio import market_screener as ms

        class _Exchange:
            def load_markets(self):
                raise ccxt.ExchangeNotAvailable("451 restricted location")

            def fetch_tickers(self):
                raise ccxt.ExchangeNotAvailable("451 restricted location")

        payload = {"symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "ETHUSDT", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
        ]}
        with patch.object(ms, "fetch_binance_vision_exchange_info",
                          return_value=payload) as vision:
            out = ms.discover_tickers(ex=_Exchange())

        self.assertIn("BTC/USDT", out)
        self.assertIn("ETH/USDT", out)
        vision.assert_called_once_with()

    def test_total_screener_feed_failure_is_not_flat_market(self):
        from tools.trading_ai2.swarm.portfolio.market_screener import (
            MarketDataUnavailable, get_top_assets)

        def failed_feed(symbol, timeframe, limit):
            raise ccxt.ExchangeNotAvailable("451 restricted location")

        with patch(
                "tools.trading_ai2.swarm.portfolio.market_screener."
                "fetch_binance_vision_ohlcv",
                side_effect=OSError("vision unavailable")):
            with self.assertRaises(MarketDataUnavailable):
                get_top_assets(tickers=["BTC/USDT", "ETH/USDT"],
                               ohlcv=failed_feed)


if __name__ == "__main__":
    unittest.main()
