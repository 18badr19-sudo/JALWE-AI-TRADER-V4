import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import Mock, patch

import market_move_trial as trial
from market_move_research import MoveStore, utc, read_packet, report_text
import test_market_move_research as fixtures
import jalwe_research_watcher as watcher


class OwnerTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'moves.db'; self.store = MoveStore(self.path)
        self.now = utc('2026-10-19T14:00:00Z')
        self.session = fixtures.session('2026-10-19')
        self.features = [dict(fixtures.feature('MATCH',day='2026-10-16'),
            range_pct=8., volume_ratio_previous_day=2., daily_return_pct=3.),
            fixtures.feature('OTHER',day='2026-10-16')]
        self.store.save_pattern_features(self.features,self.session,utc('2026-10-19T12:00:00Z'))
        self.store.publish(self.now,{'state':'READY'},patterns={'state':'COLLECTING_TRAINING'})
        self.items = [NS(symbol='OTHER'),NS(symbol='MATCH'),NS(symbol='UNKNOWN'),NS(symbol='MATCH')]
        self.kw = dict(now=self.now,symbol_of=lambda r:r.symbol,paper=True,allow_live=False,path=self.path)
        self.env = patch.dict(os.environ,{'JALWE_PRIOR_DAY_TRIAL_ENABLED':'true'})
        self.env.start(); self.addCleanup(self.env.stop)

    def rewrite(self, change):
        with self.store.connect() as conn:
            payload = json.loads(conn.execute('SELECT payload_json FROM move_pattern_features WHERE symbol=?',('MATCH',)).fetchone()[0])
            change(payload)
            conn.execute('UPDATE move_pattern_features SET payload_json=? WHERE symbol=?',(json.dumps(payload),'MATCH'))

    def test_all_three_inclusive_thresholds_required_unknown_never_matches(self):
        self.assertTrue(trial.matches(dict(trial.RULE)))
        for key in trial.RULE:
            self.assertFalse(trial.matches(dict(trial.RULE,**{key:trial.RULE[key]-.001})))
            for value in (None,True,float('inf'),float('nan'),'8'):
                self.assertIsNone(trial.matches(dict(trial.RULE,**{key:value})))

    def test_priority_is_stable_keeps_identity_duplicates_and_does_not_activate_learning(self):
        ordered,audit = trial.prioritize(self.items,**self.kw)
        self.assertEqual([id(i) for i in ordered],[id(self.items[i]) for i in (1,3,0,2)])
        self.assertEqual(audit['prioritized_symbols'],['MATCH'])
        self.assertEqual(audit['unknown_symbols'],['UNKNOWN'])
        self.assertEqual(audit['nonmatching_symbols'],['OTHER'])
        self.assertEqual(audit['matched_evidence'][0]['range_pct'],8.)
        self.assertEqual(audit['learned_policy_state'],'COLLECTING_TRAINING')
        self.assertFalse(audit['entry_authority'])
        self.assertIsNone(self.store.state('pattern_policy'))
        self.assertEqual(len(self.store.episodes()),0)

    def test_no_live_disabled_before_start_or_expired_and_expiry_survives_restart(self):
        for override in ({'paper':False},{'allow_live':True},{'now':utc(trial.START)-timedelta(seconds=1)},
                         {'now':utc(trial.END)},{'now':utc(trial.END)+timedelta(days=1)}):
            self.assertEqual(trial.prioritize(self.items,**dict(self.kw,**override))[0],self.items)
        with patch.dict(os.environ,{'JALWE_PRIOR_DAY_TRIAL_ENABLED':'false'}):
            self.assertEqual(trial.prioritize(self.items,**self.kw)[1]['state'],'DISABLED')
        self.assertEqual(trial.status(utc(trial.END),paper=True,allow_live=False)['state'],'EXPIRED')

    def test_current_day_after_open_future_wrong_feed_or_wrong_symbol_evidence_not_used(self):
        changes = [lambda p:p['features'].update(evidence_day='2026-10-19'),
            lambda p:p.update(prepared_at=self.session['open']),
            lambda p:p['features'].update(available_after='2026-10-19T14:01:00Z'),
            lambda p:p['features'].update(source_feed='iex'),
            lambda p:p['features'].update(symbol='OTHER'),
            lambda p:p['session'].update(date='2026-10-20')]
        with self.store.connect() as conn:
            original = conn.execute('SELECT payload_json FROM move_pattern_features WHERE symbol=?',('MATCH',)).fetchone()[0]
        for change in changes:
            with self.store.connect() as conn:
                conn.execute('UPDATE move_pattern_features SET payload_json=? WHERE symbol=?',(original,'MATCH'))
            self.rewrite(change)
            ordered,audit = trial.prioritize(self.items,**self.kw)
            self.assertEqual(ordered,self.items)
            self.assertIn('MATCH',audit['unknown_symbols'])

    def test_session_close_and_next_session_cannot_reuse_snapshots(self):
        for stamp in (utc(self.session['close']),utc('2026-10-20T14:00:00Z')):
            self.store.publish(stamp,{'state':'READY'})
            self.assertEqual(trial.prioritize(self.items,**dict(self.kw,now=stamp))[0],self.items)

    def test_missing_stale_future_or_unhealthy_packet_returns_original_order(self):
        for packet in (None,{'health':{'state':'FAILED'}}):
            with patch.object(trial,'read_packet',return_value=packet):
                self.assertEqual(trial.prioritize(self.items,**self.kw)[0],self.items)
        for stamp in (self.now-timedelta(seconds=3601),self.now+timedelta(seconds=1)):
            self.store.publish(stamp,{'state':'READY'})
            with self.store.connect() as conn:
                conn.execute('DELETE FROM move_packets WHERE published_at!=?',(stamp.isoformat(),))
            self.assertEqual(trial.prioritize(self.items,**self.kw)[0],self.items)

    def test_bad_row_is_unknown_and_reader_error_does_not_expose_private_text(self):
        self.rewrite(lambda p:p['features'].update(range_pct=float('nan')))
        self.assertEqual(trial.prioritize(self.items,**self.kw)[0],self.items)
        with patch.object(trial,'read_packet',side_effect=PermissionError('private secret')):
            ordered,audit = trial.prioritize(self.items,**self.kw)
        self.assertEqual(ordered,self.items)
        self.assertEqual(audit['error_type'],'PermissionError')
        self.assertNotIn('private',json.dumps(audit))

    def test_watcher_preserves_native_processing_and_position_management_in_priority_order(self):
        ordered,audit = trial.prioritize(self.items,**self.kw)
        bridge = Mock(); bridge.list_recent_research.return_value=self.items; bridge.get_age_minutes.return_value=1
        with patch('market_move_patterns.prioritize',return_value=(self.items,{})), \
             patch.object(trial,'prioritize',return_value=(ordered,audit)), \
             patch.object(watcher,'should_process',return_value=(True,'')), \
             patch.object(watcher,'process_research',return_value=True) as process, \
             patch.object(watcher,'manage_active_paper_trades') as manage, \
             patch.object(watcher,'process_emergency_paper_close'), patch.object(watcher.logger,'info') as log:
            self.assertEqual(watcher.scan_bridge(bridge,Mock(),{}),(4,4))
        self.assertEqual([c.args[0] for c in process.call_args_list],ordered)
        self.assertEqual(manage.call_count,4)
        self.assertTrue(any('MARKET_MOVE_TRIAL_APPLIED' in str(c) for c in log.call_args_list))

    def test_report_distinguishes_manual_experiment_from_training(self):
        packet = read_packet(path=self.path,now=self.now)
        packet['patterns']={'protocol':'prior-day-move-patterns-v1','state':'COLLECTING_TRAINING'}
        text = report_text(packet)
        self.assertIn('يجمع بيانات تدريب مستقبلية',text)
        self.assertIn('تجربة بدر',text)
        self.assertIn('الشروط مجتمعة',text)
        self.assertIn('لا تتجاوز شروط الشراء والمخاطر',text)


if __name__ == '__main__':
    unittest.main()
