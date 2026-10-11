import json
import unittest
from types import SimpleNamespace
import pandas as pd
from market.research_diagnostics import coverage_diagnostics, coverage_text, error_diagnostics
from research_variants import retest, runner_stops


class WeekendResearchTests(unittest.TestCase):
    def bars(self, values):
        return pd.DataFrame(values, columns=['open', 'high', 'low', 'close', 'volume'],
                            index=pd.date_range('2026-10-09T14:00Z', periods=len(values), freq='min'))

    def test_missing_and_rejected_minutes_are_distinct(self):
        frame = self.bars([[10, 11, 9, 10, 1]] * 5).drop(index=pd.Timestamp('2026-10-09T14:02Z'))
        valid = frame.drop(index=pd.Timestamp('2026-10-09T14:01Z'))
        d = coverage_diagnostics(frame, valid, pd.Timestamp('2026-10-09T14:00Z'), pd.Timestamp('2026-10-09T14:05Z'))
        self.assertEqual(d['absent_provider_minutes'], 1)
        self.assertEqual(d['rejected_minutes'], 1)
        self.assertEqual(d['interior_missing_minutes'], 1)
        self.assertEqual(d['coverage_pct'], 60)
        self.assertEqual(d['missing_minutes'], 2)
        self.assertIn('مرجعة ومرفوضة 1', coverage_text([{'metadata_json': json.dumps({'data_diagnostics': d})}]))
        self.assertEqual(len(frame), 4)  # No gap filling.

    def test_request_error_is_not_called_market_coverage(self):
        exc = RuntimeError('subscription does not permit TOKEN')
        d = error_diagnostics(exc)
        self.assertEqual(d['reason'], 'SUBSCRIPTION_REQUIRED')
        self.assertNotIn('TOKEN', str(d))
        self.assertEqual(coverage_text([{'metadata_json': json.dumps({'data_diagnostics': d})}]), '')

    def test_retest_requires_later_bar_and_next_open(self):
        bars = self.bars([[10, 10.4, 9.5, 10.2, 1], [10.2, 10.6, 10.1, 10.4, 1],
                          [10.1, 10.3, 9.99, 10.2, 1], [10.3, 10.5, 10.2, 10.4, 1],
                          [11, 12.1, 10.9, 12, 1]])
        r = retest(bars, 10, 9)
        self.assertEqual(r['retest_at'], bars.index[2].isoformat())
        self.assertEqual(r['entry_at'], bars.index[3].isoformat())
        self.assertAlmostEqual(r['entry_fill'], 10.3103)
        self.assertEqual(r['outcome'], 'TARGET')

    def test_no_retest_no_entry_and_invalidation_prevents_chasing(self):
        bars = self.bars([[10, 10.4, 9.5, 10.2, 1], [11, 11.5, 10.5, 11.2, 1]])
        self.assertEqual(retest(bars, 10, 9)['outcome'], 'NO_RETEST_ENTRY')
        bars.iloc[1] = [8.8, 9.5, 8.5, 9.2, 1]
        self.assertEqual(retest(bars, 10, 9)['outcome'], 'INVALIDATED_BEFORE_ENTRY')

    def test_runner_rule_difference_and_gap_costs(self):
        bars = self.bars([[12.2, 12.6, 12.1, 12.4, 1], [13.2, 13.4, 13.1, 13.3, 1],
                          [12.5, 12.7, 12.4, 12.6, 1], [12.2, 12.3, 11.8, 12, 1]])
        r = runner_stops(bars, 10, 9, entry_fill=10.2)
        self.assertAlmostEqual(r['rules']['T2']['net_r'], 12.5 * .999 - 10.2)
        self.assertAlmostEqual(r['rules']['T1']['net_r'], 12 * .999 - 10.2)
        self.assertFalse(r['auto_apply'])

    def test_same_bar_raised_stop_order_is_not_assumed_profitable(self):
        bars = self.bars([[10, 13.4, 9.5, 13.2, 1]])
        r = runner_stops(bars, 10, 9)
        self.assertIsNone(r['rules']['T1']['net_r'])
        self.assertIsNone(r['rules']['T2']['net_r'])


class StrategyMarkTests(unittest.TestCase):
    def mark(self, positions, trades=None):
        from railway_controller import _open_strategy_marks
        trade = SimpleNamespace(symbol='SNAP', entry_price=6.2, initial_quantity=3, remaining_quantity=2)
        return _open_strategy_marks(trades or {'x': trade}, positions)

    def test_partial_position_unrealized_uses_remaining_quantity(self):
        r = self.mark([{'symbol': 'SNAP', 'qty': '2', 'current_price': '6.5'},
                       {'symbol': 'OTHER', 'qty': '1000', 'current_price': '100'}])
        self.assertAlmostEqual(r['unrealized'], .6)
        self.assertEqual(r['positions'][0]['sold'], 1)
        self.assertEqual(r['positions'][0]['remaining'], 2)

    def test_mismatched_position_missing_and_invalid_marks_are_unknown(self):
        for positions in ([], [{'symbol': 'SNAP', 'qty': '3', 'current_price': '6.5'}],
                          [{'symbol': 'SNAP', 'qty': '2', 'current_price': 'nan'}]):
            self.assertIsNone(self.mark(positions)['unrealized'])

    def test_duplicate_managed_symbol_cannot_double_count(self):
        t = SimpleNamespace(symbol='SNAP', entry_price=6.2, initial_quantity=2, remaining_quantity=2)
        self.assertIsNone(self.mark([{'symbol': 'SNAP', 'qty': '2', 'current_price': '6.5'}], {'a': t, 'b': t})['unrealized'])

    def test_daily_and_weekly_reports_separate_realized_and_open_marks(self):
        from unittest.mock import patch
        import railway_controller as controller
        snapshot = dict(starting_capital=100, strategy_equity=101.24, marked_equity=101.84,
                        unrealized=.6, period_realized=1.24, period_pct=.0124,
                        capital_adjustments_period=0, open_trades=1, trade_count=4,
                        gross_gain=3.53, gross_loss=2.29, winning_trades=1, losing_trades=3,
                        positions=[dict(symbol='SNAP', bought=3, sold=1, remaining=2, unrealized=.6)])
        with patch.object(controller, '_strategy_report_snapshot', return_value=snapshot), \
             patch.object(controller, 'get_account', return_value={'equity': 100001.84, 'cash': 99989.44}):
            for text in (controller.daily_report_text(), controller.weekly_report_text()):
                self.assertIn('$101.24', text)
                self.assertIn('$101.84', text)
                self.assertIn('$0.60', text)
                self.assertIn('مباع 1 | متبقٍ 2 سهم', text)
                self.assertIn('ليس ربحًا محققًا', text)
