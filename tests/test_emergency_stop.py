from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jalwe_research_watcher as watcher


class EmergencyStopRaceTests(unittest.TestCase):
    def test_filled_protective_stop_prevents_second_sell(self) -> None:
        trade = SimpleNamespace(
            has_pending_exit=False,
            remaining_quantity=3,
            symbol="TEST",
            stage=SimpleNamespace(value="ACTIVE"),
            current_stop=9.0,
            metadata={},
        )
        database = Mock()
        database.load_active_managed_trades.return_value = {
            "test-trade": trade,
        }
        execution = Mock()

        with (
            patch.object(watcher, "emergency_close_requested", return_value=True),
            patch.object(watcher, "auto_paper_execution_ready", return_value=True),
            patch.object(watcher, "get_recovery_engine") as recovery,
            patch.object(watcher, "database", database),
            patch.object(watcher, "get_market_data") as market_data,
            patch.object(watcher, "get_trade_manager"),
            patch.object(watcher, "get_execution_engine", return_value=execution),
            patch.object(watcher, "get_reconciliation_engine"),
            patch.object(watcher, "_cancel_protective_stop", return_value="FILLED") as cancel,
        ):
            recovery.return_value.recover_all.return_value = {
                "safe_to_trade": True,
            }
            market_data.return_value.get_last_price.return_value = 10.0

            processed = watcher.process_emergency_paper_close()

        self.assertEqual(processed, 0)
        cancel.assert_called_once_with("test-trade", trade, execution)
        execution.submit_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
