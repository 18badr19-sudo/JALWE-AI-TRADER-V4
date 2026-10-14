import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from market_jump_common import enrich_profile, summarize_common, PROTOCOL
from market_jump_profiles import FACTORS, build_profile, upgrade_saved_profiles, summarize_profiles
from market_jump_requests import schedule_studies
from market_jump_paths import candidates
from market_move_research import MoveStore, observe_session, utc, report_text, read_packet
from market_move_research_loop import MoveResearchWorker
import test_market_jump_profiles as profile_fixtures
import test_market_jump_requests as request_fixtures
import test_market_move_research as fixtures


class CommonJumpTests(unittest.TestCase):
    def setUp(self):
        self.sessions = [fixtures.session(d) for d in ('2026-10-08','2026-10-09','2026-10-12')]
        self.daily = {'TEST':[fixtures.bar('2026-10-08'),
                              fixtures.bar('2026-10-09',close=11.,volume=300000),
                              fixtures.bar('2026-10-12',close=100.,volume=999999)]}
        self.row = profile_fixtures.JumpProfileTests().row()
        self.row['session'] = fixtures.session()

    def enrich(self, row=None, daily=None, sessions=None):
        row = row or self.row
        return enrich_profile(row['result']['jump_path']['profile'],row,
            self.daily if daily is None else daily,self.sessions if sessions is None else sessions)

    def test_only_completed_prior_sessions_explain_even_opening_jumps(self):
        p = self.enrich(); ctx = p['prior_day_context']
        self.assertEqual(ctx['evidence_day'],'2026-10-09')
        self.assertEqual(ctx['comparison_day'],'2026-10-08')
        self.assertEqual(ctx['volume_ratio_previous_day'],3.)
        self.assertTrue(all(f['state']=='PRESENT' for f in p['prior_day_factors']))
        changed = copy.deepcopy(self.daily); changed['TEST'][-1]=fixtures.bar('2026-10-12',close=500.,volume=1)
        self.assertEqual(p,self.enrich(daily=changed))
        self.assertNotIn('common_protocol',self.row['result']['jump_path']['profile'])

    def test_missing_duplicate_nonfinite_or_future_context_remains_unknown(self):
        missing = {'TEST':self.daily['TEST'][1:]}
        duplicate = {'TEST':self.daily['TEST']+[self.daily['TEST'][0]]}
        bad = copy.deepcopy(self.daily); bad['TEST'][0]['v']=float('nan')
        boolean = copy.deepcopy(self.daily); boolean['TEST'][0]['v']=True
        sessions = copy.deepcopy(self.sessions); sessions[1]['close']='2026-10-12T13:29:00Z'
        for daily,calendar in ((missing,self.sessions),(duplicate,self.sessions),(bad,self.sessions),
                               (boolean,self.sessions),(self.daily,sessions)):
            p = self.enrich(daily=daily,sessions=calendar)
            self.assertEqual(p['prior_day_context']['status'],'UNAVAILABLE')
            self.assertTrue(all(f['state']=='UNKNOWN' for f in p['prior_day_factors']))

    def test_counts_and_cooccurrences_use_intersection_not_missing_as_absent(self):
        profiles=[]
        for symbol,gain in (('ONE',30.),('TWO',60.),('THREE',110.),('UNKNOWN',40.),('SMALL',10.)):
            row=copy.deepcopy(self.row); row['symbol']=symbol
            row['result']['jump_path']['observed_rise_pct']=gain
            p=self.enrich()
            if symbol=='UNKNOWN':
                p=self.enrich(daily={})
            profiles.append((row,p))
        result=summarize_common(profiles,FACTORS)
        self.assertEqual([g['cases'] for g in result['large_jump_groups']],[4,2,1])
        first=result['large_jump_groups'][0]
        shared=next(f for f in first['cooccurrences'] if f['codes']==['PRIOR_DAY_VOLUME','PRIOR_DAY_MOMENTUM'])
        self.assertEqual((shared['present'],shared['known'],shared['unknown']),(3,3,1))
        self.assertEqual(shared['present_pct'],100.)
        self.assertEqual(len(first['cooccurrences']),21)
        self.assertFalse(result['automatic_application'])
        self.assertEqual(result['control_group_count'],0)
        # An unversioned/duplicated claim must never turn into evidence.
        profiles[0][1].pop('common_protocol')
        self.assertEqual(summarize_common(profiles,FACTORS)['large_jump_groups'][0]['factors'][4]['known'],2)

    def test_partial_future_duplicate_and_conflicting_rows_never_inflate_common_memory(self):
        row=copy.deepcopy(self.row); row['result']['jump_path']['profile']=self.enrich()
        partial=dict(row,status='PARTIAL_DATA'); future=dict(row,prospective=True)
        summary=summarize_profiles([row,copy.deepcopy(row),partial,future])
        self.assertEqual(summary['common_factors']['large_jump_groups'][0]['cases'],1)
        conflict=copy.deepcopy(row); conflict['result']['jump_path']['profile']['prior_day_factors'][0]['state']='ABSENT'
        self.assertEqual(summarize_profiles([row,conflict])['common_factors']['all_jumps']['cases'],0)

    def test_upgrade_preserves_existing_raw_profile_and_episode_audit_fields(self):
        now=utc('2026-10-14T09:00:00Z'); data,path=profile_fixtures.JumpProfileTests().evidence()
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'move.db')
            store.state('daily_cache',dict(rows=self.daily))
            store.state('jump_session_calendar',dict(sessions=self.sessions))
            store.add(fixtures.feature('TEST'),fixtures.session(),'JUMP_CASE','TEST',now,prospective=False)
            row=store.episodes()[0]; result=observe_session(data,fixtures.session()); result['jump_path']=path
            store.settle(row['episode_key'],result,now); original=store.episodes()[0]
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],1)
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)
            after=store.episodes()[0]; p=after['result']['jump_path']['profile']
            self.assertEqual(p['common_protocol'],PROTOCOL)
            for key in path['profile']:
                self.assertEqual(p[key],path['profile'][key])
            for key in ('status','attempts','checked_at','frozen_at','features_json'):
                self.assertEqual(after[key],original[key])
            store.publish(now,dict(state='READY'))
            packet=read_packet(path=store.path,now=now)
            self.assertEqual(packet['comparison']['complete_pairs'],0)
            text=report_text(packet)
            self.assertIn('حجم اليوم السابق ≥2×: 1/1 معروف',text)
            self.assertIn('قفزات مكتملة ≥25/50/100%',text)
            self.assertLessEqual(len(text),3900)

    def test_discovery_prefers_upward_candidates_without_starving_forward_or_requests(self):
        make=request_fixtures.RequestedJumpTests().row
        crash=make('CRASH',bound=10000); jump=make('JUMP',bound=30)
        request=make('MI',bound=10); forward=make('FORWARD',prospective=True)
        rows=[crash,jump,request,forward]
        overlay={'CRASH|2026-10-09':0.,'JUMP|2026-10-09':50.,'FORWARD|2026-10-09':99999.}
        self.assertEqual(schedule_studies(rows,{('MI','2026-10-09')},discovery_bounds=overlay),
                         [forward,request,jump,crash])
        self.assertEqual(crash['features']['daily_screen_bound_pct'],10000)
        retry=make('RETRY',bound=100000,status='PARTIAL_DATA')
        overlay['RETRY|2026-10-09']=100000
        self.assertEqual(schedule_studies([retry,jump],set(),discovery_bounds=overlay),[jump,retry])

    def test_daily_upward_bound_cannot_turn_a_crash_range_into_a_confirmed_jump(self):
        bars=[fixtures.bar('2026-10-09'),dict(fixtures.bar('2026-10-12'),o=10.,h=10.,l=1.,c=1.)]
        feat,_=next(candidates(['CRASH'],{'CRASH':bars},self.sessions,utc('2026-10-14T09:00:00Z')))
        self.assertEqual(feat['daily_screen_bound_pct'],900.)
        self.assertEqual(feat['daily_up_bound_pct'],0.)
        self.assertNotIn('strong_jump',feat)

    def test_unavailable_profile_enrichment_is_versioned_without_claiming_evidence(self):
        result=enrich_profile(dict(status='UNAVAILABLE'),self.row,{},[])
        self.assertEqual(result['common_protocol'],PROTOCOL)
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertNotIn('prior_day_factors',result)

    def test_missing_daily_context_recovers_later_without_replacing_known_evidence(self):
        now=utc('2026-10-14T09:00:00Z'); data,path=profile_fixtures.JumpProfileTests().evidence()
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'move.db')
            store.state('jump_session_calendar',dict(sessions=self.sessions))
            store.add(fixtures.feature('TEST'),fixtures.session(),'JUMP_CASE','TEST',now,prospective=False)
            row=store.episodes()[0]; result=observe_session(data,fixtures.session()); result['jump_path']=path
            store.settle(row['episode_key'],result,now)
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],1)
            original=store.episodes()[0]
            self.assertEqual(original['result']['jump_path']['profile']['prior_day_context']['status'],'UNAVAILABLE')
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)
            store.state('daily_cache',dict(rows=self.daily))
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],1)
            after=store.episodes()[0]
            self.assertEqual(after['result']['jump_path']['profile']['prior_day_context']['status'],'AVAILABLE')
            for key in ('status','attempts','checked_at','frozen_at','features_json'):
                self.assertEqual(after[key],original[key])
            for key in path['profile']:
                self.assertEqual(after['result']['jump_path']['profile'][key],path['profile'][key])
            changed=copy.deepcopy(self.daily); changed['TEST'][1]['v']=1
            store.state('daily_cache',dict(rows=changed))
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)
            self.assertEqual(store.episodes()[0],after)

    def test_invalid_duplicate_bar_does_not_leave_a_spurious_known_context(self):
        daily=copy.deepcopy(self.daily)
        corrupt=dict(daily['TEST'][1],v=float('nan'))
        daily['TEST'].append(corrupt)
        result=self.enrich(daily=daily)
        self.assertEqual(result['prior_day_context']['status'],'UNAVAILABLE')
        self.assertTrue(all(f['state']=='UNKNOWN' for f in result['prior_day_factors']))
        self.assertEqual(self.enrich(daily=None)['prior_day_context']['status'],'AVAILABLE')
        self.assertEqual(enrich_profile(self.row['result']['jump_path']['profile'],self.row,None,
                                       self.sessions)['prior_day_context']['status'],'UNAVAILABLE')

    def test_available_context_recovery_obeys_shared_write_budget_and_is_idempotent(self):
        now=utc('2026-10-14T09:00:00Z'); data,path=profile_fixtures.JumpProfileTests().evidence()
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'move.db')
            store.state('jump_session_calendar',dict(sessions=self.sessions))
            for symbol in ('ONE','TWO'):
                store.add(fixtures.feature(symbol),fixtures.session(),'JUMP_CASE',symbol,now,prospective=False)
                row=next(r for r in store.episodes() if r['symbol']==symbol)
                result=observe_session(data,fixtures.session()); result['jump_path']=copy.deepcopy(path)
                store.settle(row['episode_key'],result,now)
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],2)
            store.state('daily_cache',dict(rows={s:self.daily['TEST'] for s in ('ONE','TWO')}))
            self.assertEqual(upgrade_saved_profiles(store,now,limit=1),dict(upgraded=1,pending=1,provider_reads=0))
            self.assertEqual(upgrade_saved_profiles(store,now,limit=1),dict(upgraded=1,pending=0,provider_reads=0))
            self.assertEqual(upgrade_saved_profiles(store,now)['upgraded'],0)

    def test_optional_context_failure_preserves_completed_price_observations(self):
        now=utc('2026-10-14T09:00:00Z')
        calendar=[fixtures.session(d) for d in
                  ('2026-10-02','2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09',
                   '2026-10-12','2026-10-13','2026-10-14')]
        provider=Mock(); provider.calendar.return_value=calendar; provider.assets.return_value=['TEST']
        provider.daily.side_effect=RuntimeError('temporary daily outage')
        provider.intraday.side_effect=lambda symbol,s,now:fixtures.frame(s)
        provider.news_before.return_value=dict(status='AVAILABLE',articles=[])
        with tempfile.TemporaryDirectory() as tmp, patch('market_jump_common.enrich_profile',side_effect=RuntimeError('auxiliary failure')):
            store=MoveStore(Path(tmp)/'move.db')
            packet=MoveResearchWorker(store,provider).cycle(now)
            self.assertEqual(packet['requested_jumps']['counts'],dict(COMPLETE=12,PENDING=18))
            self.assertTrue(all(r['result'].get('jump_path',{}).get('status')=='COMPLETE'
                                for r in store.episodes() if r['status']=='COMPLETE'))
            self.assertEqual(packet['comparison']['complete_pairs'],0)

    def test_legacy_path_rebuild_is_bounded_before_computation_not_just_before_writes(self):
        now=utc('2026-10-14T09:00:00Z'); data,path=profile_fixtures.JumpProfileTests().evidence()
        path.pop('profile')
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'move.db')
            for symbol in ('ONE','TWO','THREE'):
                store.add(fixtures.feature(symbol),fixtures.session(),'JUMP_CASE',symbol,now,prospective=False)
                row=next(r for r in store.episodes() if r['symbol']==symbol)
                result=observe_session(data,fixtures.session()); result['jump_path']=copy.deepcopy(path)
                store.settle(row['episode_key'],result,now)
            with patch('market_jump_profiles.build_profile',wraps=build_profile) as rebuild:
                stats=upgrade_saved_profiles(store,now,limit=1)
                self.assertEqual(stats,dict(upgraded=1,pending=2,provider_reads=0))
                self.assertEqual(rebuild.call_count,1)


if __name__=='__main__':
    unittest.main()
