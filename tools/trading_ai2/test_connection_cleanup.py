"""Resource-lifecycle regression tests for trading_ai2 HTTP clients."""

from __future__ import annotations

import io
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.trading_ai2 import funding_executor, market_data  # noqa: E402
from tools.trading_ai2.executor import Executor  # noqa: E402
from tools.trading_ai2.swarm import learning_bridge, llm_factory, news_sentiment  # noqa: E402
from tools.trading_ai2.swarm.portfolio import market_screener  # noqa: E402
from tools.near_rpc import NearTestnetClient  # noqa: E402


def http_error() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/test", 503, "unavailable", {},
        io.BytesIO(b'{"error":"unavailable"}'))


class TestOpenAIClientCleanup(unittest.TestCase):
    def setUp(self):
        llm_factory.LLMFactory.reset_clients()

    def tearDown(self):
        llm_factory.LLMFactory.reset_clients()

    def _route(self, model="m-one", key="secret-one", url="https://llm.invalid/v1"):
        return llm_factory.LLMRoute(
            model=model, provider="openai-compatible", api_key=key,
            base_url=url, requested=model)

    def test_reset_clients_closes_cached_client(self):
        client = Mock()
        with patch.object(llm_factory.LLMFactory, "get_client", return_value=client):
            llm_factory.LLMFactory._cached_client(self._route())

        llm_factory.LLMFactory.reset_clients()

        client.close.assert_called_once_with()

    def test_blacklisting_key_closes_only_matching_cached_client(self):
        client = Mock()
        other = Mock()
        with patch.object(llm_factory.LLMFactory, "get_client",
                          side_effect=[client, other]):
            llm_factory.LLMFactory._cached_client(self._route())
            llm_factory.LLMFactory._cached_client(
                self._route("m-two", "secret-two", "https://other.invalid/v1"))

        llm_factory.LLMFactory.blacklist_model(
            "m-one", "rate_limit", key="secret-one",
            base_url="https://llm.invalid/v1")

        client.close.assert_called_once_with()
        other.close.assert_not_called()

    def test_blacklisting_model_closes_its_cached_client(self):
        client = Mock()
        with patch.object(llm_factory.LLMFactory, "get_client", return_value=client):
            llm_factory.LLMFactory._cached_client(self._route())

        llm_factory.LLMFactory.blacklist_model("m-one", "not_found")

        client.close.assert_called_once_with()

    def test_active_client_close_is_deferred_until_release(self):
        client = Mock()
        route = self._route()
        with patch.object(llm_factory.LLMFactory, "get_client", return_value=client):
            leased = llm_factory.LLMFactory._acquire_cached_client(route)
            llm_factory.LLMFactory.reset_clients()
            client.close.assert_not_called()
            llm_factory.LLMFactory._release_cached_client(leased)

        client.close.assert_called_once_with()


