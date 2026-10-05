import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pandas as pd
from alpaca.common.enums import Sort
from alpaca.trading.enums import TimeInForce
from broker.alpaca_client import AlpacaClient
from broker.entry_pricing import protected_entry_limit
from broker.paper_data_client import PaperAssetClient
from core.database import Database
from core.models import BrokerOrder, OrderStatus, TradeSide
from market.research_bar_quality import validated_research_bars
from news_engine import NewsEngine
from research_orchestrator import ResearchOrchestrator
from scanner_engine import ScannerEngine
from trading.execution_engine import ExecutionEngine
from trading.exit_submission import prepare_exit, submit_prepared_exit
from trading.recovery_engine import RecoveryEngine
from trading.risk_engine import RiskEngine
from trading.paper_trade_orchestrator import PaperTradeOrchestrator
import trading.paper_trade_orchestrator as orchestrator_module
from trading.trade_manager import TradeManager
import jalwe_research_watcher as watcher
import railway_controller as controller
import trading.recovery_engine as recovery_module


class InstallationFixTests(unittest.TestCase):
    def frame(self, end='2026-10-02T19:55:00Z', count=120, freq='5min'):
        return pd.DataFrame(dict(open=[10.] * count, high=[10.2] * count,
                                 low=[9.9] * count, close=[10.1] * count,
                                 volume=[10000.] * count),
                            index=pd.date_range(end=end, periods=count, freq=freq))

    def test_actual_research_constructor_has_assets_and_bars(self):
        with patch('broker.paper_data_client.TradingClient'), patch('broker.paper_data_client.StockHistoricalDataClient'):
            scanner = ScannerEngine()
        orchestrator = ResearchOrchestrator(scanner=scanner, news=Mock(), ai=Mock())
        self.assertIs(orchestrator.prebreakout.alpaca, scanner.alpaca)
        self.assertIs(orchestrator.liquidity.alpaca, scanner.alpaca)
        self.assertTrue(callable(scanner.alpaca.get_bars))
        self.assertTrue(callable(scanner.alpaca.list_assets))

    def test_adapter_requests_latest_30m_and_returns_chronological(self):
        with patch('broker.paper_data_client.TradingClient'), patch('broker.paper_data_client.StockHistoricalDataClient'):
            client = PaperAssetClient('fake', 'fake')
        frame = self.frame(count=4, freq='30min')
        client._data_client.get_stock_bars.return_value = NS(df=frame.iloc[::-1])
        result = client.get_bars('TEST', '30Min', limit=4)
        request = client._data_client.get_stock_bars.call_args.args[0]
        self.assertEqual(request.sort, Sort.DESC)
        self.assertEqual(str(request.timeframe), '30Min')
        self.assertTrue(result.df.index.is_monotonic_increasing)

    def test_limit_never_exceeds_cap_including_dollar_boundary(self):
        for price in (0.5, 0.9966, 0.999, 1.005, 10, 100.123):
            limit = protected_entry_limit(price, 0.35)
            self.assertLessEqual(limit, price * 1.0035 + 1e-12)
        self.assertEqual(protected_entry_limit(1.005, 0.35), 1.00)

    def test_nonfinite_prices_and_caps_are_rejected(self):
        for price, cap in ((float('nan'), .35), (float('inf'), .35), (1, float('nan')), (1, float('inf')), (1, -1), (1, 5.01)):
            with self.assertRaises(ValueError):
                protected_entry_limit(price, cap)

    def test_risk_cash_and_allocation_are_sized_at_broker_limit(self):
        broker = AlpacaClient.__new__(AlpacaClient)
        broker.client = Mock()
        risk = RiskEngine.__new__(RiskEngine)
        qty, amount, value = risk._calculate_quantity(10, 9.85, 100, 100, 1.5, 100)
        broker.submit_protected_entry('TEST', qty, 10)
        request = broker.client.submit_order.call_args.kwargs['order_data']
        self.assertEqual(request.time_in_force, TimeInForce.DAY)
        self.assertLessEqual(qty * (request.limit_price - 9.85), 1.5)
        self.assertLessEqual(qty * request.limit_price, 100)
        self.assertAlmostEqual(amount, qty * (request.limit_price - 9.85))
        self.assertAlmostEqual(value, qty * request.limit_price)

    def test_completed_bars_exclude_current_interval(self):
        frame = self.frame(end='2026-10-02T19:55:00Z', count=4)
        valid = validated_research_bars(frame, '5Min', '2026-10-02T19:57:00Z')
        self.assertEqual(len(valid), 3)
        self.assertEqual(valid.index[-1], pd.Timestamp('2026-10-02T19:50:00Z'))

    def test_stale_and_un_timestamped_research_data_rejected(self):
        for frame in (self.frame(end='2020-01-01T10:00:00Z'), self.frame().reset_index(drop=True)):
            with self.assertRaises(ValueError):
                validated_research_bars(frame, '5Min', '2026-10-02T19:57:00Z')

    def test_invalid_ohlcv_cannot_pass_research_quality(self):
        frame = self.frame()
        frame.iloc[-1, frame.columns.get_loc('volume')] = float('inf')
        with self.assertRaises(ValueError):
            validated_research_bars(frame, '5Min', '2026-10-02T20:00:00Z')

    def test_daily_bar_excluded_before_session_close(self):
        frame = self.frame(end='2026-10-02T04:00:00Z', count=4, freq='D')
        valid = validated_research_bars(frame, '1Day', '2026-10-02T19:59:00Z')
        self.assertEqual(len(valid), 3)

    def test_news_negation_and_word_boundaries(self):
        news = NewsEngine.__new__(NewsEngine)
        self.assertLess(news._score_text('FDA approval denied'), 0)
        self.assertNotIn('APPROVAL', news._detect_catalysts('FDA approval denied'))
        self.assertEqual(news._score_text('unprofitable'), 0)
        self.assertAlmostEqual(news._score_text('FDA approval'), .4)
        self.assertAlmostEqual(news._score_text('registered direct offering'), -.35)

    def test_finviz_percent_text_and_fraction_have_same_units(self):
        scanner = ScannerEngine.__new__(ScannerEngine)
        for value, expected in (('0.5%', .5), (.005, .5), (1.2, 120)):
            result = scanner._build_ranked_candidates(pd.DataFrame([
                {'Ticker': 'TEST', 'Price': 10, 'Change': value, 'Volume': 1000000}
            ]), {'TEST'})
            self.assertEqual(result[0].change_pct, expected)

    def test_cancelled_partial_orders_are_visible_to_alerts(self):
        partial = {'id': 'one', 'status': 'canceled', 'filled_qty': '3'}
        with patch.object(controller, 'alpaca_get', return_value=[partial]) as get:
            self.assertEqual(controller.get_recent_filled_orders(), [partial])
        self.assertEqual(get.call_args.args[1]['status'], 'all')

    def test_alerts_report_incremental_fills_without_duplicates(self):
        state = {'order_alerts_bootstrapped': True, 'notified_order_ids': []}
        order = dict(id='one', symbol='TEST', side='buy', status='partially_filled', filled_qty='2', filled_avg_price='10')
        with patch.object(controller, 'controller_state', state), patch.object(controller, 'get_recent_filled_orders', side_effect=lambda _: [dict(order)]), patch.object(controller, 'send_message') as send, patch.object(controller, 'save_state'), patch.object(controller, 'order_alert_text', side_effect=lambda o: o), patch.object(controller, 'ORDER_ALERTS_ENABLED', True), patch.object(controller, 'ORDER_ALERT_POLL_SECONDS', 0):
            controller.poll_filled_order_alerts()
            controller.poll_filled_order_alerts()
            order.update(status='canceled', filled_qty='3', filled_avg_price='10.2')
            controller.poll_filled_order_alerts()
            controller.poll_filled_order_alerts()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0]['filled_qty'], 1)
        self.assertAlmostEqual(send.call_args.args[0]['filled_avg_price'], 10.6)

    def test_restart_notification_failure_does_not_kill_controller(self):
        child = NS(desired_running=False, process=None, restart_if_needed=lambda: 'restarted')
        with patch.object(controller, 'apex', child), patch.object(controller, 'jalwe', child), patch.object(controller, 'send_message', side_effect=RuntimeError('offline')):
            controller.monitor_children()

    def test_cancelled_partial_entry_becomes_managed(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'partial.db'))
            db.create_entry_intent('i', 'JALWE-FINAL-test', 'TEST', 10, 10, 9, 12, 13, 14, 1)
            active = NS(id='entry', client_order_id='JALWE-FINAL-test', symbol='TEST', side='buy', qty='10', filled_qty='3', filled_avg_price='10', status='partially_filled', submitted_at=None)
            terminal = NS(**vars(active)); terminal.status = 'canceled'
            engine = RecoveryEngine.__new__(RecoveryEngine)
            engine.broker = Mock()
            engine.broker.get_order_by_client_id.side_effect = [active, terminal]
            engine.reconciliation = Mock()
            engine.reconciliation.verify_position.return_value = {'exists': True, 'quantity': 3, 'average_entry_price': 10}
            engine.trade_manager = TradeManager()
            with patch.object(recovery_module, 'database', db):
                result = engine.recover_entry_intent(db.get_entry_intent_row('i'))
            self.assertTrue(result['managed'])
            engine.broker.cancel_order.assert_called_once_with('entry')
            self.assertEqual(next(iter(db.load_active_managed_trades().values())).remaining_quantity, 3)

    def test_crash_after_exit_acceptance_recovers_fill_once(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'exit.db'))
            manager = TradeManager()
            trade = manager.create_trade('TEST', 10, 8, 9, 11, 12, 13)
            decision = manager.evaluate(trade, 11)
            db.save_managed_trade('t', trade)
            intent = prepare_exit(db, 't', trade, decision)
            engine = ExecutionEngine.__new__(ExecutionEngine)
            engine.broker = Mock()
            engine.broker.market_is_open.return_value = True
            engine.broker.get_position.return_value = NS(qty='8')
            def accepted_response_lost(**options):
                options['before_submit']()
                raise TimeoutError('Accepted, response lost')
            engine.broker.submit_market_order.side_effect = accepted_response_lost
            with self.assertRaises(TimeoutError):
                submit_prepared_exit(db, 't', trade, decision, engine, manager)
            persisted = db.load_managed_trade('t')
            self.assertEqual(persisted.metadata['exit_submission']['state'], 'SUBMITTING')
            self.assertFalse(persisted.has_pending_exit)
            recovery = RecoveryEngine.__new__(RecoveryEngine)
            recovery.trade_manager = manager
            recovery.broker = Mock()
            recovery.broker.get_order_by_client_id.return_value = NS(id='exit', client_order_id=intent['client_order_id'], symbol='TEST', side='sell', qty='3')
            recovery.reconciliation = Mock()
            recovery.reconciliation.reconcile_order.return_value = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='exit', status=OrderStatus.FILLED, filled_price=11, filled_quantity=3, submitted_at=datetime.now(timezone.utc))
            recovery.reconciliation.verify_position.return_value = {'exists': True, 'quantity': 5}
            with patch.object(recovery_module, 'database', db):
                self.assertTrue(recovery.recover_trade('t', persisted)['recovered'])
                again = db.load_managed_trade('t')
                self.assertTrue(recovery.recover_trade('t', again)['recovered'])
            self.assertEqual(again.remaining_quantity, 5)
            self.assertEqual(db.get_strategy_realized_pnl_total(), 3)
            self.assertNotIn('exit_submission', again.metadata)
            recovery.broker.submit_market_order.assert_not_called()

    def test_exit_preflight_rejection_can_be_retried_without_orphan(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'exit.db'))
            manager = TradeManager()
            trade = manager.create_trade('TEST', 10, 8, 9, 11, 12, 13)
            decision = manager.evaluate(trade, 11)
            prepare_exit(db, 't', trade, decision)
            engine = ExecutionEngine.__new__(ExecutionEngine)
            engine.broker = Mock(); engine.broker.market_is_open.return_value = False
            with self.assertRaises(RuntimeError):
                submit_prepared_exit(db, 't', trade, decision, engine, manager)
            self.assertEqual(db.load_managed_trade('t').metadata['exit_submission']['state'], 'PREPARED')
            engine.broker.submit_market_order.assert_not_called()

    def test_unknown_exit_lookup_blocks_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'exit.db'))
            manager = TradeManager()
            trade = manager.create_trade('TEST', 10, 8, 9, 11, 12, 13)
            decision = manager.evaluate(trade, 11)
            prepare_exit(db, 't', trade, decision)['state'] = 'SUBMITTING'
            db.save_managed_trade('t', trade)
            recovery = RecoveryEngine.__new__(RecoveryEngine)
            recovery.trade_manager = manager
            recovery.broker = Mock(); recovery.broker.get_order_by_client_id.side_effect = RuntimeError('Unavailable')
            with patch.object(recovery_module, 'database', db):
                result = recovery.recover_trade('t', db.load_managed_trade('t'))
            self.assertFalse(result['recovered'])
            with self.assertRaises(RuntimeError):
                prepare_exit(db, 't', trade, decision)
            recovery.broker.submit_market_order.assert_not_called()

    def test_stop_client_identity_is_saved_before_broker_call(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            execution = Mock()
            def fail(**kwargs):
                saved = db.load_managed_trade('t')
                self.assertEqual(saved.metadata['protective_stop_client_order_id'], kwargs['client_order_id'])
                kwargs['before_submit']()
                raise TimeoutError('Unknown broker result')
            execution.submit_protective_stop.side_effect = fail
            with patch.object(watcher, 'database', db):
                with self.assertRaises(TimeoutError):
                    watcher._sync_protective_stop('t', trade, execution)
                with self.assertRaises(RuntimeError):
                    watcher._sync_protective_stop('t', trade, execution)
            self.assertEqual(execution.submit_protective_stop.call_count, 1)

    def test_stop_acceptance_is_recovered_by_saved_client_id(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            trade.metadata.update(protective_stop_client_order_id='JALWE-STOP-test',
                                  protective_stop_submission_uncertain=True,
                                  protective_stop_quantity=8, protective_stop_price=9)
            db.save_managed_trade('t', trade)
            recovery = RecoveryEngine.__new__(RecoveryEngine)
            recovery.trade_manager = TradeManager()
            recovery.broker = Mock()
            recovery.broker.get_order_by_client_id.return_value = NS(
                id='stop', client_order_id='JALWE-STOP-test', symbol='TEST', side='sell', qty='8', status='new',
                filled_qty='0', filled_avg_price=None)
            recovery.reconciliation = Mock()
            recovery.reconciliation.verify_position.return_value = {'exists': True, 'quantity': 8}
            with patch.object(recovery_module, 'database', db):
                result = recovery.recover_trade('t', db.load_managed_trade('t'))
            self.assertTrue(result['recovered'])
            self.assertEqual(db.load_managed_trade('t').metadata['protective_stop_order_id'], 'stop')
            recovery.broker.submit_stop_order.assert_not_called()

    def test_initial_entry_stop_is_persisted_before_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(str(Path(temp)/'stop.db'))
            trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
            db.save_managed_trade('t', trade)
            orchestrator = PaperTradeOrchestrator.__new__(PaperTradeOrchestrator)
            execution = ExecutionEngine.__new__(ExecutionEngine)
            execution.broker = Mock()
            execution.broker.get_position.return_value = NS(qty='8')
            def fail(**kwargs):
                saved = db.load_managed_trade('t')
                self.assertEqual(saved.metadata['protective_stop_client_order_id'], kwargs['client_order_id'])
                kwargs['before_submit']()
                raise TimeoutError('Accepted, response lost')
            execution.broker.submit_stop_order.side_effect = fail
            orchestrator.execution_engine = execution
            with patch.object(orchestrator_module, 'database', db):
                with self.assertRaises(TimeoutError):
                    orchestrator._arm_managed_trade_stop('t')
                with self.assertRaises(RuntimeError):
                    orchestrator._arm_managed_trade_stop('t')
            self.assertEqual(execution.broker.submit_stop_order.call_count, 1)

    def test_pending_exit_does_not_create_competing_stop(self):
        trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
        manager = TradeManager()
        manager.register_exit_order(trade, manager.evaluate(trade, 11),
            BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='exit',
                        status=OrderStatus.ACCEPTED, submitted_at=datetime.now(timezone.utc)))
        execution = Mock()
        self.assertEqual(watcher._sync_protective_stop('t', trade, execution)['reason'], 'PENDING_EXIT')
        execution.submit_protective_stop.assert_not_called()

    def test_shared_storage_honors_controller_override(self):
        import os
        from core.storage import data_directory, database_path
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'JALWE_CONTROLLER_DATA_DIR': temp, 'JALWE_DATABASE_PATH': ''}):
                self.assertEqual(data_directory(), Path(temp).resolve())
                self.assertEqual(Path(database_path()).parent, data_directory())

    def test_closed_market_exit_keeps_existing_stop(self):
        trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
        database = Mock(); database.load_active_managed_trades.return_value = {'t': trade}
        execution = Mock(); execution.broker.market_is_open.return_value = False
        with patch.object(watcher, 'emergency_close_requested', return_value=True), patch.object(watcher, 'auto_paper_execution_ready', return_value=True), patch.object(watcher, 'get_recovery_engine') as recovery, patch.object(watcher, 'database', database), patch.object(watcher, 'get_market_data') as market_data, patch.object(watcher, 'get_trade_manager', return_value=TradeManager()), patch.object(watcher, 'get_execution_engine', return_value=execution), patch.object(watcher, 'get_reconciliation_engine'), patch.object(watcher, '_cancel_protective_stop') as cancel, patch.object(watcher, '_sync_protective_stop'):
            recovery.return_value.recover_all.return_value = {'safe_to_trade': True}
            market_data.return_value.get_last_price.return_value = 11
            self.assertEqual(watcher.process_emergency_paper_close(), 0)
        cancel.assert_not_called()
        execution.submit_exit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
