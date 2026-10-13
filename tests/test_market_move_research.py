import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import market_move_research as research
from market_move_provider import MoveProvider, ResearchReadError
from market_move_research_loop import MoveResearchWorker
import railway_controller as controller
import test_entry_quality_and_gap_research as entry_fixtures


def session(day='2026-10-12', hours=6.5):
    opening = research.utc(day+'T13:30:00+00:00')
    return dict(date=day, open=opening.isoformat(), close=(opening+timedelta(hours=hours)).isoformat())


def frame(s=None):
    s = s or session()
    index = pd.date_range(research.utc(s['open']), research.utc(s['close']), freq='min', inclusive='left')
    return pd.DataFrame(dict(open=10., high=10.1, low=9.9, close=10., volume=100.), index=index)


def feature(symbol='CASE', day='2026-10-09', case=True):
    return dict(symbol=symbol, evidence_day=day, available_after=day+'T20:16:00+00:00',
        close=10., dollar_volume=1000000., volume_ratio_previous_day=2. if case else 1.,
        range_pct=5., daily_return_pct=0., source_feed='sip')


def bar(day, close=10., volume=100000):
    return dict(t=day+'T04:00:00Z', o=10., h=max(11., close), l=min(9., close), c=close, v=volume)


class EvidenceTests(unittest.TestCase):
    def test_daily_evidence_requires_exact_prior_session(self):
        s, p = session('2026-10-09'), session('2026-10-08')
        bars = [bar(s['date']), bar(p['date']), bar('2026-10-12', 90.)]
        features = research.daily_features('CASE', bars, s, p)
        self.assertEqual(features['close'], 10.)
        self.assertEqual(features['evidence_day'], '2026-10-09')
        self.assertIsNone(research.daily_features('CASE', bars, s, session('2026-10-07')))
        with self.assertRaises(ValueError):
            research.daily_features('CASE', bars+[bar(s['date'])], s, p)

    def test_nonfinite_daily_products_and_unzoned_timestamps_rejected(self):
        s, p = session(), session('2026-10-09')
        self.assertIsNone(research.daily_features('CASE', [bar(s['date'], 1e200, 1e200), bar(p['date'])], s, p))
        with self.assertRaises(ValueError):
            research.utc('2026-10-12T10:00:00')
        self.assertFalse(research.valid_bar(dict(o=10, h=9, l=8, c=10, v=100)))

    def test_controls_use_only_frozen_price_liquidity_buckets(self):
        rows = [feature('C1'), feature('C2'), feature('N1', case=False), feature('N2', case=False),
                {**feature('WRONG', case=False), 'close': 90}]
        pairs = research.choose_pairs(rows, '2026-10-12')
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({control['symbol'] for _, control in pairs}), 2)
        self.assertTrue(all(research.bucket(a) == research.bucket(b) for a, b in pairs))
        self.assertEqual(pairs, research.choose_pairs(list(reversed(rows)), '2026-10-12'))
        self.assertEqual(research.choose_pairs(rows, '2026-10-12', 0), [])

    def test_normal_and_early_close_calendar_are_exact(self):
        for s, expected in ((session(), 390), (session(hours=3.5), 210)):
            result = research.observe_session(frame(s), s)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(result['expected_minutes'], expected)
            self.assertEqual(result['outcome'], 'NO_BARRIER')

    def test_gaps_duplicate_invalid_volume_and_naive_index_remain_inconclusive(self):
        data = frame()
        invalid = data.copy(); invalid.iloc[3, 4] = 0
        naive = data.copy(); naive.index = naive.index.tz_localize(None)
        for sample in (data.drop(data.index[3]), pd.concat([data, data.iloc[:1]]), invalid, naive):
            result = research.observe_session(sample, session())
            self.assertEqual(result['status'], 'PARTIAL_DATA')
            self.assertIsNone(result['outcome'])

    def test_barrier_order_and_same_bar_ambiguity(self):
        for up, down, expected in ((25, 40, 'UP8_FIRST'), (40, 25, 'DOWN3_FIRST'), (25, 25, 'AMBIGUOUS')):
            data = frame()
            data.iloc[up, 1] = 11.; data.iloc[down, 2] = 9.6
            self.assertEqual(research.observe_session(data, session())['outcome'], expected)

    def test_context_strictly_excludes_move_and_later_bars(self):
        data = frame(); data.iloc[25, 1] = 11.; data.iloc[25, 4] = 1e9
        result = research.observe_session(data, session())
        self.assertEqual(result['pre_move_context']['volume_acceleration'], 1.)
        self.assertEqual(result['pre_move_context']['cutoff'], data.index[25].isoformat())
        early = frame(); early.iloc[3, 1] = 11.
        self.assertEqual(research.observe_session(early, session())['pre_move_context']['status'], 'UNAVAILABLE_LESS_THAN_20M')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'moves.db'
        self.store = research.MoveStore(self.path)
        self.now = research.utc('2026-10-12T12:00:00Z')

    def test_prospective_freeze_is_immutable_and_before_open(self):
        s, f = session(), feature()
        self.assertTrue(self.store.add(f, s, 'ACTIVITY', 'p', self.now, prospective=True))
        self.assertFalse(self.store.add({**f, 'close': 99}, s, 'ACTIVITY', 'p', self.now, prospective=True))
        self.assertEqual(self.store.episodes()[0]['features']['close'], 10.)
        for now in (research.utc(s['open']), research.utc(f['available_after'])-timedelta(seconds=1)):
            with self.assertRaises(ValueError):
                self.store.add(f, s, 'CONTROL', 'p', now, prospective=True)
        with self.assertRaises(ValueError):
            self.store.add(f, s, 'HOT_CASE', 'p', self.now, prospective=True)

    def test_controls_in_different_pairs_do_not_collide_and_settle_once(self):
        for pair in ('one', 'two'):
            self.store.add(feature(), session(), 'CONTROL', pair, self.now, prospective=False)
        rows = self.store.episodes(); self.assertEqual(len(rows), 2)
        self.store.settle(rows[0]['episode_key'], {'status':'COMPLETE', 'outcome':'UP8_FIRST'}, self.now)
        self.store.settle(rows[0]['episode_key'], {'status':'COMPLETE', 'outcome':'DOWN3_FIRST'}, self.now)
        result = next(r for r in self.store.episodes() if r['episode_key'] == rows[0]['episode_key'])
        self.assertEqual(result['result']['outcome'], 'UP8_FIRST'); self.assertEqual(result['attempts'], 1)

    def test_packet_read_is_readonly_fresh_and_protocol_scoped(self):
        self.store.publish(self.now, {'state':'READY'})
        packet = research.read_packet(path=self.path, now=self.now)
        self.assertFalse(packet['automatic_application'])
        self.assertIsNone(research.read_packet(path=self.path, now=self.now+timedelta(hours=2)))
        self.assertIsNone(research.read_packet(path=self.path, now=self.now-timedelta(seconds=1)))
        for change in ({'automatic_application':True}, {'scope':'BUY'}, {'source_feed':'iex'}, {'protocol':'old'}, {'latest_cases':None}):
            import json
            with self.store.connect() as conn:
                row = conn.execute('SELECT packet_id, payload_json FROM move_packets').fetchone()
                payload = json.loads(row['payload_json']); original = dict(payload); payload.update(change)
                conn.execute('UPDATE move_packets SET payload_json=?', (json.dumps(payload),))
            self.assertIsNone(research.read_packet(path=self.path, now=self.now))
            with self.store.connect() as conn:
                conn.execute('UPDATE move_packets SET payload_json=?', (json.dumps(original),))

    def test_retention_prunes_before_counts(self):
        self.store.add(feature(day='2025-01-01'), session('2025-01-02'), 'HOT_CASE', 'old', self.now, prospective=False)
        _, packet = self.store.publish(self.now, {'state':'READY'})
        self.assertEqual(packet['hot_case_episodes'], 0)


