"""Historical SIP recovery; synthetic data, no broker or trading inputs."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

from delayed_sip_research import DelayedSipResearch
from decision_outcome_memory import DecisionOutcomeMemory
from test_delayed_sip_research import Store


class RetainedSipEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Store(Path(tmp.name) / 'research.db')
        self.market = Mock()
        self.memory = DecisionOutcomeMemory(self.db, self.market)
        self.research = self.memory.delayed_sip_research
        self.start = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=60)
        self.payload = dict(timestamp=self.start.isoformat(), symbol='TEST', jalwe=dict(
            state='WATCHING', reason='waiting', entry_price=10., stop_price=9., target_1=12.,
            metadata=dict(decision_data_feed='iex', decision_features=dict(
                price=10., data_quality_ok=True, data_is_stale=False))))
        self.memory.record(self.payload, 'v1')

    def frame(self):
        frame = pd.DataFrame(dict(open=10., high=10.5, low=9.5, close=10., volume=100.),
                             index=pd.date_range(self.start, periods=60, freq='min'))
        frame.attrs['data_feed'] = 'sip'
        return frame

    def observe(self, frame, minutes=30):
        self.market.get_research_observation_bars.return_value = frame
        self.research.update_due(now=self.end + timedelta(minutes=minutes))
        with self.db.connection() as conn:
            row = dict(conn.execute("SELECT * FROM delayed_sip_research WHERE symbol='TEST'").fetchone())
        return row, json.loads(row['result_json'])

    def test_complementary_recheck_preserves_previously_returned_minutes(self):
        first = self.frame().drop(self.frame().index[20])
        before, _ = self.observe(first)
        second = self.frame().drop(self.frame().index[10])
        row, result = self.observe(second, 90)
        self.assertEqual(row['status'], 'COMPLETE')
        self.assertEqual(result['observed_minutes'], 60)
        self.assertEqual(result['evidence_recovery']['retained_minutes'], 1)
        self.assertEqual(result['latest_read_diagnostics']['missing_minutes'], 1)
        with self.db.connection() as conn:
            history = conn.execute('SELECT * FROM delayed_sip_recheck_history').fetchone()
        self.assertEqual(history['previous_result_json'], before['result_json'])
        self.assertEqual(self.market.get_research_observation_bars.call_count, 2)
        self.assertIn('مكتملة منها 1', self.research.report_for_ny_date('2026-10-02'))
        audit = self.research.completed_audit_for_ny_date('2026-10-02')
        self.assertEqual(audit['records'][0]['evidence_recovery']['retained_minutes'], 1)

    def test_restart_retains_observed_bars_without_rewriting_decision_inputs(self):
        with self.db.connection() as conn:
            original = dict(conn.execute('SELECT * FROM decision_outcome_memory').fetchone())
        self.observe(self.frame().iloc[:59])
        self.research = DelayedSipResearch(self.db, self.market)
        self.memory.experiments.observe = Mock(side_effect=AssertionError('preferences changed'))
        row, result = self.observe(self.frame().iloc[59:], 90)
        self.assertEqual(row['status'], 'COMPLETE')
        self.assertEqual(result['evidence_recovery']['retained_minutes'], 59)
        with self.db.connection() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM decision_outcome_memory').fetchone()), original)
        self.memory.experiments.observe.assert_not_called()
        self.market.get_observation_bars.assert_not_called()

    def test_sparser_or_empty_read_never_discards_previously_observed_minutes(self):
        self.observe(self.frame().iloc[:50])
        row, result = self.observe(self.frame().iloc[45:55], 90)
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(result['observed_minutes'], 55)
        row, result = self.observe(self.frame().iloc[:0], 150)
        self.assertEqual(result['observed_minutes'], 55)
        self.assertFalse(result['complete_path'])
        self.assertEqual(result['latest_read_diagnostics']['reason'], 'EMPTY_PROVIDER_WINDOW')

    def test_conflicting_price_cannot_be_resolved_by_later_row_order_or_clean_read(self):
        self.observe(self.frame().iloc[:59])
        changed = self.frame()
        changed.loc[changed.index[10], 'high'] = 14.
        row, result = self.observe(changed, 90)
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(result['data_diagnostics']['rejected_minutes'], 1)
        self.assertEqual(result['evidence_recovery']['conflicting_minutes'], 1)
        self.assertEqual(result['levels']['outcome'], 'INCOMPLETE_PATH')
        row, result = self.observe(self.frame(), 150)
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(result['observed_minutes'], 59)
        self.assertEqual(len(result['bar_evidence']['minutes'][changed.index[10].isoformat()]['copies']), 2)

    def test_invalid_conflicting_copy_survives_json_and_restart(self):
        self.observe(self.frame().iloc[:59])
        invalid = self.frame().iloc[10:11].copy()
        invalid.loc[:, 'high'] = float('nan')
        row, result = self.observe(pd.concat([self.frame(), invalid]), 90)
        json.dumps(result, allow_nan=False)
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.research = DelayedSipResearch(self.db, self.market)
        row, result = self.observe(self.frame(), 150)
        self.assertEqual(result['observed_minutes'], 59)
        self.assertFalse(result['complete_path'])

    def test_provider_failure_preserves_evidence_but_cannot_claim_success(self):
        _, before = self.observe(self.frame().iloc[:59])
        self.market.get_research_observation_bars.side_effect = TimeoutError('provider')
        row, result = self.observe(self.frame(), 90)
        self.assertEqual(row['status'], 'DATA_UNAVAILABLE')
        self.assertEqual(result['bar_evidence'], before['bar_evidence'])
        self.assertFalse(result['complete_path'])
        self.assertNotIn('mfe_pct', result)
        self.market.get_research_observation_bars.side_effect = None
        row, result = self.observe(self.frame().iloc[59:], 150)
        self.assertEqual(row['status'], 'COMPLETE')

    def test_wrong_feed_never_merges_with_sip(self):
        _, before = self.observe(self.frame().iloc[:59])
        wrong = self.frame().iloc[59:].copy()
        wrong.attrs['data_feed'] = 'iex'
        row, result = self.observe(wrong, 90)
        self.assertEqual(row['status'], 'DATA_UNAVAILABLE')
        self.assertEqual(result['bar_evidence'], before['bar_evidence'])

    def test_corrupted_or_cross_window_saved_context_is_rejected(self):
        self.observe(self.frame().iloc[:59])
        with self.db.connection() as conn:
            result = json.loads(conn.execute('SELECT result_json FROM delayed_sip_research').fetchone()[0])
            result['bar_evidence']['symbol'] = 'OTHER'
            conn.execute('UPDATE delayed_sip_research SET result_json=?', (json.dumps(result),))
        row, result = self.observe(self.frame(), 90)
        self.assertEqual(row['status'], 'DATA_UNAVAILABLE')
        self.assertFalse(result['complete_path'])

    def test_other_symbol_cannot_borrow_previously_seen_minute(self):
        self.observe(self.frame().iloc[:59])
        other = copy.deepcopy(self.payload)
        other['symbol'] = 'OTHER'
        self.memory.record(other, 'v2')
        self.observe(self.frame().iloc[59:], 90)
        with self.db.connection() as conn:
            row = dict(conn.execute("SELECT * FROM delayed_sip_research WHERE symbol='OTHER'").fetchone())
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(json.loads(row['result_json'])['observed_minutes'], 1)

    def test_later_bar_or_identical_duplicate_cannot_supply_missing_minute(self):
        self.observe(self.frame().iloc[:59])
        later = self.frame().iloc[0:1].copy()
        later.index = pd.DatetimeIndex([self.end])
        later.loc[:, 'high'] = 50.
        row, result = self.observe(pd.concat([self.frame().iloc[:59], later]), 90)
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(result['observed_minutes'], 59)
        self.assertAlmostEqual(result['mfe_pct'], 5.)
