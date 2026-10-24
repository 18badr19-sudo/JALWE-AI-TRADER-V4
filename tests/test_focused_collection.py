"""Focused collection: synthetic provider frames, frozen fixtures, temporary SQLite."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd
from focused_research_collection import FocusedCollector, selection, PROTOCOL
from market_move_research import MoveStore,utc,observe_session,report_text
from market_move_research_loop import MoveResearchWorker
from market_move_provider import MoveProvider,ResearchReadError
import test_market_move_research as fixtures


class FocusedCollectionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=MoveStore(Path(tmp.name)/'focus.db')
        self.session=fixtures.session()
        self.before=utc(self.session['open'])-timedelta(hours=1)
        self.provider=Mock()
        self.provider.news_before.return_value=dict(status="AVAILABLE",articles=[])
        self.collector=FocusedCollector(self.store,self.provider)
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:self.frame(s) for symbol in symbols}
        self.provider.gap_trades.side_effect=lambda symbol,minute,now:dict(minute=minute.isoformat(),complete=True,classification='NO_TRADES_IN_FEED')

    def frame(self,session=None):
        frame=fixtures.frame(session or self.session)
        frame.attrs.update(data_feed='sip',adjustment='split')
        return frame

    def pairs(self,count=1):
        for i in range(count):
            for symbol,role in ((f'CASE{i}','ACTIVITY'),(f'CTRL{i}','CONTROL')):
                f=fixtures.feature(symbol);f['dollar_volume']=1000000*(i+1)
                self.store.add(f,self.session,role,'pair'+str(i),self.before,prospective=True)

    def freeze(self,count=1):
        self.pairs(count);self.assertTrue(self.collector.freeze(self.session,self.before))

    def test_freezes_four_complete_pairs_using_prior_liquidity_without_results(self):
        self.pairs(7);rows=self.store.episodes();before=deepcopy(rows)
        chosen=selection(rows,self.session,self.before)
        self.assertEqual(len(chosen),8)
        self.assertEqual({m['pair_key'] for m in chosen},{'pair3','pair4','pair5','pair6'})
        for r in rows:r['result']={'mfe_pct':10000,'outcome':'UP8_FIRST'};r['status']='COMPLETE'
        self.assertEqual(chosen,selection(list(reversed(rows)),self.session,self.before))
        self.assertEqual(len(before),14)

    def test_freeze_cannot_retrofit_after_open_or_change_existing_panel(self):
        self.freeze(2);first=self.collector.manifest(self.before)
        self.assertFalse(self.collector.freeze(self.session,self.before+timedelta(minutes=1)))
        self.assertEqual(first,self.collector.manifest(self.before))
        self.assertEqual(selection(self.store.episodes(),self.session,utc(self.session['open'])),[])

    def test_nominees_share_symbols_but_never_displace_original_pair_links(self):
        self.pairs(4);rows=self.store.episodes()
        for i in range(10):
            row=deepcopy(rows[0]);row.update(symbol='NOM'+str(i),role='ENTRY_STUDY',
                prospective=False,episode_key='nominee'+str(i))
            row['features'].update(entry_protocol='prior-day-orb-execution-study-v1',
                snapshot_prepared_at=self.before.isoformat(),dollar_volume=10000000+i)
            rows.append(row)
        chosen=selection(rows,self.session,self.before)
        self.assertEqual(len(chosen),12)
        self.assertEqual(sum(r['role']=='ENTRY_STUDY' for r in chosen),4)
        self.assertEqual({r['symbol'] for r in chosen if r['role']=='ENTRY_STUDY'},
                         {'NOM6','NOM7','NOM8','NOM9'})

    def test_today_or_other_feed_features_cannot_enter_frozen_selection(self):
        self.pairs();rows=self.store.episodes()
        for field,value in (('evidence_day',self.session['date']),('source_feed','iex'),
                            ('available_after',self.session['open'])):
            bad=deepcopy(rows);bad[0]['features'][field]=value
            self.assertEqual(selection(bad,self.session,self.before),[])

    def test_clock_crossing_open_rolls_back_manifest(self):
        self.pairs()
        ticks=iter([self.before,utc(self.session['open'])])
        self.assertFalse(self.collector.freeze(self.session,self.before,clock=lambda:next(ticks)))
        self.assertIsNone(self.collector.manifest(self.before))

    def test_waits_before_delayed_first_minute_and_does_not_request(self):
        self.freeze()
        result,frames=self.collector.capture(utc(self.session['open'])+timedelta(minutes=16))
        self.assertEqual(result['state'],'WAITING_DELAYED_MINUTES')
        self.assertEqual(frames,{})
        self.provider.intraday_prefix_many.assert_not_called()
        status=self.collector.summary(self.before)
        self.assertIsNone(status['coverage_pct']);self.assertEqual(status['complete_pairs'],0)

    def test_capture_excludes_future_and_inclusive_end_and_cannot_finish_early(self):
        self.freeze()
        now=utc(self.session['open'])+timedelta(minutes=46,seconds=30)
        result,_=self.collector.capture(now)
        self.assertEqual(result['window_end'],(utc(self.session['open'])+timedelta(minutes=30)).isoformat())
        status=self.collector.summary(now)
        self.assertEqual((status['observed_minutes'],status['mature_expected_minutes']),(60,60))
        self.assertEqual(status['coverage_pct'],100.)
        self.assertEqual((status['complete_paths'],status['complete_pairs']),(0,0))
        for row in self.store.episodes():
            evidence=self.collector.evidence(row,now)
            self.assertEqual(len(evidence['minutes']),30)
            self.assertEqual(row['status'],'PENDING');self.assertEqual(row['attempts'],0)

    def test_complementary_real_reads_survive_restart_without_changing_decisions(self):
        self.freeze();before=self.store.episodes()
        data=self.frame();first=data.drop(data.index[10]);second=data.drop(data.index[20])
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:first for symbol in symbols}
        close=utc(self.session['close'])+timedelta(minutes=16)
        self.collector.capture(close)
        self.assertEqual(self.collector.summary(close)['complete_paths'],0)
        restarted=FocusedCollector(MoveStore(self.store.path),self.provider)
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:second for symbol in symbols}
        restarted.capture(close+timedelta(minutes=15))
        status=restarted.summary(close+timedelta(minutes=15))
        self.assertEqual((status['complete_paths'],status['complete_pairs']),(2,1))
        self.assertEqual(status['coverage_pct'],100.)
        self.assertEqual(before,self.store.episodes())

    def test_missing_or_conflicting_minute_remains_unknown(self):
        self.freeze();data=self.frame();data=data.drop(data.index[10])
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:data for symbol in symbols}
        now=utc(self.session['close'])+timedelta(minutes=16)
        self.collector.capture(now)
        status=self.collector.summary(now)
        self.assertLess(status['coverage_pct'],100.);self.assertEqual(status['complete_pairs'],0)
        changed=data.copy();changed.iloc[0,1]=100
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:changed for symbol in symbols}
        self.collector.capture(now+timedelta(minutes=15))
        self.assertEqual(self.collector.summary(now+timedelta(minutes=15))['complete_paths'],0)
        self.assertEqual(self.provider.gap_trades.call_count,2)

    def test_empty_success_cannot_erase_real_rows_and_bools_are_invalid(self):
        self.freeze();now=utc(self.session['close'])+timedelta(minutes=16)
        data=self.frame();data['volume']=data.volume.astype(object);data.iloc[0,4]=True
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:data for symbol in symbols}
        self.collector.capture(now)
        status=self.collector.summary(now)
        self.assertEqual(status['complete_paths'],0)
        count=status['observed_minutes']
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:self.frame().iloc[:0] for symbol in symbols}
        self.collector.capture(now+timedelta(minutes=15))
        self.assertEqual(self.collector.summary(now+timedelta(minutes=15))['observed_minutes'],count)

    def test_cadence_and_three_postclose_attempts_are_durable(self):
        self.freeze();now=utc(self.session['close'])+timedelta(minutes=16)
        for i in range(3):
            self.collector=FocusedCollector(self.store,self.provider)
            r,_=self.collector.capture(now+timedelta(minutes=15*i))
            self.assertEqual(r['state'],'CAPTURED')
            r,_=self.collector.capture(now+timedelta(minutes=15*i+1))
            self.assertEqual(r['state'],'PACED_OR_BUDGETED')
        r,_=self.collector.capture(now+timedelta(hours=1))
        self.assertEqual(r['state'],'PACED_OR_BUDGETED')
        self.assertEqual(self.provider.intraday_prefix_many.call_count,3)

    def test_provider_failure_preserves_unknown_category_and_old_evidence(self):
        self.freeze();now=utc(self.session['open'])+timedelta(minutes=46)
        self.collector.capture(now);before=self.collector.evidence(self.store.episodes()[0],now)
        self.provider.intraday_prefix_many.side_effect=ResearchReadError('HTTP_403',status_code=403)
        r,frames=self.collector.capture(now+timedelta(minutes=15))
        self.assertEqual(r['reason'],'FORBIDDEN');self.assertTrue(r['halt_reads'])
        self.assertEqual(frames,{})
        self.assertEqual(before,self.collector.evidence(self.store.episodes()[0],now))
        self.assertFalse(self.collector.summary(now)['training_authority'])

    def test_feed_confusion_is_rejected_without_saving_iex_as_sip(self):
        self.freeze();data=self.frame();data.attrs['data_feed']='iex'
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:data for symbol in symbols}
        with self.assertRaisesRegex(ValueError,'FEED_MISMATCH'):
            self.collector.capture(utc(self.session['close'])+timedelta(minutes=16))
        self.assertIsNone(self.collector.evidence(self.store.episodes()[0],self.before))

    def test_whole_session_worker_reuses_capture_and_preserves_original_latest_read(self):
        self.freeze();now=utc(self.session['close'])+timedelta(minutes=16)
        data=self.frame()
        self.provider.intraday_prefix_many.side_effect=lambda symbols,s,now:{symbol:data for symbol in symbols}
        self.provider.calendar.return_value=[self.session]
        self.store.state('universe_cache',dict(date=now.date().isoformat(),symbols=['CASE0','CTRL0'],selection_version=__import__('market_jump_paths').SCAN_VERSION))
        worker=MoveResearchWorker(self.store,self.provider,study_limit=48,batch_reads=True,focused_collection=True)
        worker.prepare=Mock(return_value=dict(state='READY',completed_session=self.session['date']))
        packet=worker.cycle(now)
        self.assertEqual(packet['counts'],{'COMPLETE':2},packet['health'])
        self.provider.intraday_many.assert_not_called();self.provider.intraday.assert_not_called()
        self.assertEqual(packet['focused_collection']['complete_pairs'],1)
        self.assertIn('جمع مركز',report_text(packet))
        self.assertTrue(all(r['attempts']==1 for r in self.store.episodes()))

    def test_published_coverage_survives_readonly_report_roundtrip(self):
        from market_move_research import read_packet
        self.freeze();now=utc(self.session['open'])+timedelta(minutes=46)
        self.collector.capture(now)
        self.store.state('focused_collection_status',self.collector.summary(now))
        self.store.publish(now,dict(state='READY',completed_session='2026-10-09'))
        packet=read_packet(path=self.store.path,now=now)
        self.assertEqual(packet['focused_collection']['symbols'],2)
        self.assertIn('تغطية محفوظة',report_text(packet))

    def test_calendar_and_auth_failures_are_reported_without_fanout(self):
        self.freeze();now=utc(self.session['close'])+timedelta(minutes=16)
        worker=MoveResearchWorker(self.store,self.provider,study_limit=48,focused_collection=True)
        self.provider.calendar.side_effect=TimeoutError('secret URL')
        packet=worker.cycle(now)
        self.assertEqual(packet['health']['state'],'DATA_UNAVAILABLE')
        self.provider.intraday_prefix_many.assert_not_called()
        self.provider.calendar.side_effect=None;self.provider.calendar.return_value=[self.session]
        self.provider.intraday_prefix_many.side_effect=ResearchReadError('HTTP_403',status_code=403)
        packet=worker.cycle(now)
        self.assertEqual(packet['health']['reason'],'FORBIDDEN')
        self.provider.intraday.assert_not_called();self.provider.intraday_many.assert_not_called()
        self.assertTrue(all(r['attempts']==0 for r in self.store.episodes()))
        self.assertNotIn('secret URL',json.dumps(packet))

    def test_raw_retention_expires_after_ninety_days_not_after_completion(self):
        self.freeze();now=utc(self.session['close'])+timedelta(minutes=16)
        self.collector.capture(now)
        row=self.store.episodes()[0]
        self.assertIsNotNone(self.collector.evidence(row,now+timedelta(days=89)))
        self.assertIsNone(self.collector.evidence(row,now+timedelta(days=91)))
        self.collector.capture(now+timedelta(days=91))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM focused_collection_bars').fetchone()[0],0)
        self.assertEqual(row['features_json'],self.store.episodes()[0]['features_json'])

    def test_intraday_budget_reserves_capture_before_historical_backlog(self):
        self.freeze(4);now=utc(self.session['open'])+timedelta(minutes=46)
        self.provider.calendar.return_value=[self.session]
        self.store.state('universe_cache',dict(date=now.date().isoformat(),symbols=['CASE0'],selection_version=__import__('market_jump_paths').SCAN_VERSION))
        worker=MoveResearchWorker(self.store,self.provider,study_limit=48,batch_reads=True,focused_collection=True)
        worker.prepare=Mock(return_value=dict(state='READY',completed_session='2026-10-09'))
        packet=worker.cycle(now)
        self.assertLessEqual(packet['health']['study_batch']['selected']+8,48)
        self.assertEqual(packet['focused_collection']['symbols'],8)
        self.assertEqual(packet['counts'],{'PENDING':8})


class FocusedProviderTests(unittest.TestCase):
    def test_prefix_uses_delayed_whole_minute_ends_and_preserves_source(self):
        provider=MoveProvider('dummy','dummy',session=Mock());session=fixtures.session()
        opening=utc(session['open']);now=opening+timedelta(minutes=46,seconds=59)
        rows=[dict(t=(opening+timedelta(minutes=i)).isoformat(),o=10,h=11,l=9,c=10,v=100) for i in (0,29,30,31)]
        with patch.object(provider,'get',return_value={'bars':{'CASE':rows}}) as get:
            data=provider.intraday_prefix_many(['CASE'],session,now)['CASE']
        self.assertEqual(len(data),2)
        params=get.call_args.args[1]
        self.assertEqual(utc(params['end']),opening+timedelta(minutes=30))
        self.assertEqual(params['feed'],'sip');self.assertEqual(params['adjustment'],'split')
        self.assertEqual(data.attrs['data_feed'],'sip')

    def test_early_close_delay_and_batch_limit_cannot_read_future(self):
        provider=MoveProvider('dummy','dummy',session=Mock());session=fixtures.session(hours=3.5)
        with patch.object(provider,'get',return_value={'bars':{}}) as get:
            with self.assertRaisesRegex(ResearchReadError,'WAITING'):
                provider.intraday_prefix_many(['CASE'],session,utc(session['open'])+timedelta(minutes=16))
            with self.assertRaisesRegex(ResearchReadError,'TOO_LARGE'):
                provider.intraday_prefix_many([str(i) for i in range(13)],session,utc(session['close'])+timedelta(minutes=16))
            get.assert_not_called()
            provider.intraday_prefix_many(['CASE'],session,utc(session['close'])+timedelta(minutes=16))
            self.assertEqual(utc(get.call_args.args[1]['end']),utc(session['close']))


if __name__=='__main__':unittest.main()
