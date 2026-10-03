import tempfile
from contextlib import ExitStack
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from broker.alpaca_client import AlpacaClient
from core.database import Database
from core.manual_sells import ManualSellQueue
from core.manual_sell_ui import menu, callback
from core.models import BrokerOrder, OrderStatus, TradeSide
from trading.trade_manager import TradeManager, TradeAction, TradeStage
from trading.execution_engine import ExecutionEngine
from trading.exit_sessions import exit_session, sell_limit, prepare_exit_context, settle_exit
import trading.exit_sessions as sessions
import jalwe_research_watcher as watcher
import railway_controller as controller


class ManualSellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name)/'trades.db'))
        self.trade = TradeManager().create_trade('TEST', 10, 8, 9, 11, 12, 13)
        self.db.save_managed_trade('t', self.trade)
        self.queue = ManualSellQueue(self.db)

    def test_duplicate_confirmation_survives_restart(self):
        request = self.queue.prepare('t', 'owner')
        for _ in range(3):
            self.assertEqual(ManualSellQueue(self.db).confirm(request['request_id'], 'owner')['state'], 'QUEUED')
        self.assertEqual(self.queue.prepare('t', 'owner')['request_id'], request['request_id'])
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_sell_requests WHERE state='QUEUED'").fetchone()[0], 1)

    def test_confirmation_is_bound_to_requesting_user(self):
        request = self.queue.prepare('t', 'owner')
        with self.assertRaises(ValueError):
            self.queue.confirm(request['request_id'], 'other')
        self.assertIsNone(self.queue.pending_for('t'))

    def test_changed_quantity_requires_new_confirmation(self):
        request = self.queue.prepare('t', 'owner')
        self.trade.remaining_quantity = 6
        self.db.save_managed_trade('t', self.trade)
        self.assertEqual(self.queue.confirm(request['request_id'], 'owner')['state'], 'EXPIRED')
        self.assertIsNone(self.queue.pending_for('t'))
        self.assertEqual(self.queue.prepare('t', 'owner')['quantity'], 6)

    def test_expired_confirmation_does_not_queue_sell(self):
        request = self.queue.prepare('t', 'owner')
        with self.db.connection() as conn:
            conn.execute("UPDATE manual_sell_requests SET expires_at='2000-01-01' WHERE request_id=?", (request['request_id'],))
        with self.assertRaises(ValueError):
            self.queue.confirm(request['request_id'], 'owner')
        self.assertIsNone(self.queue.pending_for('t'))

    def test_pending_broker_exit_cannot_create_competing_manual_sell(self):
        self.trade.pending_action = TradeAction.TAKE_PROFIT_1
        self.trade.pending_order_id = 'old'
        self.db.save_managed_trade('t', self.trade)
        with self.assertRaises(ValueError):
            self.queue.prepare('t', 'owner')

    def test_cancel_draft_never_queues(self):
        request = self.queue.prepare('t', 'owner')
        self.queue.cancel_draft(request['request_id'], 'owner')
        self.assertEqual(self.queue.confirm(request['request_id'], 'owner')['state'], 'CANCELED')
        self.assertIsNone(self.queue.pending_for('t'))

    def test_inline_buttons_only_queue_after_confirmation(self):
        response = menu(self.db, 'owner')
        button = response['reply_markup']['inline_keyboard'][0][0]
        response = callback(self.db, button['callback_data'], 'owner')
        self.assertIsNone(self.queue.pending_for('t'))
        confirm_button = response['reply_markup']['inline_keyboard'][0][0]
        self.assertLessEqual(len(confirm_button['callback_data'].encode()), 64)
        callback(self.db, confirm_button['callback_data'], 'owner')
        self.assertIsNotNone(self.queue.pending_for('t'))

    def test_foreign_telegram_chat_cannot_use_callback(self):
        request = self.queue.prepare('t', 'owner')
        with patch.object(controller, 'TELEGRAM_CHAT_ID', 'allowed'), patch.object(controller, 'database', self.db), patch.object(controller, 'tg_call') as tg:
            accepted = controller.process_telegram_update({'callback_query': {'id':'x', 'from':{'id':'owner'},
                'message':{'chat':{'id':'foreign'}}, 'data':'sell-confirm:'+request['request_id']}})
        self.assertFalse(accepted)
        tg.assert_not_called()
        self.assertIsNone(self.queue.pending_for('t'))

    def test_telegram_callback_is_idempotent(self):
        request = self.queue.prepare('t', 'owner')
        update = {'callback_query': {'id':'x', 'from':{'id':'owner'}, 'message':{'chat':{'id':'allowed'}},
                  'data':'sell-confirm:'+request['request_id']}}
        with patch.object(controller, 'TELEGRAM_CHAT_ID', 'allowed'), patch.object(controller, 'database', self.db), patch.object(controller, 'tg_call'), patch.object(controller, 'send_message'):
            self.assertTrue(controller.process_telegram_update(update))
            self.assertTrue(controller.process_telegram_update(update))
        self.assertEqual(self.queue.pending_for('t')['request_id'], request['request_id'])

    def test_completed_position_finishes_request(self):
        request = self.queue.prepare('t', 'owner')
        self.queue.confirm(request['request_id'], 'owner')
        self.trade.remaining_quantity = 0
        self.trade.stage = TradeStage.CLOSED
        self.db.save_managed_trade('t', self.trade)
        self.queue.finish_closed()
        self.assertEqual(self.queue.get(request['request_id'])['state'], 'DONE')

    def test_watcher_executes_manual_close_once(self):
        request = self.queue.prepare('t', 'owner')
        self.queue.confirm(request['request_id'], 'owner')
        execution = Mock()
        execution.broker.market_is_open.return_value = True
        order = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=8, order_id='manual',
            status=OrderStatus.FILLED, filled_quantity=8, filled_price=10.1)
        def submit(**kwargs):
            kwargs['before_submit']()
            return order
        execution.submit_exit.side_effect = submit
        reconciliation = Mock()
        reconciliation.wait_for_terminal_state.return_value = order
        with patch.object(watcher, 'database', self.db), patch.object(watcher, 'auto_paper_execution_ready', return_value=True), patch.object(watcher, 'get_recovery_engine') as recovery, patch.object(watcher, 'get_execution_engine', return_value=execution), patch.object(watcher, 'get_trade_manager', return_value=TradeManager()), patch.object(watcher, 'get_reconciliation_engine', return_value=reconciliation), patch.object(watcher, 'get_market_data') as market, patch.object(watcher, 'notify_recovered_protective_stop_fills'), patch.object(watcher, 'notify_management_lifecycle'), patch.object(watcher, '_cancel_protective_stop', return_value='NONE'):
            recovery.return_value.recover_all.return_value = {'safe_to_trade':True}
            market.return_value.get_trade_range.return_value = {'last':10.1, 'high':10.1, 'count':1}
            watcher.manage_active_paper_trades({})
            watcher.manage_active_paper_trades({})
        execution.submit_exit.assert_called_once()
        self.assertEqual(self.db.load_managed_trade('t').remaining_quantity, 0)
        self.assertAlmostEqual(self.db.get_strategy_realized_pnl_total(), 0.8)
        self.assertEqual(self.queue.get(request['request_id'])['state'], 'DONE')


