from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from broker.alpaca_client import AlpacaClient
from core.database import Database
from trading.recovery_engine import RecoveryEngine
from trading.reconciliation_engine import ReconciliationEngine
from trading.trade_manager import TradeManager
import trading.recovery_engine as recovery_module


class RecoveryBrokerRetryTests(unittest.TestCase):
    def _engine(self) -> RecoveryEngine:
        return object.__new__(RecoveryEngine)

    def test_transient_broker_read_retries_then_succeeds(self):
        engine = self._engine()
        operation = Mock(
            side_effect=[
                RuntimeError("500 Server Error: Internal Server Error"),
                RuntimeError('{"code":50010000,"message":"internal server error occurred"}'),
                ["ok"],
            ]
        )

        with patch("trading.recovery_engine.time.sleep", return_value=None) as sleeper:
            result = engine._broker_read_with_retry(
                operation,
                label="unit-test",
            )

        self.assertEqual(result, ["ok"])
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_non_transient_broker_read_fails_immediately(self):
        engine = self._engine()
        operation = Mock(side_effect=RuntimeError("authentication failed"))

        with patch("trading.recovery_engine.time.sleep", return_value=None) as sleeper:
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                engine._broker_read_with_retry(
                    operation,
                    label="unit-test",
                )

        self.assertEqual(operation.call_count, 1)
        sleeper.assert_not_called()

    def recover_with_position_reads(self, reads):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        database = Database(str(Path(temp.name) / "recovery.db"))
        database.save_managed_trade("t", TradeManager().create_trade("TEST", 10, 8, 9, 11, 12, 13))
        broker = AlpacaClient.__new__(AlpacaClient)
        broker.client = Mock()
        position = NS(symbol="TEST", qty="8", avg_entry_price="10", market_value="80", unrealized_pl="0")
        broker.client.get_all_positions.side_effect = [*reads, [position]]
        broker.client.get_orders.return_value = []
        reconciliation = ReconciliationEngine.__new__(ReconciliationEngine)
        reconciliation.broker = broker
        engine = self._engine()
        engine.broker = broker
        engine.reconciliation = reconciliation
        engine.trade_manager = TradeManager()
        with patch.object(recovery_module, "database", database), patch.object(recovery_module.time, "sleep"):
            result = engine.recover_all()
        broker.client.submit_order.assert_not_called()
        return result, broker

    def test_transient_per_trade_position_read_recovers_without_blocking_management(self):
        position = NS(symbol="TEST", qty="8", avg_entry_price="10", market_value="80", unrealized_pl="0")
        result, broker = self.recover_with_position_reads([
            RuntimeError("503 service unavailable"), [position],
        ])
        self.assertTrue(result["safe_to_trade"])
        self.assertTrue(result["results"][0]["recovered"])
        self.assertEqual(broker.client.get_all_positions.call_count, 3)

    def test_exhausted_per_trade_read_stays_unsafe_even_if_later_global_read_succeeds(self):
        result, broker = self.recover_with_position_reads([RuntimeError("503 service unavailable")] * 3)
        self.assertFalse(result["safe_to_trade"])
        self.assertEqual(result["results"][0]["warning"], "BROKER_POSITION_UNAVAILABLE")
        self.assertEqual(result["broker_position_count"], 1)
        self.assertEqual(broker.client.get_all_positions.call_count, 4)

    def test_per_trade_authentication_failure_is_not_retried(self):
        result, broker = self.recover_with_position_reads([RuntimeError("authentication failed")])
        self.assertFalse(result["safe_to_trade"])
        self.assertEqual(result["results"][0]["warning"], "BROKER_POSITION_UNAVAILABLE")
        self.assertEqual(broker.client.get_all_positions.call_count, 2)


if __name__ == "__main__":
    unittest.main()
