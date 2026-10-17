import copy
from datetime import date, timedelta
import json
import unittest

import market_factor_benchmark as bench
import market_factor_statistics as stats
from market_move_research import daily_features, utc, report_text, read_packet
import test_market_factor_benchmark as benchmark_fixtures
import test_market_move_research as native


class StatisticsMathTests(unittest.TestCase):
    def test_days_not_number_of_stocks_control_interval_eligibility(self):
        small=stats.interval([('2026-10-12',50.)],100000)
        self.assertEqual(small['status'],'INSUFFICIENT_DAYS_OR_PAIRS')
        self.assertIsNone(small['lower'])
        days=[((date(2026,8,1)+timedelta(days=i)).isoformat(),float(i-10)) for i in range(20)]
        self.assertEqual(stats.interval(days,59)['status'],'INSUFFICIENT_DAYS_OR_PAIRS')
        result=stats.interval(days,60)
        self.assertEqual(result['status'],'DESCRIPTIVE_INTERVAL_ONLY')
        self.assertEqual(result,stats.interval(list(reversed(days)),60))
        self.assertAlmostEqual(result['mean'],-.5)
        self.assertLessEqual(result['lower'],result['mean'])
        self.assertGreaterEqual(result['upper'],result['mean'])
        with self.assertRaises(ValueError): stats.interval(days+days[:1],100)

    def test_missing_or_bad_returns_never_become_zero(self):
        for bar in (None,{},dict(o=True,c=2),dict(o=1,c=float('nan')),dict(o=0,c=1)):
            self.assertIsNone(stats.close_return(bar))
        self.assertEqual(stats.close_return(dict(o=10,c=10)),0.)


