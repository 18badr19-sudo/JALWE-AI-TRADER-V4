import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import opportunity_performance_tracker as module


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


class OpportunityWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Store(Path(self.temp.name) / 'test.sqlite')
        self.market = Mock()
        patcher = patch.object(module, 'database', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(module, 'get_market_data', return_value=self.market):
            self.tracker = module.OpportunityPerformanceTracker()
        self.start = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=60)
        self.payload = {'symbol': 'TEST', 'timestamp': self.start.isoformat(), 'jalwe': {
            'state': 'WATCHING', 'entry_price': 10., 'stop_price': 9.,
            'strategy': 'VWAP_BOUNCE',
            'gates': {key: True for key in self.tracker.PRE_ENTRY_GATES}}}
        self.tracker.start_from_payload(self.payload, 'v1')

    def row(self):
        with self.db.connection() as conn:
            return dict(conn.execute('SELECT * FROM opportunity_performance').fetchone())

    def frame(self, periods=60):
        return pd.DataFrame({'open': 10., 'high': 10.5, 'low': 9.5,
                             'close': 10., 'volume': 100.},
                            index=pd.date_range(self.start, periods=periods, freq='min'))

    def observe(self, frame, now=None):
        self.market.get_observation_bars.return_value = frame
        self.tracker._update_row(self.row(), now or self.end + timedelta(minutes=6))
        return self.row()

    def test_late_update_fetches_fixed_window_and_excludes_later_target(self):
        frame = self.frame(80)
        frame.loc[frame.index[60]:, ['open', 'high', 'low', 'close']] = 15.
        row = self.observe(frame, self.end + timedelta(hours=3))
        self.market.get_observation_bars.assert_called_once_with('TEST', self.start, self.end)
        self.assertEqual(row['status'], 'COMPLETE')
        self.assertIsNone(row['hit_t1_at'])
        self.assertAlmostEqual(row['mfe_pct'], 5.)
        self.assertAlmostEqual(row['mae_pct'], -5.)

    def test_sparse_window_not_complete_or_no_level_result(self):
        row = self.observe(self.frame().iloc[[59]])
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        summary = self.tracker.summary_for_ny_date('2026-10-05')
        self.assertEqual(summary['completed'], 0)
        self.assertEqual(summary['incomplete'], 1)
        self.assertEqual(summary['no_level'], 0)
        self.assertIsNone(summary['avg_mfe_pct'])

    def test_missing_checkpoint_never_substitutes_old_price(self):
        row = self.observe(self.frame().drop(self.frame().index[14]))
        self.assertIsNone(row['price_15m'])
        self.assertEqual(row['price_30m'], 10.)

    def test_positive_low_is_zero_adverse_excursion(self):
        frame = self.frame()
        frame[['open', 'high', 'low', 'close']] = 10.775
        row = self.observe(frame)
        self.assertAlmostEqual(row['mfe_pct'], 7.75)
        self.assertEqual(row['mae_pct'], 0.)

    def test_duplicate_and_invalid_bar_do_not_fill_coverage(self):
        frame = self.frame()
        frame.iloc[2, frame.columns.get_loc('low')] = 99.
        row = self.observe(pd.concat([frame, frame.iloc[[0]]]))
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertEqual(json.loads(row['metadata_json'])['observed_minutes'], 59)

    def test_empty_window_finishes_as_missing_data(self):
        row = self.observe(self.frame(0))
        self.assertEqual(row['status'], 'PARTIAL_DATA')
        self.assertIsNone(row['mfe_pct'])

    def test_waits_for_window_and_retries_during_grace(self):
        self.tracker._update_row(self.row(), self.end - timedelta(seconds=1))
        self.market.get_observation_bars.assert_not_called()
        row = self.observe(self.frame(59), self.end + timedelta(minutes=1))
        self.assertEqual(row['status'], 'TRACKING')
        row = self.observe(self.frame())
        self.assertTrue(self.tracker._verified_complete(row))

    def test_legacy_results_excluded_and_strategy_counts_separate(self):
        with self.db.connection() as conn:
            conn.execute("UPDATE opportunity_performance SET status='COMPLETE', mfe_pct=7.75, mae_pct=7.75")
        summary = self.tracker.summary_for_ny_date('2026-10-05')
        self.assertEqual(summary['unverified'], 1)
        self.assertEqual(summary['completed'], 0)
        report = self.tracker.build_daily_report(summary)
        self.assertIn('VWAP_BOUNCE: مكتملة 0/1', report)
        self.assertNotIn('أبرز الفرص:', report)

    def test_same_minute_stop_target_remains_ambiguous(self):
        frame = self.frame()
        frame.loc[frame.index[3], ['high', 'low']] = [12., 9.]
        row = self.observe(frame)
        self.assertEqual(row['first_level_hit'], 'AMBIGUOUS_STOP_T1')

    def test_provider_error_expires_instead_of_blocking_queue(self):
        self.market.get_observation_bars.side_effect = TimeoutError('provider unavailable')
        with patch.object(module, 'datetime') as clock:
            clock.now.return_value = self.end + timedelta(minutes=6)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.tracker.update_open()
        self.assertEqual(self.row()['status'], 'DATA_UNAVAILABLE')

    def test_report_lists_valid_strategy_average(self):
        self.observe(self.frame())
        summary = self.tracker.summary_for_ny_date('2026-10-05')
        report = self.tracker.build_daily_report(summary)
        self.assertIn('VWAP_BOUNCE: مكتملة 1/1', report)
        self.assertIn('متوسط MFE +5.00% | MAE -5.00%', report)
