import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import market_move_patterns as patterns
from market_move_research import MoveStore, utc, report_text, read_packet
import test_market_move_research as fixtures
import jalwe_research_watcher as watcher


class PatternTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'moves.db'; self.store = MoveStore(self.path)
        self.defined = patterns.definition(self.store,utc('2026-09-01T00:00:00Z'))['defined_at']
        self.train_days = pd.bdate_range('2026-09-01',periods=10).strftime('%Y-%m-%d').tolist()
        self.train_at = utc('2026-09-15T21:00:00Z')
        self.rows = self.cohorts(self.train_days)
        for day in self.train_days[:3]:
            r = copy.deepcopy(next(r for r in self.rows if r['session_day']==day and r['role']=='ACTIVITY'))
            r.update(prospective=False,role='HOT_CASE',pair_key='hot:'+day)
            self.rows.append(r)

    def cohorts(self, days, *, success=True):
        rows = []
        for day in days:
            preceding = (pd.Timestamp(day)-pd.offsets.BDay(1)).strftime('%Y-%m-%d')
            for i in range(8):
                for role in ('ACTIVITY','CONTROL'):
                    case = role=='ACTIVITY'
                    f = fixtures.feature(f'{role}{i}',day=preceding,case=case)
                    f.update(volume_ratio_previous_day=4. if case else 1.,range_pct=10. if case else 3.,
                        daily_return_pct=5. if case else 0.,pattern_protocol=patterns.PROTOCOL,
                        pattern_spec_hash=patterns.SPEC,pattern_defined_at=self.defined)
                    label = 'UP8_FIRST' if case==success else 'DOWN3_FIRST'
                    rows.append(dict(symbol=f['symbol'],features=f,features_json=json.dumps(f),
                        session_day=day,session=fixtures.session(day),role=role,prospective=True,
                        frozen_at=day+'T12:00:00Z',pair_key=day+':'+str(i),status='COMPLETE',
                        result={'outcome':label},attempts=1))
        return rows

    def train(self):
        model = patterns.update(self.store,self.rows,self.train_at)
        self.assertEqual(model['state'],'WAITING_FRESH_VALIDATION')
        return model

    def activate(self):
        self.train()
        days = pd.bdate_range('2026-09-16',periods=20).strftime('%Y-%m-%d').tolist()
        self.validation_rows = self.cohorts(days)
        self.active_at = utc(days[-1]+'T21:00:00Z')
        model = patterns.update(self.store,self.rows+self.validation_rows,self.active_at)
        self.assertEqual(model['state'],'ACTIVE_PRIORITY')
        self.assertGreater(model['validation']['lower_bound'],0)
        return model

    def test_old_unversioned_and_late_features_cannot_train_or_activate(self):
        old = copy.deepcopy(self.rows)
        for row in old:
            row['features'].pop('pattern_protocol',None)
        self.assertEqual(patterns.update(self.store,old,self.train_at)['state'],'COLLECTING_TRAINING')
        invalid = copy.deepcopy(self.rows)
        for row in invalid:
            row['features']['available_after'] = row['session']['open']
        self.assertEqual(patterns.update(self.store,invalid,self.train_at)['state'],'COLLECTING_TRAINING')

    def test_retrospective_winners_support_hypotheses_but_never_validation(self):
        hot = [r for r in self.rows if not r['prospective']]
        model = patterns.update(self.store,hot*100,self.train_at)
        self.assertEqual(model['state'],'COLLECTING_TRAINING')
        self.assertTrue(all(v['matched_pairs']==0 for v in model['candidates'].values()))

    def test_winner_is_sealed_and_requires_twenty_fresh_session_cohorts(self):
        sealed = self.train(); rule = sealed['rule']
        days = pd.bdate_range('2026-09-16',periods=19).strftime('%Y-%m-%d').tolist()
        early = patterns.update(self.store,self.rows+self.cohorts(days),utc(days[-1]+'T21:00:00Z'))
        self.assertEqual(early['state'],'WAITING_FRESH_VALIDATION')
        self.assertEqual(early['rule'],rule)
        # Altering already-used training outcomes cannot change the sealed winner.
        changed = copy.deepcopy(self.rows)
        for row in changed:
            row['result']['outcome']='NO_BARRIER'
        result = patterns.update(self.store,changed+self.cohorts(days),utc(days[-1]+'T21:00:00Z'))
        self.assertEqual(result['training'],sealed['training'])
        self.assertEqual(result['rule'],rule)

    def test_complete_negative_holdout_fails_without_searching_another_winner(self):
        sealed = self.train()
        days = pd.bdate_range('2026-09-16',periods=20).strftime('%Y-%m-%d').tolist()
        result = patterns.update(self.store,self.rows+self.cohorts(days,success=False),utc(days[-1]+'T21:00:00Z'))
        self.assertEqual(result['state'],'FAILED_VALIDATION')
        self.assertEqual(result['rule'],sealed['rule'])
        self.assertNotIn('activated_at',result)

    def test_holdout_days_do_not_shift_when_an_original_cohort_disappears(self):
        self.train()
        days = pd.bdate_range('2026-09-16',periods=21).strftime('%Y-%m-%d').tolist()
        original = self.cohorts(days[:20])
        original[0]['status']='PENDING'
        now = utc(days[19]+'T21:00:00Z')
        self.assertEqual(patterns.update(self.store,self.rows+original,now)['state'],'WAITING_FRESH_VALIDATION')
        remaining = [r for r in original if r['session_day'] != days[0]]
        later = self.cohorts([days[20]])
        model = patterns.update(self.store,self.rows+remaining+later,utc(days[20]+'T21:00:00Z'))
        self.assertEqual(model['validation']['session_days'],days[:20])
        self.assertAlmostEqual(model['validation']['complete_pair_coverage'],.95)

    def test_holdout_registration_commits_and_rolls_back_with_the_cohort(self):
        self.train()
        day='2026-09-16'; rows=self.cohorts([day]); pair=[r['features'] for r in rows[:2]]
        stamp=utc(day+'T12:00:00Z'); opening=utc(day+'T13:30:00Z')
        clock=Mock(side_effect=[opening-timedelta(seconds=1),opening])
        self.assertFalse(self.store.freeze_cohort([pair],fixtures.session(day),stamp,clock=clock))
        self.assertEqual(self.store.state('pattern_policy').get('validation_plan',[]),[])
        self.assertTrue(self.store.freeze_cohort([pair],fixtures.session(day),stamp))
        plan=self.store.state('pattern_policy')['validation_plan']
        self.assertEqual(len(plan),1); self.assertEqual(plan[0]['day'],day)
        self.assertEqual(len(plan[0]['members']),2)
        self.assertTrue(all(r['status']=='PENDING' for r in self.store.episodes()))

    def test_tampered_proof_or_current_policy_cannot_apply_priority(self):
        model=self.activate(); now=self.active_at+timedelta(hours=1)
        self.assertTrue(patterns.active_proof(model,now))
        changes=({'alpha':float('inf')},{'generation':True},{'policy_id':'wrong'},
                 {'training':{}},{'activated_at':'2027-01-01T00:00:00Z'})
        for change in changes:
            self.assertFalse(patterns.active_proof(dict(model,**change),now))
        for value in (float('nan'),float('inf'),None,True,1.):
            bad=copy.deepcopy(model); bad['validation']['lower_bound']=value
            self.assertFalse(patterns.active_proof(bad,now))

    def test_pending_missing_and_ambiguous_evidence_cannot_activate(self):
        self.train()
        days = pd.bdate_range('2026-09-16',periods=20).strftime('%Y-%m-%d').tolist()
        validation = self.cohorts(days)
        validation[-1]['status']='PENDING'
        now = utc(days[-1]+'T21:00:00Z')
        model = patterns.update(self.store,self.rows+validation,now)
        self.assertEqual(model['state'],'WAITING_FRESH_VALIDATION')
        for row in validation:
            row.update(status='PARTIAL_DATA',attempts=3,result={'outcome':None})
        model = patterns.update(self.store,self.rows+validation,now)
        self.assertEqual(model['state'],'FAILED_VALIDATION')

    def test_day_level_uncertainty_is_not_zero_for_constant_perfect_results(self):
        model = self.activate()
        self.assertLess(model['validation']['lower_bound'],model['validation']['daily_edge'])
        self.assertEqual(model['validation']['alpha'],.005)

    def test_ambiguous_holdout_and_unknown_features_do_not_become_successes(self):
        self.train()
        days = pd.bdate_range('2026-09-16',periods=20).strftime('%Y-%m-%d').tolist()
        rows = self.cohorts(days)
        for row in rows[:80]:
            row['result']['outcome']='AMBIGUOUS'
        model = patterns.update(self.store,self.rows+rows,utc(days[-1]+'T21:00:00Z'))
        self.assertEqual(model['state'],'FAILED_VALIDATION')
        self.assertLess(model['validation']['complete_pair_coverage'],.9)
        self.assertIsNone(patterns.matches({'volume_ratio_previous_day':float('nan'),'range_pct':10},'VOLUME_RANGE'))

    def test_stale_validation_cannot_activate_and_active_policy_expires(self):
        self.train()
        days = pd.bdate_range('2026-09-16',periods=20).strftime('%Y-%m-%d').tolist()
        model = patterns.update(self.store,self.rows+self.cohorts(days),utc(days[-1]+'T21:00:00Z')+timedelta(days=9))
        self.assertEqual(model['state'],'FAILED_VALIDATION')
        self.store.state('pattern_policy',dict(protocol=patterns.PROTOCOL,spec_hash=patterns.SPEC,
            generation=1,started_at=self.defined,state='COLLECTING_TRAINING',mode=patterns.MODE,rule=None))
        self.activate()
        model = patterns.update(self.store,self.rows+self.validation_rows,self.active_at+timedelta(days=14))
        self.assertEqual(model['state'],'EXPIRED')

    def test_feature_snapshot_crossing_open_rolls_back_and_priority_never_uses_future_inputs(self):
        features=[self.cohorts(['2026-10-14'])[0]['features']]
        opening=utc('2026-10-14T13:30:00Z')
        clock=Mock(side_effect=[opening-timedelta(seconds=1),opening])
        self.assertFalse(self.store.save_pattern_features(features,fixtures.session('2026-10-14'),
            opening-timedelta(seconds=1),clock=clock))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM move_pattern_features').fetchone()[0],0)
        self.assertIsNone(self.store.state('pattern_features:2026-10-14'))

    def test_degradation_suspends_and_next_generation_uses_only_new_frozen_data(self):
        self.activate()
        days = pd.bdate_range('2026-10-14',periods=5).strftime('%Y-%m-%d').tolist()
        rows = self.rows+self.validation_rows+self.cohorts(days,success=False)
        stamp = utc(days[-1]+'T21:00:00Z')
        model = patterns.update(self.store,rows,stamp)
        self.assertEqual(model['state'],'SUSPENDED')
        model = patterns.update(self.store,rows,stamp+timedelta(days=8))
        self.assertEqual(model['generation'],2)
        self.assertEqual(model['state'],'COLLECTING_TRAINING')
        self.assertTrue(all(v['matched_pairs']==0 for v in model['candidates'].values()))

    def test_duplicate_roles_or_repeated_symbols_cannot_inflate_sample_size(self):
        eligible = patterns.eligible_rows(self.rows,self.defined,self.defined)
        result = patterns.assessment(patterns.paired(eligible+eligible),'VOLUME_RANGE')
        self.assertEqual(result['matched_pairs'],0)
        doubled = copy.deepcopy(eligible)
        for row in doubled:
            row['pair_key'] += ':duplicate'
        result = patterns.assessment(patterns.paired(eligible+doubled),'VOLUME_RANGE')
        self.assertLess(result['complete_pair_coverage'],.9)

    def test_priority_is_a_stable_paper_permutation_and_current_policy_is_required(self):
        model = self.activate()
        day = '2026-10-14'; now = utc(day+'T14:00:00Z')
        rows = self.cohorts([day])
        self.store.save_pattern_features([r['features'] for r in rows],fixtures.session(day),utc(day+'T12:00:00Z'))
        self.store.publish(now,{'state':'READY'},patterns=model)
        items = [NS(symbol='UNKNOWN'),NS(symbol='CONTROL0'),NS(symbol='ACTIVITY0'),NS(symbol='ACTIVITY1')]
        kwargs=dict(now=now,path=self.path,symbol_of=lambda r:r.symbol,paper=True,allow_live=False)
        ordered,audit = patterns.prioritize(items,**kwargs)
        self.assertEqual([r.symbol for r in ordered],['ACTIVITY0','ACTIVITY1','UNKNOWN','CONTROL0'])
        self.assertEqual(audit['mode'],patterns.MODE)
        self.assertEqual(set(map(id,ordered)),set(map(id,items)))
        for override in ({'paper':False},{'allow_live':True},{'now':now+timedelta(hours=2)}):
            self.assertEqual(patterns.prioritize(items,**dict(kwargs,**override))[0],items)
        model['state']='SUSPENDED'; self.store.state('pattern_policy',model)
        self.assertEqual(patterns.prioritize(items,**kwargs)[0],items)

    def test_nonfinite_validation_proof_cannot_apply_paper_priority(self):
        model = self.activate()
        day='2026-10-14'; now=utc(day+'T14:00:00Z')
        self.store.save_pattern_features([r['features'] for r in self.cohorts([day])],
            fixtures.session(day),utc(day+'T12:00:00Z'))
        self.store.publish(now,{'state':'READY'},patterns=model)
        with self.store.connect() as conn:
            row=conn.execute('SELECT payload_json FROM move_packets').fetchone()
            encoded=json.loads(row[0]); encoded['patterns']['validation']['lower_bound']=float('nan')
            conn.execute('UPDATE move_packets SET payload_json=?',(json.dumps(encoded),))
        items=[NS(symbol='CONTROL0'),NS(symbol='ACTIVITY0')]
        ordered,audit=patterns.prioritize(items,now=now,path=self.path,symbol_of=lambda r:r.symbol,
                                         paper=True,allow_live=False)
        self.assertEqual(ordered,items)
        self.assertEqual(audit['mode'],'BASELINE')

    def test_pattern_inputs_are_immutable_and_cannot_be_prepared_after_open(self):
        day = '2026-10-14'; rows = self.cohorts([day]); features=[r['features'] for r in rows]
        stamp = utc(day+'T12:00:00Z')
        self.assertTrue(self.store.save_pattern_features(features,fixtures.session(day),stamp))
        self.assertFalse(self.store.save_pattern_features(features,fixtures.session(day),stamp))
        self.assertFalse(self.store.save_pattern_features(features,fixtures.session('2026-10-15'),
            utc('2026-10-15T14:00:00Z')))

    def test_optional_reader_failures_keep_original_order_without_private_text(self):
        items = [NS(symbol='A')]
        with patch.object(patterns,'read_packet',side_effect=PermissionError('private path')):
            ordered,audit = patterns.prioritize(items,now=self.train_at,symbol_of=lambda r:r.symbol,paper=True,allow_live=False)
        self.assertEqual(ordered,items)
        self.assertNotIn('private',json.dumps(audit))

    def test_report_exposes_learning_stage_and_application_scope(self):
        model = self.train(); self.store.publish(self.train_at,{'state':'READY'},patterns=model)
        message = report_text(read_packet(path=self.path,now=self.train_at))
        self.assertIn('20 جلسة لاحقة',message)
        self.assertIn('الشراء يمر بالشروط الأصلية',message)

    def test_watcher_priority_failure_does_not_skip_native_processing_or_position_management(self):
        items = [NS(symbol='A'),NS(symbol='B')]
        bridge = Mock(); bridge.list_recent_research.return_value=items; bridge.get_age_minutes.return_value=1
        with patch.object(patterns,'prioritize',side_effect=RuntimeError('private message')), \
             patch.object(watcher,'should_process',return_value=(True,'')), \
             patch.object(watcher,'process_research',return_value=True) as process, \
             patch.object(watcher,'manage_active_paper_trades') as manage, \
             patch.object(watcher,'process_emergency_paper_close'):
            self.assertEqual(watcher.scan_bridge(bridge,Mock(),{}),(2,2))
        self.assertEqual([c.args[0] for c in process.call_args_list],items)
        self.assertEqual(manage.call_count,2)


if __name__=='__main__':
    unittest.main()
