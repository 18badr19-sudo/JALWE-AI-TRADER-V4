import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from trading.protective_fills import apply_protective_stop_snapshot
from trading.recovery_engine import RecoveryEngine
from trading.trade_manager import TradeManager
import trading.recovery_engine as recovery_module


class ProtectiveStopIdentityTests(unittest.TestCase):
    def trade(self, uncertain=False):
        trade = TradeManager().create_trade('TEST', 10, 5, 9, 11, 12, 13)
        trade.metadata.update(protective_stop_client_order_id='JALWE-STOP-owned',
                              protective_stop_quantity=5, protective_stop_active=True)
        if uncertain:
            trade.metadata['protective_stop_submission_uncertain'] = True
        else:
            trade.metadata['protective_stop_order_id'] = 'owned-stop'
        return trade

    def raw(self, **changes):
        fields = dict(id='owned-stop', client_order_id='JALWE-STOP-owned',
                      symbol='TEST', side='sell', qty='5', status='canceled',
                      filled_qty='2', filled_avg_price='9', filled_at=None)
        return NS(**dict(fields, **changes))

    def test_contradictory_stop_evidence_cannot_change_trade_or_clear_protection(self):
        for changes in (dict(id=''), dict(qty='6'), dict(qty='5.5'),
                        dict(client_order_id='foreign'), dict(filled_avg_price=True)):
            with self.subTest(changes=changes):
                trade = self.trade(); before = copy.deepcopy(trade); db = Mock()
                with self.assertRaises(ValueError):
                    apply_protective_stop_snapshot(db, 'trade', trade, self.raw(**changes))
                self.assertEqual(trade, before)
                db.save_managed_trade.assert_not_called()
                db.record_strategy_pnl_event.assert_not_called()

    def test_invalid_recovered_stop_is_not_persisted_as_resolved(self):
        for changes in (dict(qty='5.5'), dict(status='filled', filled_qty='2'),
                        dict(filled_avg_price='NaN')):
            with self.subTest(changes=changes):
                trade = self.trade(uncertain=True); before = copy.deepcopy(trade)
                engine = RecoveryEngine.__new__(RecoveryEngine)
                engine.broker = Mock()
                engine.broker.get_order_by_client_id.return_value = self.raw(**changes)
                with patch.object(recovery_module, 'database') as db:
                    with self.assertRaises((ValueError, RuntimeError)):
                        engine._reconcile_protective_stop('trade', trade)
                    self.assertEqual(trade, before)
                    db.save_managed_trade.assert_not_called()
                    db.record_strategy_pnl_event.assert_not_called()

    def test_recovery_preserves_uncertainty_when_cumulative_accounting_is_invalid(self):
        for previous, notional in ((3, 27.0), (1, 30.0)):
            with self.subTest(previous=previous):
                trade = self.trade(uncertain=True)
                trade.metadata.update(protective_stop_applied_qty=previous,
                                      protective_stop_applied_notional=notional)
                before = copy.deepcopy(trade)
                engine = RecoveryEngine.__new__(RecoveryEngine); engine.broker = Mock()
                engine.broker.get_order_by_client_id.return_value = self.raw()
                with patch.object(recovery_module, 'database') as db:
                    with self.assertRaises(ValueError):
                        engine._reconcile_protective_stop('trade', trade)
                    self.assertEqual(trade, before)
                    db.save_managed_trade.assert_not_called()
                    db.record_strategy_pnl_event.assert_not_called()

    def test_valid_recovered_partial_fill_commits_identity_and_proceeds_once(self):
        trade = self.trade(uncertain=True)
        engine = RecoveryEngine.__new__(RecoveryEngine); engine.broker = Mock()
        raw = self.raw(status='partially_filled')
        engine.broker.get_order_by_client_id.return_value = raw
        engine.broker.get_order.return_value = raw
        with patch.object(recovery_module, 'database') as db:
            engine._reconcile_protective_stop('trade', trade)
            self.assertEqual(trade.remaining_quantity, 3)
            self.assertEqual(trade.metadata['protective_stop_order_id'], 'owned-stop')
            self.assertFalse(trade.metadata['protective_stop_submission_uncertain'])
            self.assertEqual(trade.metadata['realized_pnl'], -2)
            db.record_strategy_pnl_event.assert_called_once()
            engine._reconcile_protective_stop('trade', trade)
            self.assertEqual(trade.remaining_quantity, 3)
            db.record_strategy_pnl_event.assert_called_once()


if __name__ == '__main__':
    unittest.main()
