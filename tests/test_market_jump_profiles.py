import copy
from pathlib import Path
import tempfile
import unittest

from market_jump_profiles import build_profile, summarize_profiles, upgrade_saved_profiles, PROTOCOL
from market_jump_paths import jump_path
from market_move_research import MoveStore, observe_session, utc, read_packet, report_text
import test_market_move_research as fixtures


class JumpProfileTests(unittest.TestCase):
    def evidence(self):
        data = fixtures.frame()
        data.iloc[35:40,4] = 500.
        data.iloc[40] = [10.,10.,8.,9.,200.]
        data.iloc[70] = [10.,15.,9.9,12.,900.]
        data.iloc[80] = [10.,10.1,7.,8.,100.]
        path = jump_path(data,fixtures.session(),full_session_complete=True)
        return data,path

    def test_raw_profile_uses_only_twenty_minutes_before_onset(self):
        data,path = self.evidence(); p = path['profile']
        self.assertEqual(p['protocol'],PROTOCOL); self.assertEqual(p['status'],'AVAILABLE')
        before = p['before_context']
        self.assertEqual(before['status'],'RAW_20M_COMPLETE')
        self.assertEqual(before['cutoff'],data.index[40].isoformat())
        self.assertEqual(before['window_start'],data.index[20].isoformat())
        self.assertEqual(before['volume_acceleration'],5.)
        after_only = data.copy(); after_only.iloc[71:,4] = 9000.
        self.assertEqual(build_profile(path,segment=after_only)['before_context'],before)
        data.iloc[41:,4] = 9000.
        changed = build_profile(path,segment=data)
        self.assertEqual(changed['status'],'UNAVAILABLE')  # Conflicting path evidence is not silently substituted.

    def test_early_onset_missing_context_is_unknown_not_a_negative(self):
        data = fixtures.frame(); data.iloc[3] = [10.,15.,9.9,12.,1000.]
        p = jump_path(data,fixtures.session(),full_session_complete=True)['profile']
        self.assertEqual(p['before_context']['status'],'UNAVAILABLE')
        self.assertTrue(all(f['state']=='UNKNOWN' for f in p['factors']))

    def test_same_minute_low_cannot_precede_its_high_in_milestones(self):
        data,path = self.evidence()
        subset = data.iloc[40:43].copy()
        subset.iloc[0] = [10.,100.,8.,9.,100.]
        subset.iloc[1] = [9.,10.,8.9,9.,100.]
        subset.iloc[2] = [9.,12.,8.9,10.,100.]
        manual = dict(path,start_at=subset.index[0].isoformat(),peak_at=subset.index[-1].isoformat(),
            start_price=8.,peak_price=12.,start_price_source='PRIOR_MINUTE_LOW',
            trajectory=[dict(at=t.isoformat(),**{c:float(row[c]) for c in subset.columns})
                        for t,row in subset.iterrows()])
        p = build_profile(manual)
        first = next(m for m in p['milestones'] if m['threshold_pct']==8)
        self.assertEqual(first['at'],subset.index[1].isoformat())
        self.assertEqual(next(m for m in p['milestones'] if m['threshold_pct']==100)['at'],None)

    def test_same_bar_high_low_is_not_a_prior_minute_drawdown(self):
        data = fixtures.frame().iloc[:1].copy(); data.iloc[0] = [10.,20.,5.,12.,100.]
        p = jump_path(data,fixtures.session())['profile']
        self.assertEqual(p['max_prior_minute_peak_to_later_low_decline_pct'],0.)
        self.assertEqual(p['volume_phases'],[])

    def test_post_peak_windows_exclude_peak_minute_and_fail_on_any_gap(self):
        data,path = self.evidence(); p = path['profile']
        five = next(w for w in p['after_peak'] if w['minutes']==5)
        self.assertEqual(five['status'],'COMPLETE_WINDOW')
        self.assertAlmostEqual(five['max_decline_from_peak_pct'],34.)
        missing = build_profile(path,segment=data.drop(data.index[73]))
        self.assertEqual(next(w for w in missing['after_peak'] if w['minutes']==5)['status'],'UNKNOWN_INCOMPLETE_WINDOW')
        self.assertNotIn('max_decline_from_peak_pct',missing['after_peak'][0])

    def test_volume_phases_partition_real_observed_bars(self):
        data,path = self.evidence(); phases = path['profile']['volume_phases']
        self.assertEqual(sum(p['bars'] for p in phases),len(path['trajectory']))
        self.assertEqual(sum(p['volume'] for p in phases),path['phase_volume'])
        self.assertAlmostEqual(sum(p['volume_share_pct'] for p in phases),100.)

    def test_nonfinite_pre_context_aggregates_cannot_become_absent_factors(self):
        data,path = self.evidence(); data.iloc[20:40,4] = 1e308
        p = build_profile(path,segment=data)
        self.assertEqual(p['before_context']['status'],'UNAVAILABLE_NONFINITE_AGGREGATES')
        self.assertTrue(all(f['state']=='UNKNOWN' for f in p['factors']))

    def test_saved_path_upgrade_does_not_invent_post_peak_or_raw_pre_range(self):
        _,path = self.evidence(); p = build_profile(path)
        self.assertEqual(p['before_context']['status'],'SAVED_DERIVED_CONTEXT_ONLY')
        self.assertEqual(p['before_context']['volume_acceleration'],5.)
        self.assertEqual(next(f for f in p['factors'] if f['code']=='TIGHT_PRE_RANGE')['state'],'UNKNOWN')
        self.assertTrue(all(w['status']=='UNKNOWN_NOT_RECORDED' for w in p['after_peak']))

    def test_invalid_path_or_context_is_unavailable(self):
        data,path = self.evidence()
        bad = copy.deepcopy(path); bad['trajectory'].append(bad['trajectory'][-1])
        self.assertEqual(build_profile(bad)['status'],'UNAVAILABLE')
        bad = copy.deepcopy(path); bad['trajectory'][4]['volume']=float('nan')
        self.assertEqual(build_profile(bad)['status'],'UNAVAILABLE')
        for raw in (data.tz_localize(None),data.assign(volume=0),data.iloc[::-1]):
            self.assertEqual(build_profile(path,segment=raw)['status'],'UNAVAILABLE')

    def row(self,symbol='TEST',partial=False):
        data,path = self.evidence()
        if partial:
            path = jump_path(data.drop(data.index[25]),fixtures.session())
        return dict(symbol=symbol,session_day='2026-10-12',role='JUMP_CASE',prospective=False,
            status='PARTIAL_DATA' if partial else 'COMPLETE',result={'jump_path':path})

    def test_memory_excludes_partial_and_prospective_and_deduplicates(self):
        row = self.row(); partial = self.row('PARTIAL',True); future = dict(row,prospective=True)
        summary = summarize_profiles([row,copy.deepcopy(row),partial,future])
        self.assertEqual(summary['completed_profiles'],1)
        self.assertEqual(summary['partial_profiles'],1)
        self.assertEqual(next(f for f in summary['factors'] if f['code']=='VOLUME_BUILDUP')['known'],1)
        conflicting = copy.deepcopy(row); conflicting['result']['jump_path']['profile']['factors'][0]['state']='ABSENT'
        summary = summarize_profiles([row,conflicting])
        self.assertEqual(summary['completed_profiles'],0)
        self.assertEqual(summary['conflicting_symbol_sessions'],1)
        self.assertFalse(summary['automatic_application'])

    def test_profile_only_upgrade_is_bounded_idempotent_and_preserves_decisions(self):
        now = utc('2026-10-14T08:00:00Z'); data,path = self.evidence(); path.pop('profile')
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            for symbol in ('ONE','TWO'):
                store.add(fixtures.feature(symbol),fixtures.session(),'JUMP_CASE',symbol,now,prospective=False)
                row = next(r for r in store.episodes() if r['symbol']==symbol)
                result = observe_session(data,fixtures.session()); result['jump_path']=copy.deepcopy(path)
                store.settle(row['episode_key'],result,now)
            before = store.episodes()
            self.assertEqual(upgrade_saved_profiles(store,now,limit=1)['upgraded'],1)
            self.assertEqual(upgrade_saved_profiles(store,now,limit=1)['upgraded'],1)
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)
            for original,current in zip(before,store.episodes()):
                result = copy.deepcopy(current['result']); result['jump_path'].pop('profile')
                self.assertEqual(result,original['result'])
                for key in ('status','attempts','checked_at','frozen_at','features_json'):
                    self.assertEqual(original[key],current[key])
            store.publish(now,{'state':'READY','completed_session':'2026-10-13'})
            packet = read_packet(path=store.path,now=now)
            self.assertEqual(packet['historical_jump_profiles']['completed_profiles'],2)
            self.assertEqual(packet['comparison']['complete_pairs'],0)
            self.assertIn('لا توجد مجموعة ضابطة',report_text(packet))

    def test_upgrade_cannot_replace_a_changed_result_or_touch_forward_records(self):
        now = utc('2026-10-14T08:00:00Z'); data,path = self.evidence(); path.pop('profile')
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            store.add(fixtures.feature(),fixtures.session(),'JUMP_CASE','case',now,prospective=False)
            row = store.episodes()[0]; result = observe_session(data,fixtures.session()); result['jump_path']=path
            store.settle(row['episode_key'],result,now)
            wrong = copy.deepcopy(result); wrong['outcome']='NO_BARRIER'
            self.assertFalse(store.enrich_jump_profile(row['episode_key'],wrong,build_profile(path),now))
            self.assertNotIn('profile',store.episodes()[0]['result']['jump_path'])
            store.add(fixtures.feature('FORWARD'),fixtures.session(),'ACTIVITY','forward',
                      utc('2026-10-12T12:00:00Z'),prospective=True)
            forward = next(r for r in store.episodes() if r['symbol']=='FORWARD')
            store.settle(forward['episode_key'],result,now)
            self.assertFalse(store.enrich_jump_profile(forward['episode_key'],result,build_profile(path),now))

    def test_saved_profile_upgrade_respects_historical_availability_delay(self):
        data,path = self.evidence(); path.pop('profile'); s = fixtures.session()
        now = utc(s['close'])
        with tempfile.TemporaryDirectory() as tmp:
            store = MoveStore(Path(tmp)/'moves.db')
            store.add(fixtures.feature(),s,'JUMP_CASE','case',now,prospective=False)
            row = store.episodes()[0]; result = observe_session(data,s); result['jump_path']=path
            store.settle(row['episode_key'],result,now)
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)


if __name__=='__main__':
    unittest.main()
