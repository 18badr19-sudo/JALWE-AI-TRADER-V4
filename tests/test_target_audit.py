import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.target_audit import audit_trades, report


class TargetAuditTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 2, 13, 55, tzinfo=timezone.utc)
        self.end = datetime(2026, 10, 2, 20, tzinfo=timezone.utc)
        self.targets = (11.82, 11.90, 11.98)

    def audit(self, fetch, **kwargs):
        return audit_trades('CANE', self.start, self.end, self.targets, fetch, **kwargs)

    def test_target_on_second_page_and_exact_boundary(self):
        fetch = Mock(side_effect=[
            {'trades': {'CANE': [{'p': 11.81, 't': '2026-10-02T14:00:00Z'}]}, 'next_page_token': 'page2'},
            {'trades': {'CANE': [{'p': 11.82, 't': '2026-10-02T19:59:00Z'}]}, 'next_page_token': None}])
        result = self.audit(fetch)
        self.assertTrue(result['complete'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['touches'][0], '2026-10-02T19:59:00+00:00')
        self.assertIsNone(result['touches'][1])
        self.assertEqual(fetch.call_args.args[0]['page_token'], 'page2')
        self.assertEqual(fetch.call_args.args[0]['feed'], 'iex')

    def test_page_cap_does_not_claim_no_touch(self):
        result = self.audit(Mock(return_value={'trades': {'CANE': []}, 'next_page_token': 'more'}), max_pages=1)
        self.assertFalse(result['complete'])

    def test_repeated_token_stops_without_complete_coverage(self):
        fetch = Mock(return_value={'trades': {'CANE': []}, 'next_page_token': 'same'})
        self.assertFalse(self.audit(fetch)['complete'])
        self.assertEqual(fetch.call_count, 2)

    def test_invalid_or_out_of_window_rows_cannot_prove_absence(self):
        fetch = Mock(return_value={'trades': {'CANE': [
            {'p': 'NaN', 't': '2026-10-02T14:00:00Z'},
            {'p': 99, 't': '2026-10-02T13:54:00Z'},
            {'p': 99, 't': '2026-10-02T14:00:00'},
            {'p': 11.7, 't': '2026-10-02T14:00:00Z'}]}})
        result = self.audit(fetch)
        self.assertEqual(result['invalid'], 3)
        self.assertEqual(result['high'], '11.7')
        self.assertFalse(result['complete'])

    def test_empty_history_is_inconclusive(self):
        result = self.audit(Mock(return_value={'trades': {}}))
        trade = SimpleNamespace(symbol='CANE', entry_price=11.66, current_stop=11.58,
            remaining_quantity=8, target_1=11.82, target_2=11.9, target_3=11.98)
        self.assertIn('غير محسوم', report(trade, self.start, self.end, result))

    def test_controller_reads_fill_day_and_never_changes_trade(self):
        import railway_controller as controller
        trade = SimpleNamespace(symbol='CANE', entry_price=11.66, current_stop=11.58,
            remaining_quantity=8, target_1=11.82, target_2=11.9, target_3=11.98,
            metadata={'entry_filled_at': '2026-10-02T13:55:34Z'})
        db = Mock()
        db.load_active_managed_trades.return_value = {'trade': trade}
        evidence = {'count': 1, 'high': '11.82', 'high_at': '2026-10-02T19:59:00Z',
            'touches': ['2026-10-02T19:59:00Z', None, None], 'complete': True, 'invalid': 0}
        with patch.object(controller, 'database', db), patch.object(controller, 'alpaca_ready', return_value=True), \
             patch.object(controller, 'alpaca_get', return_value=[{'date':'2026-10-02', 'open':'09:30', 'close':'16:00'}]) as calendar, \
             patch('core.target_audit.audit_trades', return_value=evidence) as audit:
            text = controller.handle('/target_audit CANE')
        self.assertIn('ظهر وصول', text)
        self.assertEqual(audit.call_args.args[1], self.start.replace(second=34))
        self.assertEqual(audit.call_args.args[2], self.end)
        self.assertEqual(calendar.call_args.args[0], '/v2/calendar')
        self.assertEqual([call[0] for call in db.mock_calls], ['load_active_managed_trades'])
        self.assertEqual(trade.current_stop, 11.58)

    def test_invalid_symbol_does_not_read_database(self):
        import railway_controller as controller
        with patch.object(controller, 'database') as db:
            self.assertIn('/target_audit CANE', controller.handle('/target_audit ../secret'))
            db.load_active_managed_trades.assert_not_called()
