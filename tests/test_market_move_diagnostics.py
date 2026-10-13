from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from market_move_diagnostics import VERSION, observation_due, diagnose_gaps, aggregate, emit_evidence
from market_move_provider import MoveProvider, ResearchReadError
from market_move_research import MoveStore, observe_session, utc, read_packet, report_text
from market_move_research_loop import MoveResearchWorker
from market.research_diagnostics import error_diagnostics
import test_market_move_research as fixtures


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MoveStore(Path(self.temp.name)/'moves.db')
        self.now = utc('2026-10-12T21:00:00Z')
        self.store.add(fixtures.feature(), fixtures.session(), 'HOT_CASE', 'pair', self.now, prospective=False)
        self.row = self.store.episodes()[0]

    def test_future_cohort_waits_until_delayed_session_close(self):
        self.assertFalse(observation_due(self.row, utc('2026-10-12T19:00:00Z')))
        self.assertFalse(observation_due(self.row, utc('2026-10-12T20:15:00Z')))
        self.assertTrue(observation_due(self.row, utc('2026-10-12T20:16:00Z')))

    def test_retries_require_interval_and_stop_after_three(self):
        result = observe_session(fixtures.frame().iloc[1:], fixtures.session())
        self.store.settle(self.row['episode_key'], result, self.now)
        row = self.store.episodes()[0]
        self.assertFalse(observation_due(row, self.now+timedelta(minutes=14)))
        self.assertTrue(observation_due(row, self.now+timedelta(minutes=15)))
        self.assertFalse(self.store.settle(row['episode_key'], result, self.now, retry=True))
        self.assertTrue(self.store.settle(row['episode_key'], result, self.now+timedelta(minutes=15), retry=True))
        self.assertTrue(self.store.settle(row['episode_key'], result, self.now+timedelta(minutes=30), retry=True))
        self.assertFalse(self.store.settle(row['episode_key'], result, self.now+timedelta(hours=1), retry=True))
        self.assertEqual(self.store.episodes()[0]['attempts'], 3)

    def test_legacy_upgrade_is_once_and_horizon_is_bounded(self):
        self.store.settle(self.row['episode_key'], {'status':'PARTIAL_DATA','outcome':None}, self.now)
        row = self.store.episodes()[0]
        self.assertTrue(observation_due(row, self.now))
        self.assertFalse(observation_due(row, self.now+timedelta(days=3)))
        self.store.settle(row['episode_key'], observe_session(fixtures.frame().iloc[1:], fixtures.session()), self.now, retry=True)
        self.assertFalse(observation_due(self.store.episodes()[0], self.now))

    def test_recovered_real_bars_preserve_features_and_complete_is_immutable(self):
        missing = observe_session(fixtures.frame().iloc[1:], fixtures.session())
        complete = observe_session(fixtures.frame(), fixtures.session())
        self.store.settle(self.row['episode_key'], missing, self.now)
        self.assertTrue(self.store.settle(self.row['episode_key'], complete, self.now+timedelta(minutes=15), retry=True))
        row = self.store.episodes()[0]
        self.assertEqual(row['features_json'], self.row['features_json'])
        self.assertEqual(row['result']['observed_minutes'], 390)
        self.assertFalse(self.store.settle(row['episode_key'], missing, self.now+timedelta(hours=1), retry=True))


