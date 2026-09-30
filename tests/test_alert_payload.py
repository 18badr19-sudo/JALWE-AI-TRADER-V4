"""Exercise actual payload and formatter functions without broker/network setup."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import unittest


class AlertPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "jalwe_research_watcher.py"
        tree = ast.parse(source.read_text())
        names = {"utc_now_iso", "safe_dict", "safe_list", "safe_float", "format_number",
                 "decision_state_value", "build_decision_payload", "build_telegram_message"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        cls.ns = {"Any": Any, "Optional": Optional, "datetime": datetime,
                  "timezone": timezone, "ORDER_EXECUTION_ENABLED": True,
                  "POLL_SECONDS": 15}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), cls.ns)

    def test_payload_to_message_for_every_decision_state(self):
        for state in ("REJECTED", "WATCHING", "READY_FOR_PAPER_EXECUTION"):
            with self.subTest(state=state):
                research = SimpleNamespace(symbol="TEST", metadata={"verdict": "WATCH"})
                decision = SimpleNamespace(state=state, reason="test reason", metadata={
                    "quality_inputs": {"rvol": 1.2, "liquidity_score": 40},
                    "trigger_watch": {"current_price": 1.18, "trigger_price": 1.19,
                                      "distance_to_trigger_pct": 0.84}})
                payload = self.ns["build_decision_payload"](research, decision)
                self.assertEqual(payload["jalwe"]["state"], state)
                self.assertFalse(payload["execution"]["broker_order_submitted"])
                payload["recheck_notice"] = "bounded retry notice"
                message = self.ns["build_telegram_message"](payload)
                self.assertIn("TEST", message)
                self.assertIn("RVOL", message)
                self.assertEqual("bounded retry notice" in message, state == "REJECTED")


if __name__ == "__main__":
    unittest.main()