class ComparisonTests(unittest.TestCase):
    def rows(self, outcome='UP8_FIRST'):
        result = []
        for i in range(30):
            day = (research.utc('2026-09-01T00:00:00Z')+timedelta(days=i)).date().isoformat()
            for j in range(4):
                for role, label in (('ACTIVITY',outcome), ('CONTROL','NO_BARRIER')):
                    result.append(dict(prospective=True, role=role, pair_key=f'{i}:{j}', session_day=day,
                                       status='COMPLETE', result={'outcome':label}))
        return result

    def test_holdout_uses_whole_days_and_excludes_retrospective(self):
        rows = self.rows(); retrospective = [{**r, 'prospective':False, 'pair_key':'retro'+r['pair_key']} for r in rows]
        result = research.comparison(rows+retrospective)
        self.assertEqual(result['complete_pairs'], 120)
        self.assertEqual((result['train_days'],result['validation_days']), (20,10))
        self.assertEqual(result['state'], 'VALIDATED_RESEARCH_ASSOCIATION')
        self.assertFalse(result['automatic_application'])

    def test_insufficient_ambiguous_or_sparse_coverage_cannot_validate(self):
        rows = self.rows()
        self.assertEqual(research.comparison(rows[:30])['state'], 'WAITING_FORWARD_DATA')
        for row in rows[:48]:
            row['result']['outcome'] = 'AMBIGUOUS'
        result = research.comparison(rows)
        self.assertEqual(result['state'], 'WAITING_FORWARD_DATA')
        self.assertLess(result['complete_pair_coverage'], .9)

    def test_failed_validation_does_not_inherit_training_success(self):
        rows = self.rows()
        for row in rows[-80:]:
            if row['role'] == 'ACTIVITY':
                row['result']['outcome'] = 'DOWN3_FIRST'
        self.assertEqual(research.comparison(rows)['state'], 'NO_VALIDATED_EDGE')


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = MoveProvider('test', 'test', session=Mock(), interval=.5)

    def test_allowlist_has_no_order_endpoint(self):
        with self.assertRaises(ResearchReadError):
            self.provider.get('https://paper-api.alpaca.markets/v2/orders')
        self.provider.session.get.assert_not_called()

    def test_raw_asset_class_and_tradability(self):
        asset = dict(symbol='TEST', status='active', **{'class':'us_equity'}, tradable=True)
        with patch.object(self.provider, 'get', return_value=[asset, {**asset, 'symbol':'NO', 'tradable':False}]):
            self.assertEqual(self.provider.assets(), ['TEST'])

    def test_pagination_is_complete_or_fails_closed(self):
        now = research.utc('2026-10-12T21:00:00Z')
        with patch.object(self.provider, 'get', side_effect=[{'bars':{'TEST':[bar('2026-10-12')]}, 'next_page_token':'a'},
                                                          {'bars':{}, 'next_page_token':'a'}]):
            with self.assertRaisesRegex(ResearchReadError, 'INVALID_PAGINATION'):
                self.provider.bars(['TEST'], now-timedelta(days=1), now, '1Day')
        with patch.object(self.provider, 'get', return_value={'bars':{}, 'next_page_token':'a'}):
            with self.assertRaisesRegex(ResearchReadError, 'INCOMPLETE_PAGINATION'):
                self.provider.bars(['TEST'], now-timedelta(days=1), now, '1Day', max_pages=1)

    def test_historical_delay_checked_before_request(self):
        s = session()
        with patch.object(self.provider, 'get') as get:
            with self.assertRaisesRegex(ResearchReadError, 'WAITING_FOR_HISTORICAL_DELAY'):
                self.provider.intraday('TEST', s, research.utc(s['close'])+timedelta(minutes=15))
            get.assert_not_called()

    def test_news_filters_revised_future_content_and_marks_malformed(self):
        cutoff = research.utc('2026-10-12T13:30:00Z')
        article = dict(id=1, created_at='2026-10-12T12:00:00Z', updated_at='2026-10-12T12:01:00Z',
                       symbols=['TEST'], headline='FDA approval')
        revised = {**article, 'id':2, 'updated_at':'2026-10-12T14:00:00Z'}
        with patch.object(self.provider, 'get', return_value={'news':[article,revised]}):
            result = self.provider.news_before('TEST', cutoff)
        self.assertEqual(result['count'], 1); self.assertEqual(result['articles'][0]['keywords'], ['fda'])
        self.assertNotIn('headline', result['articles'][0])
        with patch.object(self.provider, 'get', return_value={'news':[{'id':3}]}):
            self.assertEqual(self.provider.news_before('TEST',cutoff)['status'], 'INCOMPLETE')


