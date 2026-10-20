import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.common.exceptions import APIError
from requests import HTTPError, Response

from intelligence import options_flow_engine as module


def provider_error(status, message):
    response = Response()
    response.status_code = status
    return APIError(json.dumps({"message": message}), HTTPError(response=response))


class InvalidUnderlyingTests(unittest.TestCase):
    def setUp(self):
        with patch.object(module, "OptionHistoricalDataClient"), \
             patch.object(module, "TradingClient"):
            self.engine = module.OptionsFlowEngine()
        self.engine.data_client = Mock()
        self.engine.data_client.get_option_chain.return_value = {}
        self.engine._get_contract_metadata = Mock(return_value={})
        self.market = SimpleNamespace(get_last_price=Mock(return_value=10.0))
        self.market_patch = patch.object(module, "get_market_data", return_value=self.market)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)

    def test_exact_provider_rejection_is_cached_without_fabricating_a_score(self):
        self.engine.data_client.get_option_chain.side_effect = provider_error(
            400, "invalid underlying symbol: SKYD.WS")
        with self.assertLogs(module.logger, level="INFO") as logs:
            first = self.engine.analyze(" skyd.ws ")
            second = self.engine.analyze("SKYD.WS")
        self.assertFalse(first.available)
        self.assertIsNone(first.score)
        self.assertEqual(first.bias, "UNAVAILABLE")
        self.assertFalse(first.metadata["cached"])
        self.assertTrue(second.metadata["cached"])
        self.assertEqual(second.metadata["unavailable_reason"], "PROVIDER_INVALID_UNDERLYING")
        self.engine.data_client.get_option_chain.assert_called_once()
        self.market.get_last_price.assert_called_once_with("SKYD.WS")
        self.engine._get_contract_metadata.assert_not_called()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("OPTIONS_UNDERLYING_UNSUPPORTED", logs.output[0])

    def test_rejection_does_not_disable_other_symbols(self):
        self.engine.data_client.get_option_chain.side_effect = [
            provider_error(400, "invalid underlying symbol: SKYD.WS"), {}]
        self.engine.analyze("SKYD.WS")
        result = self.engine.analyze("AAPL")
        self.assertTrue(result.available)
        self.assertEqual(self.engine.data_client.get_option_chain.call_count, 2)

    def test_cache_expires_and_provider_can_accept_the_symbol_later(self):
        self.engine.data_client.get_option_chain.side_effect = [
            provider_error(400, "invalid underlying symbol: SKYD.WS"), {}]
        with patch.object(module.time, "monotonic", return_value=100):
            self.engine.analyze("SKYD.WS")
        with patch.object(module.time, "monotonic", return_value=3699):
            self.assertTrue(self.engine.analyze("SKYD.WS").metadata["cached"])
        with patch.object(module.time, "monotonic", return_value=3700):
            self.assertTrue(self.engine.analyze("SKYD.WS").available)
        self.assertEqual(self.engine.data_client.get_option_chain.call_count, 2)
        self.assertNotIn("SKYD.WS", self.engine._invalid_underlyings)

    def test_other_provider_failures_remain_visible_and_retryable(self):
        errors = [provider_error(500, "invalid underlying symbol: AAPL"),
                  provider_error(401, "unauthorized"),
                  provider_error(400, "invalid strike price"),
                  provider_error(400, "invalid underlying symbol: OTHER"),
                  TimeoutError("provider timed out")]
        for error in errors:
            with self.subTest(error=str(error)):
                self.engine.data_client.get_option_chain.reset_mock()
                self.engine.data_client.get_option_chain.side_effect = error
                with self.assertLogs(module.logger, level="ERROR"):
                    for _ in range(2):
                        result = self.engine.analyze("AAPL")
                        self.assertFalse(result.available)
                        self.assertIsNone(result.score)
                self.assertEqual(self.engine.data_client.get_option_chain.call_count, 2)
                self.assertNotIn("AAPL", self.engine._invalid_underlyings)

    def test_cache_is_bounded_and_evicted_symbols_are_rechecked(self):
        def reject(request):
            raise provider_error(400, f"invalid underlying symbol: {request.underlying_symbol}")
        self.engine.data_client.get_option_chain.side_effect = reject
        with patch.object(self.engine, "INVALID_UNDERLYING_CACHE_SIZE", 2):
            for symbol in ("ONE.WS", "TWO.WS", "THREE.WS", "ONE.WS"):
                self.engine.analyze(symbol)
                self.assertLessEqual(len(self.engine._invalid_underlyings), 2)
        self.assertEqual(self.engine.data_client.get_option_chain.call_count, 4)

    def test_symbol_punctuation_does_not_guess_option_eligibility(self):
        for symbol in ("AAPL", "SPY", "BRK.B", "SKYD.WS"):
            result = self.engine.analyze(symbol)
            self.assertTrue(result.available)
        self.assertEqual(self.engine.data_client.get_option_chain.call_count, 4)

    def test_feature_integration_keeps_missing_options_unknown(self):
        self.engine.data_client.get_option_chain.side_effect = provider_error(
            400, "invalid underlying symbol: SKYD.WS")
        features = SimpleNamespace(symbol="SKYD.WS", options_flow_score=55.0, metadata={})
        self.engine.apply_to_features(features)
        self.engine.apply_to_features(features)
        self.assertIsNone(features.options_flow_score)
        self.assertFalse(features.metadata["options_available"])
        self.assertEqual(features.metadata["options_bias"], "UNAVAILABLE")
        self.engine.data_client.get_option_chain.assert_called_once()


if __name__ == "__main__":
    unittest.main()
