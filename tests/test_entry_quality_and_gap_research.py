import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pandas as pd

from market.gap_evidence import probe_minute
from market.research_diagnostics import coverage_diagnostics, coverage_text
from market.market_data import MarketData
from research_variants import entry_stop_matrix
from trading.entry_quality import assess_entry
from intelligence.decision_engine import DecisionState
from intelligence.breakout_confirmation_engine import BreakoutConfirmationState
import test_strategy_experiments as fixtures


class GapEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)

    def trade(self, conditions):
        return dict(t=self.at.isoformat(), p=1.28, s=10, c=conditions)

    def sample(self, rows):
        return probe_minute('CMTL', self.at, 'sip', Mock(return_value={'trades': {'CMTL': rows}}))

    def test_empty_null_and_odd_lots_are_distinct(self):
        self.assertEqual(self.sample([])['classification'], 'NO_TRADES_IN_FEED')
        null = probe_minute('CMTL', self.at, 'sip', Mock(return_value={'trades': None}))
        self.assertEqual(null['classification'], 'NO_TRADES_IN_FEED')
        self.assertEqual(self.sample([self.trade(['@', 'I'])])['classification'], 'ONLY_NON_PRICE_UPDATING_TRADES')
        self.assertEqual(self.sample([self.trade(['@'])])['classification'], 'REGULAR_TRADES_WITHOUT_BAR')

    def test_unknown_conditions_and_bad_prices_cannot_prove_no_trades(self):
        for row in (self.trade(['T']), self.trade([]), self.trade(['unknown']),
                    {**self.trade(['@']), 'p': float('nan')},
                    {**self.trade(['@']), 't': '2026-10-12T16:00:00'}):
            self.assertEqual(self.sample([row])['classification'], 'UNKNOWN_TRADE_CONDITIONS')

    def test_pagination_limits_and_repeated_tokens_are_inconclusive(self):
        fetch = Mock(return_value={'trades': {'CMTL': []}, 'next_page_token': 'same'})
        result = probe_minute('CMTL', self.at, 'sip', fetch)
        self.assertFalse(result['complete'])
        self.assertEqual(result['classification'], 'INCOMPLETE_TRADE_READ')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[0]['feed'], 'sip')
        self.assertEqual(fetch.call_args.args[0]['page_token'], 'same')

    def test_boundary_trade_is_not_attributed_to_missing_minute(self):
        result = self.sample([{**self.trade(['@']), 't': (self.at + timedelta(minutes=1)).isoformat()}])
        self.assertEqual(result['count'], 0)

    def test_malformed_payloads_and_requests_do_not_prove_coverage(self):
        for payload in ({}, {'trades': []}, {'trades': {'CMTL': None}}):
            with self.assertRaises(ValueError):
                probe_minute('CMTL', self.at, 'sip', Mock(return_value=payload))
        with self.assertRaises(TimeoutError):
            probe_minute('CMTL', self.at, 'sip', Mock(side_effect=TimeoutError()))

    def test_sample_never_fills_bars_or_promotes_a_window(self):
        frame = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'],
                             index=pd.DatetimeIndex([], tz='UTC'))
        frame.attrs['gap_trade_evidence'] = [self.sample([])]
        d = coverage_diagnostics(frame, frame, self.at, self.at + timedelta(minutes=60))
        self.assertEqual(d['missing_minutes'], 60)
        self.assertEqual(d['observed_minutes'], 0)
        text = coverage_text([{'result_json': json.dumps({'data_diagnostics': d})}], 'result_json')
        self.assertIn('بلا صفقات في المصدر 1', text)
        self.assertIn('العينة لا تفسر بقية الدقائق', text)
        self.assertTrue(frame.empty)

    def test_historical_probe_is_delayed_bounded_and_cached(self):
        md = MarketData.__new__(MarketData)
        frame = pd.DataFrame(index=pd.DatetimeIndex([], tz='UTC'))
        end = self.at + timedelta(minutes=60)
        with patch('urllib.request.urlopen') as request:
            before = md.diagnose_missing_minute('CMTL', frame, self.at, end, feed='sip', as_of=end)
            self.assertEqual(before[0]['classification'], 'WAITING_FOR_HISTORICAL_DELAY')
            request.assert_not_called()
            with patch('market.gap_evidence.probe_minute', return_value=self.sample([])) as probe:
                for _ in range(2):
                    md.diagnose_missing_minute('CMTL', frame, self.at, end, feed='sip', as_of=end + timedelta(minutes=16))
                probe.assert_called_once()


class EntryQualityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
        self.features = NS(rvol=2, volume=1000, dollar_volume=10000, liquidity_score=50)
        self.quote = dict(bid=6.44, ask=6.45, timestamp=self.now, feed='iex')

    def assess(self, **changes):
        args = dict(features=self.features, quote=self.quote, entry=6.45, stop=6.07,
                    feed='iex', now=self.now, min_rvol=1.3, max_spread_pct=1)
        args.update(changes)
        return assess_entry(**args)

    def test_fresh_healthy_activity_passes(self):
        self.assertTrue(self.assess()['approved'])

    def test_thin_risk_distance_rejects_even_small_percentage_spread(self):
        result = self.assess(entry=6.45, stop=6.42)
        self.assertEqual(result['reason'], 'SPREAD_TOO_LARGE_FOR_RISK')

    def test_weak_or_missing_current_activity_rejects(self):
        for field, value in (('rvol', 1), ('volume', None), ('dollar_volume', float('nan')), ('liquidity_score', 0)):
            features = copy.copy(self.features)
            setattr(features, field, value)
            self.assertFalse(self.assess(features=features)['approved'])

    def test_stale_future_wrong_feed_and_invalid_quotes_reject(self):
        for changes in (dict(timestamp=self.now - timedelta(seconds=31)),
                        dict(timestamp=self.now + timedelta(seconds=6)), dict(feed='sip'),
                        dict(bid=7), dict(ask=float('inf')), dict(timestamp=None)):
            self.assertFalse(self.assess(quote={**self.quote, **changes})['approved'])

    def test_submission_rechecks_quote_expiry(self):
        from trading.execution_engine import ExecutionEngine
        from test_paper_safety import ready_decision
        decision = ready_decision()
        decision.gates['entry_quality'] = True
        decision.metadata = {'entry_quality': dict(approved=True,
            quote_timestamp=(datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat())}
        self.assertIn('expired', ExecutionEngine._final_decision_reject_reason(decision))
        decision.metadata['entry_quality']['quote_timestamp'] = datetime.now(timezone.utc).isoformat()
        self.assertIsNone(ExecutionEngine._final_decision_reject_reason(decision))
        decision.metadata['entry_quality']['approved'] = False
        self.assertIn('not approved', ExecutionEngine._final_decision_reject_reason(decision))


class CurrentLatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StrategyExperimentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine = self.fixture.native_engine(waiting=True)
        self.now = self.fixture.now
        self.engine.risk_engine.evaluate.return_value = NS(approved=True, reason='ok', metadata={},
            risk_pct=1, quantity=2, stop_price=9.1, target_1=12.4, target_2=13.5, target_3=14.6)
        features = self.engine.feature_engine.build.return_value
        features.rvol, features.volume, features.dollar_volume, features.liquidity_score = 2, 1000, 10000, 50
        self.engine.market_data.get_latest_quote = Mock(return_value=dict(
            bid=10.19, ask=10.2, timestamp=self.now, feed='iex'))

    def analyze(self):
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch('intelligence.decision_engine.datetime', Clock), \
             patch('intelligence.decision_engine.database.save_feature_snapshot'), \
             patch('strategy_experiments.get_strategy_experiments', return_value=self.fixture.engine):
            return self.engine.analyze('TEST')

    def confirm(self):
        first = self.analyze()
        self.assertEqual(first.state, DecisionState.WATCHING)
        breakout = self.engine.breakout_engine.evaluate.return_value
        breakout.approved_for_entry = True
        breakout.state = BreakoutConfirmationState.CONFIRMED

    def test_current_trigger_failure_is_not_reported_as_quality_deterioration(self):
        self.confirm()
        candidate=self.engine.session_strategy_engine.evaluate.return_value.candidates[0]
        candidate.trigger_price=10.3
        decision=self.analyze()
        self.assertEqual(decision.reason,'Latched entry has not reached the current candidate trigger.')
        self.assertTrue(all(decision.gates[k] for k in ('ai','opportunity','strategy_router','session_strategy')))
        self.assertFalse(decision.gates['trigger'])
        self.assertEqual(decision.metadata['latched_quality_refresh']['failure_code'],'CURRENT_TRIGGER_NOT_REACHED')
        self.engine.risk_engine.evaluate.assert_not_called()
        self.assertFalse(decision.ready_for_execution)

    def test_current_invalidation_is_reported_separately_and_keeps_gates_consistent(self):
        self.confirm()
        candidate=self.engine.session_strategy_engine.evaluate.return_value.candidates[0]
        candidate.trigger_price=10.3
        candidate.invalidation_price=10.25
        decision=self.analyze()
        self.assertEqual(decision.reason,'Latched current candidate was invalidated before entry.')
        evidence=decision.metadata['latched_quality_refresh']
        self.assertEqual(evidence['failure_code'],'CURRENT_CANDIDATE_INVALIDATED')
        self.assertFalse(evidence['gates']['session_strategy'])
        self.assertFalse(decision.gates['session_strategy'])
        self.engine.risk_engine.evaluate.assert_not_called()

    def test_refresh_exception_is_not_reported_as_a_measured_quality_rejection(self):
        self.confirm()
        self.engine.ai_engine.evaluate.side_effect=TimeoutError('fixture')
        decision=self.analyze()
        self.assertEqual(decision.reason,'Latched current quality refresh could not be completed.')
        self.assertEqual(decision.metadata['latched_quality_refresh']['failure_code'],'NATIVE_QUALITY_REFRESH_ERROR')
        self.assertFalse(decision.ready_for_execution)
        self.engine.risk_engine.evaluate.assert_not_called()

    def test_ai_deterioration_cannot_reuse_old_buy_permission(self):
        self.confirm()
        self.engine.ai_engine.evaluate.return_value = NS(action='SELL', score=40)
        decision = self.analyze()
        self.assertEqual(decision.state, DecisionState.REJECTED)
        self.assertFalse(decision.gates['ai'])
        self.engine.risk_engine.evaluate.assert_not_called()
        self.assertEqual(self.engine._breakout_latches, {})

    def test_opportunity_deterioration_blocks_before_risk(self):
        self.confirm()
        self.engine.opportunity_engine.evaluate.return_value.approved = False
        self.engine.opportunity_engine.evaluate.return_value.score = 49
        decision = self.analyze()
        self.assertFalse(decision.ready_for_execution)
        self.assertFalse(decision.gates['opportunity'])
        self.assertEqual(decision.metadata['latched_quality_refresh']['opportunity_score'], 49)
        self.engine.risk_engine.evaluate.assert_not_called()

    def test_original_candidate_must_remain_eligible(self):
        self.confirm()
        self.engine.session_strategy_engine.evaluate.return_value.candidates[0].valid = False
        decision = self.analyze()
        self.assertFalse(decision.ready_for_execution)
        self.assertFalse(decision.gates['session_strategy'])

    def test_healthy_confirmed_latch_uses_current_ai_and_caps_risk(self):
        self.confirm()
        self.engine.strategy_router.route.return_value.final_risk_pct = 2
        decision = self.analyze()
        self.assertTrue(decision.ready_for_execution)
        self.assertTrue(decision.gates['entry_quality'])
        self.assertEqual(self.engine.risk_engine.evaluate.call_args.kwargs['recommended_risk_pct'], 1)
        self.assertIs(self.engine.risk_engine.evaluate.call_args.kwargs['ai_analysis'],
                      self.engine.ai_engine.evaluate.return_value)

    def test_unavailable_macro_quote_and_current_activity_block(self):
        self.confirm()
        self.engine.market_data.get_latest_quote.side_effect = TimeoutError()
        self.assertFalse(self.analyze().ready_for_execution)
        self.engine.market_data.get_latest_quote.assert_called_once()

    def test_unavailable_macro_does_not_reuse_old_regime(self):
        self.confirm()
        self.engine.market_context_engine.get_regime_result.side_effect = TimeoutError()
        decision = self.analyze()
        self.assertFalse(decision.ready_for_execution)
        self.assertFalse(decision.gates['market_regime'])
        self.engine.risk_engine.evaluate.assert_not_called()

    def test_normal_entry_also_requires_current_quality(self):
        self.engine.breakout_engine.evaluate.return_value.approved_for_entry = True
        self.engine.breakout_engine.evaluate.return_value.state = BreakoutConfirmationState.CONFIRMED
        self.engine.feature_engine.build.return_value.rvol = .5
        decision = self.analyze()
        self.assertFalse(decision.ready_for_execution)
        self.assertEqual(decision.metadata['entry_quality']['reason'], 'CURRENT_RVOL_BELOW_MIN')


class FixedRiskResearchTests(unittest.TestCase):
    def bars(self):
        return pd.DataFrame([[10, 10.4, 9.5, 10.2, 1], [10.3, 10.5, 10.1, 10.4, 1],
                             [10.1, 10.3, 9.99, 10.2, 1], [10.3, 10.5, 10.2, 10.4, 1],
                             [11, 12.1, 10.9, 12, 1]],
                            columns=['open', 'high', 'low', 'close', 'volume'],
                            index=pd.date_range('2026-10-12T14:00Z', periods=5, freq='min'))

    def context(self):
        return dict(available_at_decision=True, atr=.4, atr_timeframe='5m', model_low=8.8)

    def test_stop_widening_keeps_same_budget_and_reduces_quantity(self):
        result = entry_stop_matrix(self.bars(), 10, 9.4, self.context())
        self.assertFalse(result['auto_apply'])
        self.assertEqual(len(result['rules']), 4)
        current = result['rules']['DIRECT_CURRENT']
        wider = result['rules']['DIRECT_STRUCTURE_ATR']
        self.assertLess(wider['quantity'], current['quantity'])
        self.assertTrue(all(r['planned_risk_dollars'] <= 1 for r in result['rules'].values()))
        self.assertAlmostEqual(wider['stop'], 8.7)

    def test_future_low_cannot_be_used_for_stop_context(self):
        self.assertEqual(entry_stop_matrix(self.bars(), 10, 9, {})['outcome'], 'MISSING_FROZEN_STOP_CONTEXT')
        result = entry_stop_matrix(self.bars(), 10, 9.4, self.context())
        changed = self.bars()
        changed.iloc[-1, changed.columns.get_loc('low')] = 1
        later = entry_stop_matrix(changed, 10, 9.4, self.context())
        self.assertEqual(result['rules']['DIRECT_STRUCTURE_ATR']['stop'], later['rules']['DIRECT_STRUCTURE_ATR']['stop'])

    def test_same_bar_ambiguity_remains_unknown(self):
        bars = self.bars()
        bars.iloc[1] = [10.3, 14, 8, 11, 1]
        result = entry_stop_matrix(bars, 10, 9.4, self.context())
        self.assertIsNone(result['rules']['DIRECT_CURRENT']['net_pnl_dollars'])

    def test_no_retest_does_not_fake_a_trade(self):
        bars = self.bars()
        bars['low'] = 10.2
        result = entry_stop_matrix(bars, 10, 9.4, self.context())
        self.assertFalse(result['rules']['RETEST_CURRENT']['traded'])
        self.assertEqual(result['rules']['RETEST_CURRENT']['quantity'], 0)
