import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd
import archival_entry_research as archive
from market_move_research import MoveStore,utc,comparison,read_packet
from market_move_diagnostics import observation_due
from market_move_research_loop import MoveResearchWorker
from market_jump_requests import schedule_studies
import test_market_move_research as fixtures
import test_prior_day_entry_study as entry_fixtures


def evidence():
    sessions=[fixtures.session(day) for day in ('2026-10-19','2026-10-20','2026-10-21','2026-10-22')]
    daily={}
    for symbol,matching in [('YESA',True),('YESB',True),('NOA',False),('NOB',False),('SHFS',True)]:
        bars=[]
        for i,day in enumerate(sessions):
            p=10.*(1.05**i); volume=100000*(3**i) if matching else 100000
            bars.append(dict(t=day['date']+'T04:00:00Z',o=p,h=p*1.06,l=p*.94,c=p,v=volume))
        if symbol=='SHFS':
            for b in bars:
                for k in ('o','h','l','c'):b[k]*=.02
        daily[symbol]=bars
    return daily,sessions,utc('2026-10-23T04:00Z')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'moves.db';self.store=MoveStore(self.path)
        self.daily,self.sessions,self.now=evidence()

    def test_pairing_never_uses_target_outcomes_and_focus_is_separate(self):
        before,focus=archive.selection(self.daily,self.sessions,self.now)
        changed=copy.deepcopy(self.daily)
        # Change only the last session's outcome. Its membership cannot change.
        for bars in changed.values():bars[-1].update(o=40.,h=90.,l=1.,c=2.,v=900000000.)
        after,_=archive.selection(changed,self.sessions,self.now)
        self.assertEqual(before[-1],after[-1])
        self.assertTrue(before[-1]['pairs'])
        self.assertEqual(focus['symbol'],'SHFS');self.assertLess(focus['features']['close'],.5)
        self.assertTrue(all('SHFS' not in p for p in before[-1]['pairs']))
        reversed_plan,_=archive.selection(dict(reversed(list(self.daily.items()))),self.sessions,self.now)
        self.assertEqual(before,reversed_plan)

    def test_initial_queue_is_immutable_and_never_enters_forward_training(self):
        result=archive.prepare(self.store,self.daily,self.sessions,self.now)
        before=self.store.episodes();self.assertGreater(result['queued'],0)
        self.assertTrue(all(r['role']=='ARCHIVE_STUDY' and not r['prospective'] for r in before))
        self.assertEqual(comparison(before)['pair_episodes'],0)
        changed=copy.deepcopy(self.daily)
        for bars in changed.values():bars[-2]['v']=1.
        self.assertEqual(archive.prepare(self.store,changed,self.sessions,self.now)['queued'],0)
        self.assertEqual([(r['episode_key'],r['features_json'],r['frozen_at']) for r in before],
                         [(r['episode_key'],r['features_json'],r['frozen_at']) for r in self.store.episodes()])
        self.assertIsNone(self.store.state('pattern_policy'))
        focus=next(r for r in before if r['features']['archive_group']=='FOCUS')
        with self.assertRaises(ValueError):self.store.add(focus['features'],focus['session'],
            'ARCHIVE_STUDY','bad',utc(focus['session']['open']),prospective=False)
        with self.assertRaises(ValueError):self.store.add(focus['features'],focus['session'],
            'ARCHIVE_STUDY','bad',self.now,prospective=True)

    def test_archive_retry_uses_enqueue_time_and_bounded_original_limits(self):
        archive.prepare(self.store,self.daily,self.sessions,self.now)
        row=self.store.episodes()[0]
        result=dict(status='PARTIAL_DATA',outcome=None,diagnostic_version='market-move-coverage-v2')
        self.assertTrue(self.store.settle(row['episode_key'],result,self.now))
        row=self.store.episodes()[0]
        self.assertTrue(observation_due(row,self.now+timedelta(minutes=16)))
        self.assertFalse(observation_due(row,self.now+timedelta(hours=49)))
        row['attempts']=3
        self.assertFalse(observation_due(row,self.now+timedelta(minutes=16)))

    def test_scheduler_preserves_forward_requests_and_total_budget(self):
        archive.prepare(self.store,self.daily,self.sessions,self.now);rows=self.store.episodes()*4
        # Distinct synthetic historical work, without duplicating real episode keys.
        rows=[dict(r,episode_key=str(i)) for i,r in enumerate(rows)]
        forwards=[dict(rows[0],episode_key='f'+str(i),role='ACTIVITY',prospective=True) for i in range(16)]
        selected=schedule_studies(forwards+rows,set(),limit=48)
        self.assertEqual({r['episode_key'] for r in selected[:16]},{r['episode_key'] for r in forwards})
        self.assertEqual(sum(r['role']=='ARCHIVE_STUDY' for r in selected),8)
        self.assertEqual(len({r['episode_key'] for r in selected}),len(selected))
        self.assertLessEqual(len(selected),48)

    def test_model_preserves_cost_stress_and_missing_data_not_profit(self):
        session=entry_fixtures.SESSION; data=entry_fixtures.PriorDayEntryTests().data()
        model=archive.run_model(data,session)
        self.assertEqual(model['state'],'SIMULATED_EXIT')
        self.assertEqual(model['original_bot_decision'],'UNKNOWN_NOT_RECONSTRUCTED')
        self.assertLess(model['scenarios']['25']['net_r'],model['scenarios']['10']['net_r'])
        partial=archive.run_model(data.iloc[:-1],session)
        self.assertEqual(partial['state'],'UNKNOWN_DATA');self.assertEqual(partial['scenarios'],{})

    def test_summary_excludes_focus_incomplete_models_and_native_pnl(self):
        archive.prepare(self.store,self.daily,self.sessions,self.now);rows=self.store.episodes()
        result=archive.run_model(entry_fixtures.PriorDayEntryTests().data(),entry_fixtures.SESSION)
        for r in rows:r.update(status='COMPLETE',result={'archive_entry':copy.deepcopy(result)})
        focus=next(r for r in rows if r['features']['archive_group']=='FOCUS')
        focus['result']['archive_entry']['scenarios']['10']['net_r']=1000.
        partial=next(r for r in rows if r['features']['archive_group']=='MATCH');partial['status']='PARTIAL_DATA'
        value=archive.summary(rows)
        self.assertEqual(value['pairs_selected'],4)
        self.assertEqual(value['pairs_complete_paths'],3)
        self.assertEqual(value['groups']['MATCH']['costs']['10']['simulated_exits'],3)
        self.assertLess(value['groups']['MATCH']['costs']['10']['mean_net_r'],2.)
        self.assertEqual(value['groups']['FOCUS']['costs']['10']['mean_net_r'],1000.)
        self.assertFalse(value['automatic_application']);self.assertFalse(value['execution_authority'])
        text=archive.summary_text(value)
        self.assertIn('ناقص',text);self.assertIn('اختبار بعد الحدث',text)

    def test_worker_runs_existing_reader_and_readonly_stock_report(self):
        archive.prepare(self.store,self.daily,self.sessions,self.now)
        provider=Mock();provider.calendar.return_value=self.sessions
        provider.intraday_many.side_effect=lambda symbols,s,n:{symbol:fixtures.frame(s) for symbol in symbols}
        provider.gap_trades.return_value={}
        self.store.state('universe_cache',dict(date=self.now.date().isoformat(),symbols=list(self.daily),
            selection_version=__import__('market_jump_paths').SCAN_VERSION))
        worker=MoveResearchWorker(self.store,provider,study_limit=48,batch_reads=True)
        worker.prepare=Mock(return_value=dict(state='READY',completed_session=self.sessions[-1]['date']))
        worker.cycle(self.now)
        rows=self.store.episodes()
        focus=next(r for r in rows if r['features']['archive_group']=='FOCUS')
        self.assertEqual(focus['status'],'COMPLETE');self.assertIn('archive_entry',focus['result'])
        self.assertFalse(provider.news_before.called)
        before=self.path.read_bytes()
        text=archive.stock_text('SHFS','2026-10-22',path=self.path)
        self.assertIn('ليست مدخلات الترشيح الأصلية',text)
        self.assertIn('لا يجتاز الشرط الحالي',text)
        self.assertIn('مستبعدة من مقارنة',text)
        self.assertEqual(self.path.read_bytes(),before)
        packet=read_packet(path=self.path,now=self.now)
        self.assertEqual(packet['archive_study']['total'],len(rows))
        self.assertEqual(packet['comparison']['pair_episodes'],0)

    def test_controller_exposes_archive_without_mutating_paper(self):
        import railway_controller as controller
        data=dict(archive_study=archive.summary([]))
        with patch('market_move_research.read_packet',return_value=data):
            self.assertIn('اختبار الأرشيف',controller.handle('/research_archive'))
        with (patch('market_research_findings.stock_research_text',return_value='study'),
              patch('stock_decision_evidence.stock_decision_text',return_value='decisions'),
              patch('prior_day_entry_study.stock_trace_text',return_value='trace'),
              patch('archival_entry_research.stock_text',return_value='archive') as read):
            self.assertEqual(controller.handle('/research_stock SHFS 2026-10-22'),'study\n\ndecisions\n\ntrace\n\narchive')
            read.assert_called_once_with('SHFS','2026-10-22')
