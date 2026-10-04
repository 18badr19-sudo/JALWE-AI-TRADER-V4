from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from alpaca.trading.enums import OrderSide, TimeInForce
from broker.alpaca_client import AlpacaClient
from broker.entry_pricing import protected_entry_limit
from core.config import settings
from core.database import Database
from trading.execution_engine import ExecutionEngine
from trading.paper_trade_orchestrator import PaperOrchestratorState, PaperTradeOrchestrator
from trading.recovery_engine import RecoveryEngine
import trading.paper_trade_orchestrator as orchestrator_module
import trading.recovery_engine as recovery_module
import test_paper_safety as safety_helpers


class EntrySubmissionPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "entries.db"))
        self.broker = AlpacaClient.__new__(AlpacaClient)
        self.broker.client = Mock()
        self.broker.client.get_clock.return_value = NS(is_open=True)
        self.broker.client.get_all_positions.return_value = []

    def intent(self, metadata=None):
        self.db.create_entry_intent(
            "i", "JALWE-FINAL-phase-test", "TEST", 1, 10, 9.5, 11, 11.5, 12, 1,
            metadata=metadata,
        )
        return self.db.get_entry_intent_row("i")

    def prepared(self):
        return self.intent({"entry_submission_state": "PREPARED", "keep": "audit"})

    def phase(self, intent_id):
        return json.loads(self.db.get_entry_intent_row(intent_id)["metadata_json"])

    def orchestrator(self):
        decision = safety_helpers.ready_decision()
        orchestrator = safety_helpers.PaperMarketOpenGateTests()._orchestrator(decision)
        orchestrator._create_entry_intent = PaperTradeOrchestrator._create_entry_intent.__get__(
            orchestrator, PaperTradeOrchestrator
        )
        execution = ExecutionEngine.__new__(ExecutionEngine)
        execution.broker = self.broker
        orchestrator.execution_engine = execution
        return orchestrator

    def run_entry(self, orchestrator):
        # Outer preflight passes; all SDK operations remain mocks.
        outer = NS(market_is_open=Mock(return_value=True))
        with patch.object(orchestrator_module, "database", self.db), patch.object(
            orchestrator_module, "get_alpaca_client", return_value=outer
        ):
            return orchestrator.run_symbol("TEST")

    def assert_never_submitted(self, result):
        self.assertEqual(result.state, PaperOrchestratorState.ENTRY_TERMINAL_NO_FILL)
        self.assertFalse(result.metadata["broker_order_submitted"])
        self.assertEqual(self.db.get_entry_intent_row(result.intent_id)["state"], "FAILED")
        self.assertEqual(self.db.get_unresolved_entry_intent_rows(), [])
        self.broker.client.submit_order.assert_not_called()

    def test_market_closes_between_outer_and_inner_gate_does_not_deadlock(self):
        self.broker.client.get_clock.return_value = NS(is_open=False)
        result = self.run_entry(self.orchestrator())
        self.assert_never_submitted(result)
        self.broker.client.get_all_positions.assert_not_called()

    def test_position_read_failure_before_post_does_not_deadlock(self):
        self.broker.client.get_all_positions.side_effect = RuntimeError("503 unavailable")
        self.assert_never_submitted(self.run_entry(self.orchestrator()))

    def test_duplicate_position_preflight_does_not_deadlock(self):
        self.broker.client.get_all_positions.return_value = [NS(symbol="TEST")]
        self.assert_never_submitted(self.run_entry(self.orchestrator()))

    def test_invalid_limit_request_is_rejected_before_phase_changes(self):
        with patch("broker.alpaca_client.LimitOrderRequest", side_effect=ValueError("invalid request")):
            result = self.run_entry(self.orchestrator())
        self.assert_never_submitted(result)
        self.assertEqual(self.phase(result.intent_id)["entry_submission_state"], "PREPARED")

    def test_phase_persistence_failure_prevents_sdk_post(self):
        with patch.object(self.db, "mark_entry_submission_started", side_effect=RuntimeError("disk full")):
            result = self.run_entry(self.orchestrator())
        self.assert_never_submitted(result)

    def test_ambiguous_phase_commit_failure_is_not_cleared(self):
        original = self.db.mark_entry_submission_started

        def commit_then_fail(intent_id):
            original(intent_id)
            raise RuntimeError("connection failed after commit")

        with patch.object(self.db, "mark_entry_submission_started", side_effect=commit_then_fail):
            result = self.run_entry(self.orchestrator())
        self.assertEqual(result.state, PaperOrchestratorState.ENTRY_RECOVERY_REQUIRED)
        self.assertEqual(self.phase(result.intent_id)["entry_submission_state"], "SUBMITTING")
        self.assertEqual(self.db.get_entry_intent_row(result.intent_id)["state"], "ERROR")
        self.broker.client.submit_order.assert_not_called()

    def test_timeout_after_post_preserves_client_identity_and_blocks_replacement(self):
        def lost_response(order_data):
            row = self.db.get_unresolved_entry_intent_rows()[0]
            self.assertEqual(row["client_order_id"], order_data.client_order_id)
            self.assertEqual(self.phase(row["intent_id"])["entry_submission_state"], "SUBMITTING")
            self.assertEqual(order_data.side, OrderSide.BUY)
            self.assertEqual(order_data.time_in_force, TimeInForce.DAY)
            self.assertEqual(order_data.limit_price, protected_entry_limit(10, settings.MAX_ENTRY_SLIPPAGE_PCT))
            raise TimeoutError("broker response lost")

        self.broker.client.submit_order.side_effect = lost_response
        result = self.run_entry(self.orchestrator())
        self.assertEqual(result.state, PaperOrchestratorState.ENTRY_RECOVERY_REQUIRED)
        self.assertEqual(self.db.get_entry_intent_row(result.intent_id)["state"], "ERROR")
        self.assertEqual(len(self.db.get_unresolved_entry_intent_rows()), 1)
        self.assertFalse(self.db.resolve_unsubmitted_entry(result.intent_id))
        self.broker.client.submit_order.assert_called_once()

    def recovery(self):
        engine = RecoveryEngine.__new__(RecoveryEngine)
        engine.broker = Mock()
        engine._lookup_entry_order = Mock(side_effect=RuntimeError("order not found"))
        return engine

    def test_restart_after_prepare_releases_without_broker_lookup_or_order(self):
        intent = self.prepared()
        # A fresh database instance reads the committed phase after restart.
        reopened = Database(str(self.db.database_path))
        engine = self.recovery()
        with patch.object(recovery_module, "database", reopened):
            result = engine.recover_entry_intent(intent)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["state_after"], "FAILED")
        self.assertEqual(reopened.get_unresolved_entry_intent_rows(), [])
        engine._lookup_entry_order.assert_not_called()
        engine.broker.cancel_order.assert_not_called()
        engine.broker.submit_protected_entry.assert_not_called()

    def test_legacy_prepared_without_marker_stays_uncertain_on_not_found(self):
        intent = self.intent()
        engine = self.recovery()
        with patch.object(recovery_module, "database", self.db):
            result = engine.recover_entry_intent(intent)
        self.assertFalse(result["resolved"])
        self.assertEqual(result["state_after"], "ERROR")
        engine._lookup_entry_order.assert_called_once_with(intent)

    def test_stale_prepared_snapshot_cannot_clear_committed_submission(self):
        stale = self.prepared()
        self.db.mark_entry_submission_started("i")
        engine = self.recovery()
        with patch.object(recovery_module, "database", self.db):
            result = engine.recover_entry_intent(stale)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.phase("i")["entry_submission_state"], "SUBMITTING")
        engine._lookup_entry_order.assert_called_once()

    def test_phase_cannot_be_submitted_twice_or_cleared_after_submit_starts(self):
        self.prepared()
        self.db.mark_entry_submission_started("i")
        self.assertEqual(self.phase("i")["keep"], "audit")
        with self.assertRaisesRegex(RuntimeError, "not safe to submit"):
            self.db.mark_entry_submission_started("i")
        self.assertFalse(self.db.resolve_unsubmitted_entry("i"))

    def test_prepared_marker_with_broker_id_or_fill_cannot_be_released(self):
        self.prepared()
        for column, value in (("broker_order_id", "accepted-order"), ("filled_quantity", 1)):
            with self.subTest(column=column):
                with self.db.connection() as conn:
                    conn.execute(
                        "UPDATE entry_intents SET broker_order_id = NULL, filled_quantity = 0 WHERE intent_id = 'i'"
                    )
                    conn.execute(f"UPDATE entry_intents SET {column} = ? WHERE intent_id = 'i'", (value,))
                self.assertFalse(self.db.resolve_unsubmitted_entry("i"))


if __name__ == "__main__":
    unittest.main()