class StatisticsIntegrationTests(unittest.TestCase):
    setUp=benchmark_fixtures.FactorBenchmarkTests.setUp
    run_benchmark=benchmark_fixtures.FactorBenchmarkTests.run_benchmark
    freeze_fixture=benchmark_fixtures.FactorBenchmarkTests.freeze_fixture

    def with_market(self,daily=None):
        data=copy.deepcopy(self.daily if daily is None else daily)
        data['SPY']=[dict(native.bar('2026-10-12'),o=10.,c=10.2,h=10.3)]
        return data

    def test_daily_high_success_is_not_close_return_or_profit(self):
        result=self.run_benchmark(self.with_market())
        s=result['retrospective']['all_days']['statistics']
        r=s['rules']['ALL_THREE']
        self.assertEqual(s['benchmark_available_days'],1)
        self.assertEqual(r['return_pairs'],2)
        self.assertAlmostEqual(r['matching_excess_pct'],-2.)
        self.assertAlmostEqual(r['control_excess_pct'],-2.)
        self.assertAlmostEqual(r['paired_excess_difference_pp']['mean'],0.)
        self.assertEqual(r['outcome_groups']['reached_daily_high_8']['factor_present'],1)
        self.assertEqual(r['outcome_groups']['did_not_reach_daily_high_8']['factor_present'],1)
        self.assertFalse(result['validated_edge']); self.assertFalse(result['automatic_application'])

    def test_missing_duplicate_and_invalid_market_do_not_erase_barrier_results(self):
        for issue in ('missing','duplicate','bad'):
            data=self.with_market()
            if issue=='missing': data.pop('SPY')
            elif issue=='duplicate': data['SPY']*=2
            else: data['SPY'][0]['c']=True
            result=self.run_benchmark(data)
            r=result['retrospective']['all_days']['statistics']['rules']['ALL_THREE']
            self.assertEqual(r['return_pairs'],0)
            self.assertEqual(r['missing_benchmark_pairs'],2)
            self.assertIsNone(r['matching_excess_pct'])
            self.assertEqual(result['retrospective']['all_days']['rules']['ALL_THREE']['pairs']['complete'],2)
            # Force a new read with the next test mutation, like a new daily source revision.
            self.store.state('daily_factor_benchmark',{})

    def test_missing_stock_does_not_get_substituted_from_a_prior_day(self):
        data=self.with_market(); data['A'].pop()
        result=self.run_benchmark(data)['retrospective']['all_days']['statistics']['rules']['ALL_THREE']
        self.assertEqual(result['return_pairs'],1)
        self.assertEqual(result['missing_stock_pairs'],1)

    def test_pairing_is_frozen_on_prior_features_not_stock_or_market_outcomes(self):
        records=[bench.record(daily_features(s,b[:2],self.sessions[1],self.sessions[0])) for s,b in self.daily.items()]
        pairs={rule:bench.paired(records,self.sessions[-1]['date'],rule) for rule in bench.RULES}
        before=stats.measure(records,self.sessions[-1]['date'],dict.fromkeys('ABCD',False),
                             dict(A=10.,B=4.,C=2.,D=-1.),1.,pairs)
        after=stats.measure(list(reversed(records)),self.sessions[-1]['date'],dict.fromkeys('ABCD',True),
                            dict(A=20.,B=14.,C=12.,D=9.),11.,pairs)
        for rule in bench.RULES:
            self.assertEqual(before['rules'][rule]['matching_excess_pct'],after['rules'][rule]['matching_excess_pct'])
            self.assertEqual(before['rules'][rule]['pairs_selected'],after['rules'][rule]['pairs_selected'])

    def test_forward_returns_survive_cache_expiry_with_unchanged_membership(self):
        session,stamp=self.freeze_fixture()
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp)
        sealed=self.store.state(bench.PREFIX+session['date'])
        data=self.with_market()
        for symbol in 'ABCD':
            data[symbol].append(dict(native.bar(session['date']),o=10.,c=10.5,h=11.))
        data['SPY'].append(dict(native.bar(session['date']),o=10.,c=10.2,h=10.3))
        mature=utc(session['close'])+bench.DELAY
        first=bench.update(self.store,data,self.sessions+[session],list(data),mature,source_session=session['date'])
        later=bench.update(self.store,{},self.sessions+[session],[],mature+timedelta(days=1),source_session='2026-10-20')
        self.assertEqual(first['forward']['evidence'],later['forward']['evidence'])
        r=later['forward']['evidence']['statistics']['rules']['ALL_THREE']
        self.assertEqual(r['return_pairs'],2)
        self.assertAlmostEqual(r['matching_excess_pct'],3.)
        self.assertEqual(sealed,self.store.state(bench.PREFIX+session['date']))

    def test_new_statistical_definition_never_backdates_a_forward_endpoint(self):
        session,stamp=self.freeze_fixture()
        bench.update(self.store,self.daily,self.sessions,list(self.daily),stamp)
        with self.store.connect() as conn:
            conn.execute('DELETE FROM move_state WHERE key=?',('daily_factor_statistics_definition',))
        mature=utc(session['close'])+bench.DELAY
        result=bench.update(self.store,self.with_market(),self.sessions+[session],list(self.daily),mature,
                            source_session=session['date'])
        self.assertEqual(result['forward']['evidence']['days'],1)
        self.assertEqual(result['forward']['evidence']['statistics']['days'],0)

    def test_packet_and_arabic_report_keep_endpoints_and_missing_news_explicit(self):
        self.run_benchmark(self.with_market()); self.store.publish(self.now,{'state':'READY'})
        packet=read_packet(path=self.store.path,now=self.now)
        self.assertEqual(packet['news_statistics']['protocol'],stats.PROTOCOL)
        text=report_text(packet)
        self.assertIn('بيتا=1',text)
        self.assertIn('العينة غير كافية لفاصل ثقة',text)
        self.assertIn('قبل الافتتاح فقط',text)
        json.dumps(packet,allow_nan=False)


