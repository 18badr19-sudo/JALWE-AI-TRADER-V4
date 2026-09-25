from __future__ import annotations

import sys
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[1]
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(PROJECT_ROOT),
    )


from alpaca.trading.enums import OrderSide, TimeInForce

from broker.alpaca_client import AlpacaClient
from core.models import OrderStatus
from intelligence.decision_engine import (
    DecisionState,
    FinalTradeDecision,
)
from trading.execution_engine import ExecutionEngine
from trading.paper_trade_orchestrator import (
    PaperOrchestratorState,
    PaperTradeOrchestrator,
)


def ready_decision(
    symbol: str = "TEST",
) -> FinalTradeDecision:
    return FinalTradeDecision(
        symbol=symbol,
        state=(
            DecisionState
            .READY_FOR_PAPER_EXECUTION
        ),
        ready_for_execution=True,
        market_regime="BULL_TREND",
        strategy="COMPRESSION_BREAKOUT",
        setup_grade="A",
        opportunity_score=90.0,
        session_strategy_score=90.0,
        breakout_score=90.0,
        ai_score=85.0,
        risk_pct=1.0,
        quantity=1,
        entry_price=10.0,
        stop_price=9.5,
        target_1=11.0,
        target_2=11.5,
        target_3=12.0,
        gates={
            "market_data": True,
            "features": True,
            "ai": True,
            "opportunity": True,
            "market_regime": True,
            "strategy_router": True,
            "session_strategy": True,
            "trigger": True,
            "breakout_confirmation": True,
            "risk": True,
        },
    )


class PaperMarketOpenGateTests(
    unittest.TestCase
):
    def _orchestrator(
        self,
        decision: FinalTradeDecision,
    ) -> PaperTradeOrchestrator:
        orchestrator = (
            PaperTradeOrchestrator
            .__new__(
                PaperTradeOrchestrator
            )
        )

        orchestrator.auto_execution = True
        orchestrator.broker_submission_enabled = True
        orchestrator.entry_timeout_seconds = 1

        orchestrator.run_startup_recovery = Mock(
            return_value={
                "safe_to_trade": True,
            }
        )

        orchestrator.analyze_symbol = Mock(
            return_value=decision
        )

        orchestrator._config_execution_ready = Mock(
            return_value=True
        )

        orchestrator._create_entry_intent = Mock(
            side_effect=AssertionError(
                "EntryIntent must not be created "
                "while market is closed."
            )
        )

        orchestrator.execution_engine = (
            SimpleNamespace(
                submit_final_decision=Mock(
                    side_effect=AssertionError(
                        "BUY must not be submitted "
                        "while market is closed."
                    )
                )
            )
        )

        return orchestrator

    def test_market_closed_blocks_before_intent(
        self,
    ) -> None:
        decision = ready_decision()

        orchestrator = (
            self._orchestrator(
                decision
            )
        )

        fake_broker = SimpleNamespace(
            market_is_open=Mock(
                return_value=False
            )
        )

        with patch(
            "trading.paper_trade_orchestrator."
            "get_alpaca_client",
            return_value=fake_broker,
        ):
            result = orchestrator.run_symbol(
                "TEST"
            )

        self.assertEqual(
            result.state,
            PaperOrchestratorState
            .READY_MARKET_CLOSED,
        )

        self.assertIsNotNone(
            result.decision
        )

        self.assertEqual(
            result.decision.state,
            DecisionState.WATCHING,
        )

        self.assertFalse(
            result.decision
            .ready_for_execution
        )

        self.assertFalse(
            result.metadata[
                "broker_order_submitted"
            ]
        )

        self.assertFalse(
            result.metadata[
                "entry_intent_created"
            ]
        )

        orchestrator._create_entry_intent            .assert_not_called()

        orchestrator.execution_engine            .submit_final_decision            .assert_not_called()

    def test_market_clock_failure_blocks_buy(
        self,
    ) -> None:
        decision = ready_decision()

        orchestrator = (
            self._orchestrator(
                decision
            )
        )

        fake_broker = SimpleNamespace(
            market_is_open=Mock(
                side_effect=RuntimeError(
                    "clock unavailable"
                )
            )
        )

        with patch(
            "trading.paper_trade_orchestrator."
            "get_alpaca_client",
            return_value=fake_broker,
        ):
            result = orchestrator.run_symbol(
                "TEST"
            )

        self.assertEqual(
            result.state,
            PaperOrchestratorState
            .READY_MARKET_CLOSED,
        )

        self.assertIsNone(
            result.metadata[
                "market_open"
            ]
        )

        self.assertFalse(
            result.metadata[
                "broker_order_submitted"
            ]
        )

        orchestrator._create_entry_intent            .assert_not_called()


class ProtectiveStopTests(
    unittest.TestCase
):
    def test_alpaca_stop_is_sell_gtc_and_rounded(
        self,
    ) -> None:
        captured = {}

        def submit_order(
            *,
            order_data,
        ):
            captured[
                "request"
            ] = order_data

            return SimpleNamespace(
                id="STOP-1",
                client_order_id=(
                    "JALWE-STOP-TEST"
                ),
                status="accepted",
                filled_avg_price=None,
                filled_qty=0,
            )

        alpaca = AlpacaClient.__new__(
            AlpacaClient
        )

        alpaca.client = SimpleNamespace(
            submit_order=submit_order
        )

        result = alpaca.submit_stop_order(
            symbol="test",
            quantity=3,
            stop_price=9.876,
            client_order_id=(
                "JALWE-STOP-TEST"
            ),
        )

        request = captured[
            "request"
        ]

        self.assertEqual(
            request.symbol,
            "TEST",
        )

        self.assertEqual(
            request.qty,
            3,
        )

        self.assertEqual(
            request.side,
            OrderSide.SELL,
        )

        self.assertEqual(
            request.time_in_force,
            TimeInForce.GTC,
        )

        self.assertEqual(
            float(
                request.stop_price
            ),
            9.88,
        )

        self.assertEqual(
            result.id,
            "STOP-1",
        )

    def test_execution_protective_stop_uses_existing_position(
        self,
    ) -> None:
        raw_order = SimpleNamespace(
            id="STOP-2",
            client_order_id=(
                "JALWE-STOP-UNIT"
            ),
            status="accepted",
            filled_avg_price=None,
            filled_qty=0,
        )

        fake_broker = SimpleNamespace(
            get_position=Mock(
                return_value=SimpleNamespace(
                    qty="5"
                )
            ),
            submit_stop_order=Mock(
                return_value=raw_order
            ),
        )

        engine = ExecutionEngine.__new__(
            ExecutionEngine
        )

        engine.broker = fake_broker

        order = (
            engine
            .submit_protective_stop(
                symbol="TEST",
                quantity=5,
                stop_price=9.5,
                client_order_id=(
                    "JALWE-STOP-UNIT"
                ),
            )
        )

        fake_broker.submit_stop_order            .assert_called_once_with(
                symbol="TEST",
                quantity=5,
                stop_price=9.5,
                client_order_id=(
                    "JALWE-STOP-UNIT"
                ),
            )

        self.assertEqual(
            order.status,
            OrderStatus.ACCEPTED,
        )

        self.assertEqual(
            order.quantity,
            5,
        )

        self.assertEqual(
            order.metadata[
                "order_role"
            ],
            "PROTECTIVE_STOP",
        )

        self.assertEqual(
            order.metadata[
                "time_in_force"
            ],
            "GTC",
        )


if __name__ == "__main__":
    unittest.main()
