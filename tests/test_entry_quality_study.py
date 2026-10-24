from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch,Mock
import json,tempfile,unittest
import entry_quality_study as quality
import loss_reduction_study as loss
import test_decision_outcome_memory as fixtures
import test_loss_reduction_study as plans

NOW=datetime(2026,10,24,tzinfo=timezone.utc)


def cohort():
    rows=[]
    for i in range(16):
        day=(datetime(2026,9,1)+timedelta(days=i)).date().isoformat()
        for j in range(8):
            # Artificial association; no real trade or market/provider evidence.
            good=j%2==0;pnl=.8 if good else -.7
            rows.append(dict(day=day,symbol='S'+str(j),trial_key=f'{day}-{j}',decision_feed='iex',closed=True,
                path_known=True,facts=dict(score=75.+j,grade='A+' if j<6 else 'B',liquidity_score=80. if good else 20.),
                results={str(c):dict(traded=True,net_pnl=pnl-(c-.001)*100.) for c in quality.SPEC['costs']}))
    return rows


class QualityStudyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'native.db';self.db=fixtures.Store(self.path)
        self.plan=plans.plan('2026-10-21T14:00:00-04:00')
        self.at=loss.utc(self.plan['decided_at']);self.clock=lambda:self.at+timedelta(seconds=1)
        self.study=loss.LossReductionStudy(self.db,calendar_reader=lambda at:deepcopy(self.plan['calendar']))
        with self.db.connection() as c:c.execute('DELETE FROM entry_quality_definitions')
        self.recorder=quality.Recorder(self.db,self.at-timedelta(days=1),clock=self.clock)
        self.study.quality_study=self.recorder

    def payload(self):
        return dict(symbol='TEST',timestamp=self.at.isoformat(),jalwe=dict(
            setup_grade='A+',opportunity_score=90.,metadata=dict(decision_data_feed='iex',quality_inputs=dict(timeframe='1m'),
            loss_study_stop_context=deepcopy(self.plan['stop_context']),decision_features=dict(
                timestamp=(self.at-timedelta(minutes=1)).isoformat(),data_quality_ok=True,
                data_is_stale=False,liquidity_score=45.,secret='DO_NOT_EXPORT'))))

    def record(self,key='one',payload=None):
        p=self.payload() if payload is None else payload
        with self.db.connection() as c:self.study.record(c,key,'decision',p,dict(trigger_price=10.,invalidation_price=9.8))

    def test_original_facts_are_frozen_once_per_symbol_day_not_replaced_by_later_grade(self):
        p=self.payload();self.record(payload=p)
        p['jalwe']['setup_grade']='B';p['jalwe']['metadata']['decision_features']['liquidity_score']=99.
        self.record('two',p)
        with self.db.connection() as c:
            rows=c.execute('SELECT * FROM entry_quality_trials').fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0]['facts_json'])['liquidity_score'],45.)
        self.assertNotIn('DO_NOT_EXPORT',rows[0]['facts_json'])

    def test_old_eight_trades_future_time_and_sip_decisions_cannot_enter_cohort(self):
        for change in ('old','future','sip'):
            p=self.payload()
            if change=='old':p['timestamp']=(self.at-timedelta(days=3)).isoformat()
            elif change=='future':p['timestamp']=(self.at+timedelta(minutes=1)).isoformat()
            else:p['jalwe']['metadata']['decision_data_feed']='sip'
            self.record(change,p)
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM entry_quality_trials').fetchone()[0],0)

    def test_invalid_numbers_unclosed_stale_and_missing_features_stay_unknown(self):
        cases=[]
        for v in (True,'70',float('nan'),10**1000,-1.,101.):
            p=self.payload();p['jalwe']['metadata']['decision_features']['liquidity_score']=v;cases.append(p)
        for name in ('unclosed','stale','old','missing'):
            p=self.payload();f=p['jalwe']['metadata']['decision_features']
            if name=='unclosed':f['timestamp']=p['timestamp']
            elif name=='stale':f['data_is_stale']=True
            elif name=='old':f['timestamp']=(self.at-timedelta(hours=1)).isoformat()
            else:p['jalwe']['metadata']['quality_inputs']={}
            cases.append(p)
        for p in cases:self.assertIsNone(quality.entry_facts(p))
        self.record(payload=cases[0])
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT facts_json FROM entry_quality_trials').fetchone()[0],'null')

    def test_definition_hash_cannot_change_in_place(self):
        with self.db.connection() as c:c.execute('UPDATE entry_quality_definitions SET spec_hash=?',('wrong',))
        with self.assertRaises(ValueError):quality.Recorder(self.db,clock=self.clock)

    def test_replays_saved_sip_path_and_never_labels_iex_or_future_observation(self):
        self.record();f=plans.frame(self.plan,'sip');asof=self.at+timedelta(hours=2)
        with self.db.connection() as c:self.study.observe(c,'decision',plans.frame(self.plan,'iex'),'iex',asof)
        self.assertFalse(quality.collect(self.path,NOW)[1][0]['path_known'])
        with self.db.connection() as c:self.study.observe(c,'decision',f,'sip',asof)
        before=self.path.read_bytes();definition,rows=quality.collect(self.path,NOW)
        self.assertEqual(before,self.path.read_bytes());self.assertTrue(rows[0]['path_known']);self.assertTrue(rows[0]['closed'])
        _,future=quality.collect(self.path,self.at+timedelta(minutes=70));self.assertFalse(future[0]['path_known'])
        with self.db.connection() as c:
            r=json.loads(c.execute("SELECT result_json FROM loss_reduction_results WHERE feed='sip'").fetchone()[0])
            r['rules']['0.001']['CURRENT']['net_pnl']=12345.
            c.execute("UPDATE loss_reduction_results SET result_json=? WHERE feed='sip'",(json.dumps(r),))
        self.assertFalse(quality.collect(self.path,NOW)[1][0]['path_known'])

    def test_gaps_ambiguity_and_invalid_stop_context_stay_unknown_not_zero_profit(self):
        self.record();f=plans.frame(self.plan,'sip').iloc[:-1]
        with self.db.connection() as c:self.study.observe(c,'decision',f,'sip',self.at+timedelta(hours=2))
        r=quality.collect(self.path,NOW)[1][0]
        self.assertTrue(r['closed']);self.assertFalse(r['path_known']);self.assertIsNone(r['results'])
        with self.db.connection() as c:
            p=json.loads(c.execute('SELECT plan_json FROM loss_reduction_trials').fetchone()[0]);p['stop_context']=None
            c.execute('UPDATE loss_reduction_trials SET plan_json=?',(json.dumps(p),))
        self.assertTrue(quality.collect(self.path,NOW)[1][0]['closed'])

    def test_chronological_train_embargo_holdout_and_train_only_scaling(self):
        rows=cohort();r=quality.evaluate_cohort(rows,NOW)
        self.assertEqual(r['state'],'HOLDOUT_RECORDED');self.assertEqual(r['train']['usable_entries'],80)
        self.assertEqual(r['test']['usable_entries'],40);self.assertEqual(r['embargo_days'],['2026-09-11'])
        self.assertLess(r['metrics']['GRADE_SCORE_PLUS_LIQUIDITY']['brier'],r['metrics']['GRADE_SCORE']['brier'])
        changed=deepcopy(rows)
        for row in changed:
            if row['day']>='2026-09-11':
                row['facts']['score']=100.;row['facts']['liquidity_score']=100.
                for result in row['results'].values():result['net_pnl']*=-1
        after=quality.evaluate_cohort(changed,NOW)
        self.assertEqual(r['artifact'],after['artifact'])
        self.assertFalse(r['automatic_promotion']);self.assertFalse(r['artifact']['inference_authority'])

    def test_unknown_coverage_and_low_sample_or_class_variation_cannot_report_success(self):
        rows=cohort()
        for row in rows[::2]:row['path_known']=False;row['results']=None
        self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_INCOMPLETE_DATA')
        self.assertEqual(quality.evaluate_cohort(cohort()[:8],NOW)['state'],'WAITING_NEW_COHORT')
        rows=cohort()
        for row in rows:
            for v in row['results'].values():v['net_pnl']=1.
        self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_CLASS_VARIATION')

    def test_missing_features_no_entry_and_duplicates_do_not_manufacture_training_labels(self):
        rows=cohort()
        for row in rows[::2]:row['facts']=None
        self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_INCOMPLETE_DATA')
        rows=cohort()
        for row in rows:
            for v in row['results'].values():v.update(traded=False,net_pnl=0.)
        self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_INCOMPLETE_DATA')
        rows=cohort();rows.append(deepcopy(rows[0]))
        self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_DUPLICATE_EVIDENCE')

    def test_invalid_numeric_outcome_and_cross_feed_cannot_be_fit(self):
        for kind in ('nan','bool','feed'):
            rows=cohort()
            if kind=='feed':rows[0]['decision_feed']='sip'
            else:rows[0]['results']['0.001']['net_pnl']=float('nan') if kind=='nan' else True
            self.assertEqual(quality.evaluate_cohort(rows,NOW)['state'],'BLOCKED_INPUT_CONTRACT')

    def test_fixed_liquidity_filter_reports_avoided_losses_and_missed_winners_at_both_costs(self):
        rows=cohort();rows[-8]['facts']['liquidity_score']=20.
        r=quality.evaluate_cohort(rows,NOW)
        for cost in ('0.001','0.0025'):
            v=r['sensitivity'][cost];self.assertEqual(v['avoided_losses'],15);self.assertEqual(v['missed_winners'],1)
            self.assertAlmostEqual(v['difference'],v['a_plus_liquidity55_proxy_pnl']-v['a_plus_proxy_pnl'])

    def test_recorder_failure_cannot_block_parent_research_or_native_candidate(self):
        with patch.object(self.recorder,'record',side_effect=ValueError('optional')):self.record()
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM loss_reduction_trials').fetchone()[0],1)

    def test_optional_initialization_failure_cannot_break_the_parent(self):
        with patch.object(quality,'Recorder',side_effect=ValueError('invalid research definition')):
            parent=loss.LossReductionStudy(self.db)
        self.assertIsNone(parent.quality_study)

    def test_native_registration_only_records_selected_eligible_unlatched_candidate(self):
        from decision_outcome_memory import DecisionOutcomeMemory
        memory=DecisionOutcomeMemory(self.db,Mock())
        memory.experiments.loss_study.calendar_reader=lambda at:deepcopy(self.plan['calendar'])
        p=self.payload();d=p['jalwe'];m=d['metadata']
        d.update(state='WATCHING',reason='waiting',strategy='OPENING_RANGE_BREAKOUT',market_regime='BULL_TREND',
            gates=dict.fromkeys(('market_data','features','ai','opportunity','market_regime','strategy_router'),True))
        m['decision_features']['price']=10.
        m.update(session_strategy_baseline='OPENING_RANGE_BREAKOUT',session_strategy_candidates=[dict(
            strategy='OPENING_RANGE_BREAKOUT',trigger_price=10.,invalidation_price=9.8,eligible_at_decision=True)])
        before=deepcopy(p);self.assertTrue(memory.record(p,'first'));self.assertEqual(p,before)
        for name in ('quality_rejected','latched','ineligible'):
            bad=deepcopy(p);bad['symbol']=name.upper()
            if name=='quality_rejected':bad['jalwe']['gates']['ai']=False
            elif name=='latched':bad['jalwe']['metadata']['breakout_latch']={'active':True}
            else:bad['jalwe']['metadata']['session_strategy_candidates'][0]['eligible_at_decision']=False
            memory.record(bad,name)
        with self.db.connection() as c:
            rows=c.execute('SELECT * FROM entry_quality_trials').fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['symbol'],'TEST')

    def test_command_is_readonly_and_never_trains_or_creates_a_missing_database(self):
        missing=self.path.with_name('missing.db')
        with patch.object(quality,'refresh',side_effect=AssertionError('must not train')):
            text=quality.read_report(missing)
            self.assertIn('غير متاح',text);self.assertFalse(missing.exists())
            import railway_controller
            with patch.object(quality,'read_report',return_value='saved') as reader:
                self.assertEqual(railway_controller.handle('/quality_study'),'saved');reader.assert_called_once()

    def test_hourly_refresh_and_final_holdout_cannot_be_retrained_after_peeking(self):
        class State:
            def __init__(self):self.values={}
            def state(self,k,v=None):
                if v is not None:self.values[k]=v
                return self.values.get(k)
        s=State()
        with patch.object(quality,'collect',return_value=(dict(defined_at=self.at.isoformat()),cohort())) as read:
            a=quality.refresh(s,NOW,self.path);self.assertEqual(a['state'],'HOLDOUT_RECORDED')
            self.assertEqual(a,quality.refresh(s,NOW+timedelta(days=2),self.path));self.assertEqual(read.call_count,1)
        s=State()
        with patch.object(quality,'collect',return_value=(None,[])) as read:
            quality.refresh(s,NOW,self.path);quality.refresh(s,NOW+timedelta(minutes=30),self.path)
            self.assertEqual(read.call_count,1)

    def test_nonconvergence_does_not_become_a_success_or_export_model(self):
        from sklearn.exceptions import ConvergenceWarning
        with patch('sklearn.linear_model.LogisticRegression.fit',side_effect=ConvergenceWarning('test')):
            r=quality.evaluate_cohort(cohort(),NOW)
        self.assertEqual(r['state'],'BLOCKED_NONCONVERGENCE');self.assertIsNone(r['artifact'])
