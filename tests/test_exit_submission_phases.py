import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from broker.alpaca_client import AlpacaClient
from core.database import Database
from trading.execution_engine import ExecutionEngine
from trading.exit_submission import prepare_exit, recover_exit_submission, submit_prepared_exit
from trading.trade_manager import TradeManager


class ExitSubmissionPhaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(str(Path(temp.name) / 'exits.db'))
        self.manager = TradeManager()
        self.trade = self.manager.create_trade('TEST', 10, 8, 9, 11, 12, 13)
        self.decision = self.manager.evaluate(self.trade, 11)
        self.broker = AlpacaClient.__new__(AlpacaClient)
        self.broker.client = Mock()
        self.broker.client.get_clock.return_value = NS(is_open=True)
        self.broker.client.get_all_positions.return_value = [NS(symbol='TEST', qty='8')]
        self.execution = ExecutionEngine.__new__(ExecutionEngine)
        self.execution.broker = self.broker
        prepare_exit(self.db, 't', self.trade, self.decision)

    def saved(self):
        return Database(str(self.db.database_path)).load_active_managed_trades()['t']

    def submit(self):
        return submit_prepared_exit(self.db, 't', self.trade, self.decision,
                                    self.execution, self.manager)

    def test_request_validation_failure_stays_prepared_and_recovers(self):
        with patch('broker.alpaca_client.MarketOrderRequest', side_effect=ValueError('invalid request')):
            with self.assertRaises(ValueError):
                self.submit()
        restarted = self.saved()
        self.assertEqual(restarted.metadata['exit_submission']['state'], 'PREPARED')
        recover_exit_submission(self.db, 't', restarted, self.broker, self.manager)
        self.assertNotIn('exit_submission', restarted.metadata)
        self.broker.client.get_order_by_client_id.assert_not_called()
        self.broker.client.submit_order.assert_not_called()
        self.assertEqual(restarted.remaining_quantity, 8)
        self.assertEqual(restarted.current_stop, 9)

    def test_position_read_failure_never_starts_submission(self):
        self.broker.client.get_all_positions.side_effect = TimeoutError('position read')
        with self.assertRaises(TimeoutError):
            self.submit()
        self.assertEqual(self.saved().metadata['exit_submission']['state'], 'PREPARED')
        self.broker.client.submit_order.assert_not_called()

    def test_timeout_after_post_keeps_uncertain_client_identity(self):
        identity = self.trade.metadata['exit_submission']['client_order_id']

        def lost_response(order_data):
            self.assertEqual(self.saved().metadata['exit_submission']['state'], 'SUBMITTING')
            self.assertEqual(order_data.client_order_id, identity)
            self.assertEqual(order_data.side.value, 'sell')
            raise TimeoutError('lost broker response')

        self.broker.client.submit_order.side_effect = lost_response
        with self.assertRaises(TimeoutError):
            self.submit()
        self.assertEqual(self.saved().metadata['exit_submission']['client_order_id'], identity)
        with self.assertRaises(RuntimeError):
            self.submit()
        self.broker.client.submit_order.assert_called_once()

    def test_phase_persistence_failure_prevents_post(self):
        with patch.object(self.db, 'save_managed_trade', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.broker.client.submit_order.assert_not_called()

    def test_legacy_market_order_without_callback_still_submits(self):
        self.broker.submit_market_order('TEST', 1, 'SELL', 'JALWE-EXIT-direct')
        self.broker.client.submit_order.assert_called_once()
