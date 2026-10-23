"""Full-session research recovery without fetching prices or changing cohorts."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from market.retained_research_bars import merge_session_evidence
from market_move_research import MoveStore, observe_session, utc
from market_move_research_loop import MoveResearchWorker
import test_market_move_research as fixtures


class RetainedSessionEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store=MoveStore(Path(tmp.name)/'moves.db')
        self.session=fixtures.session()
        self.now=utc('2026-10-12T21:00Z')
        self.store.add(fixtures.feature('TEST'),self.session,'ACTIVITY','pair',
                       utc('2026-10-12T12:00Z'),prospective=True)
        self.provider=Mock()
        self.provider.calendar.return_value=[self.session]
        self.provider.news_before.return_value=dict(status='AVAILABLE',articles=[])
        self.provider.gap_trades.side_effect=lambda symbol,minute,now:dict(
            minute=minute.isoformat(),complete=True,classification='NO_TRADES_IN_FEED')
        self.store.state('universe_cache',dict(date=self.now.date().isoformat(),symbols=['TEST'],
            selection_version=__import__('market_jump_paths').SCAN_VERSION))

    def cycle(self, data, now=None):
        self.provider.intraday.return_value=data
        worker=MoveResearchWorker(self.store,self.provider)
        worker.prepare=Mock(return_value=dict(state='READY',completed_session=self.session['date']))
        with patch('market_move_research_loop.recheck_absent_bars',side_effect=lambda p,s,f,*a:f):
            worker.cycle(now or self.now)
        return self.store.episodes()[0]

    def test_complementary_reads_survive_restart_and_complete_original_frozen_cohort(self):
        data=fixtures.frame(self.session)
        before=self.store.episodes()[0]
        first=self.cycle(data.drop(data.index[20]))
        self.assertEqual(first['status'],'PARTIAL_DATA')
        self.assertIsNotNone(self.store.retained_evidence(first['episode_key'],self.now))
        self.store=MoveStore(self.store.path)
        second=self.cycle(data.drop(data.index[10]),self.now+timedelta(minutes=16))
        self.assertEqual(second['status'],'COMPLETE')
        self.assertEqual(second['result']['observed_minutes'],390)
        self.assertEqual(second['result']['latest_read_coverage']['missing_minutes'],1)
        self.assertEqual(second['features_json'],before['features_json'])
        self.assertEqual(second['frozen_at'],before['frozen_at'])
        self.assertIsNone(self.store.retained_evidence(first['episode_key'],self.now))
        self.assertEqual(self.provider.intraday.call_count,2)

    def test_conflicting_price_remains_partial_after_another_clean_read(self):
        data=fixtures.frame(self.session)
        self.cycle(data.iloc[:-1])
        changed=data.copy(); changed.loc[changed.index[10],'high']=20.
        row=self.cycle(changed,self.now+timedelta(minutes=16))
        self.assertEqual(row['status'],'PARTIAL_DATA')
        self.assertIsNone(row['result']['outcome'])
        row=self.cycle(data,self.now+timedelta(minutes=32))
        self.assertEqual(row['status'],'PARTIAL_DATA')
        self.assertIsNone(row['result']['outcome'])

    def test_transport_error_keeps_retry_evidence_without_old_outcome_metrics(self):
        data=fixtures.frame(self.session)
        first=self.cycle(data.iloc[:-1])
        self.provider.intraday.side_effect=TimeoutError('provider')
        failed=self.cycle(data,self.now+timedelta(minutes=16))
        self.assertEqual(failed['status'],'DATA_UNAVAILABLE')
        self.assertNotIn('mfe_pct',failed['result'])
        self.assertIsNotNone(self.store.retained_evidence(first['episode_key'],self.now))
        self.provider.intraday.side_effect=None
        row=self.cycle(data.iloc[-1:],self.now+timedelta(minutes=32))
        self.assertEqual(row['status'],'COMPLETE')

    def test_cache_expiry_and_stale_settlement_cannot_overwrite_evidence(self):
        row=self.cycle(fixtures.frame().iloc[:10])
        saved=self.store.retained_evidence(row['episode_key'],self.now)
        self.assertFalse(self.store.settle(row['episode_key'],dict(status='PARTIAL_DATA'),
            self.now+timedelta(minutes=16),retry=True,expected_attempts=0,bar_evidence={'corrupt':True}))
        self.assertEqual(self.store.retained_evidence(row['episode_key'],self.now),saved)
        self.assertEqual(self.store.prune_retained_evidence(self.now+timedelta(hours=49)),1)
        self.assertEqual(self.store.episodes()[0]['result_json'],row['result_json'])

    def test_early_close_duplicates_and_feed_window_isolation(self):
        session=fixtures.session(hours=3.5); data=fixtures.frame(session)
        start,end=utc(session['open']),utc(session['close'])
        def merge(raw,old=None,symbol='TEST'):
            return merge_session_evidence(raw,old,symbol=symbol,start=start,end=end,checked_at=self.now)
        combined,evidence=merge(data.iloc[:-1])
        combined,evidence=merge(data.iloc[-1:],evidence)
        self.assertEqual(observe_session(combined,session)['status'],'COMPLETE')
        repeated=pd.concat([data,data.iloc[:1]])
        combined,evidence=merge(repeated)
        self.assertEqual(observe_session(combined,session)['status'],'PARTIAL_DATA')
        combined,evidence=merge(data,evidence)
        self.assertEqual(observe_session(combined,session)['status'],'PARTIAL_DATA')
        with self.assertRaises(ValueError):merge(data,evidence,symbol='OTHER')
        wrong=data.copy(); wrong.attrs['data_feed']='iex'
        with self.assertRaises(ValueError):merge(wrong)
