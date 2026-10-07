import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from core.models import FeatureSnapshot
from intelligence.decision_engine import DecisionEngine, DecisionState


class FeatureFreshnessTests(unittest.TestCase):
    def refresh_case(self, replacement_age=5, replacement_feed='iex'):
        engine = DecisionEngine.__new__(DecisionEngine)
        original = pd.DataFrame({'close': [10]})
        original.attrs['data_feed'] = 'iex'
        replacement = pd.DataFrame({'close': [11]})
        replacement.attrs['data_feed'] = replacement_feed
        features = self.features(15.1)
        diagnostics = DecisionEngine._apply_feature_freshness(features, '5m')
        engine.market_data = Mock()
        engine.market_data.get_bars.return_value = replacement
        candidate = self.features(replacement_age)
        engine.feature_engine = Mock()
        engine.feature_engine.build.return_value = candidate
        engine.feature_engine.diagnose_input.return_value = {}
        return engine, original, features, diagnostics, replacement, candidate

    def test_boundary_refresh_accepts_only_new_fresh_same_feed_features(self):
        engine, bars, features, diagnostics, replacement, candidate = self.refresh_case()
        result = engine._refresh_boundary_stale_bars('TEST', '5m', 300, bars, features, diagnostics)
        self.assertIs(result[0], replacement)
        self.assertIs(result[1], candidate)
        self.assertEqual(result[3]['status'], 'FRESH_REPLACEMENT')
        engine.market_data.get_bars.assert_called_once_with('TEST', '5m', 300)

    def test_boundary_refresh_keeps_original_on_stale_invalid_feed_or_failure(self):
        for case in ('stale', 'quality', 'feed', 'failure'):
            with self.subTest(case=case):
                engine, bars, features, diagnostics, _, candidate = self.refresh_case(
                    replacement_age=15.1 if case == 'stale' else 5,
                    replacement_feed='sip' if case == 'feed' else 'iex')
                if case == 'quality':
                    candidate.data_quality_ok = False
                if case == 'failure':
                    engine.market_data.get_bars.side_effect = TimeoutError()
                result = engine._refresh_boundary_stale_bars('TEST', '5m', 300, bars, features, diagnostics)
                self.assertIs(result[0], bars)
                self.assertIs(result[1], features)
                self.assertTrue(features.data_is_stale)
                engine.market_data.get_bars.assert_called_once()

    def test_boundary_refresh_skips_old_future_missing_and_fresh_data(self):
        for age in (20, -1, None, 5):
            with self.subTest(age=age):
                engine, bars, features, diagnostics, _, _ = self.refresh_case()
                diagnostics['latest_bar_age_minutes'] = age
                result = engine._refresh_boundary_stale_bars('TEST', '5m', 300, bars, features, diagnostics)
                self.assertIsNone(result[3])
                engine.market_data.get_bars.assert_not_called()

    def features(self, age):
        return FeatureSnapshot(symbol='TEST', data_quality_ok=True,
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=age))

    def test_stale_and_future_data_are_rejected(self):
        for age in (16, 3000, -1):
            with self.subTest(age=age):
                features = self.features(age)
                DecisionEngine._apply_feature_freshness(features, '5m')
                self.assertTrue(features.data_is_stale)

    def test_freshness_respects_bar_interval(self):
        for timeframe, age in [('1m', 2), ('5m', 10), ('1h', 70), ('1d', 1500)]:
            with self.subTest(timeframe=timeframe):
                features = self.features(age)
                DecisionEngine._apply_feature_freshness(features, timeframe)
                self.assertFalse(features.data_is_stale)

    def test_missing_timestamp_and_unknown_interval_fail_closed(self):
        features = SimpleNamespace(timestamp=None, data_is_stale=False)
        DecisionEngine._apply_feature_freshness(features, '5m')
        self.assertTrue(features.data_is_stale)
        features = self.features(1)
        DecisionEngine._apply_feature_freshness(features, 'invalid')
        self.assertTrue(features.data_is_stale)

    def test_pipeline_stops_before_enrichment_for_stale_bars(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine._load_external_research = Mock(return_value={})
        bars = pd.DataFrame({'close': [10]})
        engine.market_data = Mock()
        engine.market_data.get_bars.return_value = bars
        engine.feature_engine = Mock()
        diagnostics = {'valid_rows': 60, 'required_rows': 51, 'missing_columns': []}
        engine.feature_engine.diagnose_input.return_value = diagnostics
        engine._smart_backfill_bars = Mock(return_value=(bars, diagnostics, None))
        engine.feature_engine.build.return_value = self.features(60)
        engine._apply_news = Mock()
        result = engine.analyze('TEST')
        self.assertEqual(result.state, DecisionState.REJECTED)
        self.assertFalse(result.ready_for_execution)
        self.assertIn('freshness', result.reason)
        engine._apply_news.assert_not_called()

    def test_missing_timestamp_does_not_break_rejection_when_evidence_is_captured(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine._load_external_research = Mock(return_value={})
        bars = pd.DataFrame({'close': [10]})
        engine.market_data = Mock()
        engine.market_data.get_bars.return_value = bars
        engine.feature_engine = Mock()
        diagnostics = {'valid_rows': 60, 'required_rows': 51, 'missing_columns': []}
        engine.feature_engine.diagnose_input.return_value = diagnostics
        engine._smart_backfill_bars = Mock(return_value=(bars, diagnostics, None))
        features = self.features(1)
        features.timestamp = None
        engine.feature_engine.build.return_value = features
        result = engine.analyze('TEST')
        self.assertEqual(result.state, DecisionState.REJECTED)
        self.assertIn('freshness', result.reason)
        self.assertEqual(result.metadata['decision_features'], {})


if __name__ == '__main__':
    unittest.main()