class ExtendedSessionTests(unittest.TestCase):
    def broker(self, stamp, *, closes=16, trading_day=True):
        broker = Mock()
        broker.market_is_open.return_value = False
        broker.get_clock.return_value = NS(timestamp=stamp)
        day = stamp.astimezone(sessions.NY).date()
        broker.get_calendar_day.return_value = NS(open=datetime.combine(day, datetime.min.time()).replace(hour=9,minute=30),
            close=datetime.combine(day, datetime.min.time()).replace(hour=closes)) if trading_day else None
        return broker

    def test_pre_and_post_sessions_use_calendar(self):
        for hour in (8, 17):
            stamp = datetime(2026,10,2,hour,tzinfo=sessions.NY)
            with patch.object(sessions, 'utc_now', return_value=stamp):
                self.assertEqual(exit_session(self.broker(stamp)), 'EXTENDED')

    def test_closed_overnight_and_weekend_do_not_trade(self):
        for stamp, day in ((datetime(2026,10,2,21,tzinfo=sessions.NY),True),
                           (datetime(2026,10,3,8,tzinfo=sessions.NY),False)):
            with patch.object(sessions, 'utc_now', return_value=stamp):
                self.assertEqual(exit_session(self.broker(stamp, trading_day=day)), 'CLOSED')

    def test_early_close_uses_calendar_close(self):
        stamp = datetime(2026,11,27,14,tzinfo=sessions.NY)
        with patch.object(sessions, 'utc_now', return_value=stamp):
            self.assertEqual(exit_session(self.broker(stamp, closes=13)), 'EXTENDED')

    def test_holiday_and_disabled_extended_do_not_trade(self):
        stamp = datetime(2026,12,25,8,tzinfo=sessions.NY)
        with patch.object(sessions, 'utc_now', return_value=stamp):
            self.assertEqual(exit_session(self.broker(stamp, trading_day=False)), 'CLOSED')
            self.assertEqual(exit_session(self.broker(stamp), enabled=False), 'CLOSED')

    def test_stale_clock_refuses_session(self):
        stamp = datetime(2026,10,2,17,tzinfo=sessions.NY)
        with patch.object(sessions, 'utc_now', return_value=stamp+timedelta(minutes=2)):
            with self.assertRaises(ValueError):
                exit_session(self.broker(stamp))

    def test_limit_rounding_does_not_exceed_slippage_and_preserves_target(self):
        quote = {'bid':11.57,'ask':11.59,'timestamp':datetime.now(timezone.utc)}
        self.assertGreaterEqual(sell_limit(quote), 11.57*(1-.0035))
        self.assertEqual(sell_limit(quote, floor=11.79), 11.79)
        quote.update(bid=.12345,ask=.12350)
        self.assertGreaterEqual(sell_limit(quote), .12345*(1-.0035))

    def test_stale_nan_and_crossed_quotes_do_not_submit(self):
        for changes in ({'timestamp':datetime.now(timezone.utc)-timedelta(minutes=2)},
                        {'bid':float('nan')}, {'bid':11.6,'ask':11.5}, {'timestamp':None}):
            quote = {'bid':11.57,'ask':11.59,'timestamp':datetime.now(timezone.utc)}
            quote.update(changes)
            with self.assertRaises(ValueError):
                sell_limit(quote)

    def test_extended_broker_request_is_sell_day_limit(self):
        broker = AlpacaClient.__new__(AlpacaClient)
        broker.client = Mock()
        before = Mock()
        broker.submit_extended_exit(symbol='TEST', quantity=6, limit_price=11.54,
            client_order_id='JALWE-EXIT-test', before_submit=before)
        request = broker.client.submit_order.call_args.kwargs['order_data']
        self.assertEqual(request.side.value, 'sell')
        self.assertEqual(request.type.value, 'limit')
        self.assertEqual(request.time_in_force.value, 'day')
        self.assertTrue(request.extended_hours)
        before.assert_called_once()

    def test_timeout_cancel_reconciles_racing_fill(self):
        partial = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='x',
            status=OrderStatus.PARTIALLY_FILLED, filled_quantity=1, filled_price=11.79)
        terminal = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='x',
            status=OrderStatus.CANCELED, filled_quantity=2, filled_price=11.79)
        reconciliation = Mock()
        reconciliation.wait_for_terminal_state.side_effect = [partial,terminal]
        execution = Mock()
        actual = settle_exit(partial,execution,reconciliation,extended_hours=True)
        execution.broker.cancel_order.assert_called_once_with('x')
        trade = TradeManager().create_trade('TEST',11.66,8,11.58,11.79,11.92,12.05)
        manager = TradeManager()
        manager.register_exit_order(trade, manager.evaluate(trade,11.79), partial)
        manager.apply_exit_reconciliation(trade,actual)
        self.assertEqual(trade.remaining_quantity,6)
        self.assertEqual(trade.t1_realized_quantity,2)
        self.assertFalse(trade.has_pending_exit)

    def test_extended_preflight_failure_does_not_mark_submission(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.broker = Mock()
        engine.broker.get_position.return_value = NS(qty='2')
        before = Mock()
        with patch('trading.execution_engine.exit_session', return_value='EXTENDED'):
            with self.assertRaises(RuntimeError):
                engine.submit_exit('TEST',3,client_order_id='JALWE-EXIT-test', before_submit=before,
                    extended_hours=True,limit_price=10,quote_at=datetime.now(timezone.utc).isoformat())
        before.assert_not_called()
        engine.broker.submit_extended_exit.assert_not_called()

    def test_extended_execution_never_uses_market_sell(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.broker = Mock()
        engine.broker.get_position.return_value = NS(qty='8')
        engine._build_broker_order = Mock()
        with patch('trading.execution_engine.exit_session', return_value='EXTENDED'):
            engine.submit_exit('TEST',3,client_order_id='JALWE-EXIT-test', extended_hours=True,
                limit_price=11.79,quote_at=datetime.now(timezone.utc).isoformat())
        engine.broker.submit_extended_exit.assert_called_once()
        engine.broker.submit_market_order.assert_not_called()


class WatcherIntegrationTests(unittest.TestCase):
    setUp = ManualSellTests.setUp

    def fixtures(self, session):
        stack = ExitStack()
        self.addCleanup(stack.close)
        execution, reconciliation, market = Mock(), Mock(), Mock()
        execution.broker.market_is_open.return_value = session == 'REGULAR'
        market.get_trade_range.return_value = {'last': 10.1, 'high': 10.1, 'count': 1}
        market.get_execution_quote.return_value = {'bid': 11.1, 'ask': 11.12,
            'timestamp': datetime.now(timezone.utc)}
        for name, value in [('database', self.db), ('get_execution_engine', lambda: execution),
                            ('get_reconciliation_engine', lambda: reconciliation),
                            ('get_market_data', lambda: market), ('get_trade_manager', TradeManager)]:
            stack.enter_context(patch.object(watcher, name, value))
        stack.enter_context(patch.object(watcher, 'auto_paper_execution_ready', return_value=True))
        recovery = stack.enter_context(patch.object(watcher, 'get_recovery_engine'))
        recovery.return_value.recover_all.return_value = {'safe_to_trade': True}
        stack.enter_context(patch.object(watcher, 'notify_recovered_protective_stop_fills'))
        stack.enter_context(patch.object(watcher, 'notify_management_lifecycle'))
        stack.enter_context(patch.object(watcher, 'exit_session', return_value=session))
        stack.enter_context(patch.object(sessions, 'exit_session', return_value=session))
        cancel = stack.enter_context(patch.object(watcher, '_cancel_protective_stop', return_value='NONE'))
        sync = stack.enter_context(patch.object(watcher, '_sync_protective_stop', return_value={}))
        return execution, reconciliation, market, cancel, sync

    def test_extended_target_partial_cancel_rearms_actual_remainder(self):
        execution, reconciliation, market, cancel, sync = self.fixtures('EXTENDED')
        partial = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='extended',
            status=OrderStatus.PARTIALLY_FILLED, filled_quantity=1, filled_price=11.1)
        terminal = BrokerOrder(symbol='TEST', side=TradeSide.SELL, quantity=3, order_id='extended',
            status=OrderStatus.CANCELED, filled_quantity=2, filled_price=11.1)
        def submit(**kwargs):
            kwargs['before_submit']()
            return partial
        execution.submit_exit.side_effect = submit
        reconciliation.wait_for_terminal_state.side_effect = [partial, terminal]
        watcher.manage_active_paper_trades({})
        kwargs = execution.submit_exit.call_args.kwargs
        self.assertTrue(kwargs['extended_hours'])
        self.assertGreaterEqual(kwargs['limit_price'], self.trade.target_1)
        execution.broker.cancel_order.assert_called_once_with('extended')
        actual = self.db.load_managed_trade('t')
        self.assertEqual(actual.remaining_quantity, 6)
        self.assertEqual(actual.t1_realized_quantity, 2)
        self.assertFalse(actual.has_pending_exit)
        self.assertEqual(sync.call_args.args[1].remaining_quantity, 6)
        self.assertAlmostEqual(self.db.get_strategy_realized_pnl_total(), 2.2)

    def test_closed_session_keeps_manual_request_and_protection(self):
        request = self.queue.prepare('t', 'owner')
        self.queue.confirm(request['request_id'], 'owner')
        execution, reconciliation, market, cancel, sync = self.fixtures('CLOSED')
        watcher.manage_active_paper_trades({})
        execution.submit_exit.assert_not_called()
        cancel.assert_not_called()
        sync.assert_called_once()
        self.assertEqual(self.db.load_managed_trade('t').remaining_quantity, 8)
        self.assertIsNotNone(self.queue.pending_for('t'))

    def test_unavailable_extended_quote_does_not_cancel_stop(self):
        execution, reconciliation, market, cancel, sync = self.fixtures('EXTENDED')
        market.get_execution_quote.side_effect = RuntimeError('SIP unavailable')
        with self.assertLogs(watcher.logger, level='ERROR'):
            watcher.manage_active_paper_trades({})
        cancel.assert_not_called()
        execution.submit_exit.assert_not_called()
        self.assertEqual(self.db.load_managed_trade('t').remaining_quantity, 8)

    def test_lifecycle_checkpoint_records_changes_once(self):
        from core.lifecycle_audit import checkpoint
        with patch.object(self.db, 'log_event', wraps=self.db.log_event) as log:
            checkpoint(self.db, 't', self.trade)
            checkpoint(self.db, 't', self.db.load_managed_trade('t'))
            self.assertEqual(log.call_count, 1)
            self.trade.remaining_quantity = 6
            self.trade.t1_realized_quantity = 2
            checkpoint(self.db, 't', self.trade)
            self.assertEqual(log.call_count, 2)
        self.assertEqual(log.call_args.kwargs['metadata']['t1_filled'], 2)


class DataAccessDiagnosticsTests(unittest.TestCase):
    def run_probe(self, message):
        class ProviderError(RuntimeError):
            status_code = 403
        db = Mock()
        db.load_active_managed_trades.return_value = {'t': NS(symbol='CANE')}
        with patch.object(watcher, 'database', db), patch.object(watcher, 'get_market_data') as market:
            market.return_value.get_execution_quote.side_effect = ProviderError(message)
            result = watcher.extended_exit_data_access()
        self.assertNotIn(message, str(result))
        return result

    def test_subscription_denial_has_actionable_safe_category(self):
        self.assertEqual(self.run_probe('subscription does not permit querying recent SIP data')['reason'],
                         'SUBSCRIPTION_REQUIRED')

    def test_other_forbidden_errors_are_not_mislabeled_subscription(self):
        self.assertEqual(self.run_probe('provider details containing a private value')['reason'], 'FORBIDDEN')


if __name__ == '__main__':
    unittest.main()
