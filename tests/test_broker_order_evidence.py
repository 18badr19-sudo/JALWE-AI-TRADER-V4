import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from core.database import Database
from core.models import BrokerOrder, OrderStatus, TradeSide
from trading.execution_engine import ExecutionEngine
from trading.reconciliation_engine import ReconciliationEngine
from trading.recovery_engine import RecoveryEngine
from trading.trade_manager import TradeManager, TradeAction, TradeManagementDecision
from trading.protective_fills import apply_protective_stop_snapshot
import trading.recovery_engine as recovery_module


class BrokerOrderEvidenceTests(unittest.TestCase):
    def raw(self, **changes):
        values = dict(id='broker-order', client_order_id='JALWE-ENTRY-test', symbol='TEST',
                      side='buy', qty='5', status='accepted', filled_qty='0',
                      filled_avg_price=None, filled_at=None, submitted_at=None)
        return NS(**dict(values, **changes))

    def intent(self):
        return dict(intent_id='entry', client_order_id='JALWE-ENTRY-test', symbol='TEST',
                    broker_order_id='broker-order', quantity=5, planned_entry=10)

    def local(self):
        return BrokerOrder(symbol='TEST', side=TradeSide.BUY, quantity=5,
                           order_id='broker-order', client_order_id='JALWE-ENTRY-test',
                           status=OrderStatus.ACCEPTED)

    def test_nonterminal_alpaca_states_cannot_become_canceled_or_rejected(self):
        for mapper in (ExecutionEngine._map_status, ReconciliationEngine._map_status,
                       RecoveryEngine._map_order_status):
            for status in ('done_for_day', 'stopped', 'suspended', 'replaced', 'calculated',
                           'pending_cancel', 'pending_replace', 'unknown_future_state'):
                with self.subTest(mapper=mapper.__qualname__, status=status):
                    self.assertNotIn(mapper(status),
                                     {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})

    def test_paused_exit_keeps_pending_identity_and_applies_only_confirmed_partial_fill(self):
        manager = TradeManager()
        trade = manager.create_trade('TEST', 10, 5, 9, 11, 12, 13)
        decision = TradeManagementDecision(symbol='TEST', action=TradeAction.EXIT_MANUAL,
            quantity=5, current_price=11, stage_before=trade.stage, stage_after=trade.stage,
            stop_before=trade.current_stop, stop_after=trade.current_stop, reason='owner')
        order = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=5,
                            order_id='broker-order', status=OrderStatus.ACCEPTED)
        manager.register_exit_order(trade, decision, order)
        reconciler = ReconciliationEngine.__new__(ReconciliationEngine)
        reconciler.broker = Mock()
        reconciler.broker.get_order.return_value = self.raw(side='sell', client_order_id='',
            status='done_for_day', filled_qty='2', filled_avg_price='11')
        manager.apply_exit_reconciliation(trade, reconciler.reconcile_order(order))
        self.assertTrue(trade.has_pending_exit)
        self.assertEqual(trade.pending_order_id, 'broker-order')
        self.assertEqual(trade.remaining_quantity, 3)
        self.assertEqual(trade.metadata['realized_pnl'], 2)
        manager.apply_exit_reconciliation(trade, reconciler.reconcile_order(order))
        self.assertEqual(trade.remaining_quantity, 3)
        self.assertEqual(trade.metadata['realized_pnl'], 2)
        reconciler.broker.get_order.return_value = self.raw(side='sell', client_order_id='',
            status='filled', filled_qty='5', filled_avg_price='11.3')
        manager.apply_exit_reconciliation(trade, reconciler.reconcile_order(order))
        self.assertFalse(trade.has_pending_exit)
        self.assertEqual(trade.remaining_quantity, 0)
        self.assertAlmostEqual(trade.metadata['realized_pnl'], 6.5)

    def test_mismatched_entry_is_rejected_before_any_cancel_or_database_snapshot(self):
        engine = RecoveryEngine.__new__(RecoveryEngine)
        engine.broker = Mock()
        for changes in (dict(symbol='OTHER'), dict(side='sell'), dict(qty='6'),
                        dict(client_order_id='foreign-client'), dict(id='foreign-order')):
            with self.subTest(changes=changes), patch.object(recovery_module, 'database') as db:
                db.resolve_unsubmitted_entry.return_value = False
                engine.broker.reset_mock()
                engine.broker.get_order.return_value = self.raw(**changes)
                result = engine.recover_entry_intent(self.intent())
                self.assertFalse(result['resolved'])
                self.assertEqual(result['state_after'], 'ERROR')
                engine.broker.cancel_order.assert_not_called()
                db.update_entry_intent_from_broker_order.assert_not_called()

    def test_invalid_snapshot_leaves_local_order_unchanged(self):
        engine = ReconciliationEngine.__new__(ReconciliationEngine)
        engine.broker = Mock()
        cases = [dict(id='foreign'), dict(symbol='OTHER'), dict(side='sell'), dict(qty='6'),
                 dict(client_order_id='foreign'), dict(filled_qty='1.5', filled_avg_price='10'),
                 dict(filled_qty='-1'), dict(filled_qty='6', filled_avg_price='10'),
                 dict(status='filled', filled_qty='0'),
                 dict(filled_qty='2', filled_avg_price='NaN'),
                 dict(filled_qty='NaN'), dict(filled_qty=True)]
        for changes in cases:
            order = self.local(); before = copy.deepcopy(order)
            engine.broker.get_order.return_value = self.raw(**changes)
            with self.subTest(changes=changes), self.assertRaises((ValueError, RuntimeError)):
                engine.reconcile_order(order)
            self.assertEqual(order, before)

    def test_recovery_cannot_truncate_fractional_or_overfilled_entry(self):
        engine = RecoveryEngine.__new__(RecoveryEngine)
        for changes in (dict(qty='5.5'), dict(filled_qty='1.5', filled_avg_price='10'),
                        dict(filled_qty='6', filled_avg_price='10'),
                        dict(status='filled', filled_qty='0')):
            with self.subTest(changes=changes), self.assertRaises((ValueError, RuntimeError)):
                engine._raw_entry_to_broker_order(self.raw(**changes), self.intent())

    def test_initial_response_cannot_rebind_client_identity(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        with self.assertRaises((ValueError, RuntimeError)):
            engine._build_broker_order(self.raw(client_order_id='foreign'), 'TEST',
                TradeSide.BUY, 5, 10, 'JALWE-ENTRY-test')

    def test_incomplete_filled_stop_cannot_remove_protection_or_mutate_quantity(self):
        manager = TradeManager()
        for filled in ('0', '2'):
            trade = manager.create_trade('TEST', 10, 5, 9, 11, 12, 13)
            trade.metadata.update(protective_stop_order_id='broker-order',
                                  protective_stop_quantity=5, protective_stop_active=True)
            before = copy.deepcopy(trade)
            db = Mock()
            with self.subTest(filled=filled), self.assertRaises(ValueError):
                apply_protective_stop_snapshot(db, 'trade', trade,
                    self.raw(side='sell', status='filled', filled_qty=filled, filled_avg_price='9'))
            self.assertEqual(trade, before)
            db.record_strategy_pnl_event.assert_not_called()
            db.save_managed_trade.assert_not_called()

    def test_untracked_ambiguous_jalwe_orders_block_recovery(self):
        engine = RecoveryEngine.__new__(RecoveryEngine)
        for status in ('replaced', 'calculated', 'unknown_future_state'):
            with self.subTest(status=status):
                unresolved = engine._check_unresolved_jalwe_orders({}, [], [self.raw(status=status)])
                self.assertEqual(len(unresolved), 1)

    def test_paused_entry_stays_unresolved_when_cancel_has_no_terminal_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'orders.db'))
            db.create_entry_intent(intent_id='entry', client_order_id='JALWE-ENTRY-test',
                symbol='TEST', quantity=5, planned_entry=10, stop_price=9.8,
                target_1=10.4, target_2=10.6, target_3=10.8, risk_pct=1,
                strategy='TEST', setup_grade='A', decision_state='READY_FOR_PAPER_EXECUTION',
                decision_gates={}, metadata={'entry_submission_state':'PREPARED'})
            db.mark_entry_submission_started('entry')
            engine = RecoveryEngine.__new__(RecoveryEngine); engine.broker = Mock()
            engine.broker.get_order_by_client_id.return_value = self.raw(status='stopped')
            with patch.object(recovery_module, 'database', db):
                result = engine.recover_entry_intent(db.get_entry_intent_row('entry'))
            self.assertFalse(result['resolved'])
            self.assertEqual(db.get_entry_intent_row('entry')['state'], 'SUBMITTED')
            self.assertTrue(db.get_unresolved_entry_intent_rows())
            engine.broker.get_order.side_effect = [self.raw(status='stopped'), self.raw(status='canceled')]
            with patch.object(recovery_module, 'database', db):
                result = engine.recover_entry_intent(db.get_entry_intent_row('entry'))
            self.assertTrue(result['resolved'])
            self.assertEqual(db.get_entry_intent_row('entry')['state'], 'CANCELED')
            self.assertFalse(db.get_unresolved_entry_intent_rows())