class GapDiagnosticsTests(unittest.TestCase):
    def test_outside_session_duplicates_do_not_invalidate_regular_coverage(self):
        session = fixtures.session(); raw = fixtures.frame(session)
        for stamp in (utc(session['open'])-timedelta(minutes=1),utc(session['close'])):
            extra = raw.iloc[:1].copy(); extra.index = pd.DatetimeIndex([stamp])
            result = observe_session(pd.concat([raw,extra,extra]),session)
            self.assertEqual(result['status'],'COMPLETE')
            self.assertEqual(result['observed_minutes'],390)
            self.assertEqual(result['duplicates'],0)
            self.assertEqual(result['data_diagnostics']['duplicate_rows'],0)

    def test_closing_duplicates_do_not_discard_valid_pre_gap_context(self):
        session = fixtures.session(); raw = fixtures.frame(session)
        raw.iloc[25,1] = 11.; raw = raw.drop(raw.index[60])
        extra = raw.iloc[:1].copy(); extra.index = pd.DatetimeIndex([utc(session['close'])])
        baseline = observe_session(raw,session)
        result = observe_session(pd.concat([raw,extra,extra]),session)
        self.assertEqual(result['status'],'PARTIAL_DATA')
        self.assertEqual(result['missing_minutes'],1)
        self.assertEqual(result['prefix_context'],baseline['prefix_context'])

    def test_completed_prefix_can_describe_a_move_without_promoting_the_session(self):
        raw = fixtures.frame(); raw.iloc[25,1] = 11.; raw = raw.drop(raw.index[60])
        result = observe_session(raw,fixtures.session())
        prefix = result['prefix_context']
        self.assertEqual(result['status'],'PARTIAL_DATA'); self.assertIsNone(result['outcome'])
        self.assertEqual(prefix['bars'],60)
        self.assertEqual(prefix['observed_barrier_order'],'UP8_FIRST')
        self.assertEqual(prefix['pre_move_context']['bars'],20)
        self.assertIn('PREFIX_ONLY',prefix['scope'])

    def test_prices_after_first_gap_cannot_change_prefix_evidence(self):
        raw = fixtures.frame(); raw.iloc[25,1] = 11.; raw = raw.drop(raw.index[60])
        first = observe_session(raw,fixtures.session())['prefix_context']
        raw.loc[raw.index>fixtures.frame().index[60],'high'] = 100
        self.assertEqual(first,observe_session(raw,fixtures.session())['prefix_context'])

    def test_missing_open_or_duplicate_evidence_cannot_prove_a_prefix(self):
        samples = [fixtures.frame().iloc[1:], pd.concat([fixtures.frame(),fixtures.frame().iloc[:1]])]
        for raw in samples:
            self.assertEqual(observe_session(raw,fixtures.session())['prefix_context']['status'],'UNAVAILABLE')

    def test_same_bar_prefix_crossing_remains_ambiguous(self):
        raw = fixtures.frame(); raw.iloc[25,1] = 11.; raw.iloc[25,2] = 9.6; raw = raw.drop(raw.index[60])
        self.assertEqual(observe_session(raw,fixtures.session())['prefix_context']['observed_barrier_order'],'AMBIGUOUS')

    def test_no_barrier_in_prefix_does_not_mean_no_barrier_in_the_day(self):
        raw = fixtures.frame(); raw.iloc[70,1] = 11.; raw = raw.drop(raw.index[60])
        result = observe_session(raw,fixtures.session())
        self.assertEqual(result['prefix_context']['observed_barrier_order'],'NO_BARRIER_OBSERVED_IN_PREFIX')
        self.assertIsNone(result['outcome'])

    def test_absent_and_rejected_minutes_have_separate_counts(self):
        raw = fixtures.frame(); raw.iloc[2,4] = 0; raw = raw.drop(raw.index[1])
        result = observe_session(raw, fixtures.session())
        diagnostics = result['data_diagnostics']
        self.assertEqual(diagnostics['absent_provider_minutes'], 1)
        self.assertEqual(diagnostics['rejected_minutes'], 1)
        self.assertEqual(result['missing_minutes'], 2)
        self.assertEqual(diagnostics['first_absent_minutes'], [fixtures.frame().index[1].isoformat()])
        self.assertIsNone(result['outcome'])

    def test_trade_samples_never_promote_or_change_price_coverage(self):
        result = observe_session(fixtures.frame().iloc[3:], fixtures.session())
        provider = Mock(); provider.gap_trades.side_effect = lambda symbol,minute,now:dict(
            minute=minute.isoformat(), complete=True, classification='NO_TRADES_IN_FEED')
        samples = diagnose_gaps(provider,'TEST',result,utc('2026-10-12T21:00:00Z'))
        self.assertEqual(len(samples), 2); self.assertEqual(provider.gap_trades.call_count, 2)
        self.assertEqual(result['status'], 'PARTIAL_DATA'); self.assertIsNone(result['outcome'])
        self.assertEqual(result['observed_minutes'], 387)
        cached = {s['minute']:s for s in samples}; provider.reset_mock()
        diagnose_gaps(provider,'TEST',result,utc('2026-10-12T21:00:00Z'),cached=cached)
        provider.gap_trades.assert_not_called()

    def test_probe_failures_stay_unavailable_without_private_exception_text(self):
        result = observe_session(fixtures.frame().iloc[1:], fixtures.session())
        provider = Mock(); provider.gap_trades.side_effect = TimeoutError('private headers')
        samples = diagnose_gaps(provider,'TEST',result,utc('2026-10-12T21:00:00Z'))
        self.assertEqual(samples[0]['classification'], 'TRADE_READ_UNAVAILABLE')
        self.assertNotIn('private', json.dumps(samples))

    def test_invalid_returned_bar_is_not_probed_as_absent(self):
        raw = fixtures.frame(); raw.iloc[0,4] = 0
        result = observe_session(raw, fixtures.session()); provider = Mock()
        self.assertEqual(diagnose_gaps(provider,'TEST',result,utc('2026-10-12T21:00:00Z')), [])
        provider.gap_trades.assert_not_called()

    def test_trade_endpoint_is_historical_delayed_sip_and_paginated(self):
        provider = MoveProvider('test','test',session=Mock(),interval=.5)
        minute = utc('2026-10-12T13:30:00Z')
        with patch.object(provider, 'get') as get:
            with self.assertRaises(ResearchReadError):
                provider.gap_trades('TEST',minute,minute+timedelta(minutes=16))
            get.assert_not_called()
        with patch.object(provider, 'get', return_value={'trades':{'TEST':[]}}) as get:
            result = provider.gap_trades('TEST',minute,minute+timedelta(minutes=17))
        self.assertEqual(result['classification'], 'NO_TRADES_IN_FEED')
        self.assertEqual(get.call_args.args[1]['feed'], 'sip')
        self.assertEqual(get.call_args.args[1]['end'], (minute+timedelta(minutes=1)).isoformat())

    def test_http_status_is_preserved_for_sanitized_failure_reason(self):
        result = error_diagnostics(ResearchReadError('HTTP_403', status_code=403))
        self.assertEqual(result['http_status'], 403)
        self.assertEqual(result['reason'], 'FORBIDDEN')


