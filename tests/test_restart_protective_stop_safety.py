from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import jalwe_research_watcher as watcher

from core.database import Database
from core.models import (
    BrokerOrder,
    OrderStatus,
    TradeSide,
)
from trading.trade_manager import (
    TradeAction,
    TradeManager,
    TradeStage,
)


class RestartAndProtectiveStopSafetyTests(
    unittest.TestCase
):

    @staticmethod
    def _fill_exit(
        manager: TradeManager,
        trade,
        decision,
        fill_price: float,
        order_id: str,
    ) -> None:
        order = BrokerOrder(
            symbol=trade.symbol,
            side=TradeSide.SELL,
            quantity=decision.quantity,
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_price=fill_price,
            filled_quantity=decision.quantity,
        )

        manager.register_exit_order(
            trade,
            decision,
            order,
        )

        manager.apply_exit_reconciliation(
            trade,
            order,
        )

    def test_runner_state_survives_database_restart(
        self,
    ) -> None:
        manager = TradeManager(
            trailing_distance_pct=20.0
        )

        trade = manager.create_trade(
            symbol="CANE",
            entry_price=10.0,
            quantity=8,
            stop_price=9.5,
            target_1=11.0,
            target_2=12.0,
            target_3=13.0,
        )

        t1 = manager.evaluate(
            trade,
            current_price=11.1,
        )
        self._fill_exit(
            manager,
            trade,
            t1,
            11.1,
            "RESTART-T1",
        )

        t2 = manager.evaluate(
            trade,
            current_price=12.1,
        )
        self._fill_exit(
            manager,
            trade,
            t2,
            12.1,
            "RESTART-T2",
        )

        t3 = manager.evaluate(
            trade,
            current_price=13.1,
        )
        self._fill_exit(
            manager,
            trade,
            t3,
            13.1,
            "RESTART-T3",
        )

        runner = manager.evaluate(
            trade,
            current_price=15.1,
            observed_high=15.2,
        )

        self.assertEqual(
            runner.action,
            TradeAction.HOLD,
        )

        trade.metadata.update(
            {
                "management_trade_scan_at":
                    "2026-10-02T20:00:00+00:00",
                "management_trade_scan_count": 9,
                "protective_stop_order_id":
                    "RESTART-PROTECTIVE",
                "protective_stop_quantity": 2,
                "protective_stop_price": 14.0,
                "protective_stop_status": "accepted",
                "protective_stop_active": True,
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(
                Path(temp_dir)
                / "restart_safety.db"
            )

            before_restart = Database(
                db_path
            )

            before_restart.save_managed_trade(
                "TRADE-RESTART-CANE",
                trade,
                entry_order_id="ENTRY-CANE",
                entry_fill_price=10.0,
            )

            # New Database object simulates a fresh process
            # loading state from the persisted Railway volume.
            after_restart = Database(
                db_path
            )

            restored = (
                after_restart
                .load_managed_trade(
                    "TRADE-RESTART-CANE"
                )
            )

        self.assertIsNotNone(
            restored
        )

        self.assertEqual(
            restored.stage,
            TradeStage.RUNNER,
        )
        self.assertTrue(
            restored.t1_completed
        )
        self.assertTrue(
            restored.t2_completed
        )
        self.assertTrue(
            restored.t3_completed
        )
        self.assertTrue(
            restored.trailing_active
        )
        self.assertEqual(
            restored.remaining_quantity,
            2,
        )
        self.assertAlmostEqual(
            restored.current_stop,
            14.0,
        )
        self.assertAlmostEqual(
            restored.highest_price,
            15.2,
        )
        self.assertAlmostEqual(
            restored.metadata[
                "profit_lock_floor"
            ],
            14.0,
        )
        self.assertAlmostEqual(
            restored.metadata[
                "dynamic_target_last_price"
            ],
            15.0,
        )
        self.assertAlmostEqual(
            restored.metadata[
                "dynamic_target_next_price"
            ],
            16.0,
        )
        self.assertEqual(
            restored.metadata[
                "management_trade_scan_count"
            ],
            9,
        )
        self.assertEqual(
            restored.metadata[
                "protective_stop_order_id"
            ],
            "RESTART-PROTECTIVE",
        )
        self.assertEqual(
            restored.metadata[
                "protective_stop_quantity"
            ],
            2,
        )
        self.assertAlmostEqual(
            restored.metadata[
                "protective_stop_price"
            ],
            14.0,
        )

        resumed = manager.evaluate(
            restored,
            current_price=15.5,
        )

        self.assertEqual(
            resumed.action,
            TradeAction.HOLD,
        )
        self.assertEqual(
            restored.stage,
            TradeStage.RUNNER,
        )
        self.assertAlmostEqual(
            restored.current_stop,
            14.0,
        )

    def test_t1_profit_lock_replaces_old_protective_stop(
        self,
    ) -> None:
        manager = TradeManager()

        trade = manager.create_trade(
            symbol="CANE",
            entry_price=11.66,
            quantity=8,
            stop_price=11.58,
            target_1=11.79,
            target_2=11.92,
            target_3=12.05,
        )

        t1 = manager.evaluate(
            trade,
            current_price=11.80,
            observed_high=11.81,
        )

        self._fill_exit(
            manager,
            trade,
            t1,
            11.80,
            "CANE-T1-SYNC",
        )

        trade.metadata.update(
            {
                "protective_stop_order_id":
                    "OLD-CANE-STOP",
                "protective_stop_client_order_id":
                    "JALWE-STOP-OLD",
                "protective_stop_quantity": 8,
                "protective_stop_price": 11.58,
                "protective_stop_status": "accepted",
                "protective_stop_active": True,
                "protective_stop_fill_applied": False,
                "protective_stop_applied_qty": 0,
                "protective_stop_applied_notional": 0.0,
            }
        )

        new_stop = BrokerOrder(
            symbol="CANE",
            side=TradeSide.SELL,
            quantity=5,
            order_id="NEW-CANE-STOP",
            client_order_id="JALWE-STOP-NEW",
            status=OrderStatus.ACCEPTED,
            requested_price=11.79,
            filled_quantity=0,
        )

        broker = SimpleNamespace(
            get_order=Mock(
                side_effect=[
                    SimpleNamespace(
                        status="new"
                    ),
                    SimpleNamespace(
                        status="new"
                    ),
                    SimpleNamespace(
                        status="canceled"
                    ),
                ]
            )
        )

        execution_engine = SimpleNamespace(
            broker=broker,
            cancel_protective_stop=Mock(),
            submit_protective_stop=Mock(
                return_value=new_stop
            ),
        )

        fake_database = Mock()

        with (
            patch.object(
                watcher,
                "settings",
                SimpleNamespace(
                    BROKER_PROTECTIVE_STOP_ENABLED=True
                ),
            ),
            patch.object(
                watcher,
                "database",
                fake_database,
            ),
            patch.object(
                watcher.time,
                "sleep",
                return_value=None,
            ),
        ):
            result = (
                watcher
                ._sync_protective_stop(
                    "TRADE-CANE",
                    trade,
                    execution_engine,
                )
            )

        execution_engine.cancel_protective_stop.assert_called_once_with(
            "OLD-CANE-STOP"
        )
        execution_engine.submit_protective_stop.assert_called_once_with(
            symbol="CANE",
            quantity=5,
            stop_price=11.79,
            client_order_id=ANY,
        )

        self.assertTrue(
            result["changed"]
        )
        self.assertEqual(
            result["order_id"],
            "NEW-CANE-STOP",
        )
        self.assertEqual(
            result["quantity"],
            5,
        )
        self.assertAlmostEqual(
            result["stop_price"],
            11.79,
        )

        self.assertEqual(
            trade.metadata[
                "protective_stop_order_id"
            ],
            "NEW-CANE-STOP",
        )
        self.assertEqual(
            trade.metadata[
                "protective_stop_quantity"
            ],
            5,
        )
        self.assertAlmostEqual(
            trade.metadata[
                "protective_stop_price"
            ],
            11.79,
        )
        self.assertTrue(
            trade.metadata[
                "protective_stop_active"
            ]
        )

        fake_database.save_broker_order.assert_called_once_with(
            new_stop
        )
        fake_database.save_managed_trade.assert_called()

    def test_filled_protective_stop_wins_race_without_second_sell(
        self,
    ) -> None:
        manager = TradeManager()

        trade = manager.create_trade(
            symbol="CANE",
            entry_price=11.66,
            quantity=5,
            stop_price=11.58,
            target_1=11.79,
            target_2=11.92,
            target_3=12.05,
        )

        trade.metadata.update(
            {
                "protective_stop_order_id":
                    "FILLED-CANE-STOP",
                "protective_stop_quantity": 5,
                "protective_stop_price": 11.58,
                "protective_stop_status": "accepted",
                "protective_stop_active": True,
            }
        )

        execution_engine = SimpleNamespace(
            broker=SimpleNamespace(
                get_order=Mock(
                    return_value=SimpleNamespace(
                        status="filled"
                    )
                )
            ),
            cancel_protective_stop=Mock(),
            submit_protective_stop=Mock(),
        )

        fake_database = Mock()

        with (
            patch.object(
                watcher,
                "settings",
                SimpleNamespace(
                    BROKER_PROTECTIVE_STOP_ENABLED=True
                ),
            ),
            patch.object(
                watcher,
                "database",
                fake_database,
            ),
        ):
            result = (
                watcher
                ._sync_protective_stop(
                    "TRADE-CANE-FILLED",
                    trade,
                    execution_engine,
                )
            )

        self.assertTrue(
            result["filled"]
        )
        self.assertFalse(
            result["active"]
        )
        execution_engine.cancel_protective_stop.assert_not_called()
        execution_engine.submit_protective_stop.assert_not_called()
        self.assertFalse(
            trade.metadata[
                "protective_stop_active"
            ]
        )


if __name__ == "__main__":
    unittest.main()
