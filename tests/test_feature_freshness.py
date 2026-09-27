import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from core.models import FeatureSnapshot
from intelligence.decision_engine import DecisionEngine, DecisionState


class FeatureFreshnessTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
