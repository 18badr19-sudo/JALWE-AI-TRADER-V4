import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from core.database import Database
from core.models import BrokerOrder, OrderStatus, TradeSide
from intelligence.feature_engine import FeatureEngine
from market.market_data import MarketData, MarketDataError
from trading.trade_manager import TradeManager
from trading.paper_trade_orchestrator import PaperTradeOrchestrator
import trading.paper_trade_orchestrator as orchestrator_module
import jalwe_research_watcher as watcher


class DeepAuditRegressionTests(unittest.TestCase):
    def test_rsi_all_gains_is_100_without_erasing_warmup(self):
        values = pd.Series(range(1, 32), dtype=float)
        rsi = FeatureEngine._calculate_rsi(values)
        self.assertEqual(rsi.iloc[-1], 100)
        self.assertTrue(rsi.iloc[:14].isna().all())
        falling = FeatureEngine._calculate_rsi(values.iloc[::-1].reset_index(drop=True))
        self.assertEqual(falling.iloc[-1], 0)
        # With no gains or losses the ratio remains undefined.
        self.assertTrue(FeatureEngine._calculate_rsi(pd.Series([10.] * 31)).isna().all())

    def test_bad_management_prices_cannot_mutate_targets_or_high_water(self):
        manager = TradeManager()
        for field in ('current_price', 'observed_high', 'atr', 'structure_stop'):
            for value in (float('inf'), float('-inf'), float('nan')):
                with self.subTest(field=field, value=value):
                    trade = manager.create_trade('TEST', 10, 3, 9, 11, 12, 13)
                    before = copy.deepcopy(trade)
                    args = dict(current_price=10.5)
                    args[field] = value
                    with self.assertRaises(ValueError):
                        manager.evaluate(trade, **args)
                    self.assertEqual(trade, before)

    def test_infinite_target_and_fractional_stock_quantity_cannot_create_trade(self):
        manager = TradeManager()
        for changes in (dict(target_3=float('inf')), dict(quantity=3.9)):
            args = dict(symbol='TEST', entry_price=10, quantity=3, stop_price=9,
                        target_1=11, target_2=12, target_3=13)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                manager.create_trade(**{**args, **changes})

    def test_invalid_latest_trade_uses_valid_bar_or_raises(self):
        md = MarketData.__new__(MarketData)
        md.feed = DataFeed.IEX
        md.client = Mock()
        md.client.get_stock_latest_trade.return_value = {'TEST': NS(price=float('inf'))}
        md.get_bars = Mock(return_value=pd.DataFrame({'close': [10.]}))
        self.assertEqual(md.get_last_price('TEST'), 10)
        md.get_bars.return_value = pd.DataFrame({'close': [float('nan')]})
        with self.assertRaises(MarketDataError):
            md.get_last_price('TEST')

    def test_range_excludes_nonfinite_prices_from_target_evidence(self):
        md = MarketData.__new__(MarketData)
        md.feed = DataFeed.IEX
        md.client = Mock()
        md.client.get_stock_trades.return_value = {'TEST': [NS(price=10), NS(price=float('inf'))]}
        now = datetime.now(timezone.utc)
        result = md.get_trade_range('TEST', now - timedelta(seconds=2), now)
        self.assertEqual(result['high'], 10)
        self.assertEqual(result['last'], 10)
        self.assertEqual(result['count'], 1)

    def test_terminal_stop_response_never_claims_live_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / 'audit.db'))
            for arm in (False, True):
                for status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.FILLED):
                    with self.subTest(arm=arm, status=status):
                        trade = TradeManager().create_trade('TEST', 10, 3, 9, 11, 12, 13)
                        db.save_managed_trade('t', trade)
                        execution = Mock()
                        execution._new_client_order_id.return_value = 'JALWE-STOP-test'
                        def respond(**kwargs):
                            kwargs['before_submit']()
                            return BrokerOrder('TEST', TradeSide.SELL, 3, order_id='stop',
                                client_order_id=kwargs['client_order_id'], status=status)
                        execution.submit_protective_stop.side_effect = respond
                        with patch.object(watcher, 'database', db), patch.object(orchestrator_module, 'database', db):
                            if arm:
                                obj = PaperTradeOrchestrator.__new__(PaperTradeOrchestrator)
                                obj.execution_engine = execution
                                result = obj._arm_managed_trade_stop('t')
                            else:
                                result = watcher._sync_protective_stop('t', trade, execution)
                        self.assertFalse(result['active'])
                        saved = db.load_managed_trade('t')
                        self.assertFalse(saved.metadata['protective_stop_active'])
                        self.assertEqual(saved.metadata['protective_stop_order_id'], 'stop')
                        # A terminal submission still needs broker reconciliation;
                        # it does not establish an execution quantity or price.
                        self.assertEqual(saved.remaining_quantity, 3)

    def test_quote_expiring_during_broker_preflight_blocks_before_intent_commit(self):
        from test_paper_safety import ready_decision
        from trading.execution_engine import ExecutionEngine
        decision = ready_decision()
        now = datetime.now(timezone.utc)
        decision.gates['entry_quality'] = True
        decision.metadata['entry_quality'] = {'approved': True, 'quote_timestamp': now.isoformat()}
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.broker = Mock()
        engine.broker.market_is_open.return_value = True
        def delayed_positions():
            decision.metadata['entry_quality']['quote_timestamp'] = (now - timedelta(seconds=31)).isoformat()
            return []
        engine.broker.get_all_positions.side_effect = delayed_positions
        broker = Mock()
        engine.broker.submit_protected_entry.side_effect = lambda **kwargs: (
            kwargs.get('before_submit', lambda: None)(), broker.submit_order())[1]
        commit = Mock()
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            engine.submit_final_decision(decision, before_submit=commit)
        broker.submit_order.assert_not_called()
        commit.assert_not_called()

    def test_invalid_runtime_control_file_cannot_resume_new_entries(self):
        import core.runtime_controls as controls
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'controls.json'
            with patch.object(controls, 'CONTROL_FILE', path):
                self.assertTrue(controls.new_entries_allowed())  # fresh installation
                for text in ('{', '[]', '{"allow_new_entries": "false"}',
                             '{"allow_new_entries": 1}', '{}'):
                    with self.subTest(text=text):
                        path.write_text(text)
                        self.assertFalse(controls.new_entries_allowed())
                        self.assertEqual(path.read_text(), text)
                path.write_text('{"allow_new_entries": false}')
                self.assertFalse(controls.new_entries_allowed())
                path.write_text('{"allow_new_entries": true}')
                self.assertTrue(controls.new_entries_allowed())
                with patch.object(Path, 'read_text', side_effect=PermissionError()):
                    self.assertFalse(controls.new_entries_allowed())

    def test_pause_during_preflight_blocks_buy_before_submission_commit(self):
        import core.runtime_controls as controls
        from test_paper_safety import ready_decision
        from trading.execution_engine import ExecutionEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'controls.json'
            path.write_text('{"allow_new_entries": true}')
            engine = ExecutionEngine.__new__(ExecutionEngine)
            engine.broker = Mock()
            engine.broker.market_is_open.return_value = True
            def positions_then_pause():
                path.write_text('{"allow_new_entries": false}')
                return []
            engine.broker.get_all_positions.side_effect = positions_then_pause
            post = Mock()
            engine.broker.submit_protected_entry.side_effect = lambda **kwargs: (
                kwargs['before_submit'](), post())[1]
            commit = Mock()
            with patch.object(controls, 'CONTROL_FILE', path), self.assertRaisesRegex(RuntimeError, 'paused'):
                engine.submit_final_decision(ready_decision(), before_submit=commit)
            commit.assert_not_called()
            post.assert_not_called()
