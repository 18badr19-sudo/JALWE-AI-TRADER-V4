"""Exercise real competing SQLite writers without issuing broker orders."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from core.database import Database
from core.lifecycle_audit import checkpoint
from trading.trade_manager import TradeManager
import jalwe_research_watcher as watcher
import railway_controller as controller


class ManagedWriteContentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'native.db'
        self.db = Database(str(self.path))
        self.db.MANAGED_WRITE_TIMEOUT_SECONDS = .01
        self.trade = TradeManager().create_trade('TEST', 10., 5, 9., 11., 12., 13.)
        self.db.save_managed_trade('t', self.trade)
        cache = patch.dict(watcher._protective_stop_observed_at, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def test_another_writer_releases_after_first_busy_and_upsert_survives_restart(self):
        locked, release = threading.Event(), threading.Event()
        def hold():
            conn = sqlite3.connect(self.path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                locked.set()
                release.wait(3)
                conn.rollback()
            finally:
                conn.close()
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(locked.wait(3))
        def busy(exc):
            release.set()
            return Database._is_busy(exc)
        try:
            self.trade.metadata['test_checkpoint'] = 'saved'
            with patch.object(self.db, '_is_busy', side_effect=busy) as retry:
                self.db.save_managed_trade('t', self.trade, 'BUY', 10.)
            self.assertGreaterEqual(retry.call_count, 1)
        finally:
            release.set()
            thread.join(3)
        restarted = Database(str(self.path))
        actual = restarted.load_managed_trade('t')
        self.assertEqual(actual.remaining_quantity, 5)
        self.assertEqual(actual.metadata['test_checkpoint'], 'saved')
        self.assertEqual(restarted.get_managed_trade_row('t')['entry_order_id'], 'BUY')
        self.assertEqual(restarted.get_strategy_realized_pnl_total(), 0)

    def test_persistent_writer_exhausts_three_attempts_without_changing_saved_state(self):
        conn = sqlite3.connect(self.path)
        conn.execute('BEGIN IMMEDIATE')
        try:
            self.trade.remaining_quantity = 4
            with patch.object(self.db, '_is_busy', wraps=Database._is_busy) as busy:
                with self.assertRaises(sqlite3.OperationalError):
                    self.db.save_managed_trade('t', self.trade)
                self.assertEqual(busy.call_count, 3)
        finally:
            conn.rollback()
            conn.close()
        self.assertEqual(self.db.load_managed_trade('t').remaining_quantity, 5)

    def test_other_storage_errors_and_caller_owned_fill_transactions_are_not_replayed(self):
        conn = Mock()
        conn.execute.side_effect = sqlite3.OperationalError('disk I/O error')
        @contextmanager
        def connect(**kwargs):
            yield conn
        with patch.object(self.db, 'connection', side_effect=connect) as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O'):
                self.db.save_managed_trade('t', self.trade)
            self.assertEqual(connections.call_count, 1)
            conn.execute.side_effect = sqlite3.OperationalError('database is locked')
            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_managed_trade('t', self.trade, _connection=conn)
            self.assertEqual(connections.call_count, 1)

    def protected(self):
        self.trade.metadata.update(protective_stop_order_id='STOP', protective_stop_quantity=5,
            protective_stop_client_order_id='owned', protective_stop_applied_qty=0,
            protective_stop_price=9.)
        execution = Mock()
        execution.broker.get_order.return_value = NS(id='STOP', symbol='TEST', side='sell',
            qty='5', client_order_id='owned', status='new', filled_qty='0',
            filled_avg_price=None, stop_price='9', submitted_at=None, filled_at=None)
        return execution

    def test_unchanged_stop_and_blocked_reason_can_be_polled_while_another_writer_holds_db(self):
        execution = self.protected()
        with patch.object(watcher, 'database', self.db):
            watcher._defer_extended_exit('t', self.trade, execution, 'SUBSCRIPTION_REQUIRED')
            checkpoint(self.db, 't', self.trade)
            conn = sqlite3.connect(self.path)
            conn.execute('BEGIN IMMEDIATE')
            try:
                with patch.object(self.db, 'save_managed_trade', wraps=self.db.save_managed_trade) as save:
                    watcher._defer_extended_exit('t', self.trade, execution, 'SUBSCRIPTION_REQUIRED')
                    save.assert_not_called()
            finally:
                conn.rollback()
                conn.close()
        self.assertEqual(execution.broker.get_order.call_count, 2)
        execution.submit_protective_stop.assert_not_called()
        execution.broker.cancel_order.assert_not_called()
        execution.submit_exit.assert_not_called()

    def test_broker_observation_is_fresh_bounded_and_does_not_claim_a_fill(self):
        execution = self.protected()
        with (patch.object(watcher, 'database', self.db),
              patch.object(watcher.time, 'monotonic', side_effect=[0., 1., 901.]),
              self.assertLogs(watcher.logger, level='INFO') as logs):
            for _ in range(3):
                watcher._sync_protective_stop('t', self.trade, execution)
        messages = [x for x in logs.output if 'BROKER_PROTECTION_OBSERVED' in x]
        self.assertEqual(len(messages), 2)
        record = json.loads(messages[0].split('BROKER_PROTECTION_OBSERVED ', 1)[1])
        self.assertEqual(record['source'], 'BROKER_GET_ORDER')
        self.assertEqual(record['broker_quantity'], '5')
        self.assertEqual(record['broker_stop_price'], '9')
        self.assertEqual(record['filled_quantity'], 0)
        execution.submit_exit.assert_not_called()

    def test_disabled_extended_management_checks_protection_without_quote_or_sale(self):
        execution, market, recovery = Mock(), Mock(), Mock()
        execution.broker.market_is_open.return_value = False
        recovery.recover_all.return_value = {'safe_to_trade': True}
        with (patch.object(watcher, 'database', self.db),
              patch.object(watcher, 'settings', replace(watcher.settings, EXTENDED_EXIT_ENABLED=False)),
              patch.object(watcher, 'get_execution_engine', return_value=execution),
              patch.object(watcher, 'get_market_data', return_value=market),
              patch.object(watcher, 'get_recovery_engine', return_value=recovery),
              patch.object(watcher, 'auto_paper_execution_ready', return_value=True),
              patch.object(watcher, 'notify_recovered_protective_stop_fills'),
              patch.object(watcher, '_sync_protective_stop', return_value={}) as sync,
              patch.object(watcher, 'checkpoint')):
            self.assertEqual(watcher.manage_active_paper_trades({}), 1)
        sync.assert_called_once()
        market.get_execution_quote.assert_not_called()
        market.get_trade_range.assert_not_called()
        execution.broker.get_clock.assert_not_called()
        execution.submit_exit.assert_not_called()
        self.assertFalse(self.db.load_managed_trade('t').t1_completed)

    def test_stop_inspection_precedes_a_failed_optional_diagnostic_write(self):
        execution = Mock()
        calls = []
        def fail(*args):
            calls.append('write')
            raise sqlite3.OperationalError('database is locked')
        with (patch.object(watcher, '_sync_protective_stop', side_effect=lambda *a: calls.append('stop')),
              patch.object(watcher.database, 'save_managed_trade', side_effect=fail)):
            with self.assertRaises(sqlite3.OperationalError):
                watcher._defer_extended_exit('t', self.trade, execution, 'SUBSCRIPTION_REQUIRED')
        self.assertEqual(calls, ['stop', 'write'])


class RegularSessionDefaultTests(unittest.TestCase):
    def test_default_config_and_status_disable_extended_without_requesting_quotes(self):
        env = dict(os.environ)
        env.pop('JALWE_EXTENDED_EXIT_ENABLED', None)
        result = subprocess.run([sys.executable, '-c',
            'from core.config import settings; print(settings.EXTENDED_EXIT_ENABLED)'],
            env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'False')
        with patch.dict(os.environ, {}, clear=True), patch.object(controller, 'database') as db:
            self.assertIn('معطّل بالإعدادات', controller.extended_exit_status_text())
            db.connection.assert_not_called()
