import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from broker.alpaca_client import AlpacaClient
import broker.alpaca_client as broker_module
from core.database import Database
from core.models import BrokerOrder, OrderStatus, TradeSide
from trading.execution_engine import ExecutionEngine
from trading.paper_trade_orchestrator import PaperTradeOrchestrator
from trading.trade_manager import TradeManager
from trading.protective_fills import apply_protective_stop_snapshot
from trading.recovery_engine import RecoveryEngine
from trading.paper_trade_orchestrator import PaperOrchestratorState
import test_paper_safety as safety_helpers
import trading.recovery_engine as recovery_module
import jalwe_research_watcher as watcher
import trading.paper_trade_orchestrator as orchestrator_module


class StopCancellationRecoveryTests(unittest.TestCase):
    def trade(self):
        trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
        trade.metadata.update(protective_stop_order_id='old-stop',
                              protective_stop_client_order_id='JALWE-STOP-old',
                              protective_stop_quantity=8, protective_stop_price=9,
                              protective_stop_active=True, protective_stop_applied_qty=0,
                              protective_stop_applied_notional=0.0)
        return trade

    def snapshot(self, status='canceled', quantity=2):
        return NS(id='old-stop', symbol='TEST', side='sell', status=status,
                  filled_qty=str(quantity), filled_avg_price='9', filled_at=None)

    def test_partial_stop_fill_is_committed_before_cancel_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = self.trade(); db.save_managed_trade('t', trade)
            execution = Mock()
            execution.broker.get_order.side_effect = [self.snapshot('new', 0), self.snapshot('canceled', 2)]
            with patch.object(watcher, 'database', db), patch.object(watcher.time, 'sleep'):
                result = watcher._cancel_protective_stop('t', trade, execution)
            self.assertEqual(result, 'PARTIAL_FILL')
            self.assertEqual(db.load_managed_trade('t').remaining_quantity, 6)
            self.assertEqual(db.get_strategy_realized_pnl_total(), -2)

    def test_replacement_stop_uses_quantity_after_cancellation_fill(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = self.trade(); trade.current_stop = 9.5
            db.save_managed_trade('t', trade)
            execution = Mock()
            execution.broker.get_order.side_effect = [self.snapshot('new', 0), self.snapshot('new', 0), self.snapshot('canceled', 2)]
            def submit(**kwargs):
                self.assertEqual(db.load_managed_trade('t').remaining_quantity, 6)
                self.assertEqual(kwargs['quantity'], 6)
                if kwargs.get('before_submit'):
                    kwargs['before_submit']()
                return BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=6,
                    order_id='new-stop', client_order_id=kwargs['client_order_id'], status=OrderStatus.ACCEPTED)
            execution.submit_protective_stop.side_effect = submit
            with patch.object(watcher, 'database', db), patch.object(watcher.time, 'sleep'):
                result = watcher._sync_protective_stop('t', trade, execution)
            self.assertEqual(result['quantity'], 6)
            self.assertEqual(db.get_strategy_realized_pnl_total(), -2)

    def test_initial_stop_preflight_failure_is_not_an_uncertain_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            db.save_managed_trade('t', trade)
            execution = ExecutionEngine.__new__(ExecutionEngine)
            execution.broker = Mock(); execution.broker.get_position.return_value = NS(qty='6')
            orchestrator = PaperTradeOrchestrator.__new__(PaperTradeOrchestrator)
            orchestrator.execution_engine = execution
            with patch.object(orchestrator_module, 'database', db):
                with self.assertRaises(RuntimeError):
                    orchestrator._arm_managed_trade_stop('t')
            self.assertFalse(db.load_managed_trade('t').metadata.get('protective_stop_submission_uncertain', False))
            execution.broker.submit_stop_order.assert_not_called()

    def test_synced_stop_preflight_failure_is_not_an_uncertain_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            db.save_managed_trade('t', trade)
            execution = ExecutionEngine.__new__(ExecutionEngine)
            execution.broker = Mock(); execution.broker.get_position.return_value = NS(qty='6')
            with patch.object(watcher, 'database', db):
                with self.assertRaises(RuntimeError):
                    watcher._sync_protective_stop('t', trade, execution)
            self.assertFalse(db.load_managed_trade('t').metadata.get('protective_stop_submission_uncertain', False))
            execution.broker.submit_stop_order.assert_not_called()

    def test_done_for_day_and_stopped_are_not_confirmed_cancellations(self):
        trade = self.trade()
        for status in ('done_for_day', 'stopped', 'suspended', 'replaced'):
            result = apply_protective_stop_snapshot(Mock(), 't', trade, self.snapshot(status, 0))
            self.assertTrue(result['active'])
            self.assertFalse(result['filled'])

    def test_stop_request_validation_failure_is_not_uncertain(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            db.save_managed_trade('t', trade)
            execution = ExecutionEngine.__new__(ExecutionEngine)
            execution.broker = AlpacaClient.__new__(AlpacaClient)
            execution.broker.client = Mock()
            execution.broker.get_position = Mock(return_value=NS(qty='8'))
            orchestrator = PaperTradeOrchestrator.__new__(PaperTradeOrchestrator)
            orchestrator.execution_engine = execution
            with patch.object(orchestrator_module, 'database', db), patch.object(broker_module, 'StopOrderRequest', side_effect=ValueError('invalid request')):
                with self.assertRaises(ValueError):
                    orchestrator._arm_managed_trade_stop('t')
            self.assertFalse(db.load_managed_trade('t').metadata['protective_stop_submission_uncertain'])
            execution.broker.client.submit_order.assert_not_called()

    def test_done_for_day_stop_is_canceled_before_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = self.trade(); db.save_managed_trade('t', trade)
            execution = Mock()
            execution.broker.get_order.side_effect = [self.snapshot('done_for_day', 0), self.snapshot('canceled', 0)]
            with patch.object(watcher, 'database', db), patch.object(watcher.time, 'sleep'):
                result = watcher._cancel_protective_stop('t', trade, execution)
            self.assertEqual(result, 'CANCELED')
            execution.cancel_protective_stop.assert_called_once_with('old-stop')

    def test_fill_ledger_failure_does_not_persist_new_quantity(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = self.trade(); db.save_managed_trade('t', trade)
            original_save = db.save_managed_trade
            def fail_atomic(*args, **kwargs):
                if kwargs.get('_connection') is not None:
                    raise RuntimeError('disk write failed')
                return original_save(*args, **kwargs)
            with patch.object(db, 'save_managed_trade', side_effect=fail_atomic):
                with self.assertRaises(RuntimeError):
                    apply_protective_stop_snapshot(db, 't', trade, self.snapshot('canceled', 2))
            self.assertEqual(db.load_managed_trade('t').remaining_quantity, 8)
            self.assertEqual(db.get_strategy_realized_pnl_total(), 0)
            restored = db.load_managed_trade('t')
            apply_protective_stop_snapshot(db, 't', restored, self.snapshot('canceled', 2))
            apply_protective_stop_snapshot(db, 't', db.load_managed_trade('t'), self.snapshot('canceled', 2))
            self.assertEqual(db.load_managed_trade('t').remaining_quantity, 6)
            self.assertEqual(db.get_strategy_realized_pnl_total(), -2)

    def test_management_discards_exit_decision_after_stop_fill(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = self.trade(); db.save_managed_trade('t', trade)
            execution = Mock(); execution.broker.market_is_open.return_value = True
            execution.broker.get_order.side_effect = [self.snapshot('new', 0), self.snapshot('canceled', 2)]
            with patch.object(watcher, 'auto_paper_execution_ready', return_value=True), patch.object(watcher, 'get_recovery_engine') as recovery, patch.object(watcher, 'database', db), patch.object(watcher, 'get_market_data') as market_data, patch.object(watcher, 'get_trade_manager', return_value=TradeManager()), patch.object(watcher, 'get_execution_engine', return_value=execution), patch.object(watcher, 'get_reconciliation_engine'), patch.object(watcher, 'notify_recovered_protective_stop_fills'), patch.object(watcher, '_sync_protective_stop') as sync, patch.object(watcher.time, 'sleep'):
                recovery.return_value.recover_all.return_value = {'safe_to_trade': True}
                market_data.return_value.get_trade_range.return_value = {'last': 11, 'high': 11, 'count': 1}
                watcher.manage_active_paper_trades({})
            execution.submit_exit.assert_not_called()
            self.assertEqual(db.load_managed_trade('t').remaining_quantity, 6)
            self.assertNotIn('exit_submission', db.load_managed_trade('t').metadata)
            self.assertEqual(sync.call_args.args[1].remaining_quantity, 6)

    def test_entry_timeout_cancels_remainder_and_manages_only_actual_fill(self):
        for filled_quantity in (3, 0):
            with self.subTest(filled_quantity=filled_quantity), tempfile.TemporaryDirectory() as temp:
                db = Database(str(Path(temp)/'entry.db'))
                decision = safety_helpers.ready_decision(); decision.quantity = 10
                orchestrator = safety_helpers.PaperMarketOpenGateTests()._orchestrator(decision)
                client_id = 'JALWE-FINAL-timeout-test'
                def create(_decision):
                    db.create_entry_intent('i', client_id, 'TEST', 10, 10, 9.5, 11, 11.5, 12, 1)
                    return 'i', client_id
                orchestrator._create_entry_intent = create
                local = BrokerOrder(symbol='TEST', side=TradeSide.BUY, quantity=10,
                    order_id='entry', client_order_id=client_id,
                    status=OrderStatus.PARTIALLY_FILLED if filled_quantity else OrderStatus.ACCEPTED,
                    filled_quantity=filled_quantity, filled_price=10 if filled_quantity else None)
                orchestrator.execution_engine = Mock()
                orchestrator.execution_engine.submit_final_decision.return_value = local
                orchestrator.reconciliation_engine = Mock()
                orchestrator.reconciliation_engine.wait_for_terminal_state.return_value = local
                recovery = RecoveryEngine.__new__(RecoveryEngine)
                recovery.trade_manager = TradeManager()
                recovery.broker = Mock()
                active = NS(id='entry', client_order_id=client_id, symbol='TEST', side='buy',
                    qty='10', filled_qty=str(filled_quantity), filled_avg_price='10' if filled_quantity else None,
                    status='partially_filled' if filled_quantity else 'new', submitted_at=None)
                terminal = NS(**vars(active)); terminal.status = 'canceled'
                recovery.broker.get_order.side_effect = [active, terminal]
                recovery.reconciliation = Mock()
                recovery.reconciliation.verify_position.return_value = {'exists': True, 'quantity': filled_quantity, 'average_entry_price': 10}
                orchestrator.recovery_engine = recovery
                orchestrator._arm_managed_trade_stop = Mock(return_value={'enabled': True, 'active': True})
                with patch.object(orchestrator_module, 'database', db), patch.object(recovery_module, 'database', db), patch.object(orchestrator_module, 'get_alpaca_client') as broker:
                    broker.return_value.market_is_open.return_value = True
                    result = orchestrator.run_symbol('TEST')
                recovery.broker.cancel_order.assert_called_once_with('entry')
                if filled_quantity:
                    self.assertEqual(result.state, PaperOrchestratorState.ENTRY_MANAGED)
                    self.assertEqual(next(iter(db.load_active_managed_trades().values())).remaining_quantity, 3)
                else:
                    self.assertEqual(result.state, PaperOrchestratorState.ENTRY_TERMINAL_NO_FILL)
                    self.assertEqual(len(db.load_active_managed_trades()), 0)


if __name__ == '__main__':
    unittest.main()
