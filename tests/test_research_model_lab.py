"""Chronological ML contract: artificial panels only; no real broker/model calls."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock

from intelligence import research_model_lab as lab
from market_move_research import MoveStore,utc
from focused_research_collection import FocusedCollector
import test_market_move_research as fixtures

NOW=datetime(2026,10,24,tzinfo=timezone.utc)


def days(count=16):
    result=[]
    for i in range(count):
        day=(datetime(2026,8,1)+timedelta(days=i)).date().isoformat()
        pairs=[]
        for j in range(4):
            pair=day+'-P'+str(j)
            rows=[]
            for k in range(2):
                label=(j+k)%2
                x=[float(label*2),float(label*3),float(label*4),float(j),float(i)]
                rows.append(dict(day=day,pair_key=pair,symbol='S'+str(j*2+k),x=x,y=label))
            pairs.append(dict(pair_key=pair,usable=True,rows=rows))
        result.append(dict(day=day,closed=True,pairs=pairs))
    return result


class ModelLabTests(unittest.TestCase):
    def test_not_enough_panels_or_open_day_never_imports_or_fits_model(self):
        with patch('sklearn.linear_model.LogisticRegression') as fit:
            self.assertEqual(lab.evaluate(days(15),NOW,'data')['state'],'WAITING_CLOSED_PANELS')
            sample=days();sample[-1]['closed']=False
            self.assertEqual(lab.evaluate(sample,NOW,'data')['state'],'WAITING_CLOSED_PANELS')
            fit.assert_not_called()

    def test_fixed_day_split_embargo_and_independent_brier_reference(self):
        sample=days();result=lab.evaluate(sample,NOW,'data')
        self.assertEqual(result['state'],'BACKTEST_RECORDED')
        self.assertEqual(result['train_days'],[d['day'] for d in sample[:10]])
        self.assertEqual(result['embargo_days'],[sample[10]['day']])
        self.assertEqual(result['test_days'],[d['day'] for d in sample[11:]])
        predictions=result['predictions']
        reference=sum((p['uncalibrated_score']-p['observed_label'])**2 for p in predictions)/len(predictions)
        self.assertAlmostEqual(reference,result['metrics']['candidate_brier'])
        self.assertAlmostEqual(result['metrics']['constant_baseline_brier'],.25)
        self.assertLess(reference,.25)
        self.assertFalse(result['execution_authority']);self.assertFalse(result['automatic_promotion'])
        self.assertFalse(result['metrics']['forward_validation_complete'])
        json.dumps(result,allow_nan=False)

    def test_test_labels_and_test_scaling_cannot_change_fitted_model(self):
        sample=days();before=deepcopy(sample)
        first=lab.evaluate(sample,NOW,'first')
        for day in sample[11:]:
            for pair in day['pairs']:
                for row in pair['rows']:row['y']=1-row['y'];row['x']=[v*100 for v in row['x']]
        other=lab.evaluate(sample,NOW,'other')
        for name in ('coefficients','intercept','mean','scale'):
            self.assertEqual(first['artifact'][name],other['artifact'][name])
        training=[r for d in before[:10] for p in d['pairs'] for r in p['rows']]
        expected=[sum(r['x'][i] for r in training)/len(training) for i in range(5)]
        for a,b in zip(expected,first['artifact']['mean']):self.assertAlmostEqual(a,b)

    def test_unknown_pairs_remain_in_coverage_denominator(self):
        sample=days()
        for d in sample[:10]:d['pairs'][0]['usable']=False
        result=lab.evaluate(sample,NOW,'data')
        self.assertEqual(result['state'],'BLOCKED_INCOMPLETE_DATA')
        self.assertEqual(result['train_coverage'],.75)
        self.assertIsNone(result['artifact'])

    def test_one_class_or_duplicate_evidence_never_trains(self):
        sample=days()
        for d in sample[:10]:
            for p in d['pairs']:
                for r in p['rows']:r['y']=0
        self.assertEqual(lab.evaluate(sample,NOW,'data')['state'],'BLOCKED_CLASS_VARIATION')
        sample=days();sample[0]['pairs'][0]['rows'][1]['symbol']=sample[0]['pairs'][0]['rows'][0]['symbol']
        self.assertEqual(lab.evaluate(sample,NOW,'data')['state'],'BLOCKED_DUPLICATE_EVIDENCE')

    def test_invalid_numeric_contract_and_duplicate_days_are_blocked(self):
        for value in (float('nan'),float('inf'),True,'12'):
            sample=days();sample[0]['pairs'][0]['rows'][0]['x'][0]=value
            self.assertEqual(lab.evaluate(sample,NOW,'data')['state'],'BLOCKED_INPUT_CONTRACT')
        sample=days();sample[1]['day']=sample[0]['day']
        with self.assertRaisesRegex(ValueError,'DUPLICATE_SESSION'):lab.evaluate(sample,NOW,'data')

    def test_nonconverged_fit_cannot_record_artifact_or_metrics(self):
        from sklearn.exceptions import ConvergenceWarning
        with patch('sklearn.linear_model.LogisticRegression.fit',side_effect=ConvergenceWarning('fixture')):
            value=lab.evaluate(days(),NOW,'data')
        self.assertEqual(value['state'],'BLOCKED_NONCONVERGENCE')
        self.assertIsNone(value['artifact']);self.assertIsNone(value['metrics'])

    def test_vector_domains_and_log_transform_do_not_impute(self):
        good=dict(range_pct=8.,daily_return_pct=-5.,volume_ratio_previous_day=2.,close=10.,dollar_volume=1000000.)
        self.assertEqual(lab.vector(good)[:2],[8.,-5.])
        for name in lab.FEATURES:
            for value in (None,True,float('nan'),float('inf'),'10'):
                self.assertIsNone(lab.vector(dict(good,**{name:value})))


class CapturedDatasetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=MoveStore(Path(tmp.name)/'lab.db')
        self.session=fixtures.session();self.before=utc(self.session['open'])-timedelta(hours=1)
        self.now=utc(self.session['close'])+timedelta(minutes=16)
        for symbol,role in (('ONE','ACTIVITY'),('TWO','CONTROL')):
            f=fixtures.feature(symbol);f.update(range_pct=8.,daily_return_pct=3.,volume_ratio_previous_day=2.)
            self.store.add(f,self.session,role,'pair',self.before,prospective=True)
        self.provider=Mock();self.collector=FocusedCollector(self.store,self.provider)
        self.collector.freeze(self.session,self.before)
        frame=fixtures.frame(self.session);frame.attrs.update(data_feed='sip',adjustment='split')
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:frame for symbol in symbols}
        self.collector.capture(self.now)
        self.definition=dict(defined_at=self.before.isoformat())

    def test_dataset_requires_prior_features_and_independent_raw_full_path(self):
        result,checksum=lab.dataset(self.store,self.definition,self.now)
        self.assertEqual(len(result),1);self.assertTrue(result[0]['pairs'][0]['usable'])
        self.assertEqual({r['y'] for r in result[0]['pairs'][0]['rows']},{0})
        self.assertEqual(len(checksum),64)
        self.assertTrue(all(r['status']=='PENDING' for r in self.store.episodes()))

    def test_old_post_selected_panels_and_future_labels_are_excluded(self):
        result,_=lab.dataset(self.store,dict(defined_at=self.now.isoformat()),self.now)
        self.assertEqual(result,[])
        result,_=lab.dataset(self.store,self.definition,self.before)
        self.assertFalse(result[0]['closed']);self.assertFalse(result[0]['pairs'][0]['usable'])

    def test_partial_or_future_dated_capture_cannot_be_a_training_label(self):
        with self.store.connect() as c:
            row=c.execute("SELECT evidence_json FROM focused_collection_bars WHERE symbol='ONE'").fetchone()
            evidence=json.loads(row[0]);evidence['minutes'].pop(next(iter(evidence['minutes'])))
            c.execute("UPDATE focused_collection_bars SET evidence_json=? WHERE symbol='ONE'",(json.dumps(evidence),))
        result,_=lab.dataset(self.store,self.definition,self.now)
        self.assertFalse(result[0]['pairs'][0]['usable'])

    def test_corrupt_raw_context_is_unknown_and_stays_in_pair_denominator(self):
        with self.store.connect() as c:
            row=c.execute("SELECT evidence_json FROM focused_collection_bars WHERE symbol='ONE'").fetchone()
            evidence=json.loads(row[0]);evidence['symbol']='OTHER'
            c.execute("UPDATE focused_collection_bars SET evidence_json=? WHERE symbol='ONE'",(json.dumps(evidence),))
        result,_=lab.dataset(self.store,self.definition,self.now)
        self.assertEqual(len(result[0]['pairs']),1)
        self.assertFalse(result[0]['pairs'][0]['usable'])
        row=next(r for r in result[0]['pairs'][0]['rows'] if r['symbol']=='ONE')
        self.assertEqual(row['reason'],'INVALID_SAVED_EVIDENCE');self.assertIsNone(row['y'])

    def test_future_capture_timestamps_cannot_supply_labels(self):
        for field in ('as_of','first_seen','last_read_at'):
            with self.subTest(field=field),self.store.connect() as c:
                saved=c.execute("SELECT * FROM focused_collection_bars WHERE symbol='ONE'").fetchone()
                evidence=json.loads(saved['evidence_json']);coverage=json.loads(saved['coverage_json'])
                original=(saved['evidence_json'],saved['coverage_json'])
                future=(self.now+timedelta(minutes=1)).isoformat()
                if field=='as_of':coverage[field]=future
                elif field=='last_read_at':evidence[field]=future
                else:next(iter(evidence['minutes'].values()))[field]=future
                c.execute("UPDATE focused_collection_bars SET evidence_json=?,coverage_json=? WHERE symbol='ONE'",
                          (json.dumps(evidence),json.dumps(coverage)))
            result,_=lab.dataset(self.store,self.definition,self.now)
            self.assertFalse(result[0]['pairs'][0]['usable'],field)
            with self.store.connect() as c:
                c.execute("UPDATE focused_collection_bars SET evidence_json=?,coverage_json=? WHERE symbol='ONE'",original)

    def test_hourly_registry_is_idempotent_and_has_no_executable_model_files(self):
        first=lab.refresh(self.store,self.before)
        second=lab.refresh(self.store,self.before+timedelta(minutes=5))
        self.assertEqual(first,second)
        later=lab.refresh(self.store,self.before+timedelta(hours=1,minutes=1))
        self.assertEqual(first,later)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM ai_research_experiments').fetchone()[0],1)
        self.assertEqual(first['state'],'WAITING_CLOSED_PANELS')
        self.assertFalse(first['execution_authority'])


if __name__=='__main__':unittest.main()
