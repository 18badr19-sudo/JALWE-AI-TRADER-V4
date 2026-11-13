import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from intelligence.opportunity_engine import (
    OpportunityEngine, OpportunityAnalysis, OpportunityGrade,
    StrategyCandidate, StrategyName,
)
from intelligence.strategy_router import StrategyRouter, get_strategy_router
from intelligence.session_strategy_engine import SessionStrategyEngine, SessionStrategyCandidate, SessionStrategyName
from intelligence.trigger_engine import TriggerEngine
from market.market_regime import MarketRegime, MarketRegimeResult


class StrategySafetyTests(unittest.TestCase):
    def test_paper_bear_boundary_risk_and_evidence(self):
        router = get_strategy_router(paper_trading=True, allow_live_trading=False)
        for strategy in (StrategyName.MOMENTUM_BREAKOUT, StrategyName.PULLBACK_CONTINUATION):
            for score, approved in ((58, False), (94.99, False), (95, True), (100, True)):
                with self.subTest(strategy=strategy, score=score):
                    opportunity = self.opportunity()
                    opportunity.score = score
                    opportunity.strategy = strategy
                    opportunity.candidates = [StrategyCandidate(strategy, score, 10, 9)]
                    original = copy.deepcopy(opportunity)
                    result = router.route(opportunity, MarketRegimeResult(MarketRegime.BEAR_TREND, .9))
                    self.assertEqual(result.approved, approved)
                    self.assertEqual(result.minimum_score, 90)
                    self.assertEqual(result.final_risk_pct, .5 if approved else 0)
                    self.assertEqual(result.candidates[0].metadata['regime_adjustment'], -5)
                    self.assertEqual(result.candidates[0].metadata['routing_policy'], 'PAPER_BEAR_95_V1')
                    self.assertEqual(opportunity, original)

    def test_paper_policy_never_leaks_to_live_or_ambiguous_config(self):
        for paper, live in ((False, False), (True, True), (False, True), ('true', False), (True, 'false'), (1, 0)):
            result = get_strategy_router(paper_trading=paper, allow_live_trading=live).route(
                self.opportunity(), MarketRegimeResult(MarketRegime.BEAR_TREND, .9))
            self.assertFalse(result.approved)
        self.assertFalse(get_strategy_router().paper_bear_policy)

    def test_paper_policy_preserves_other_regimes_and_rejection_gates(self):
        router = StrategyRouter(paper_trading=True, allow_live_trading=False)
        for regime in MarketRegime:
            market = MarketRegimeResult(regime, .9)
            if regime != MarketRegime.BEAR_TREND:
                self.assertEqual(router.route(self.opportunity(), market),
                                 StrategyRouter().route(self.opportunity(), market))
            rejected = self.opportunity()
            rejected.approved = False
            self.assertFalse(router.route(rejected, market).approved)
        for field, values in (('recommended_risk_pct', (0, float('nan'), float('inf'))),
                              ('score', (float('nan'), float('inf')))):
            for value in values:
                opportunity = self.opportunity()
                setattr(opportunity, field, value)
                self.assertFalse(router.route(opportunity, MarketRegimeResult(MarketRegime.BEAR_TREND, .9)).approved)
        opportunity = self.opportunity()
        opportunity.recommended_risk_pct = .2
        self.assertEqual(router.route(opportunity, MarketRegimeResult(MarketRegime.BEAR_TREND, .9)).final_risk_pct, .2)
        self.assertFalse(router.route(self.opportunity(), MarketRegimeResult(MarketRegime.BEAR_TREND, .49)).approved)

    def test_bear_exception_requires_perfect_native_score(self):
        for strategy in (StrategyName.MOMENTUM_BREAKOUT, StrategyName.PULLBACK_CONTINUATION):
            for score, approved in ((99.99, False), (100, True)):
                opportunity = self.opportunity()
                opportunity.strategy = strategy
                opportunity.score = score
                opportunity.candidates = [StrategyCandidate(strategy, score, 10, 9)]
                result = StrategyRouter().route(opportunity,
                    MarketRegimeResult(MarketRegime.BEAR_TREND, .9))
                self.assertEqual(result.approved, approved)
                self.assertEqual(result.minimum_score, 90)
                self.assertLessEqual(result.final_risk_pct, .75)
                self.assertEqual(result.candidates[0].metadata['raw_strategy_score'], score)
                self.assertEqual(opportunity.candidates[0].score, score)

    def test_valid_setup_still_routes_and_confirms_entry(self):
        routed = StrategyRouter().route(self.opportunity(),
            MarketRegimeResult(MarketRegime.BULL_TREND, .9))
        self.assertTrue(routed.approved)
        self.assertGreater(routed.final_risk_pct, 0)
        result = TriggerEngine().evaluate(routed, current_price=10.01)
        self.assertTrue(result.approved_for_entry)

    def test_session_nonfinite_scores_do_not_become_high_scores(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            self.assertEqual(SessionStrategyEngine._clamp(value), 0)
            self.assertEqual(SessionStrategyEngine._num(value, -1), -1)

    def test_session_rejects_invalid_candidate_score(self):
        engine = SessionStrategyEngine()
        for method in ('_premarket_high_break', '_opening_range_breakout',
                       '_gap_and_go', '_compression_breakout'):
            setattr(engine, method, Mock(return_value=SessionStrategyCandidate(
                SessionStrategyName.GAP_AND_GO, float('inf'), True, 10, 9)))
        self.assertFalse(engine.evaluate(SimpleNamespace(symbol='TEST'),
            SimpleNamespace(symbol='TEST')).approved)

    def test_nonfinite_trigger_levels_are_rejected(self):
        for field in ('trigger_price', 'invalidation_price'):
            analysis = SimpleNamespace(symbol='TEST', approved=True,
                trigger_price=10, invalidation_price=9)
            setattr(analysis, field, float('inf'))
            result = TriggerEngine().evaluate(analysis, current_price=10)
            self.assertFalse(result.approved_for_entry)
            self.assertIn('unavailable', result.reason)

    def test_invalid_latest_close_is_not_used_as_price(self):
        for value in (float('nan'), float('inf'), -float('inf'), 'bad'):
            self.assertIsNone(TriggerEngine._extract_last_price(
                pd.DataFrame({'close': [10, value]})))

    def opportunity(self):
        candidate = StrategyCandidate(StrategyName.MOMENTUM_BREAKOUT, 95, 10, 9)
        return OpportunityAnalysis('TEST', True, candidate.strategy, 95,
            OpportunityGrade.A_PLUS, 1.0, 10, 9, candidates=[candidate])

    def test_invalid_price_cannot_produce_approved_opportunity(self):
        for price in (float('nan'), float('inf'), -float('inf')):
            f = SimpleNamespace(symbol='TEST', data_quality_ok=True,
                data_is_stale=False, price=price, rvol=4,
                volume_acceleration=3, liquidity_score=100, momentum_score=100,
                above_vwap=True, high_20=10, atr=1, news_score=90,
                options_flow_score=90)
            with self.subTest(price=price):
                result = OpportunityEngine().evaluate(f)
                self.assertFalse(result.approved)
                self.assertEqual(result.recommended_risk_pct, 0)

    def test_nonfinite_evidence_is_missing(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            self.assertEqual(OpportunityEngine._num(value, default=-1), -1)

    def test_invalid_confidence_blocks_routing(self):
        for confidence in (float('nan'), float('inf'), -1, 1.1):
            with self.subTest(confidence=confidence):
                result = StrategyRouter().route(self.opportunity(),
                    MarketRegimeResult(MarketRegime.BULL_TREND, confidence))
                self.assertFalse(result.approved)
                self.assertEqual(result.final_risk_pct, 0)

    def test_invalid_risk_blocks_routing(self):
        for risk in (float('nan'), float('inf'), -1, 0):
            opportunity = self.opportunity()
            opportunity.recommended_risk_pct = risk
            with self.subTest(risk=risk):
                self.assertFalse(StrategyRouter().route(opportunity,
                    MarketRegimeResult(MarketRegime.BULL_TREND, .9)).approved)

    def test_invalid_candidate_score_cannot_become_perfect_score(self):
        for score in (float('nan'), float('inf'), -float('inf')):
            opportunity = self.opportunity()
            opportunity.candidates[0].score = score
            result = StrategyRouter().route(opportunity,
                MarketRegimeResult(MarketRegime.BULL_TREND, .9))
            self.assertFalse(result.approved)
            self.assertEqual(result.final_risk_pct, 0)

    def test_rejected_opportunity_never_revived_by_regime(self):
        for regime in MarketRegime:
            opportunity = self.opportunity()
            opportunity.approved = False
            result = StrategyRouter().route(opportunity, MarketRegimeResult(regime, .9))
            self.assertFalse(result.approved)
            self.assertEqual(result.final_risk_pct, 0)

    def test_regime_routing_caps_risk_and_preserves_input(self):
        for regime in MarketRegime:
            opportunity = self.opportunity()
            original = copy.deepcopy(opportunity)
            result = StrategyRouter().route(opportunity, MarketRegimeResult(regime, .9))
            self.assertGreaterEqual(result.final_risk_pct, 0)
            self.assertLessEqual(result.final_risk_pct, opportunity.recommended_risk_pct)
            self.assertLessEqual(result.final_risk_pct, 1.5)
            self.assertEqual(opportunity, original)
            if regime in (MarketRegime.PANIC, MarketRegime.UNKNOWN):
                self.assertFalse(result.approved)


if __name__ == '__main__':
    unittest.main()
