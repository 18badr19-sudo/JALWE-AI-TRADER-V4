from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

import pandas as pd

from decision_outcome_memory import DecisionOutcomeMemory
from market.research_diagnostics import coverage_diagnostics
from test_decision_outcome_memory import Store


class ConflictingResearchBarsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 15, 14, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=60)
        self.frame = pd.DataFrame(dict(open=10., high=10.5, low=9.5,
                                       close=10., volume=100.),
                                  index=pd.date_range(self.start, periods=60, freq='min'))

    def test_conflicting_duplicate_cannot_choose_target_or_stop_by_row_order(self):
        changed = self.frame.iloc[10:11].copy()
        changed.loc[:, 'high'] = 12.5
        for pieces in ((self.frame, changed), (changed, self.frame)):
            with self.subTest(reversed=pieces[0] is changed):
                raw = pd.concat(pieces)
                valid = DecisionOutcomeMemory._valid_bars(raw, self.start, self.end)
                self.assertEqual(len(valid), 59)
                self.assertNotIn(changed.index[0], valid.index)
                info = coverage_diagnostics(raw, valid, self.start, self.end)
                self.assertEqual(info['reason'], 'INVALID_OR_DUPLICATE_BARS')
                self.assertEqual(info['rejected_minutes'], 1)
                self.assertEqual(info['absent_provider_minutes'], 0)

    def test_identical_repeated_row_does_not_create_a_missing_minute(self):
        raw = pd.concat([self.frame, self.frame.iloc[10:11]])
        valid = DecisionOutcomeMemory._valid_bars(raw, self.start, self.end)
        pd.testing.assert_frame_equal(valid, self.frame, check_freq=False)

    def test_invalid_conflicting_copy_cannot_be_discarded_to_claim_completeness(self):
        changed = self.frame.iloc[10:11].copy()
        changed.loc[:, 'high'] = float('nan')
        raw = pd.concat([self.frame, changed])
        self.assertEqual(len(DecisionOutcomeMemory._valid_bars(raw, self.start, self.end)), 59)

    def test_conflicting_window_remains_inconclusive_in_saved_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            market = Mock()
            memory = DecisionOutcomeMemory(Store(Path(temp) / 'test.db'), market)
            payload = dict(symbol='TEST', timestamp=self.start.isoformat(), jalwe=dict(
                state='REJECTED', reason='quality', entry_price=10., stop_price=9., target_1=12.,
                metadata=dict(decision_data_feed='iex', decision_features=dict(
                    price=10., data_quality_ok=True, data_is_stale=False))))
            memory.record(payload, 'v1')
            changed = self.frame.iloc[10:11].copy(); changed.loc[:, 'high'] = 12.5
            market.get_observation_bars.return_value = pd.concat([self.frame, changed])
            memory.update_due(now=self.end + timedelta(minutes=11))
            with memory.db.connection() as conn:
                row = conn.execute('SELECT * FROM decision_outcome_memory').fetchone()
                self.assertEqual(row['status'], 'PARTIAL_DATA')
                result = json.loads(row['result_json'])
                self.assertFalse(result['complete_path'])
                self.assertEqual(result['levels']['outcome'], 'INCOMPLETE_PATH')
            sip = market.get_observation_bars.return_value.copy()
            sip.attrs['data_feed'] = 'sip'
            market.get_research_observation_bars.return_value = sip
            memory.delayed_sip_research.update_due(now=self.end + timedelta(minutes=30))
            with memory.db.connection() as conn:
                row = conn.execute('SELECT * FROM delayed_sip_research').fetchone()
                self.assertEqual(row['status'], 'PARTIAL_DATA')
                self.assertFalse(json.loads(row['result_json'])['complete_path'])


if __name__ == '__main__':
    unittest.main()
