"""AI component contract: synthetic snapshots and a frozen learning stub only."""
from copy import deepcopy
from dataclasses import replace
import math
import unittest
from unittest.mock import Mock, patch

from core.models import FeatureSnapshot, MarketRegime, SignalAction
from intelligence.ai_engine import AIEngine


def features(**changes):
    values = dict(symbol='TEST', data_quality_ok=True, data_is_stale=False,
        price=10., vwap=9.98, above_vwap=True, distance_from_vwap_pct=.2,
        ema_9=9.9, ema_20=9.8, momentum_score=85., rsi_14=60.,
        distance_to_high_20_pct=.1, range_compression=.2,
        news_score=80., options_flow_score=80., liquidity_score=90., rvol=3.,
        market_regime=MarketRegime.BULL_TREND)
    return FeatureSnapshot(**{**values, **changes})


class AIInputEvidenceTests(unittest.TestCase):
    def setUp(self):
        learner = Mock(enabled=False)
        learner.apply.side_effect = lambda factor, points: points
        learner.profile.return_value = {'fixture': 1.0}
        with patch('intelligence.ai_engine.get_learning_engine', return_value=learner):
            self.engine = AIEngine(minimum_buy_score=74.)
        self.learner = learner

    def test_invalid_mandatory_price_never_buys_even_with_quality_flag(self):
        for price in (None, float('nan'), float('inf'), -float('inf'), -1, 0, True, '10', {}, []):
            with self.subTest(price=price):
                result = self.engine.evaluate(features(price=price))
                self.assertEqual(result.action, SignalAction.REJECT)
                self.assertEqual((result.score, result.confidence), (0, 0))
                self.assertIn('price', result.metadata['invalid_inputs'])
        self.learner.apply.assert_not_called()

    def test_invalid_optional_inputs_are_missing_not_negative_or_positive_evidence(self):
        for name in ('vwap', 'ema_9', 'ema_20', 'liquidity_score', 'rvol',
                     'momentum_score', 'rsi_14', 'news_score', 'options_flow_score',
                     'range_compression', 'distance_to_high_20_pct', 'distance_from_vwap_pct'):
            for value in (float('nan'), float('inf'), -float('inf'), True, '80'):
                with self.subTest(name=name, value=value):
                    bad = self.engine.evaluate(features(**{name: value}))
                    missing = self.engine.evaluate(features(**{name: None}))
                    self.assertEqual((bad.score, bad.confidence, bad.action),
                                     (missing.score, missing.confidence, missing.action))
                    self.assertIn(name, bad.metadata['invalid_inputs'])
                    self.assertTrue(math.isfinite(bad.score))

    def test_out_of_domain_values_are_excluded(self):
        for name in ('liquidity_score', 'momentum_score', 'rsi_14', 'news_score', 'options_flow_score'):
            for value in (-.001, 100.001):
                r = self.engine.evaluate(features(**{name: value}))
                self.assertIn(name, r.metadata['invalid_inputs'])
                self.assertLess(r.confidence, 1.)
        for name in ('rvol', 'range_compression', 'vwap', 'ema_9', 'ema_20'):
            self.assertIn(name, self.engine.evaluate(features(**{name: -1})).metadata['invalid_inputs'])

    def test_zero_scores_and_signed_distances_are_valid(self):
        f = features(liquidity_score=0, rvol=0, momentum_score=0, rsi_14=0,
                     news_score=0, options_flow_score=0, range_compression=0,
                     distance_to_high_20_pct=-5, distance_from_vwap_pct=-3)
        r = self.engine.evaluate(f)
        self.assertEqual(r.metadata['invalid_inputs'], [])
        self.assertEqual(r.confidence, 1.)

    def test_nonfinite_features_cannot_claim_full_coverage(self):
        f = features(**{name: float('nan') for name in (
            'vwap','ema_9','ema_20','liquidity_score','rvol','momentum_score',
            'rsi_14','news_score','options_flow_score','range_compression',
            'distance_to_high_20_pct','distance_from_vwap_pct')},
            market_regime=MarketRegime.UNKNOWN)
        r = self.engine.evaluate(f)
        self.assertEqual((r.confidence, r.score, r.metadata['evidence_count']), (0,0,0))

    def test_unknown_vwap_side_does_not_mean_below_vwap(self):
        for side in (None, 1, 'true'):
            r = self.engine.evaluate(features(above_vwap=side))
            self.assertFalse(any(x['factor']=='vwap' for x in r.metadata['factor_breakdown']))
            self.assertEqual(r.confidence, .9)

    def test_metadata_rvol_cannot_poison_or_inflate_score(self):
        for value in (float('nan'), float('inf'), -1, True, '3', {}):
            r = self.engine.evaluate(features(rvol=.5, metadata={'scoring_rvol':value}))
            baseline = self.engine.evaluate(features(rvol=.5))
            self.assertEqual((r.score, r.confidence), (baseline.score, baseline.confidence))
            self.assertIn('metadata.scoring_rvol', r.metadata['invalid_inputs'])
        self.assertEqual(self.engine.evaluate(features(rvol=.5,metadata={'scoring_rvol':3})).score,100.)

    def test_evaluation_does_not_mutate_shared_snapshot_or_nested_metadata(self):
        f=features(news_score=True,metadata={'scoring_rvol':float('inf'),'nested':{'preserve':1}})
        before=deepcopy(f)
        self.engine.evaluate(f)
        self.assertEqual(f,before)

    def test_breakdown_reconstructs_score_with_caps_and_bearish_discount(self):
        for f in (features(), features(liquidity_score=10,news_score=20,rsi_14=90),
                  features(ema_9=11,ema_20=12,above_vwap=False,market_regime=MarketRegime.PANIC)):
            r = self.engine.evaluate(f)
            rows=r.metadata['factor_breakdown']
            bull=sum(x['weighted_points'] for x in rows if x['side']=='bullish')
            bear=sum(x['weighted_points'] for x in rows if x['side']=='bearish')
            expected=round(max(0,min(100,min(bull,100)-.7*min(bear,100))),2)
            self.assertEqual(r.score,expected)
            self.assertEqual((bull,bear),(r.metadata['raw_bullish_score'],r.metadata['raw_bearish_score']))
            self.assertEqual(r.metadata['minimum_buy_score'],74.)
            self.assertEqual(r.metadata['probability_kind'],'SCORE_PROXY_NOT_CALIBRATED')
            self.assertEqual(r.metadata['confidence_kind'],'FEATURE_COVERAGE')

    def test_learned_weights_record_actual_contribution(self):
        self.learner.apply.side_effect=lambda factor,points:points*1.1
        r=self.engine.evaluate(features())
        self.assertTrue(all(math.isclose(x['weighted_points'],x['base_points']*1.1)
                            for x in r.metadata['factor_breakdown']))
        self.learner.apply.return_value=float('nan'); self.learner.apply.side_effect=None
        with self.assertRaisesRegex(ValueError,'Invalid learned factor'):
            self.engine.evaluate(features())

    def test_invalid_thresholds_fail_before_learning_initialization(self):
        for value in (True, '74', float('inf'), float('nan'), -1, 101):
            with self.subTest(value=value), patch('intelligence.ai_engine.get_learning_engine') as learner:
                with self.assertRaises(ValueError): AIEngine(minimum_buy_score=value)
                learner.assert_not_called()

    def test_quality_and_staleness_still_reject(self):
        for f in (features(data_is_stale=True),features(data_quality_ok=False)):
            r=self.engine.evaluate(f)
            self.assertEqual((r.action,r.score,r.confidence),(SignalAction.REJECT,0,0))


if __name__=='__main__': unittest.main()