class TestUrlopenCleanup(unittest.TestCase):
    def _assert_funding_error_closed(self, call):
        error = http_error()
        with patch.object(funding_executor.urllib.request, "urlopen",
                          side_effect=error):
            result = call()
        self.assertTrue(error.fp.closed)
        return result

    def test_funding_get_closes_http_error_before_reraise(self):
        error = http_error()
        with patch.object(funding_executor.urllib.request, "urlopen",
                          side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                funding_executor._get("/price?symbol=BTC")
        self.assertTrue(error.fp.closed)

    def test_funding_post_reads_and_closes_http_error(self):
        result = self._assert_funding_error_closed(
            lambda: funding_executor._post("/open", {}, "key"))
        self.assertEqual(result["error"], "http_503")
        self.assertIn("unavailable", result["body"])

    def test_funding_positions_and_balance_close_http_errors(self):
        positions = self._assert_funding_error_closed(
            lambda: funding_executor.list_positions("key"))
        balance = self._assert_funding_error_closed(
            lambda: funding_executor.get_balance("key"))
        self.assertEqual(positions["error"], "http_503")
        self.assertEqual(balance["error"], "http_503")

    def test_market_data_vision_closes_http_error_before_reraise(self):
        error = http_error()
        with patch.object(market_data, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                market_data._vision_get("klines", {})
        self.assertTrue(error.fp.closed)

    def test_news_http_get_closes_http_error_before_retry(self):
        error = http_error()
        with patch.object(news_sentiment.urllib.request, "urlopen",
                          side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                news_sentiment._http_get("https://example.invalid", attempts=1)
        self.assertTrue(error.fp.closed)

    def test_learning_bridge_closes_http_error(self):
        learner = object.__new__(learning_bridge.TradingLearning)
        learner.api_url = "http://127.0.0.1:3124"
        learner.symbol = "BTC/USDT"
        error = http_error()
        with patch.object(learning_bridge.urllib.request, "urlopen",
                          side_effect=error):
            result = learner._save_memory(
                action="BUY", pnl=-1, snap={"price": 1}, position_id="p1",
                code="loss", personas=[], mode="test")
        self.assertTrue(error.fp.closed)
        self.assertEqual(result[0], "")
        self.assertIn("HTTP 503", result[1])

    def test_near_rpc_closes_http_error(self):
        error = http_error()
        client = NearTestnetClient(endpoint="https://example.invalid/rpc")
        with patch("tools.near_rpc.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "NEAR RPC HTTP 503"):
                client.call("query", {})
        self.assertTrue(error.fp.closed)


class TestOtherClientCleanup(unittest.TestCase):
    def test_news_llm_client_closes_after_success(self):
        route = llm_factory.LLMRoute(
            model="m-one", provider="openai-compatible", api_key="key",
            base_url="https://llm.invalid/v1", requested="m-one")
        client = Mock()
        with patch.object(news_sentiment.LLMFactory, "resolve", return_value=route), \
             patch.object(news_sentiment, "_resolve_providers", return_value=[]), \
             patch.object(news_sentiment.LLMFactory, "client_for", return_value=client), \
             patch.object(news_sentiment, "_ask_llm", return_value='{"sentiment":0.2}'):
            result = news_sentiment._evaluate("BTC", ["headline"])
        self.assertEqual(result.mode, "live")
        client.close.assert_called_once_with()

    def test_news_llm_client_closes_after_provider_error(self):
        route = llm_factory.LLMRoute(
            model="m-one", provider="openai-compatible", api_key="key",
            base_url="https://llm.invalid/v1", requested="m-one")
        client = Mock()
        with patch.object(news_sentiment.LLMFactory, "resolve", return_value=route), \
             patch.object(news_sentiment, "_resolve_providers", return_value=[]), \
             patch.object(news_sentiment.LLMFactory, "client_for", return_value=client), \
             patch.object(news_sentiment, "_ask_llm", side_effect=RuntimeError("offline")):
            result = news_sentiment._evaluate("BTC", ["headline"])
        self.assertEqual(result.mode, "stub")
        client.close.assert_called_once_with()

    def test_market_data_close_closes_exchange(self):
        exchange = Mock()
        with patch.object(market_data.ccxt, "binance", return_value=exchange):
            market = market_data.MarketData()
        market.close()
        exchange.close.assert_called_once_with()

    def test_executor_close_closes_exchange_even_if_db_close_fails(self):
        executor = object.__new__(Executor)
        executor.db = Mock()
        executor.ex = Mock()
        exchange = executor.ex
        executor.db.close.side_effect = RuntimeError("db close")
        with self.assertRaisesRegex(RuntimeError, "db close"):
            executor.close()
        exchange.close.assert_called_once_with()

    def test_funding_spread_closes_exchange_after_fetch_error(self):
        import ccxt

        exchange = Mock()
        exchange.fetch_ticker.side_effect = RuntimeError("offline")
        with patch.object(ccxt, "binance", return_value=exchange):
            self.assertIsNone(funding_executor.FundingExecutor._spread_pct("BTC"))
        exchange.close.assert_called_once_with()

    def test_market_screener_close_releases_singleton(self):
        exchange = Mock()
        old_exchange = market_screener._EX
        try:
            market_screener._EX = exchange
            market_screener.close_exchange()
            self.assertIsNone(market_screener._EX)
            exchange.close.assert_called_once_with()
        finally:
            market_screener._EX = old_exchange


if __name__ == "__main__":
    unittest.main()
