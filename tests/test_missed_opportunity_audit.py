import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.database import Database
from decision_outcome_memory import DecisionOutcomeMemory
from missed_opportunity_audit import read_audit, summarize, emit_audit


class MissedOpportunityAuditTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 0, tzinfo=timezone.utc)
        self.day = datetime(2026, 10, 12).date()
        self.at = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)
        self.result = dict(complete_path=True, observed_minutes=60, observation_feed='sip',
            levels=dict(outcome='TARGET_FIRST', trigger_price=10, stop_price=9,
                target_1=12, activation_close=10.1, theoretical_target=False,
                activated_at=(self.at+timedelta(minutes=1)).isoformat(),
                target_touch_at=(self.at+timedelta(minutes=10)).isoformat(), stop_touch_at=None))
        self.row = dict(decision_key='k', symbol='TEST', decided_at=self.at.isoformat(),
            research_version='v1', source_state='REJECTED', reason='OpportunityEngine rejected the setup.',
            strategy='VWAP_BOUNCE', data_feed='iex', status='PARTIAL_DATA', result_json='{}',
            window_start=self.at.isoformat(), window_end=(self.at+timedelta(minutes=60)).isoformat(),
            payload_json=json.dumps({'jalwe': {'metadata': {'opportunity_evidence': {
                'score': 60, 'minimum_score': 70, 'approved': False}}}}))
        self.sip = dict(source_kind='DECISION', source_key='k', status='COMPLETE',
                        result_json=json.dumps(self.result))

    def run_summary(self, rows=None, sips=None, entered=None):
        return summarize(rows or [self.row], sips if sips is not None else [self.sip], entered or set(),
                         start_day=self.day, end_day=self.day, as_of=self.now)

    def test_rejected_complete_sip_path_keeps_original_reason_and_times(self):
        report = self.run_summary()
        candidate = report['candidates'][0]
        self.assertEqual(candidate['reason'], self.row['reason'])
        self.assertEqual(candidate['decided_at_ny'], '2026-10-12T10:00:00-04:00')
        self.assertEqual(candidate['opportunity_evidence']['score'], 60)
        self.assertEqual(candidate['execution_feasibility'], 'NOT_ESTABLISHED_FROM_BARS')
        self.assertEqual(candidate['observation_feed'], 'sip')

    def test_partial_unobservable_and_false_complete_are_inconclusive(self):
        for changes in (dict(status='PARTIAL_DATA'), dict(status='UNOBSERVABLE'),
                        dict(result_json=json.dumps({**self.result, 'observed_minutes': 59})),
                        dict(result_json=json.dumps({**self.result, 'complete_path': False}))):
            report = self.run_summary(sips=[{**self.sip, **changes}])
            self.assertEqual(report['candidates'], [])
            self.assertEqual(report['days']['2026-10-12']['outcomes']['INCONCLUSIVE'], 1)

    def test_ambiguous_stop_first_not_activated_and_past_target_are_not_candidates(self):
        for outcome in ('STOP_FIRST', 'AMBIGUOUS_STOP_TARGET', 'NOT_ACTIVATED',
                        'ACTIVATION_PAST_TARGET', 'NO_VALID_SETUP_LEVELS'):
            result = copy.deepcopy(self.result)
            result['levels']['outcome'] = outcome
            self.assertEqual(self.run_summary(sips=[{**self.sip, 'result_json': json.dumps(result)}])['candidates'], [])

    def test_bad_chronology_price_and_future_evidence_are_not_candidates(self):
        for changes in (dict(stop_price=11), dict(activation_close=12),
                        dict(target_touch_at=self.at.isoformat()),
                        dict(target_touch_at=(self.at+timedelta(minutes=61)).isoformat()),
                        dict(stop_touch_at=(self.at+timedelta(minutes=5)).isoformat()),
                        dict(trigger_price=float('nan'))):
            result = copy.deepcopy(self.result)
            result['levels'].update(changes)
            self.assertEqual(self.run_summary(sips=[{**self.sip, 'result_json': json.dumps(result)}])['candidates'], [])

    def test_earliest_qualifying_decision_per_report_is_not_double_counted(self):
        later = {**self.row, 'decision_key': 'later', 'decided_at': (self.at+timedelta(seconds=1)).isoformat()}
        # Its window starts at the next full minute.
        later['window_start'] = (self.at+timedelta(minutes=1)).isoformat()
        result = copy.deepcopy(self.result)
        result['levels']['activated_at'] = (self.at+timedelta(minutes=2)).isoformat()
        sip = {**self.sip, 'source_key': 'later', 'result_json': json.dumps(result)}
        report = self.run_summary(rows=[later, self.row], sips=[self.sip, sip])
        self.assertEqual(report['target_first_rejected_watching_decisions'], 2)
        self.assertEqual(report['target_first_report_episodes'], 1)
        self.assertEqual(report['target_first_symbol_days'], 1)
        self.assertEqual(report['candidates'][0]['decision_key'], 'k')

    def test_ready_rows_and_same_day_entries_are_not_called_missed_profits(self):
        ready = {**self.row, 'source_state': 'READY_FOR_PAPER_EXECUTION'}
        self.assertEqual(self.run_summary(rows=[ready])['candidates'], [])
        candidate = self.run_summary(entered={('TEST', '2026-10-12')})['candidates'][0]
        self.assertTrue(candidate['symbol_entered_same_day'])
        self.assertNotIn('pnl', candidate)

    def test_native_complete_fallback_and_cross_feed_disagreement_are_explicit(self):
        native = copy.deepcopy(self.result)
        native.pop('observed_minutes')
        native['observed_bars'] = 60
        row = {**self.row, 'status': 'COMPLETE', 'result_json': json.dumps(native)}
        self.assertEqual(self.run_summary(rows=[row], sips=[])['candidates'][0]['observation_feed'], 'iex')
        native['levels']['outcome'] = 'STOP_FIRST'
        row['result_json'] = json.dumps(native)
        self.assertTrue(self.run_summary(rows=[row])['candidates'][0]['cross_feed_outcome_disagreement'])

    def test_read_only_database_is_unchanged_and_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'audit.db'
            db = Database(str(path))
            memory = DecisionOutcomeMemory(db, Mock())
            payload = {'timestamp': self.at.isoformat(), 'symbol': 'TEST', 'jalwe': {
                'state':'WATCHING', 'reason':'waiting', 'entry_price':10, 'stop_price':9,
                'target_1':12, 'metadata':{'decision_data_feed':'iex', 'decision_features': {
                    'price':10, 'data_quality_ok':True, 'data_is_stale':False}}}}
            memory.record(payload, 'v1')
            with db.connection() as conn:
                before = [tuple(r) for r in conn.execute('SELECT * FROM decision_outcome_memory')]
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
                read_audit(path, now=self.now)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with db.connection() as conn:
                self.assertEqual(before, [tuple(r) for r in conn.execute('SELECT * FROM decision_outcome_memory')])
            missing = Path(tmp)/'missing.db'
            with self.assertRaises(Exception):
                read_audit(missing, now=self.now)
            self.assertFalse(missing.exists())

    def test_log_errors_are_sanitized(self):
        with patch('missed_opportunity_audit.read_audit', side_effect=RuntimeError('private data')), patch('builtins.print') as print_mock:
            emit_audit('unused')
        self.assertNotIn('private data', str(print_mock.call_args))
        self.assertIn('RuntimeError', str(print_mock.call_args))