class WorkerDiagnosticTests(unittest.TestCase):
    setUp = RetryTests.setUp
    def test_nine_legacy_cases_are_rechecked_without_new_cohort_selection(self):
        for i in range(8):
            self.store.add(fixtures.feature('TEST'+str(i)), fixtures.session(), 'HOT_CASE',str(i),self.now,prospective=False)
        for row in self.store.episodes():
            self.store.settle(row['episode_key'], {'status':'PARTIAL_DATA','outcome':None}, self.now)
        provider = Mock()
        provider.calendar.return_value = [fixtures.session(day) for day in ('2026-10-08','2026-10-09','2026-10-12','2026-10-13')]
        provider.assets.return_value = ['TEST']; provider.daily.return_value = {}
        provider.intraday.side_effect = lambda symbol,s,now:fixtures.frame(s).iloc[1:]
        provider.gap_trades.side_effect = lambda symbol,minute,now:dict(
            minute=minute.isoformat(), complete=True, classification='NO_TRADES_IN_FEED')
        before = {r['episode_key']:r['features_json'] for r in self.store.episodes()}
        packet = MoveResearchWorker(self.store,provider).cycle(self.now)
        self.assertEqual(provider.intraday.call_count, 9)
        self.assertEqual(packet['counts']['PARTIAL_DATA'], 9)
        self.assertEqual(packet['diagnostics']['incomplete_episodes'], 9)
        self.assertEqual(before, {r['episode_key']:r['features_json'] for r in self.store.episodes()})
        self.assertTrue(all(r['attempts']==2 for r in self.store.episodes()))

    def test_manifest_is_deduplicated_and_future_schedule_survives_packets(self):
        future = fixtures.session('2026-10-13')
        self.store.add(fixtures.feature(day='2026-10-12'),future,'ACTIVITY','future',self.now,prospective=True)
        rows = self.store.episodes(); logger = Mock()
        emit_evidence(self.store,rows,logger); emit_evidence(self.store,rows,logger)
        self.assertEqual(logger.info.call_count, 2)
        records = [json.loads(call.args[1]) for call in logger.info.call_args_list]
        self.assertTrue(all(r['kind']=='COHORT' for r in records))
        _,packet = self.store.publish(self.now, {'state':'READY'})
        packet = read_packet(path=self.store.path,now=self.now)
        self.assertEqual(len(packet['forward_cohort']), 1)
        self.assertEqual(packet['forward_cohort'][0]['evaluate_after'], '2026-10-13T20:16:00+00:00')
        self.assertIn('23:16 السعودية',report_text(packet))

    def test_aggregate_counts_distinct_symbol_minutes_and_flags_conflicts(self):
        self.store.settle(self.row['episode_key'],observe_session(fixtures.frame().iloc[1:],fixtures.session()),self.now)
        row = self.store.episodes()[0]
        stamp = row['result']['data_diagnostics']['first_absent_minutes'][0]
        row['result']['data_diagnostics']['gap_trade_evidence'] = [dict(minute=stamp,classification='NO_TRADES_IN_FEED')]
        result = aggregate([row,row],self.now)
        self.assertEqual(result['incomplete_episodes'],2)
        self.assertEqual(result['unique_incomplete_symbol_days'],1)
        self.assertEqual(result['sampled_minutes']['NO_TRADES_IN_FEED'],1)
        import copy
        other = copy.deepcopy(row)
        other['result']['data_diagnostics']['gap_trade_evidence'][0]['classification'] = 'UNKNOWN_TRADE_CONDITIONS'
        self.assertEqual(aggregate([row,other],self.now)['sampled_minutes'],{'CONFLICTING_TRADE_SAMPLES':1})

    def test_duplicate_retrospective_controls_reuse_reads_within_cycle(self):
        self.store.add(fixtures.feature(),fixtures.session(),'CONTROL','second',self.now,prospective=False)
        for row in self.store.episodes():
            self.store.settle(row['episode_key'],{'status':'PARTIAL_DATA','outcome':None},self.now)
        provider = Mock()
        provider.calendar.return_value = [fixtures.session(day) for day in ('2026-10-08','2026-10-09','2026-10-12','2026-10-13')]
        provider.assets.return_value = ['CASE']; provider.daily.return_value = {}
        provider.intraday.side_effect = lambda symbol,s,now:fixtures.frame(s).iloc[1:]
        provider.gap_trades.side_effect = lambda symbol,minute,now:dict(
            minute=minute.isoformat(),complete=True,classification='NO_TRADES_IN_FEED')
        packet = MoveResearchWorker(self.store,provider).cycle(self.now)
        self.assertEqual(provider.intraday.call_count,1)
        self.assertEqual(provider.gap_trades.call_count,1)
        self.assertEqual(packet['diagnostics']['unique_sampled_minutes'],1)


if __name__ == '__main__':
    unittest.main()