class FrozenNewsTests(unittest.TestCase):
    def row(self,*,label='yes',symbol='A',outcome='UP8_FIRST'):
        news=dict(status='AVAILABLE',historical_updated_content_filtered=True,
            cutoff='2026-10-12T11:00:00Z',count=1,articles=[dict(id=1,
                created_at='2026-10-12T10:00:00Z',updated_at='2026-10-12T10:30:00Z',keywords=['earnings'])])
        if label=='no': news.update(count=0,articles=[])
        if label=='unknown': news.update(status='UNAVAILABLE')
        return dict(symbol=symbol,session_day='2026-10-12',session=native.session(),
            frozen_at='2026-10-12T12:00:00Z',role='ACTIVITY',prospective=True,status='COMPLETE',
            features=dict(pre_session_news=news),result=dict(status='COMPLETE',outcome=outcome,
                expected_minutes=390,observed_minutes=390,missing_minutes=0,duplicates=0))

    def test_preopen_known_zero_is_distinct_from_missing_and_truncated_keywords(self):
        self.assertEqual(stats.news_evidence(self.row())[0],'HAS_NEWS')
        self.assertEqual(stats.news_evidence(self.row(label='no'))[0],'NO_NEWS_IN_FROZEN_LOOKUP')
        self.assertEqual(stats.news_evidence(self.row(label='unknown'))[0],'UNKNOWN_NEWS')
        row=self.row(); row['features']['pre_session_news']['count']=10
        label,words=stats.news_evidence(row)
        self.assertEqual(label,'HAS_NEWS'); self.assertIs(words['earnings'],True)
        self.assertIsNone(words['fda'])

    def test_updated_after_cutoff_postopen_cutoff_and_duplicate_articles_fail_closed(self):
        for mutation in ('late_update','late_cutoff','duplicate','wrong_window','bad_count'):
            row=self.row(); news=row['features']['pre_session_news']
            if mutation=='late_update': news['articles'][0]['updated_at']='2026-10-12T15:00:00Z'
            elif mutation=='late_cutoff': news['cutoff']='2026-10-12T15:00:00Z'
            elif mutation=='duplicate': news.update(articles=news['articles']*2,count=2)
            elif mutation=='wrong_window': news['articles'][0]['created_at']='2026-10-09T10:00:00Z'
            else: news['count']=True
            self.assertEqual(stats.news_evidence(row)[0],'UNKNOWN_NEWS')

    def test_historical_partial_ambiguous_and_duplicate_rows_cannot_inflate_news_evidence(self):
        yes=self.row(); no=self.row(label='no',symbol='B',outcome='DOWN3_FIRST')
        unknown=self.row(label='unknown',symbol='C',outcome='NO_BARRIER')
        partial=self.row(symbol='D'); partial.update(status='PARTIAL_DATA')
        partial['result']['prefix_context']=dict(observed_barrier_order='UP8_FIRST')
        ambiguous=self.row(symbol='E',outcome='AMBIGUOUS')
        historical=self.row(symbol='F'); historical.update(prospective=False,role='HOT_CASE')
        entry=self.row(symbol='G'); entry.update(role='ENTRY_STUDY',prospective=False)
        result=stats.news_summary([yes,copy.deepcopy(yes),no,unknown,partial,ambiguous,historical,entry])
        self.assertEqual(result['symbol_days'],5)
        self.assertEqual(result['groups']['HAS_NEWS']['known_paths'],1)
        self.assertEqual(result['groups']['NO_NEWS_IN_FROZEN_LOOKUP']['known_paths'],1)
        self.assertEqual(result['groups']['UNKNOWN_NEWS']['known_paths'],1)
        self.assertEqual(result['incomplete'],1); self.assertEqual(result['ambiguous'],1)
        self.assertEqual(result['keywords']['earnings']['present_up8_first'],1)
        self.assertFalse(result['automatic_application'])

    def test_bad_coverage_and_conflicting_duplicates_stay_out_of_comparison(self):
        bad=self.row(symbol='B'); bad['result']['observed_minutes']=389
        calendar=self.row(symbol='C'); calendar['session']={}
        a=self.row(); other=copy.deepcopy(a); other['result']['outcome']='DOWN3_FIRST'
        result=stats.news_summary([a,other,bad,calendar])
        self.assertEqual(result['conflicts'],1); self.assertEqual(result['invalid'],2)
        self.assertEqual(sum(g['known_paths'] for g in result['groups'].values()),0)


if __name__=='__main__': unittest.main()
