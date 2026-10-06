import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from alpaca.common.exceptions import APIError
from core.database import Database
from trading.trade_manager import TradeManager
from trading.recovery_engine import RecoveryEngine
import trading.recovery_engine as recovery_module
import trading.paper_trade_orchestrator as orchestrator_module
from trading.paper_trade_orchestrator import PaperTradeOrchestrator
import jalwe_research_watcher as watcher


REJECTION = {'code': 42210000, 'message': 'stop price must be less than current price',
             'stop_price': '9', 'market_price': '8.99'}


def api_error(status, payload=REJECTION):
    return APIError(json.dumps(payload), NS(response=NS(status_code=status)))


class PartialStopRejectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'stop.db'))
        self.trade = TradeManager().create_trade('TEST', 10, 3, 9, 11, 12, 13)

    def test_live_partial_stop_keeps_unfilled_share_and_applies_fill_once(self):
        self.trade.metadata.update(protective_stop_order_id='original',
                                   protective_stop_quantity=3, protective_stop_price=9)
        self.db.save_managed_trade('t', self.trade)
        execution = Mock()
        execution.broker.get_order.return_value = NS(id='original', symbol='TEST',
            side='sell', status='partially_filled', filled_qty='2', filled_avg_price='9')
        with patch.object(watcher, 'database', self.db):
            for _ in range(2):
                result = watcher._sync_protective_stop('t', self.trade, execution)
                self.assertTrue(result['active'])
                self.assertEqual(result['quantity'], 1)
        execution.cancel_protective_stop.assert_not_called()
        execution.submit_protective_stop.assert_not_called()
        self.assertEqual(self.db.load_managed_trade('t').remaining_quantity, 1)
        self.assertEqual(self.db.get_strategy_realized_pnl_total(), -2)

    def test_explicit_rejection_clears_uncertainty_in_both_stop_paths(self):
        for arm in (False, True):
            with self.subTest(arm=arm):
                self.trade.metadata = {}
                self.db.save_managed_trade('t', self.trade)
                execution = Mock()
                execution._new_client_order_id.return_value = 'JALWE-STOP-test'
                def reject(**kwargs):
                    kwargs['before_submit']()
                    raise api_error(422)
                execution.submit_protective_stop.side_effect = reject
                with patch.object(watcher, 'database', self.db), patch.object(orchestrator_module, 'database', self.db):
                    with self.assertRaises(APIError):
                        if arm:
                            obj = PaperTradeOrchestrator.__new__(PaperTradeOrchestrator)
                            obj.execution_engine = execution
                            obj._arm_managed_trade_stop('t')
                        else:
                            watcher._sync_protective_stop('t', self.trade, execution)
                saved = self.db.load_managed_trade('t')
                self.assertFalse(saved.metadata['protective_stop_submission_uncertain'])
                self.assertEqual(saved.metadata['protective_stop_submission_state'], 'REJECTED')

    def test_timeout_and_unrecognized_rejection_remain_uncertain(self):
        for error in (TimeoutError('timeout'), api_error(500), api_error(422, {'code':42210000, 'message':'other'})):
            with self.subTest(error=error):
                self.trade.metadata = {}
                self.db.save_managed_trade('t', self.trade)
                execution = Mock()
                def fail(**kwargs):
                    kwargs['before_submit']()
                    raise error
                execution.submit_protective_stop.side_effect = fail
                with patch.object(watcher, 'database', self.db), self.assertRaises(Exception):
                    watcher._sync_protective_stop('t', self.trade, execution)
                self.assertTrue(self.db.load_managed_trade('t').metadata['protective_stop_submission_uncertain'])

    def legacy(self):
        self.trade.metadata.update(protective_stop_submission_uncertain=True,
            protective_stop_submission_state='SUBMITTING', protective_stop_price=9,
            protective_stop_client_order_id='JALWE-STOP-missing')
        self.db.save_managed_trade('t', self.trade)
        self.db.log_event(event_type='PAPER_TRADE_MANAGEMENT_ERROR', message=json.dumps(REJECTION),
                          metadata={'trade_id':'t', 'symbol':'TEST'})
        engine = RecoveryEngine.__new__(RecoveryEngine)
        engine.broker = Mock()
        engine.broker.get_order_by_client_id.side_effect = api_error(404, {'code':40410000, 'message':'not found'})
        engine._get_broker_orders = Mock(return_value=[])
        engine.reconciliation = Mock()
        engine.reconciliation.verify_position.return_value = {'exists':True, 'quantity':3}
        return engine

    def test_legacy_rejected_stop_recovers_with_recorded_422_and_matched_broker(self):
        engine = self.legacy()
        with patch.object(recovery_module, 'database', self.db):
            result = engine._reconcile_protective_stop('t', self.trade)
        self.assertTrue(result['rejected'])
        self.assertFalse(self.db.load_managed_trade('t').metadata['protective_stop_submission_uncertain'])

    def test_legacy_recovery_blocks_without_evidence_or_with_possible_sell(self):
        for scenario in ('no_evidence', 'open_sell', 'mismatch', 'new_intent'):
            with self.subTest(scenario=scenario):
                engine = self.legacy()
                if scenario == 'no_evidence':
                    with self.db.connection() as conn:
                        conn.execute('DELETE FROM system_events')
                elif scenario == 'open_sell':
                    engine._get_broker_orders.return_value = [NS(symbol='TEST', side='sell', status='new')]
                elif scenario == 'mismatch':
                    engine.reconciliation.verify_position.return_value = {'exists':True, 'quantity':2}
                else:
                    self.trade.metadata['protective_stop_prepared_at'] = '2026-10-06T14:00:00+00:00'
                with patch.object(recovery_module, 'database', self.db), self.assertRaises(APIError):
                    engine._reconcile_protective_stop('t', self.trade)
                self.assertTrue(self.trade.metadata['protective_stop_submission_uncertain'])
                self.trade.metadata = {}

    def test_legacy_rejection_matches_broker_price_precision_only(self):
        for stored, reported, expected in ((9.0018, '9', True),
                                           (0.65184, '0.6518', True),
                                           (9.0118, '9', False)):
            with self.subTest(stored=stored):
                engine = self.legacy()
                self.trade.metadata['protective_stop_price'] = stored
                payload = {**REJECTION, 'stop_price': reported}
                self.db.log_event(event_type='PAPER_TRADE_MANAGEMENT_ERROR',
                                  message=json.dumps(payload), metadata={'trade_id':'t'})
                with patch.object(recovery_module, 'database', self.db):
                    if expected:
                        self.assertTrue(engine._reconcile_protective_stop('t', self.trade)['rejected'])
                    else:
                        with self.assertRaises(APIError):
                            engine._reconcile_protective_stop('t', self.trade)
                self.trade.metadata = {}
