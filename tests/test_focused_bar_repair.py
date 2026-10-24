from datetime import timedelta
import json,unittest
from unittest.mock import Mock
import pandas as pd
from focused_research_collection import FocusedCollector
from market_move_research import utc
from market_move_provider import ResearchReadError
from market_move_research_loop import MoveResearchWorker
import test_focused_collection as fixtures


class FocusedBarRepairTests(unittest.TestCase):
    setUp=fixtures.FocusedCollectionTests.setUp
    frame=fixtures.FocusedCollectionTests.frame
    pairs=fixtures.FocusedCollectionTests.pairs
    freeze=fixtures.FocusedCollectionTests.freeze

    def holes(self,n=1):
        self.freeze();self.data=self.frame();self.missing=list(self.data.index[10:10+n])
        self.raw=self.data.drop(self.missing)
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:self.raw for symbol in symbols}
        return utc(self.session['close'])+timedelta(minutes=16)

    def read_minute(self,symbol,window,now):
        start,end=utc(window['open']),utc(window['close'])
        return self.data.loc[(self.data.index>=start)&(self.data.index<=end)].copy()

    def test_two_real_bars_complete_original_pair_without_changing_latest_read_or_cohort(self):
        now=self.holes();self.provider.intraday.side_effect=self.read_minute
        with self.store.connect() as c:before=c.execute('SELECT manifest_json FROM focused_collection_sessions').fetchone()[0]
        r,frames=self.collector.capture(now);s=self.collector.summary(now)
        self.assertEqual((r['targeted_bar_reads'],r['targeted_bars_recovered']),(2,2))
        self.assertEqual((s['complete_pairs'],s['complete_paths'],s['coverage_pct']),(1,2,100.))
        self.assertEqual(self.provider.intraday.call_count,2);self.provider.gap_trades.assert_not_called()
        self.assertTrue(all(len(v)==389 for v in frames.values()))
        self.assertTrue(all(row['attempts']==0 for row in self.store.episodes()))
        with self.store.connect() as c:self.assertEqual(before,c.execute('SELECT manifest_json FROM focused_collection_sessions').fetchone()[0])

    def test_partial_prefix_uses_diagnostics_only_and_never_target_reads(self):
        self.holes();self.collector.capture(utc(self.session['open'])+timedelta(minutes=46))
        self.provider.intraday.assert_not_called()
        self.assertEqual(self.provider.gap_trades.call_count,2)

    def test_durable_three_postclose_polls_bound_six_reads_and_do_not_repeat_minutes(self):
        now=self.holes(3);self.provider.intraday.side_effect=lambda *a:self.data.iloc[:0].copy()
        for i in range(3):
            self.collector=FocusedCollector(self.store,self.provider)
            self.collector.capture(now+timedelta(minutes=15*i))
        r,_=self.collector.capture(now+timedelta(minutes=60))
        self.assertEqual(r['state'],'PACED_OR_BUDGETED')
        self.assertEqual(self.provider.intraday.call_count,6)
        targets=[(c.args[0],c.args[1]['open']) for c in self.provider.intraday.call_args_list]
        self.assertEqual(len(targets),len(set(targets)))
        s=self.collector.summary(now+timedelta(minutes=60))
        self.assertEqual(s['targeted_bar_reads'],6);self.assertEqual(s['targeted_bars_recovered'],0)
        self.assertLess(s['coverage_pct'],100.)

    def test_inclusive_end_extreme_price_cannot_fill_a_second_minute(self):
        now=self.holes(2)
        def read(symbol,window,now):
            data=self.read_minute(symbol,window,now);end=utc(window['close'])
            data.loc[end]=[999.,1000.,998.,999.,1.]
            return data
        self.provider.intraday.side_effect=read
        self.collector.capture(now)
        s=self.collector.summary(now);self.assertLess(s['coverage_pct'],100.)
        self.assertEqual(s['targeted_bars_recovered'],2)
        for row in self.store.episodes():
            ev=self.collector.evidence(row,now)
            if self.missing[0].isoformat() in ev['minutes']:
                self.assertLess(ev['minutes'][self.missing[0].isoformat()]['copies'][0][1],1000.)

    def test_wrong_feed_bool_duplicate_or_invalid_target_cannot_create_coverage(self):
        for mode in ('feed','bool','duplicate','nan'):
            self.setUp();now=self.holes()
            def bad(symbol,window,now):
                data=self.read_minute(symbol,window,now).iloc[:1].copy()
                if mode=='feed':data.attrs['data_feed']='iex'
                elif mode=='bool':data=data.astype(object);data.iloc[0,4]=True
                elif mode=='duplicate':data=pd.concat([data,data]);data.attrs.update(data_feed='sip',adjustment='split')
                else:data.iloc[0,0]=float('nan')
                return data
            self.provider.intraday.side_effect=bad
            self.collector.capture(now);s=self.collector.summary(now)
            self.assertEqual(s['complete_pairs'],0,mode);self.assertEqual(s['targeted_bars_recovered'],0,mode)
            self.assertLess(s['coverage_pct'],100.)

    def test_access_failure_halts_worker_without_fanning_out_or_advancing_episode_attempts(self):
        now=self.holes();self.provider.intraday.side_effect=ResearchReadError('HTTP_403',status_code=403)
        self.provider.calendar.return_value=[self.session]
        worker=MoveResearchWorker(self.store,self.provider,study_limit=48,focused_collection=True)
        p=worker.cycle(now)
        self.assertEqual(p['health']['state'],'DATA_UNAVAILABLE');self.assertEqual(p['health']['reason'],'FORBIDDEN')
        self.assertEqual(self.provider.intraday.call_count,1)
        self.provider.assets.assert_not_called();self.provider.intraday_many.assert_not_called()
        self.assertTrue(all(r['attempts']==0 for r in self.store.episodes()))

    def test_returned_invalid_row_is_not_absent_and_cannot_be_replaced_silently(self):
        self.freeze();data=self.frame().astype(object);data.iloc[10,4]=True
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:data for symbol in symbols}
        now=utc(self.session['close'])+timedelta(minutes=16);self.collector.capture(now)
        self.provider.intraday.assert_not_called()
        self.assertEqual(self.collector.summary(now)['complete_pairs'],0)

    def test_original_pair_closest_to_completion_gets_priority_not_price_outcome(self):
        self.freeze(2);self.data=self.frame()
        def bulk(symbols,s,now):
            return {symbol:self.data.drop(self.data.index[10:13] if symbol.endswith('0') else self.data.index[10:11]) for symbol in symbols}
        self.provider.intraday_prefix_many.side_effect=bulk;self.provider.intraday.side_effect=self.read_minute
        now=utc(self.session['close'])+timedelta(minutes=16);self.collector.capture(now)
        self.assertEqual({c.args[0] for c in self.provider.intraday.call_args_list},{'CASE1','CTRL1'})
        self.assertEqual(self.collector.summary(now)['complete_pairs'],1)


if __name__=='__main__':unittest.main()
