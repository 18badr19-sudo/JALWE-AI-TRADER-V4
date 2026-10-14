from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from market_jump_paths import candidates, jump_path
from market_move_research import MoveStore, observe_session, utc, read_packet, report_text
from market_move_diagnostics import observation_due
from market_move_research_loop import MoveResearchWorker
import test_market_move_research as fixtures


class JumpPathTests(unittest.TestCase):
    def test_faded_gains_and_more_than_five_old_symbols_are_queued(self):
        days = [fixtures.session(d) for d in ('2026-09-29','2026-09-30','2026-10-01')]
        now = utc('2026-10-13T18:00:00Z')
        symbols = ['OLD'+str(n) for n in range(7)]
        daily = {s:[dict(fixtures.bar(d['date']),h=10.1,l=9.9) for d in days] for s in symbols}
        for rows in daily.values():
            rows[1].update(h=15.,c=10.,l=9.9)  # Closed flat, still a strong intraday range.
        found = list(candidates(symbols,daily,days,now))
        self.assertEqual(len(found),7)
        self.assertTrue(all(s['date']=='2026-09-30' for _,s in found))
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            for f,s in found:
                self.assertTrue(store.add(f,s,'JUMP_CASE','jump',now,prospective=False))
                self.assertFalse(store.add(f,s,'JUMP_CASE','jump',now,prospective=False))
            self.assertEqual(len(store.episodes()),7)
            self.assertFalse(any(r['prospective'] for r in store.episodes()))

    def test_lookback_exact_prior_duplicates_and_liquidity(self):
        days = [fixtures.session(d) for d in ('2026-09-09','2026-09-10','2026-10-09','2026-10-12')]
        now = utc('2026-10-13T18:00:00Z')
        rows = [dict(fixtures.bar(d['date']),h=10.1,l=9.9) for d in days]
        rows[-1].update(c=12.,h=12.)
        self.assertTrue(list(candidates(['OLD'],{'OLD':rows},days,now)))
        found = list(candidates(['OLD'],{'OLD':rows+[rows[-1]]},days,now))
        self.assertEqual(found,[])
        self.assertEqual(list(candidates(['OLD'],{'OLD':rows[:2]},days,now)),[])
        low = [dict(r,v=1) for r in rows]
        self.assertEqual(list(candidates(['OLD'],{'OLD':low},days,now)),[])

    def test_start_to_peak_uses_chronological_lows_and_saves_real_path(self):
        data = fixtures.frame()
        data.iloc[40] = [10.,10.,8.,9.,200.]
        data.iloc[70] = [10.,15.,9.9,12.,900.]
        data.iloc[80] = [10.,10.1,7.,8.,100.]  # Later low cannot explain earlier peak.
        result = jump_path(data,fixtures.session(),full_session_complete=True)
        self.assertEqual(result['start_at'],data.index[40].isoformat())
        self.assertEqual(result['peak_at'],data.index[70].isoformat())
        self.assertAlmostEqual(result['observed_rise_pct'],87.5)
        self.assertEqual(result['duration_minutes'],30)
        self.assertEqual(len(result['trajectory']),31)
        self.assertEqual(result['pre_jump_context']['cutoff'],data.index[40].isoformat())
        self.assertEqual(result['pre_jump_context']['volume_acceleration'],1.)
        self.assertEqual(result['phase_volume'],float(data.iloc[40:71].volume.sum()))

    def test_same_bar_low_high_cannot_invent_chronological_jump(self):
        data = fixtures.frame().iloc[:1].copy()
        data.iloc[0] = [10.,10.1,5.,10.,100.]
        result = jump_path(data,fixtures.session())
        self.assertFalse(result['strong_jump'])
        self.assertEqual(result['start_price_source'],'SEGMENT_OPEN')

    def test_jump_report_context_precedes_onset_not_later_opening_barrier(self):
        import copy
        now = utc('2026-10-13T18:00:00Z')
        s = fixtures.session()
        data = fixtures.frame()
        data.iloc[35:40,4] = 500.
        data.iloc[40] = [10.,10.,8.,9.,200.]
        data.iloc[70] = [10.,15.,9.9,12.,900.]
        result = observe_session(data,s)
        result['jump_path'] = jump_path(data,s,full_session_complete=True)
        onset = result['jump_path']['start_at']
        self.assertLess(utc(onset),utc(result['up_at']))
        self.assertEqual(result['jump_path']['pre_jump_context']['volume_acceleration'],5.)
        self.assertEqual(result['pre_move_context']['volume_acceleration'],1.)
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            store.add(fixtures.feature(),s,'JUMP_CASE','jump',now,prospective=False)
            store.settle(store.episodes()[0]['episode_key'],result,now)
            store.publish(now,dict(state='READY',completed_session=s['date']))
            packet = read_packet(path=store.path,now=now)
            text = report_text(packet)
            label = 'قبل بداية القفزة المرصودة 20 دقيقة'
            context_line = next(line for line in text.splitlines() if label in line)
            self.assertIn('5.00x',context_line)
            # An unavailable onset context must never fall back to post-onset bars.
            missing = copy.deepcopy(packet)
            missing['cases'][0]['result']['jump_path']['pre_jump_context'] = {
                'status':'UNAVAILABLE_LESS_THAN_20M','bars':10}
            self.assertNotIn(label,report_text(missing))
            self.assertNotIn('قبل الحركة 20 دقيقة',report_text(missing))
            partial = copy.deepcopy(packet)
            partial['cases'][0]['result']['jump_path']['status'] = 'OBSERVED_SEGMENT'
            partial['cases'][0]['result'].update(status='PARTIAL_DATA',outcome=None,missing_minutes=1)
            partial_text = report_text(partial)
            self.assertIn(label,partial_text)
            self.assertIn('جزء متصل فقط',partial_text)
            self.assertEqual(partial['comparison']['complete_pairs'],0)
            # Non-jump studies retain their original opening-barrier context.
            original = copy.deepcopy(packet)
            original['cases'][0]['role'] = 'HOT_CASE'
            original['cases'][0]['result'].pop('jump_path')
            old_line = next(line for line in report_text(original).splitlines() if 'قبل الحركة 20 دقيقة' in line)
            self.assertIn('1.00x',old_line)

    def test_nonfinite_aggregate_cannot_be_published_as_valid_path(self):
        data = fixtures.frame()
        data.iloc[20,1] = 15.
        data['volume'] = 1e308
        result = jump_path(data,fixtures.session(),full_session_complete=True)
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertNotIn('trajectory',result)

    def test_gaps_and_duplicate_minutes_do_not_connect_an_invented_path(self):
        data = fixtures.frame()
        data.iloc[10] = [10.,10.1,5.,10.,100.]
        data.iloc[11:15] = [5.,5.1,4.9,5.,100.]
        data.iloc[20] = [10.,15.,9.9,10.,100.]
        for raw in (data.drop(data.index[15]),pd.concat([data,data.iloc[15:16]])):
            observation = observe_session(raw,fixtures.session())
            self.assertEqual(observation['status'],'PARTIAL_DATA')
            path = jump_path(raw,fixtures.session(),full_session_complete=True)
            self.assertEqual(path['status'],'OBSERVED_SEGMENT')
            self.assertLess(path['observed_rise_pct'],60)
            self.assertFalse(path['full_session_complete'])

    def test_worker_backfills_and_reports_without_rewriting_forward_cohort(self):
        test = fixtures.WorkerTests(); test.setUp()
        try:
            worker,sessions,daily = test.setup_worker()
            packet = worker.cycle(test.now)
            jumps = [r for r in test.store.episodes() if r['role']=='JUMP_CASE']
            self.assertGreater(len(jumps),0)
            self.assertTrue(any(r['session_day']=='2026-10-07' for r in jumps))
            cohort = [r for r in test.store.episodes() if r['prospective']]
            self.assertEqual(len(cohort),2)
            worker.cycle(test.now)
            self.assertEqual(cohort,[r for r in test.store.episodes() if r['prospective']])
            self.assertEqual(len(jumps),len([r for r in test.store.episodes() if r['role']=='JUMP_CASE']))
            loaded = read_packet(path=test.path,now=test.now)
            self.assertGreater(loaded['historical_jumps']['episodes'],0)
            self.assertIn('مسارات القفزات',report_text(loaded))
            self.assertIn('jump_path',next(c['result'] for c in loaded['cases'] if c['role']=='JUMP_CASE'))
            self.assertEqual(packet['comparison']['complete_pairs'],0)
            self.assertFalse(packet['automatic_application'])
        finally:
            test.doCleanups()

    def test_old_jump_retries_are_bounded_from_discovery_not_old_close(self):
        now = utc('2026-10-13T18:00:00Z')
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            s = fixtures.session('2026-09-30')
            store.add(fixtures.feature(),s,'JUMP_CASE','jump',now,prospective=False)
            row = store.episodes()[0]
            store.settle(row['episode_key'],observe_session(fixtures.frame(s).iloc[1:],s),now)
            row = store.episodes()[0]
            self.assertTrue(observation_due(row,now+timedelta(minutes=15)))
            self.assertFalse(observation_due(row,now+timedelta(hours=49)))

    def test_batch_manifest_rolls_back_on_error_and_survives_restart(self):
        import sqlite3
        now = utc('2026-10-13T18:00:00Z')
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            studies = [(dict(fixtures.feature(s),jump_protocol='test-v1'),fixtures.session())
                       for s in ('ONE','TWO')]
            with store.connect() as conn:
                conn.execute("CREATE TRIGGER fail_jump BEFORE INSERT ON move_episodes WHEN NEW.symbol='TWO' BEGIN SELECT RAISE(ABORT,'failure'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.enqueue_jumps(studies,'jump_scan:test',now,30)
            self.assertEqual(store.episodes(),[])
            self.assertIsNone(store.state('jump_scan:test'))
            with store.connect() as conn:
                conn.execute('DROP TRIGGER fail_jump')
            manifest = store.enqueue_jumps(studies,'jump_scan:test',now,30)
            self.assertEqual(manifest['selected'],2)
            restarted = MoveStore(store.path)
            self.assertEqual(restarted.enqueue_jumps([], 'jump_scan:test',now,30),manifest)
            self.assertEqual(len(restarted.episodes()),2)

    def test_observed_paths_are_not_hidden_by_the_pending_history_backlog(self):
        now = utc('2026-10-13T18:00:00Z')
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            for n in range(80):
                store.add(fixtures.feature('STUDY'+str(n)),fixtures.session(),
                          'JUMP_CASE','jump',now,prospective=False)
            first = store.episodes()[0]
            result = observe_session(fixtures.frame().iloc[1:],fixtures.session())
            result['jump_path'] = jump_path(fixtures.frame().iloc[1:],fixtures.session())
            store.settle(first['episode_key'],result,now)
            _,packet = store.publish(now,dict(state='READY',completed_session='2026-10-12'))
            self.assertEqual(packet['latest_cases'][0]['symbol'],first['symbol'])


if __name__ == '__main__':
    unittest.main()
