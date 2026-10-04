import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from research_recheck import update_recheck, recheck_due


class RecheckTests(unittest.TestCase):
    def test_only_previously_watched_report_can_retry(self):
        state = {}
        reason = "OpportunityEngine rejected the setup."
        self.assertFalse(update_recheck(state, "NAUT", "v1", "REJECTED", reason, 100))
        update_recheck(state, "NAUT", "v1", "WATCHING", "", 100)
        update_recheck(state, "NAUT", "v1", "REJECTED", reason, 110)
        state = json.loads(json.dumps(state))  # restart retains cooldown/deadline
        self.assertFalse(recheck_due(state, "NAUT", "v1", 169))
        self.assertTrue(recheck_due(state, "NAUT", "v1", 170))
        self.assertFalse(recheck_due(state, "NAUT", "v2", 170))
        update_recheck(state, "NAUT", "v1", "WATCHING", "", 650)
        self.assertFalse(recheck_due(state, "NAUT", "v1", 700))

    def test_watching_recheck_advances_on_sixty_second_cadence(self):
        state = {}
        update_recheck(state, "NAUT", "v1", "WATCHING", "", 100)
        self.assertFalse(recheck_due(state, "NAUT", "v1", 159))
        self.assertTrue(recheck_due(state, "NAUT", "v1", 160))

        # Simulate the scheduled WATCHING analysis actually running.
        update_recheck(state, "NAUT", "v1", "WATCHING", "", 160)
        self.assertFalse(recheck_due(state, "NAUT", "v1", 219))
        self.assertTrue(recheck_due(state, "NAUT", "v1", 220))

        # The original ten-minute window is never extended.
        update_recheck(state, "NAUT", "v1", "WATCHING", "", 220)
        self.assertFalse(recheck_due(state, "NAUT", "v1", 700))

    def test_hard_reject_and_execution_clear_retry(self):
        for status, reason in [("REJECTED", "Price reached the setup invalidation level before entry."),
                               ("REJECTED", "Risk rejected."), ("READY_FOR_PAPER_EXECUTION", "")]:
            state = {}
            update_recheck(state, "X", "v", "WATCHING", "", 0)
            update_recheck(state, "X", "v", status, reason, 1)
            self.assertFalse(recheck_due(state, "X", "v", 100))

    def test_corrupt_state_fails_closed(self):
        self.assertFalse(recheck_due({"rechecks": {"X": {"version": "v", "next": 0, "expires": float('inf')}}}, "X", "v", 1))

    def test_scan_preserves_freshness_and_management_precedes_analysis(self):
        # Run the production scan function without importing broker clients.
        tree = ast.parse(Path("jalwe_research_watcher.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "scan_bridge")
        events = []
        bridge = Mock()
        old = SimpleNamespace(age=31)
        fresh = SimpleNamespace(age=1)
        bridge.list_recent_research.return_value = [old, fresh]
        bridge.get_age_minutes.side_effect = lambda r: r.age
        ns = {"Any": object, "MAX_REPORTS_PER_SCAN": 50, "MAX_RESEARCH_AGE_MINUTES": 30,
              "should_process": lambda r, s: (True, "retry"),
              "process_emergency_paper_close": lambda: events.append("emergency"),
              "manage_active_paper_trades": lambda s: events.append("protect"),
              "process_research": lambda r, d, s: events.append(r) or True}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "scan", "exec"), ns)
        self.assertEqual(ns["scan_bridge"](bridge, None, {}), (1, 1))
        self.assertEqual(events, ["emergency", "protect", fresh])


if __name__ == "__main__":
    unittest.main()
