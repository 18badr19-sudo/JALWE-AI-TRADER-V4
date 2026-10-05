from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from alpaca.data.enums import DataFeed

from core.models import (
    BrokerOrder,
    OrderStatus,
    TradeSide,
)
from market.market_data import MarketData
from trading.trade_manager import (
    TradeAction,
    TradeManager,
)


class PaperTargetTouchTests(
    unittest.TestCase
):

    def test_actual_cane_target_waits_for_confirmed_fill_before_raising_stop(self):
        manager = TradeManager()
        trade = manager.create_trade('CANE', 11.66, 8, 11.58, 11.82, 11.90, 11.98)
        self.assertNotEqual(manager.evaluate(trade, 11.815).action, TradeAction.TAKE_PROFIT_1)
        decision = manager.evaluate(trade, 11.82)
        self.assertEqual(decision.action, TradeAction.TAKE_PROFIT_1)
        self.assertEqual(decision.quantity, 3)
        self.assertEqual(trade.remaining_quantity, 8)
        self.assertAlmostEqual(trade.current_stop, 11.58)
        self._fill_exit(manager, trade, decision, 11.82, 'CANE-T1')
        self.assertEqual(trade.remaining_quantity, 5)
        self.assertTrue(trade.t1_completed)
        self.assertAlmostEqual(trade.current_stop, 11.82)

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

    def test_cane_t1_uses_observed_high_between_polls(
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

        decision = manager.evaluate(
            trade,
            current_price=11.76,
            observed_high=11.81,
        )

        self.assertEqual(
            decision.action,
            TradeAction.TAKE_PROFIT_1,
        )

        self.assertEqual(
            decision.quantity,
            3,
        )

        self.assertAlmostEqual(
            trade.highest_price,
            11.81,
        )

    def test_high_water_carries_t2_t3_and_ratchets_protection(
        self,
    ) -> None:
        manager = TradeManager()

        trade = manager.create_trade(
            symbol="TEST",
            entry_price=10.0,
            quantity=8,
            stop_price=9.5,
            target_1=11.0,
            target_2=12.0,
            target_3=13.0,
        )

        t1 = manager.evaluate(
            trade,
            current_price=10.8,
            observed_high=13.2,
        )

        self.assertEqual(
            t1.action,
            TradeAction.TAKE_PROFIT_1,
        )

        self._fill_exit(
            manager,
            trade,
            t1,
            11.0,
            "T1-ORDER",
        )

        self.assertTrue(
            trade.t1_completed
        )
        self.assertEqual(
            trade.remaining_quantity,
            5,
        )
        self.assertAlmostEqual(
            trade.current_stop,
            11.0,
        )

        t2 = manager.evaluate(
            trade,
            current_price=11.5,
        )

        self.assertEqual(
            t2.action,
            TradeAction.TAKE_PROFIT_2,
        )

        self._fill_exit(
            manager,
            trade,
            t2,
            12.0,
            "T2-ORDER",
        )

        self.assertTrue(
            trade.t2_completed
        )
        self.assertEqual(
            trade.remaining_quantity,
            3,
        )
        self.assertAlmostEqual(
            trade.current_stop,
            11.0,
        )

        t3 = manager.evaluate(
            trade,
            current_price=12.5,
        )

        self.assertEqual(
            t3.action,
            TradeAction.TAKE_PROFIT_3,
        )

        self._fill_exit(
            manager,
            trade,
            t3,
            13.0,
            "T3-ORDER",
        )

        self.assertTrue(
            trade.t3_completed
        )
        self.assertEqual(
            trade.remaining_quantity,
            2,
        )
        self.assertAlmostEqual(
            trade.current_stop,
            12.0,
        )
        self.assertTrue(
            trade.trailing_active
        )

    def test_t1_fill_locks_remaining_cane_position_at_t1(
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
            "CANE-T1",
        )

        self.assertTrue(
            trade.t1_completed
        )
        self.assertEqual(
            trade.remaining_quantity,
            5,
        )
        self.assertAlmostEqual(
            trade.current_stop,
            11.79,
        )
        self.assertAlmostEqual(
            trade.metadata[
                "profit_lock_floor"
            ],
            11.79,
        )

        stop_decision = manager.evaluate(
            trade,
            current_price=11.78,
        )

        self.assertEqual(
            stop_decision.action,
            TradeAction.EXIT_STOP,
        )
        self.assertEqual(
            stop_decision.quantity,
            5,
        )

    def test_runner_builds_dynamic_targets_beyond_t3(
        self,
    ) -> None:
        manager = TradeManager(
            trailing_distance_pct=20.0
        )

        trade = manager.create_trade(
            symbol="TEST",
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
            "DYN-T1",
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
            "DYN-T2",
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
            "DYN-T3",
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
        self.assertTrue(
            trade.trailing_active
        )
        self.assertAlmostEqual(
            trade.metadata[
                "dynamic_target_last_price"
            ],
            15.0,
        )
        self.assertAlmostEqual(
            trade.metadata[
                "dynamic_target_next_price"
            ],
            16.0,
        )
        self.assertAlmostEqual(
            trade.current_stop,
            14.0,
        )

    def test_trade_range_preserves_transient_high(
        self,
    ) -> None:
        market_data = MarketData.__new__(
            MarketData
        )

        market_data.feed = DataFeed.IEX
        market_data.client = SimpleNamespace(
            get_stock_trades=Mock(
                return_value={
                    "CANE": [
                        SimpleNamespace(
                            price=11.76
                        ),
                        SimpleNamespace(
                            price=11.81
                        ),
                        SimpleNamespace(
                            price=11.77
                        ),
                    ]
                }
            )
        )

        end = datetime.now(
            timezone.utc
        )
        start = (
            end
            - timedelta(
                seconds=20
            )
        )

        observed = (
            market_data
            .get_trade_range(
                symbol="CANE",
                start=start,
                end=end,
            )
        )

        self.assertEqual(
            observed["count"],
            3,
        )
        self.assertAlmostEqual(
            observed["high"],
            11.81,
        )
        self.assertAlmostEqual(
            observed["last"],
            11.77,
        )
        self.assertEqual(
            observed["feed"],
            "iex",
        )


if __name__ == "__main__":
    unittest.main()
