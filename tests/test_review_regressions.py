import json
import sqlite3
import tempfile
import unittest
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.database import Database
from core.models import BrokerOrder, OrderStatus, TradeSide
from intelligence.learning_engine import LearningEngine
from trading.risk_engine import RiskEngine
from trading.trade_manager import TradeManager, TradeStage
from trading.recovery_engine import RecoveryEngine


class ReviewRegressionTests(unittest.TestCase):
    def test_fill_ledger_and_trade_state_commit_together(self):
        trade = TradeManager().create_trade('TEST', 10, 10, 9, 11, 12, 13)
        with tempfile.TemporaryDirectory() as d:
            db = Database(d + '/atomic.db')
            event = dict(event_key='order:2', trade_id='t', order_id='order',
                         symbol='TEST', action='EXIT', quantity=2, fill_price=9,
                         entry_price=10, realized_pnl=-2,
                         event_time='2026-09-25T14:00:00+00:00', trade_state=trade)
            with patch.object(db, 'save_managed_trade', side_effect=RuntimeError('disk failure')):
                with self.assertRaises(RuntimeError):
                    db.record_strategy_pnl_event(**event)
            self.assertEqual(db.get_strategy_realized_pnl_total(), 0)
            trade.remaining_quantity = 8
            db.record_strategy_pnl_event(**event)
            self.assertEqual(db.get_strategy_realized_pnl_total(), -2)
            self.assertEqual(db.load_managed_trade('t').remaining_quantity, 8)
            db.record_strategy_pnl_event(**event)
            self.assertEqual(db.get_strategy_realized_pnl_total(), -2)

    def test_five_crash_recovery_scenarios_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'JALWE_DATABASE_PATH': d + '/crash.db',
                   'ALPACA_API_KEY': 'test-key', 'ALPACA_SECRET_KEY': 'test-secret'}
            result = subprocess.run([sys.executable, '-c',
                "import runpy, socket; from unittest.mock import patch; "
                "guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')); "
                "guard.start(); runpy.run_path('tests/test_crash_recovery.py', run_name='__main__')"],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wallet_deducts_open_capital(self):
        cash = RiskEngine._strategy_available_cash(100, 100000, [
            {'entry_price': 10, 'remaining_quantity': 7},
        ])
        self.assertEqual(cash, 30)
        self.assertEqual(RiskEngine._strategy_available_cash(100, 20, []), 20)
        self.assertEqual(RiskEngine._strategy_available_cash(100, 100000, [
            {'entry_price': 10, 'remaining_quantity': 12},
        ]), 0)
        with self.assertRaises(ValueError):
            RiskEngine._strategy_available_cash(float('nan'), 100, [])

    def test_learning_excludes_future_and_old_snapshots(self):
        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.executescript('''
            CREATE TABLE managed_trades(trade_id, symbol, metadata_json,
                initial_quantity, entry_price, closed_at, created_at, updated_at, stage);
            CREATE TABLE feature_snapshots(id INTEGER PRIMARY KEY, symbol, created_at, snapshot_json);
        ''')
        c.execute('INSERT INTO managed_trades VALUES (?,?,?,?,?,?,?,?,?)', (
            't', 'TEST', '{"realized_pnl": 2}', 2, 10,
            '2026-09-25 15:00:00', '2026-09-25 14:00:00', '2026-09-25 15:00:00', 'CLOSED'))
        for i, stamp, label in [(1,'13:55:00','before'),(2,'14:00:01','future'),(3,'12:00:00','old')]:
            c.execute('INSERT INTO feature_snapshots VALUES (?,?,?,?)',
                (i,'TEST','2026-09-25 '+stamp,json.dumps({'label':label})))
        @contextmanager
        def connection():
            yield c
        engine = LearningEngine.__new__(LearningEngine)
        engine.max_history = 100
        with patch('intelligence.learning_engine.database', SimpleNamespace(connection=connection)):
            self.assertEqual(engine._load_closed_trade_samples()[0]['snapshot']['label'], 'before')
            c.execute('DELETE FROM feature_snapshots WHERE id=1')
            self.assertEqual(engine._load_closed_trade_samples(), [])
        c.close()

    def test_exit_cumulative_average_survives_restart(self):
        manager = TradeManager()
        trade = manager.create_trade('TEST', 10, 10, 9, 11, 12, 13)
        decision = manager.evaluate(trade, 8)
        order = BrokerOrder('TEST', TradeSide.SELL, decision.quantity,
            order_id='exit-1',status=OrderStatus.ACCEPTED)
        manager.register_exit_order(trade, decision, order)
        order.status = OrderStatus.PARTIALLY_FILLED
        order.filled_quantity = 2
        order.filled_price = 9
        manager.apply_exit_reconciliation(trade, order)
        self.assertEqual(trade.metadata['realized_pnl'], -2)
        with tempfile.TemporaryDirectory() as d:
            db = Database(d+'/test.db')
            db.save_managed_trade('t', trade)
            trade = db.load_managed_trade('t')
            repeated = manager.apply_exit_reconciliation(trade, order)
            self.assertEqual(repeated['new_fill_quantity'], 0)
            order.filled_quantity = 10
            order.filled_price = 8.2  # 2*9 + 8*8 = 82 total proceeds
            order.status = OrderStatus.FILLED
            manager.apply_exit_reconciliation(trade, order)
            self.assertAlmostEqual(trade.metadata['realized_pnl'], -18)
            self.assertEqual(trade.remaining_quantity, 0)
            self.assertEqual(trade.stage, TradeStage.CLOSED)

    def test_partial_stop_then_cancel_is_applied_once(self):
        manager = TradeManager()
        trade = manager.create_trade('TEST', 10, 10, 9, 11, 12, 13)
        trade.metadata['protective_stop_order_id'] = 'stop-1'
        raw = SimpleNamespace(status='partially_filled', filled_qty='2', filled_avg_price='9', filled_at=None)
        recovery = RecoveryEngine.__new__(RecoveryEngine)
        recovery.broker = SimpleNamespace(get_order=Mock(return_value=raw))
        with patch('trading.recovery_engine.database') as db:
            recovery._reconcile_protective_stop('t', trade)
            self.assertEqual(trade.remaining_quantity, 8)
            recovery._reconcile_protective_stop('t', trade)
            self.assertEqual(db.record_strategy_pnl_event.call_count, 1)
            raw.status = 'canceled'
            raw.filled_qty = '4'
            raw.filled_avg_price = '8.5'
            recovery._reconcile_protective_stop('t', trade)
            self.assertEqual(trade.remaining_quantity, 6)
            self.assertEqual(trade.metadata['realized_pnl'], -6)
            self.assertFalse(trade.metadata['protective_stop_active'])
            recovery._reconcile_protective_stop('t', trade)
            self.assertEqual(db.record_strategy_pnl_event.call_count, 2)

if __name__ == '__main__':
    unittest.main()
