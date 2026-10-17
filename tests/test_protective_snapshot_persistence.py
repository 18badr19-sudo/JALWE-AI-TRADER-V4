from datetime import datetime,timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from core.database import Database
from core.models import BrokerOrder,TradeSide
from trading.protective_fills import apply_protective_stop_snapshot
from trading.trade_manager import TradeManager


class ProtectiveSnapshotPersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.db=Database(str(Path(tmp.name)/'native.db'))
        self.trade=TradeManager().create_trade('TEST',10.,5,9.,11.,12.,13.)
        self.trade.metadata.update(protective_stop_order_id='STOP',protective_stop_quantity=5,
            protective_stop_client_order_id='owned',protective_stop_applied_qty=0)
        self.db.save_managed_trade('trade',self.trade)

    def raw(self,filled,average):
        return NS(id='STOP',symbol='TEST',side='sell',qty='5',client_order_id='owned',
            status='partially_filled',filled_qty=str(filled),filled_avg_price=str(average),
            submitted_at=datetime(2026,10,16,14,tzinfo=timezone.utc),
            filled_at=datetime(2026,10,16,15,tzinfo=timezone.utc))

    def test_future_partial_snapshots_and_ledger_are_consistent_and_idempotent(self):
        apply_protective_stop_snapshot(self.db,'trade',self.trade,self.raw(1,9.))
        apply_protective_stop_snapshot(self.db,'trade',self.trade,self.raw(3,9.1))
        order=self.db.get_broker_order_row('STOP')
        self.assertEqual(order['filled_quantity'],3);self.assertEqual(order['filled_price'],9.1)
        self.assertEqual(order['status'],'PARTIALLY_FILLED')
        self.assertEqual(order['client_order_id'],'owned')
        self.assertEqual(self.db.load_managed_trade('trade').remaining_quantity,2)
        self.assertAlmostEqual(self.db.get_strategy_realized_pnl_total(),-2.7)
        apply_protective_stop_snapshot(self.db,'trade',self.trade,self.raw(3,9.1))
        self.assertAlmostEqual(self.db.get_strategy_realized_pnl_total(),-2.7)
        with self.db.connection() as conn:
            rows=conn.execute('SELECT quantity,fill_price FROM strategy_pnl_ledger ORDER BY event_key').fetchall()
        self.assertEqual(sum(r['quantity'] for r in rows),3)
        self.assertAlmostEqual(sum(r['quantity']*r['fill_price'] for r in rows),27.3)

    def test_snapshot_ledger_and_position_roll_back_together_on_persistence_failure(self):
        with patch.object(self.db,'save_managed_trade',side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                apply_protective_stop_snapshot(self.db,'trade',self.trade,self.raw(1,9.))
        self.assertIsNone(self.db.get_broker_order_row('STOP'))
        self.assertEqual(self.db.get_strategy_realized_pnl_total(),0.)
        self.assertEqual(self.db.load_managed_trade('trade').remaining_quantity,5)

    def test_snapshot_identity_must_match_the_realized_sell_event(self):
        for order_id,side in [('OTHER',TradeSide.SELL),('STOP',TradeSide.BUY)]:
            snapshot=BrokerOrder('TEST',side,5,order_id=order_id,filled_quantity=1,filled_price=9.)
            with self.assertRaises(ValueError):
                self.db.record_strategy_pnl_event(event_key='STOP:1',trade_id='trade',order_id='STOP',
                    symbol='TEST',action='BROKER_PROTECTIVE_STOP',quantity=1,fill_price=9.,entry_price=10.,
                    realized_pnl=-1.,event_time='2026-10-16T15:00:00Z',trade_state=self.trade,broker_order_snapshot=snapshot)
            self.assertEqual(self.db.get_strategy_realized_pnl_total(),0.)
            self.assertIsNone(self.db.get_broker_order_row(order_id))


if __name__=='__main__':unittest.main()