class WorkerTests(unittest.TestCase):
    setUp = StoreTests.setUp
    def setup_worker(self):
        sessions = [session(d) for d in ('2026-10-06','2026-10-07','2026-10-08','2026-10-09','2026-10-12','2026-10-13')]
        daily = {symbol:[bar(s['date'], close=11. if symbol == 'CASE' and s['date'] == '2026-10-09' else 10.,
                            volume=200000 if symbol == 'CASE' and s['date'] == '2026-10-09' else 100000)
                         for s in sessions[:4]] for symbol in ('CASE','CONTROL')}
        provider = Mock()
        provider.calendar.return_value = sessions
        provider.assets.return_value = list(daily)
        provider.daily.return_value = daily
        provider.intraday.side_effect = lambda symbol,s,now: frame(s)
        provider.news_before.return_value = {'status':'AVAILABLE','articles':[]}
        return MoveResearchWorker(self.store,provider), sessions, daily

    def test_forward_cohort_freezes_once_and_survives_restart(self):
        worker, _, daily = self.setup_worker()
        worker.cycle(self.now)
        rows = [r for r in self.store.episodes() if r['prospective']]
        self.assertEqual(len(rows), 2); self.assertTrue(all(r['session_day']=='2026-10-12' for r in rows))
        worker.provider.daily.return_value = {'CONTROL': []}
        worker.cycle(self.now)
        self.assertEqual(rows, [r for r in self.store.episodes() if r['prospective']])
        worker.provider.daily.assert_called_once()

    def test_no_next_day_cohort_while_preceding_session_unfinished(self):
        worker, _, _ = self.setup_worker()
        worker.cycle(research.utc('2026-10-12T15:00:00Z'))
        self.assertFalse(any(r['prospective'] for r in self.store.episodes()))
        self.assertIsNone(self.store.state('frozen:2026-10-13'))

    def test_clock_crossing_open_does_not_freeze_late(self):
        worker, sessions, _ = self.setup_worker()
        worker.prepare(self.now, sessions, ['CASE','CONTROL'], clock=lambda:research.utc(session()['open']))
        self.assertFalse(any(r['prospective'] for r in self.store.episodes()))

    def test_provider_failure_publishes_unavailability_without_outcome(self):
        worker, _, _ = self.setup_worker(); worker.provider.calendar.side_effect = ResearchReadError('HTTP_403')
        packet = worker.cycle(self.now)
        self.assertEqual(packet['health']['state'], 'DATA_UNAVAILABLE')
        self.assertEqual(packet['comparison']['complete_pairs'], 0)
        self.assertFalse(packet['automatic_application'])


