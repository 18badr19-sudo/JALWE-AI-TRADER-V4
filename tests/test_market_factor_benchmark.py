import copy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import market_factor_benchmark as bench
from market_move_research import MoveStore, daily_features, utc, report_text, read_packet
from market_move_research_loop import MoveResearchWorker
import test_market_move_research as fixtures


class FactorBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=MoveStore(Path(self.tmp.name)/'moves.db')
        self.sessions=[fixtures.session(d) for d in ('2026-10-08','2026-10-09','2026-10-12')]
        self.now=utc('2026-10-14T00:00:00Z')
        self.daily={}
        for symbol,match,hit in [('A',True,True),('B',True,False),('C',False,True),('D',False,False)]:
            prior=dict(fixtures.bar('2026-10-09'),c=10.5 if match else 10.,
                       v=200000 if match else 100000,h=11. if match else 10.1,l=9. if match else 9.9)
            target=dict(fixtures.bar('2026-10-12'),o=10.,h=10.8 if hit else 10.7,c=10.)
            self.daily[symbol]=[fixtures.bar('2026-10-08'),prior,target]

    def run_benchmark(self,daily=None,**kwargs):
        return bench.update(self.store,self.daily if daily is None else daily,self.sessions,list(self.daily),
                            self.now,source_session='2026-10-12',**kwargs)

    def test_v2_preserves_old_definition_and_separates_price_bands(self):
        old={'protocol':'daily-factor-benchmark-v1','spec_hash':'old','defined_at':'2026-10-01T00:00:00Z'}
        self.store.state('daily_factor_definition',old)
        data=copy.deepcopy(self.daily)
        # Scale all prices together; adjust volume so research liquidity remains eligible.
        for bars in data.values():
            for bar in bars:
                for key in ('o','h','l','c'): bar[key]*=.02
                bar['v']*=100
        result=self.run_benchmark(data)
        self.assertEqual(self.store.state('daily_factor_definition'),old)
        self.assertEqual(result['research_price_range']['minimum'],.1)
        self.assertIn('0.10–0.49',result['retrospective_price_bands'])
        self.assertEqual(result['forward']['cohort_days'],0)
        self.assertFalse(result['automatic_application'])

    def test_all_universe_denominators_include_signal_failures_and_nonmatching_winners(self):
        result=self.run_benchmark(); r=result['retrospective']['all_days']['rules']['ALL_THREE']
        self.assertEqual(r['matching'],dict(total=2,reached=1,not_reached=1,unknown=0,known_coverage=1.,reached_frequency=.5))
        self.assertEqual(r['nonmatching']['reached'],1)
        self.assertEqual(r['nonmatching']['not_reached'],1)
        self.assertEqual(r['pairs']['selected'],2)
        self.assertEqual(r['pairs']['complete'],2)
        self.assertEqual(r['pairs']['day_weighted_frequency_difference'],0.)
        self.assertFalse(result['validated_edge']); self.assertFalse(result['execution_authority'])

    def test_pair_membership_never_reads_future_outcomes(self):
        features=[daily_features(s,bars[:2],self.sessions[1],self.sessions[0]) for s,bars in self.daily.items()]
        records=[bench.record(f) for f in features]
        pairs=bench.paired(records,'2026-10-12','ALL_THREE')
        changed=copy.deepcopy(self.daily)
        for bars in changed.values(): bars[-1].update(h=100.,c=99.)
        before=bench.measure(records,self.sessions[-1],bench.daily_index(self.daily))
        after=bench.measure(records,self.sessions[-1],bench.daily_index(changed))
        self.assertEqual(pairs,bench.paired(list(reversed(records)),'2026-10-12','ALL_THREE'))
        self.assertNotEqual(before['rules']['ALL_THREE']['matching']['reached'],after['rules']['ALL_THREE']['matching']['reached'])
        self.assertEqual(before['rules']['ALL_THREE']['pairs']['selected'],after['rules']['ALL_THREE']['pairs']['selected'])

    def test_missing_duplicate_invalid_and_boolean_outcomes_stay_unknown(self):
        for mutation in ('missing','duplicate','invalid','boolean'):
            data=copy.deepcopy(self.daily)
            if mutation=='missing': data['A'].pop()
            elif mutation=='duplicate': data['A'].append(copy.deepcopy(data['A'][-1]))
            elif mutation=='invalid': data['A'][-1]['h']=float('nan')
            else: data['A'][-1]['v']=True
            indexed=bench.daily_index(data)
            records=[bench.record(daily_features(s,bars[:2],self.sessions[1],self.sessions[0])) for s,bars in self.daily.items()]
            r=bench.measure(records,self.sessions[-1],indexed)['rules']['ALL_THREE']
            self.assertEqual(r['matching']['unknown'],1)
            self.assertEqual(r['matching']['not_reached'],1)
            self.assertEqual(r['pairs']['complete'],1)
            self.assertEqual(r['pairs']['selected'],2)

    def test_missing_exact_prior_session_never_substitutes_an_older_day(self):
        data=copy.deepcopy(self.daily); data['A'].pop(1)
        result=self.run_benchmark(data)
        self.assertEqual(result['retrospective']['all_days']['rules']['ALL_THREE']['matching']['total'],1)
        self.assertEqual(result['retrospective']['excluded_inputs'],1)

    def test_conservative_daily_bar_delay_and_threshold_boundary(self):
        close=utc(self.sessions[-1]['close'])
        before=bench.update(self.store,self.daily,self.sessions,list(self.daily),close+bench.DELAY-timedelta(seconds=1))
        self.assertEqual(before['retrospective']['all_days']['days'],0)
        after=bench.update(self.store,self.daily,self.sessions,list(self.daily),close+bench.DELAY)
        self.assertEqual(after['retrospective']['all_days']['days'],1)
        self.assertEqual(after['retrospective']['all_days']['rules']['ALL_THREE']['matching']['reached'],1)

    def freeze_fixture(self):
        session=fixtures.session('2026-10-19')
        features=[dict(fixtures.feature(s,day='2026-10-16'),range_pct=8. if s in 'AB' else 2.,
            daily_return_pct=3. if s in 'AB' else 0.,volume_ratio_previous_day=2. if s in 'AB' else 1.) for s in 'ABCD']
        self.store.save_pattern_features(features,session,utc('2026-10-17T00:00:00Z'))
        return session,utc('2026-10-17T04:00:00Z')

    def test_future_snapshot_is_immutable_separate_and_never_relabels_original_samples(self):
        session,stamp=self.freeze_fixture()
        result=bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp)
        self.assertEqual(result['forward']['pending_symbol_days'],4)
        self.assertEqual(result['forward']['evidence']['days'],0)
        key=bench.PREFIX+session['date']; sealed=self.store.state(key)
        with self.store.connect() as conn:
            conn.execute('DELETE FROM move_pattern_features')
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp+timedelta(minutes=1))
        self.assertEqual(sealed,self.store.state(key))
        self.assertEqual(self.store.episodes(),[])
        self.assertIsNone(self.store.state('pattern_policy'))

    def test_forward_missing_members_are_not_replaced_or_dropped(self):
        session,stamp=self.freeze_fixture()
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp)
        data=copy.deepcopy(self.daily); data['A'].append(dict(fixtures.bar('2026-10-19'),o=10.,h=11.,c=10.))
        result=bench.update(self.store,data,self.sessions+[session],['A'],utc(session['close'])+bench.DELAY,source_session='2026-10-19')
        forward=result['forward']['evidence']['rules']['ALL_THREE']
        self.assertEqual(forward['matching']['total'],2)
        self.assertEqual(forward['matching']['reached'],1)
        self.assertEqual(forward['matching']['unknown'],1)
        self.assertEqual(forward['nonmatching']['unknown'],2)
        self.assertEqual(forward['pairs']['selected'],2)
        self.assertEqual(forward['pairs']['complete'],0)

    def test_freeze_crossing_open_rolls_back_and_after_open_cannot_freeze(self):
        session,stamp=self.freeze_fixture(); opening=utc(session['open'])
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp,
            clock=Mock(side_effect=[opening-timedelta(seconds=1),opening]))
        self.assertIsNone(self.store.state(bench.PREFIX+session['date']))
        bench.update(self.store,self.daily,self.sessions,list(self.daily),opening)
        self.assertIsNone(self.store.state(bench.PREFIX+session['date']))

    def test_forward_observed_outcomes_survive_daily_cache_expiry(self):
        session,stamp=self.freeze_fixture()
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp)
        data=copy.deepcopy(self.daily)
        for symbol in 'ABCD':
            data[symbol].append(dict(fixtures.bar('2026-10-19'),o=10.,h=11. if symbol in 'AC' else 10.5,c=10.))
        mature=utc(session['close'])+bench.DELAY
        first=bench.update(self.store,data,self.sessions+[session],list(data),mature,source_session='2026-10-19')
        later=bench.update(self.store,{},self.sessions+[session],[],mature+timedelta(days=1),source_session='2026-10-20')
        self.assertEqual(first['forward']['evidence'],later['forward']['evidence'])
        self.assertEqual(later['forward']['evidence']['rules']['ALL_THREE']['pairs']['complete'],2)

    def test_nonfinite_or_boolean_features_cannot_signal(self):
        f=dict(fixtures.feature(),range_pct=8.,daily_return_pct=3.,volume_ratio_previous_day=2.)
        self.assertTrue(bench.signals(f)['ALL_THREE'])
        for key in ('range_pct','daily_return_pct','volume_ratio_previous_day'):
            for bad in (None,True,float('nan'),float('inf')):
                self.assertIsNone(bench.record(dict(f,**{key:bad})))

    def test_chronological_aggregation_equal_day_weighting_not_sample_count_weighting(self):
        records=[bench.record(daily_features(s,bars[:2],self.sessions[1],self.sessions[0])) for s,bars in self.daily.items()]
        r=bench.measure(records,self.sessions[-1],bench.daily_index(self.daily))
        d1={'day':'2026-10-12','rules':copy.deepcopy(r['rules'])}
        d2={'day':'2026-10-13','rules':copy.deepcopy(r['rules'])}
        d1['rules']['ALL_THREE']['pairs'].update(complete=100,selected=100,day_edge=1.)
        d2['rules']['ALL_THREE']['pairs'].update(complete=1,selected=1,day_edge=-1.)
        result=bench.aggregate([d1,d2])
        self.assertEqual(result['rules']['ALL_THREE']['pairs']['day_weighted_frequency_difference'],0.)
        self.assertEqual((result['first_day'],result['last_day']),('2026-10-12','2026-10-13'))

    def test_optional_benchmark_failure_does_not_stop_worker_preparation(self):
        provider=Mock(); provider.daily.return_value=self.daily
        worker=MoveResearchWorker(self.store,provider)
        with patch.object(bench,'update',side_effect=RuntimeError('private secret')):
            health=worker.prepare(self.now,self.sessions,list(self.daily))
        self.assertEqual(health['state'],'READY')

    def test_packet_and_report_do_not_mix_daily_endpoint_with_first_barrier_or_pnl(self):
        self.run_benchmark(); self.store.publish(self.now,{'state':'READY'})
        packet=read_packet(path=self.store.path,now=self.now)
        self.assertEqual(packet['factor_benchmark']['protocol'],bench.PROTOCOL)
        report=report_text(packet)
        self.assertIn('لم تبلغ',report)
        self.assertIn('ليس مسار دقائق أو ربح صفقة',report)
        self.assertIn('العينات المستقبلية منفصلة',report)


if __name__=='__main__':
    unittest.main()
