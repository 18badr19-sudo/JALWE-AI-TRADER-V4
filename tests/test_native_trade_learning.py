from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import json,sqlite3,tempfile,unittest
import numpy as np
import native_trade_learning as model
from intelligence.learning_engine import LearningEngine
import test_trade_chain_and_feature_integrity as fixtures

NOW=datetime(2026,10,24,tzinfo=timezone.utc)


def samples():
    rows=[]
    for i in range(15):
        for j in range(3):
            at=datetime(2026,9,1,14,tzinfo=timezone.utc)+timedelta(days=i,minutes=j)
            good=j!=1
            rows.append(dict(trade_id=f'{i}-{j}',symbol='TEST',entry_at=at.isoformat(),
                closed_at=(at+timedelta(minutes=30)).isoformat(),decision_feed='iex',
                pnl_before_fees=1.5 if good else -1.,entry_notional=100.,exit_notional=101.5 if good else 99.,
                initial_risk_dollars=1.,snapshot=dict(liquidity_score=80. if good else 20.,rvol=3. if good else 1.,
                    momentum_score=90. if good else 60.,atr_pct=1.,distance_from_vwap_pct=1.,above_vwap=True)))
    return rows


class NativeSourceTests(unittest.TestCase):
    setUp=fixtures.TradeChainTests.setUp
    seed=fixtures.TradeChainTests.seed
    insert=fixtures.TradeChainTests.insert

    def enrich(self):
        self.decision['jalwe'].update(strategy='VWAP_BOUNCE',market_regime='BULL_TREND')
        self.decision['jalwe']['metadata'].update(decision_data_feed='iex',quality_inputs={'timeframe':'5m'},
            decision_features=dict(timestamp='2026-10-16T13:55:00Z',data_quality_ok=True,data_is_stale=False,
                liquidity_score=70.,rvol=2.,momentum_score=85.,atr_pct=1.,distance_from_vwap_pct=.5,above_vwap=True))
        with self.db.connection() as c:c.execute('UPDATE system_events SET metadata_json=? WHERE id=1',(json.dumps(self.decision),))

    def test_native_ledger_partial_exit_sum_replaces_corrupt_metadata_and_nearby_features(self):
        self.enrich()
        with self.db.connection() as c:
            c.execute('UPDATE managed_trades SET metadata_json=?',(json.dumps({'realized_pnl':99999.}),))
            c.execute('INSERT INTO feature_snapshots(symbol,snapshot_json,created_at) VALUES (?,?,?)',
                ('TEST',json.dumps({'liquidity_score':1.}),'2026-10-16T13:59:00Z'))
        before=self.path.read_bytes();r=model.load_samples(self.path,as_of=NOW);x=r['samples'][0]
        self.assertEqual(before,self.path.read_bytes());self.assertEqual(x['pnl_before_fees'],-2.)
        self.assertEqual(x['snapshot']['liquidity_score'],70.);self.assertEqual(x['initial_risk_dollars'],10.)
        self.assertAlmostEqual(x['pnl_pct'],-2.);self.assertEqual(x['exit_notional'],98.)
        self.assertAlmostEqual(model.reward(x,.001),(-2.-.001*198.)/10.)

    def test_wrong_identity_nearby_or_future_unclosed_features_do_not_enter_learning(self):
        self.enrich()
        for kind in ('unclosed','missing_timeframe','wrong_order','future_close'):
            d=deepcopy(self.decision)
            if kind=='unclosed':d['jalwe']['metadata']['decision_features']['timestamp']='2026-10-16T14:00:00Z'
            elif kind=='missing_timeframe':d['jalwe']['metadata']['quality_inputs']={}
            elif kind=='wrong_order':d['execution']['broker_order_id']='OTHER'
            with self.db.connection() as c:
                c.execute('UPDATE system_events SET metadata_json=? WHERE id=1',(json.dumps(d),))
                c.execute('UPDATE managed_trades SET closed_at=?',('2026-10-25T14:00:00Z' if kind=='future_close' else self.trade['closed_at'],))
            self.assertEqual(model.load_samples(self.path,as_of=NOW)['samples'],[],kind)

    def test_closed_trade_with_missing_original_features_stays_in_coverage_population(self):
        self.enrich()
        d=deepcopy(self.decision);d['jalwe']['metadata']['quality_inputs']={}
        with self.db.connection() as c:
            c.execute('UPDATE system_events SET metadata_json=? WHERE id=1',(json.dumps(d),))
        before=self.path.read_bytes();r=model.load_samples(self.path,as_of=NOW)
        self.assertEqual(r['samples'],[])
        self.assertEqual(r['cohort'],[dict(trade_id=self.trade['trade_id'],entry_at='2026-10-16T14:00:03+00:00')])
        self.assertEqual(before,self.path.read_bytes())

    def test_open_mismatched_or_repeated_pnl_cannot_become_training_reward(self):
        self.enrich()
        with self.db.connection() as c:c.execute('UPDATE strategy_pnl_ledger SET realized_pnl=99 WHERE event_key=?',('SELL1:10',))
        r=model.load_samples(self.path,as_of=NOW)
        self.assertEqual(r['samples'],[]);self.assertIn('LEDGER_INCOMPLETE_OR_MISMATCHED',r['excluded'])

    def test_status_and_learning_use_same_eligible_sample_definition(self):
        self.enrich()
        with patch('intelligence.learning_engine.database',self.db):
            engine=LearningEngine();engine.max_history=50
            self.assertEqual(engine.status()['eligible_trades'],1)
            r=engine.run_learning_cycle();self.assertEqual(r['eligible_trades'],1)
            self.assertEqual(r['source_protocol'],model.SOURCE_PROTOCOL);self.assertFalse(r['learning_active'])
            self.assertFalse(engine.status()['active'])
            with self.db.connection() as c:c.execute('DELETE FROM system_events')
            self.assertEqual(engine.status()['eligible_trades'],0)

    def test_old_weight_cache_is_not_applied_without_exact_source_provenance(self):
        with patch('intelligence.learning_engine.database',self.db):
            engine=LearningEngine()
            with self.db.connection() as c:c.execute('UPDATE learning_factor_weights SET multiplier=1.15')
            engine._refresh_cache(force=True)
            self.assertEqual(set(engine.profile().values()),{1.})


class NativeModelTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.path=Path(t.name)/'research.db'

    def save(self,r):
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS move_state(key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT OR REPLACE INTO move_state VALUES (?,?)',('native_local_model',json.dumps(r)))

    def payload(self):
        return dict(timestamp=(NOW+timedelta(minutes=1)).isoformat(),jalwe=dict(metadata=dict(
            decision_data_feed='iex',quality_inputs={'timeframe':'5m'},decision_features=dict(
            samples()[0]['snapshot'],timestamp=(NOW-timedelta(minutes=5)).isoformat(),data_quality_ok=True,data_is_stale=False))))

    def test_true_local_regression_matches_independent_augmented_least_squares(self):
        rows=samples();r=model.fit_evaluate(rows,NOW);a=r['artifact']
        self.assertEqual(r['state'],'NATIVE_HOLDOUT_RECORDED');self.assertTrue(r['metrics']['hypothesis_supported'])
        x=np.array([model.vector(t['snapshot']) for t in rows[:30]]);mean=x.mean(0);scale=x.std(0);scale[scale<1e-12]=1
        y=np.clip([model.reward(t,.001) for t in rows[:30]],-10,10)
        z=(x-mean)/scale
        independent=np.linalg.lstsq(np.vstack([z,np.sqrt(10)*np.eye(6)]),np.r_[y-y.mean(),np.zeros(6)],rcond=None)[0]
        np.testing.assert_allclose(a['coefficients'],independent,atol=1e-10)
        self.assertFalse(r['automatic_promotion']);self.assertFalse(r['coefficients_are_causal'])

    def test_test_labels_and_extreme_features_do_not_train_scaling_or_coefficients(self):
        rows=samples();before=model.fit_evaluate(rows,NOW)['artifact']
        for row in rows[30:]:
            row['pnl_before_fees']*=-1;row['snapshot']['rvol']=100000.
        after=model.fit_evaluate(rows,NOW)['artifact']
        self.assertEqual(before,after)

    def test_training_label_overlap_into_test_is_not_allowed(self):
        rows=samples()
        for r in rows[:6]:r['closed_at']=rows[31]['closed_at']
        r=model.fit_evaluate(rows,NOW)
        self.assertEqual(r['state'],'BLOCKED_SAMPLE_OR_COVERAGE');self.assertIsNone(r['artifact'])

    def test_original_exclusions_stay_in_each_split_coverage_denominator(self):
        rows=samples()
        cohort=[dict(trade_id=r['trade_id'],entry_at=r['entry_at']) for r in rows]
        # Seven missing original decisions must not vanish from 30 training rows.
        cohort += [dict(trade_id=f'missing-{i}',entry_at=rows[i]['entry_at']) for i in range(7)]
        r=model.fit_evaluate(rows,NOW,cohort=cohort)
        self.assertEqual(r['state'],'BLOCKED_SAMPLE_OR_COVERAGE')
        self.assertEqual(r['train']['recorded'],37)
        self.assertAlmostEqual(r['train']['coverage'],30/37)
        self.assertIsNone(r['artifact'])

    def test_hourly_refresh_uses_original_population_not_only_surviving_samples(self):
        class State:
            def __init__(self):self.v={}
            def state(self,k,v=None):
                if v is not None:self.v[k]=v
                return self.v.get(k)
        rows=samples();cohort=[dict(trade_id=r['trade_id'],entry_at=r['entry_at']) for r in rows]
        cohort += [dict(trade_id=f'missing-{i}',entry_at=rows[i]['entry_at']) for i in range(7)]
        report=dict(samples=rows,cohort=cohort,protocol=model.SOURCE_PROTOCOL,audit_state='AUDITED',excluded={'EXACT_ENTRY_LINK_MISSING':7})
        store=State()
        with patch.object(model,'load_samples',return_value=report):r=model.refresh(store,NOW)
        self.assertEqual(r['state'],'BLOCKED_SAMPLE_OR_COVERAGE')
        self.assertEqual(r['coverage_population'],52)
        self.assertNotIn('cohort',r['source_audit'])
        self.assertEqual(store.v['native_local_model'],r)

    def test_unknown_cohort_time_or_identity_cannot_be_silently_dropped(self):
        rows=samples();cohort=[dict(trade_id=r['trade_id'],entry_at=r['entry_at']) for r in rows]
        for bad in (dict(trade_id='unknown',entry_at=None),cohort[0]):
            r=model.fit_evaluate(rows,NOW,cohort=cohort+[bad])
            self.assertEqual(r['state'],'BLOCKED_COHORT_EVIDENCE')
            self.assertIsNone(r['artifact'])

    def test_only_days_with_eligible_vectors_and_nonoverlapping_labels_count(self):
        rows=samples()
        # Still 30 valid training trades and >90% coverage, but only nine valid days.
        for r in rows[:3]:r['snapshot']['rvol']=None
        for j in range(3):
            extra=deepcopy(rows[3+j]);extra['trade_id']=f'extra-{j}';rows.append(extra)
        r=model.fit_evaluate(rows,NOW)
        self.assertEqual(r['state'],'BLOCKED_ELIGIBLE_DAYS')
        self.assertEqual(r['train']['eligible'],30)
        self.assertEqual(r['train']['eligible_days'],9)
        self.assertIsNone(r['artifact'])

    def test_missing_test_day_cannot_count_towards_five_later_days(self):
        rows=samples()
        # One unmeasurable test day among 33 test trades still passes numeric coverage.
        for r in rows[30:33]:r['snapshot']['rvol']=None
        for j in range(18):
            extra=deepcopy(rows[33+j%12]);extra['trade_id']=f'extra-{j}';rows.append(extra)
        r=model.fit_evaluate(rows,NOW)
        self.assertEqual(r['state'],'BLOCKED_ELIGIBLE_DAYS')
        self.assertEqual(r['test']['eligible_days'],4)
        self.assertIsNone(r['artifact'])

    def test_missing_sample_variation_and_numeric_corruption_are_not_fake_training(self):
        self.assertEqual(model.fit_evaluate(samples()[:8],NOW)['state'],'COLLECTING_NATIVE_TRADES')
        for v in (True,'70',float('nan'),10**1000):
            f=samples()[0]['snapshot'];f['liquidity_score']=v;self.assertIsNone(model.vector(f))
        rows=samples()
        for r in rows:r['pnl_before_fees']=-1.
        self.assertEqual(model.fit_evaluate(rows,NOW)['state'],'BLOCKED_OUTCOME_VARIATION')
        rows=samples();rows.append(deepcopy(rows[0]))
        self.assertEqual(model.fit_evaluate(rows,NOW)['state'],'BLOCKED_DUPLICATE_TRADES')

    def test_hypothetical_filter_reports_missed_profit_not_just_avoided_losses(self):
        r=model.fit_evaluate(samples(),NOW)
        for cost,v in r['metrics']['comparisons'].items():
            self.assertGreater(v['avoided_losses'],0);self.assertEqual(v['missed_winners'],0)
            self.assertGreater(v['kept'],0);self.assertGreater(v['filtered_proxy'],0)
            self.assertAlmostEqual(v['difference'],v['filtered_proxy']-v['native_ledger_after_assumed_costs'])

    def test_cold_start_expired_future_or_tampered_model_cannot_issue_forecast(self):
        p=self.payload();self.assertIsNone(model.forecast(p,self.path)['expected_r']);self.assertFalse(self.path.exists())
        good=model.fit_evaluate(samples(),NOW)
        for kind in ('expired','future','hash','length','unqualified'):
            r=deepcopy(good);a=r['artifact']
            if kind=='expired':a['expires_at']=NOW.isoformat()
            elif kind=='future':a['created_at']=(NOW+timedelta(days=1)).isoformat()
            elif kind=='hash':a['artifact_hash']='bad'
            elif kind=='length':a['coefficients']=[]
            else:r['metrics']['hypothesis_supported']=False
            if kind!='hash':a['artifact_hash']=model.digest({k:v for k,v in a.items() if k!='artifact_hash'})
            self.save(r);self.assertIsNone(model.forecast(p,self.path)['expected_r'],kind)

    def test_local_forecast_is_readonly_original_closed_features_only_and_not_order_authority(self):
        self.save(model.fit_evaluate(samples(),NOW));before=self.path.read_bytes();p=self.payload()
        r=model.forecast(p,self.path);self.assertEqual(r['state'],'LOCAL_FORECAST_RECORDED')
        self.assertFalse(r['execution_authority']);self.assertEqual(before,self.path.read_bytes())
        for kind in ('future_bar','stale','feed'):
            bad=deepcopy(p);m=bad['jalwe']['metadata']
            if kind=='future_bar':m['decision_features']['timestamp']=p['timestamp']
            elif kind=='stale':m['decision_features']['data_is_stale']=True
            else:m['decision_data_feed']='sip'
            self.assertIsNone(model.forecast(bad,self.path)['expected_r'])

    def test_command_is_readonly_and_does_not_train_or_create_missing_state(self):
        missing=self.path.with_name('missing.db')
        with patch.object(model,'fit_evaluate',side_effect=AssertionError('reader must not fit')):
            self.assertIn('غير متاح',model.status_text(missing));self.assertFalse(missing.exists())
        import railway_controller
        with patch.object(model,'status_text',return_value='local saved') as read:
            self.assertEqual(railway_controller.handle('/local_ai'),'local saved');read.assert_called_once()
        with patch.dict('os.environ',{'JALWE_SHADOW_AI_ENABLED':'false'}),patch.object(model,'status_text',return_value='local saved') as read:
            self.assertEqual(railway_controller.handle('/brain SNAP'),'local saved');read.assert_called_once_with(symbol='SNAP')

    def test_status_explains_exclusions_days_truncation_and_saudi_time(self):
        r=model.fit_evaluate(samples()[:3],NOW)
        r.update(checked_at=NOW.isoformat(),source_audit=dict(audited_trades=8,truncated=True,
            excluded={'ORIGINAL_CLOSED_FEATURE_EVIDENCE_MISSING':5}))
        self.save(r);before=self.path.read_bytes();message=model.status_text(self.path)
        self.assertIn('التدريب لم يكتمل',message)
        self.assertIn('مستبعد 5: دليل الشمعة الأصلية',message)
        self.assertIn('2026-10-24 03:00:00 السعودية',message)
        self.assertIn('آخر 50',message);self.assertEqual(before,self.path.read_bytes())

    def test_refresh_is_local_hourly_and_never_consumes_simulation_as_native_reward(self):
        class State:
            def __init__(self):self.v={}
            def state(self,k,v=None):
                if v is not None:self.v[k]=v
                return self.v.get(k)
        store=State();report=dict(samples=samples(),protocol=model.SOURCE_PROTOCOL,audit_state='AUDITED',excluded={})
        with patch.object(model,'load_samples',return_value=report) as load:
            r=model.refresh(store,NOW);self.assertEqual(r['state'],'NATIVE_HOLDOUT_RECORDED')
            self.assertEqual(model.refresh(store,NOW+timedelta(minutes=30)),r);self.assertEqual(load.call_count,1)