class BotBoundaryTests(unittest.TestCase):
    def test_research_never_overrides_native_rejection_or_entry(self):
        fixture = entry_fixtures.CurrentLatchTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        with patch('market_move_research.read_packet', return_value={'scope':'RESEARCH_ONLY_NOT_TRADE_SIGNAL', 'claim':'UP8_FIRST'}):
            fixture.confirm()
            decision = fixture.analyze()
            self.assertTrue(decision.ready_for_execution)
            self.assertIn('market_move_research', decision.metadata)
            fixture.engine.ai_engine.evaluate.return_value.action = 'SELL'
            fixture.engine.ai_engine.evaluate.return_value.score = 40
            self.assertFalse(fixture.analyze().ready_for_execution)

    def test_auto_report_waits_for_due_cases_and_is_once_per_session(self):
        packet = dict(packet_id='one', health=dict(state='READY', completed_session='2026-10-12', completed_session_pending=1))
        state = {}
        with patch('market_move_research.read_packet', return_value=packet), \
             patch('market_move_research.report_text', return_value='research'), \
             patch.object(controller, 'controller_state', state), patch.object(controller, 'AUTO_DAILY_REPORT', True), \
             patch.object(controller, 'save_state'), patch.object(controller, 'send_message') as send:
            controller.maybe_send_move_research_report(); send.assert_not_called()
            packet['health']['completed_session_pending'] = 0
            controller.maybe_send_move_research_report(); controller.maybe_send_move_research_report()
            send.assert_called_once_with('research')

    def test_command_is_available_and_failure_does_not_interrupt_controller(self):
        with patch('market_move_research.read_packet', return_value=None):
            self.assertIn('ينتظر', controller.handle('/move_research'))
        with patch('market_move_research.read_packet', side_effect=RuntimeError('offline')):
            controller.maybe_send_move_research_report()

    def test_worker_lifecycle_is_managed_independently(self):
        with patch.object(controller, 'MANAGE_APEX_CHILD', False), patch.object(controller, 'jalwe') as jalwe, \
             patch.object(controller, 'move_research') as worker:
            controller.start_all(); worker.start.assert_called_once(); jalwe.start.assert_called_once()
            controller.stop_all(); worker.stop.assert_called_once(); jalwe.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
