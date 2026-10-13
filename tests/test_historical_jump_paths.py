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


if __name__ == '__main__':
    unittest.main()
