"""Batch transport/worker regressions; all data and HTTP are synthetic."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from market_move_provider import MoveProvider, ResearchReadError
from market_move_research import MoveStore, utc, observe_session, report_text
from market_move_research_loop import MoveResearchWorker, next_cycle_delay, PRODUCTION_STUDY_LIMIT
from market_jump_requests import schedule_studies
import test_market_move_research as fixtures


class BatchedMinuteTests(unittest.TestCase):
    def setUp(self):
        self.provider = MoveProvider('test','test',session=Mock())
        self.session = fixtures.session()
        self.now = utc('2026-10-12T21:00Z')

    def raw(self, minute, price=10.):
        return dict(t=minute,o=price,h=price+.1,l=price-.1,c=price,v=100)

    def test_all_pages_read_before_later_symbols_can_be_called_missing(self):
        pages = [{'bars':{'A':[self.raw('2026-10-12T13:30Z')]},'next_page_token':'next'},
                 {'bars':{'B':[self.raw('2026-10-12T13:31Z',20.)]},'next_page_token':None}]
        with patch.object(self.provider,'get',side_effect=pages) as get:
            frames = self.provider.intraday_many(['A','B','C'],self.session,self.now)
        self.assertEqual(get.call_count,2)
        self.assertEqual(get.call_args_list[1].args[1]['page_token'],'next')
        self.assertEqual(get.call_args_list[0].args[1]['feed'],'sip')
        self.assertEqual(float(frames['B'].open.iloc[0]),20.)
        self.assertTrue(frames['C'].empty)
        self.assertEqual(observe_session(frames['C'],self.session)['status'],'PARTIAL_DATA')

    def test_repeated_token_or_failed_pagination_returns_no_partial_batch(self):
        page = {'bars':{'A':[self.raw('2026-10-12T13:30Z')]},'next_page_token':'repeat'}
        with patch.object(self.provider,'get',return_value=page):
            with self.assertRaisesRegex(ResearchReadError,'INVALID_PAGINATION'):
                self.provider.intraday_many(['A','B'],self.session,self.now)

    def test_delay_limit_empty_and_symbol_deduplication(self):
        with patch.object(self.provider,'get',return_value={'bars':{}}) as get:
            self.assertEqual(self.provider.intraday_many([],self.session,self.now),{})
            with self.assertRaisesRegex(ResearchReadError,'BATCH_TOO_LARGE'):
                self.provider.intraday_many([str(i) for i in range(25)],self.session,self.now)
            with self.assertRaisesRegex(ResearchReadError,'WAITING_FOR_HISTORICAL_DELAY'):
                self.provider.intraday_many(['A'],self.session,utc(self.session['close']))
            get.assert_not_called()
            frames = self.provider.intraday_many(['A','A'],self.session,self.now)
            self.assertEqual(list(frames),['A'])
            self.assertEqual(get.call_args.args[1]['symbols'],'A')

    def test_batch_frame_is_identical_to_single_read_including_conflicts(self):
        rows = [self.raw('2026-10-12T13:30Z'),self.raw('2026-10-12T13:30Z',20.)]
        with patch.object(self.provider,'get',return_value={'bars':{'A':rows}}):
            single = self.provider.intraday('A',self.session,self.now)
            batched = self.provider.intraday_many(['A'],self.session,self.now)['A']
        pd.testing.assert_frame_equal(single,batched)
        self.assertEqual(observe_session(batched,self.session)['status'],'PARTIAL_DATA')
        self.assertEqual(len(batched),2)  # Conflicts are not silently discarded.


class ResearchThroughputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = MoveStore(Path(self.tmp.name)/'moves.db')
        self.now = utc('2026-10-12T21:00Z')
        self.session = fixtures.session()
        self.provider = Mock()
        self.provider.intraday_many.side_effect = lambda symbols,s,now:{symbol:fixtures.frame(s) for symbol in symbols}
        self.provider.news_before.return_value = {'status':'AVAILABLE','articles':[]}

    def add(self, symbol, *, session=None, pair=None, prospective=False):
        self.store.add(fixtures.feature(symbol),session or self.session,
                       'ACTIVITY' if prospective else 'JUMP_CASE',pair or symbol,
                       utc('2026-10-12T12:00Z'),prospective=prospective)

    def worker(self):
        worker = MoveResearchWorker(self.store,self.provider,study_limit=48,batch_reads=True)
        worker.prepare = Mock(return_value=dict(state='READY',completed_session=self.session['date']))
        self.provider.calendar.return_value = [self.session]
        self.store.state('universe_cache',dict(date=self.now.date().isoformat(),symbols=['A'],selection_version='historical-jump-selection-v3'))
        return worker

    def test_48_observations_use_two_same_session_reads_and_no_fallback(self):
        for i in range(60): self.add('S'+str(i))
        worker = self.worker()
        before = {r['episode_key']:r['features_json'] for r in self.store.episodes()}
        packet = worker.cycle(self.now)
        self.assertEqual(packet['counts'],{'COMPLETE':48,'PENDING':12})
        self.assertEqual(self.provider.intraday_many.call_count,2)
        self.assertTrue(all(len(c.args[0])==24 for c in self.provider.intraday_many.call_args_list))
        self.provider.intraday.assert_not_called()
        batch = packet['health']['study_batch']
        self.assertEqual(batch['processed'],48)
        self.assertEqual(batch['prefetched_symbol_sessions'],48)
        self.assertEqual(before,{r['episode_key']:r['features_json'] for r in self.store.episodes()})
        self.assertFalse(packet['automatic_application'])
        self.assertEqual(packet['comparison']['complete_pairs'],0)
        self.assertIn('عولجت 48 من 48',report_text(packet))

    def test_symbol_sessions_are_isolated_and_duplicate_roles_share_one_read(self):
        self.add('A'); self.add('A',pair='second')
        self.add('A',session=fixtures.session('2026-10-09'),pair='other-day')
        worker = self.worker()
        frames = worker.prefetch(self.store.episodes(),self.now)
        self.assertEqual(self.provider.intraday_many.call_count,2)
        self.assertEqual(len(frames),2)

    def test_partial_batched_window_stays_partial_without_price_inference(self):
        self.add('A')
        self.provider.intraday_many.side_effect=lambda symbols,s,now:{symbol:fixtures.frame(s).iloc[1:] for symbol in symbols}
        self.provider.gap_trades.return_value={'minute':self.session['open'],'complete':True,'classification':'NO_TRADES_IN_FEED'}
        packet = self.worker().cycle(self.now)
        row = self.store.episodes()[0]
        self.assertEqual(row['status'],'PARTIAL_DATA')
        self.assertEqual(row['result']['missing_minutes'],1)
        self.assertIsNone(row['result']['outcome'])
        self.assertEqual(packet['comparison']['complete_pairs'],0)

    def test_batch_malformed_response_falls_back_to_isolated_reads(self):
        self.add('A'); self.add('B')
        self.provider.intraday_many.side_effect = ResearchReadError('INVALID_BARS_RESPONSE')
        self.provider.intraday.side_effect = lambda symbol,s,now:fixtures.frame(s)
        packet = self.worker().cycle(self.now)
        self.assertEqual(packet['counts'],{'COMPLETE':2})
        self.assertEqual(self.provider.intraday.call_count,2)

    def test_global_rate_or_auth_failure_does_not_fan_out_or_spend_attempts(self):
        for code in (401,403,429,500,503):
            with self.subTest(code=code):
                self.provider.intraday_many.side_effect = ResearchReadError('HTTP_'+str(code),status_code=code)
                if not self.store.episodes(): self.add('A'); self.add('B')
                packet = self.worker().cycle(self.now)
                self.assertEqual(packet['health']['state'],'DATA_UNAVAILABLE')
                self.assertEqual(packet['health']['study_batch']['processed'],0)
                self.assertTrue(all(r['attempts']==0 for r in self.store.episodes()))
                self.provider.intraday.assert_not_called()

    def test_larger_budget_keeps_forward_priority_and_scales_retry_seats(self):
        from test_market_jump_requests import RequestedJumpTests
        make = RequestedJumpTests.row
        pending = [make(self,'NEW'+str(i)) for i in range(100)]
        retries = [dict(make(self,'RETRY'+str(i),status='PARTIAL_DATA'),frozen_at=f'2026-10-{i+1:02d}T00:00Z') for i in range(12)]
        forward = [make(self,'F'+str(i),prospective=True) for i in range(16)]
        before = copy.deepcopy(pending+retries+forward)
        chosen = schedule_studies(pending+retries+forward,set(),limit=48)
        self.assertTrue(all(r['prospective'] for r in chosen[:16]))
        self.assertEqual(sum(r['status']=='PARTIAL_DATA' for r in chosen),8)
        self.assertEqual(len(chosen),48)
        self.assertEqual(before,pending+retries+forward)

    def test_limit_is_bounded_and_cadence_never_spins_after_slow_cycle(self):
        for value in (0,49,True,1.5):
            with self.assertRaises(ValueError):
                MoveResearchWorker(self.store,self.provider,study_limit=value)
        self.assertEqual(PRODUCTION_STUDY_LIMIT,48)
        self.assertEqual(next_cycle_delay(20),280)
        self.assertEqual(next_cycle_delay(290),60)
        self.assertEqual(next_cycle_delay(1200),60)
