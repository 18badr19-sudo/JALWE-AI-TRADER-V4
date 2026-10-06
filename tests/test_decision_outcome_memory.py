import copy
import ast
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from decision_outcome_memory import DecisionOutcomeMemory


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


class OutcomeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Store(Path(self.directory.name) / "memory.sqlite")
        self.market = Mock()
        self.memory = DecisionOutcomeMemory(self.db, self.market)
        self.start = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=60)

    def payload(self, **changes):
        decision = {
            "state": "REJECTED", "reason": "OpportunityEngine rejected the setup.",
            "strategy": "MOMENTUM_BREAKOUT", "market_regime": "SIDEWAYS",
            "entry_price": 10., "stop_price": 9., "target_1": 12.,
            "gates": {"features": True, "opportunity": False},
            "metadata": {"decision_data_feed": "iex", "decision_features": {
                "price": 10., "rvol": 1.2, "data_quality_ok": True,
                "data_is_stale": False, "timestamp": self.start.isoformat(),
            }},
        }
        decision.update(changes)
        return {"timestamp": self.start.isoformat(), "symbol": "TEST",
                "jalwe": decision, "execution": {"broker_order_submitted": False}}

    def frame(self):
        return pd.DataFrame({"open": 10., "high": 10.5, "low": 9.5,
                             "close": 10., "volume": 100.},
                            index=pd.date_range(self.start, periods=60, freq="min"))

    def row(self):
        with self.db.connection() as conn:
            return dict(conn.execute("SELECT * FROM decision_outcome_memory").fetchone())

    def observe(self, frame, **decision):
        self.memory.record(self.payload(**decision), "v1")
        self.market.get_observation_bars.return_value = frame
        self.memory.update_due(now=self.end + timedelta(minutes=11))
        row = self.row()
        return row, json.loads(row["result_json"])

    def test_all_states_are_saved_and_exact_rechecks_deduplicate(self):
        payload = self.payload()
        self.assertTrue(self.memory.record(payload, "v1"))
        later = copy.deepcopy(payload)
        later["timestamp"] = (self.start + timedelta(seconds=40)).isoformat()
        self.assertFalse(self.memory.record(later, "v1"))
        for state in ("WATCHING", "READY_FOR_PAPER_EXECUTION"):
            self.assertTrue(self.memory.record(self.payload(state=state), "v1"))
        self.assertTrue(self.memory.record(payload, "v2"))
        payload["jalwe"]["reason"] = "changed later"
        saved = json.loads(self.row()["payload_json"])
        self.assertNotEqual(saved["jalwe"]["reason"], "changed later")
        self.assertEqual(saved["jalwe"]["metadata"]["decision_features"]["rvol"], 1.2)

    def test_stale_or_missing_reference_is_saved_but_not_labelled(self):
        payload = self.payload()
        payload["jalwe"]["metadata"]["decision_features"]["data_is_stale"] = True
        self.memory.record(payload, "v1")
        self.assertEqual(self.row()["status"], "UNOBSERVABLE")
        self.assertIsNone(self.row()["reference_price"])
        self.memory.update_due(now=self.end + timedelta(days=4))
        self.market.get_observation_bars.assert_not_called()

    def test_restart_reads_original_window_and_excludes_later_prices(self):
        payload = self.payload()
        payload["timestamp"] = (self.start - timedelta(seconds=25)).isoformat()
        self.memory.record(payload, "v1")
        self.memory = DecisionOutcomeMemory(self.db, self.market)
        frame = self.frame()
        frame.loc[self.start - timedelta(minutes=1)] = [10, 99, 1, 10, 100]
        frame.loc[self.end] = [10, 999, 1, 10, 100]
        self.market.get_observation_bars.return_value = frame
        self.memory.update_due(now=self.end + timedelta(days=3))
        self.market.get_observation_bars.assert_called_once_with("TEST", self.start, self.end)
        row = self.row()
        self.assertEqual(row["status"], "COMPLETE")
        result = json.loads(row["result_json"])
        self.assertAlmostEqual(result["max_observed_return_pct"], 5.)
        self.assertEqual(result["observed_bars"], 60)

    def test_gaps_do_not_become_negative_labels_or_stale_checkpoint_prices(self):
        frame = self.frame().drop(self.start + timedelta(minutes=4))
        row, result = self.observe(frame)
        self.assertEqual(row["status"], "PARTIAL_DATA")
        self.assertIsNone(result["checkpoint_close_prices"]["5"])
        self.assertEqual(result["levels"]["outcome"], "INCOMPLETE_PATH")
        self.assertFalse(result["complete_path"])

    def test_stop_target_and_same_bar_order(self):
        for stop_minute, target_minute, expected in (
            (3, 5, "STOP_FIRST"), (5, 3, "TARGET_FIRST"),
            (3, 3, "AMBIGUOUS_STOP_TARGET"),
        ):
            with self.subTest(expected=expected):
                frame = self.frame()
                frame.loc[self.start + timedelta(minutes=stop_minute), "low"] = 8.5
                frame.loc[self.start + timedelta(minutes=target_minute), "high"] = 12.5
                result = self.memory._level_observation(frame, self.payload()["jalwe"], True)
                self.assertEqual(result["outcome"], expected)

    def test_levels_in_activation_bar_are_not_post_activation_hits(self):
        frame = self.frame()
        frame.iloc[0, frame.columns.get_loc("high")] = 12.5
        frame.iloc[0, frame.columns.get_loc("low")] = 8.5
        _, result = self.observe(frame)
        self.assertEqual(result["levels"]["outcome"], "NO_LEVEL")
        self.assertEqual(result["levels"]["activated_at"], (self.start + timedelta(minutes=1)).isoformat())

    def test_stop_before_trigger_is_ignored_and_gap_past_target_is_flagged(self):
        frame = self.frame()
        frame["close"] = 9.8
        frame["open"] = 9.8
        frame.iloc[0, frame.columns.get_loc("low")] = 8.5
        frame.iloc[5, frame.columns.get_loc("close")] = 10.1
        result = self.memory._level_observation(frame, self.payload()["jalwe"], True)
        self.assertEqual(result["outcome"], "NO_LEVEL")
        frame.iloc[5, frame.columns.get_loc("close")] = 12.1
        frame.iloc[5, frame.columns.get_loc("high")] = 12.2
        result = self.memory._level_observation(frame, self.payload()["jalwe"], True)
        self.assertEqual(result["outcome"], "ACTIVATION_PAST_TARGET")

    def test_not_activated_and_no_setup_levels_are_distinct(self):
        _, result = self.observe(self.frame(), entry_price=11.)
        self.assertEqual(result["levels"]["outcome"], "NOT_ACTIVATED")
        self.assertAlmostEqual(result["max_observed_return_pct"], 5.)
        result = self.memory._level_observation(self.frame(), {}, True)
        self.assertEqual(result["outcome"], "NO_VALID_SETUP_LEVELS")

    def test_derived_targets_are_explicitly_theoretical(self):
        _, result = self.observe(self.frame(), target_1=None)
        self.assertTrue(result["levels"]["theoretical_target"])
        self.assertEqual(result["levels"]["target_1"], 12.)

    def test_provider_failure_retries_during_grace_then_is_data_unavailable(self):
        self.memory.record(self.payload(), "v1")
        self.market.get_observation_bars.side_effect = TimeoutError("provider details")
        self.memory.update_due(now=self.end + timedelta(minutes=2))
        self.assertEqual(self.row()["status"], "PENDING")
        self.memory = DecisionOutcomeMemory(self.db, self.market)
        self.memory.update_due(now=self.end + timedelta(minutes=11))
        row = self.row()
        self.assertEqual(row["status"], "DATA_UNAVAILABLE")
        self.assertEqual(row["check_count"], 2)
        self.assertNotIn("provider details", row["result_json"])

    def test_no_future_window_queries_and_batch_rotates_after_failed_reads(self):
        for i in range(5):
            payload = self.payload()
            payload["symbol"] = f"TEST{i}"
            self.memory.record(payload, "v1")
        self.memory.update_due(now=self.start)
        self.market.get_observation_bars.assert_not_called()
        self.market.get_observation_bars.side_effect = TimeoutError()
        self.assertEqual(self.memory.update_due(now=self.end), 4)
        self.memory.update_due(now=self.end + timedelta(minutes=1))
        symbols = {call.args[0] for call in self.market.get_observation_bars.call_args_list}
        self.assertEqual(len(symbols), 5)

    def test_invalid_zero_volume_future_and_duplicate_bars_do_not_fake_coverage(self):
        frame = self.frame()
        frame.iloc[0, frame.columns.get_loc("volume")] = 0
        frame.iloc[1, frame.columns.get_loc("high")] = float("inf")
        frame = pd.concat([frame, frame.iloc[2:3]])
        row, result = self.observe(frame)
        self.assertEqual(row["observed_bars"], 58)
        self.assertEqual(row["status"], "PARTIAL_DATA")

    def test_daily_summary_groups_rejections_and_excludes_other_ny_days(self):
        self.observe(self.frame())
        earlier = self.payload(state="WATCHING")
        earlier["timestamp"] = "2026-10-02T03:59:00+00:00"  # October 1 New York
        self.memory.record(earlier, "v2")
        summary = self.memory.summary_for_ny_date("2026-10-02")
        self.assertEqual(summary["decisions"]["REJECTED"], 1)
        self.assertEqual(summary["decisions"]["WATCHING"], 0)
        self.assertEqual(summary["outcomes"][0]["strategy"], "MOMENTUM_BREAKOUT")
        self.assertIn("نتائج بحثية", self.memory.report_text(summary))

    def test_changed_feed_cannot_silently_mix_with_original_evidence(self):
        frame = self.frame()
        frame.attrs["data_feed"] = "sip"
        row, result = self.observe(frame)
        self.assertEqual(row["status"], "DATA_UNAVAILABLE")
        self.assertNotIn("levels", result)

    def test_scheduler_never_starts_concurrent_workers_and_releases_after_failure(self):
        self.memory.update_due = Mock(side_effect=RuntimeError("test"))
        with patch("decision_outcome_memory.threading.Thread") as thread:
            self.assertTrue(self.memory.schedule_update())
            self.memory._next_update = 0
            self.assertFalse(self.memory.schedule_update())
            self.assertEqual(thread.call_count, 1)
            thread.call_args.kwargs["target"]()
            self.memory._next_update = 0
            self.assertTrue(self.memory.schedule_update())
            thread.call_args.kwargs["target"]()

    def test_production_historical_reader_uses_original_bounds_and_feed(self):
        from market.market_data import MarketData
        from alpaca.data.enums import DataFeed
        from alpaca.common.enums import Sort
        client = Mock()
        frame = self.frame()
        frame.loc[self.end] = [10., 999., 1., 10., 100.]
        client.get_stock_bars.return_value = SimpleNamespace(df=frame)
        market = MarketData.__new__(MarketData)
        market.client, market.feed = client, DataFeed.IEX
        result = market.get_observation_bars("TEST", self.start, self.end)
        request = client.get_stock_bars.call_args.args[0]
        # Alpaca SDK serializes aware timestamps as naive UTC.
        self.assertEqual(request.start.replace(tzinfo=timezone.utc), self.start)
        self.assertEqual(request.end.replace(tzinfo=timezone.utc), self.end)
        self.assertEqual(request.sort, Sort.ASC)
        self.assertEqual(request.feed, DataFeed.IEX)
        self.assertEqual(len(result), 60)
        self.assertEqual(result.attrs["data_feed"], "iex")
        client.get_stock_bars.return_value = SimpleNamespace(df=pd.DataFrame())
        self.assertTrue(market.get_observation_bars("TEST", self.start, self.end).empty)
        with self.assertRaises(ValueError):
            market.get_observation_bars("TEST", self.start, self.end + timedelta(minutes=1))

    def test_recording_failure_does_not_change_or_block_real_decision_processing(self):
        source = Path(__file__).resolve().parents[1] / "jalwe_research_watcher.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "process_research")
        payload = self.payload()
        engine = SimpleNamespace(analyze=Mock(return_value=SimpleNamespace()))
        broken_memory = SimpleNamespace(record=Mock(side_effect=sqlite3.OperationalError("disk test")))
        save = Mock(return_value=True)
        events = []
        ns = {"Any": object, "time": SimpleNamespace(time=lambda: 1),
              "logger": logging.getLogger(__name__), "utc_now_iso": lambda: "now",
              "auto_paper_execution_ready": lambda: False,
              "get_paper_trade_orchestrator": Mock(side_effect=AssertionError("broker must not run")),
              "build_decision_payload": lambda *args: payload, "safe_dict": lambda d: d,
              "update_recheck": lambda *args: False, "research_version": lambda r: "v1",
              "load_runtime_controls": lambda: {},
              "get_opportunity_performance_tracker": lambda: SimpleNamespace(start_from_payload=lambda *args: False),
              "get_decision_outcome_memory": lambda: broken_memory,
              "save_decision_to_database": save, "notify_if_changed": lambda *args: {},
              "notify_execution_lifecycle": lambda *args: None,
              "print_decision": lambda *args: None, "save_state": lambda *args: events.append("saved"),
              "database": SimpleNamespace(log_event=Mock())}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
        state = {}
        self.assertTrue(ns["process_research"](SimpleNamespace(symbol="TEST"), engine, state))
        engine.analyze.assert_called_once_with("TEST")
        save.assert_called_once_with(payload)
        self.assertEqual(state["processed"]["TEST"], "v1")
        self.assertEqual(payload["jalwe"]["state"], "REJECTED")
        ns["get_paper_trade_orchestrator"].assert_not_called()

    def test_audit_uses_recorded_reason_not_default_false_gates(self):
        self.memory.record(self.payload(gates={"market_data": True, "features": True,
                           "opportunity": False, "trigger": False, "risk": False}), "v1")
        audit = self.memory.rejection_audit("2026-10-02")
        reasons = audit["states"]["REJECTED"]["reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["reason"], "OpportunityEngine rejected the setup.")
        report = self.memory.rejection_report_text(audit)
        self.assertIn("OpportunityEngine", report)
        self.assertNotIn("risk:", report)

    def test_audit_counts_rechecks_separately_from_reports_and_symbols(self):
        self.memory.record(self.payload(), "v1")
        self.memory.record(self.payload(reason="changed reason"), "v1")
        self.memory.record(self.payload(), "v2")
        self.memory.record(self.payload(state="WATCHING"), "v1")
        audit = self.memory.rejection_audit("2026-10-02")
        self.assertEqual(audit["states"]["REJECTED"]["count"], 3)
        self.assertEqual(audit["states"]["REJECTED"]["episodes"], 2)
        self.assertEqual(audit["states"]["REJECTED"]["symbols"], 1)
        self.assertEqual(audit["states"]["WATCHING"]["count"], 1)

    def test_audit_complete_target_first_only_is_candidate_for_review(self):
        self.memory.record(self.payload(), "v1")
        self.memory.record(self.payload(reason="other"), "v2")
        with self.db.connection() as conn:
            conn.execute("UPDATE decision_outcome_memory SET status='COMPLETE', result_json=? WHERE research_version='v1'",
                         (json.dumps({"levels": {"outcome": "TARGET_FIRST"}}),))
            conn.execute("UPDATE decision_outcome_memory SET status='PARTIAL_DATA', result_json=? WHERE research_version='v2'",
                         (json.dumps({"levels": {"outcome": "TARGET_FIRST"}}),))
        audit = self.memory.rejection_audit("2026-10-02")
        self.assertEqual(audit["rejected_complete"], 1)
        self.assertEqual(audit["rejected_incomplete"], 1)
        self.assertEqual(len(audit["review_candidates"]), 1)
        self.assertEqual(audit["review_candidates"][0]["research_version"], "v1")
        self.assertEqual(audit["review_candidates"][0]["decided_at"], self.payload()["timestamp"])
        self.assertIn("لا يثبت خطأ الرفض", self.memory.rejection_report_text(audit))

    def test_audit_ny_boundary_and_empty_day(self):
        self.memory.record(self.payload(), "v1")
        earlier = self.payload()
        earlier["timestamp"] = "2026-10-02T03:59:00+00:00"
        self.memory.record(earlier, "v2")
        audit = self.memory.rejection_audit("2026-10-02")
        self.assertEqual(audit["states"]["REJECTED"]["count"], 1)
        empty = self.memory.rejection_audit("2026-10-03")
        self.assertEqual(empty["states"]["REJECTED"]["count"], 0)

    def test_audit_threshold_comes_from_immutable_snapshot_only(self):
        payload = self.payload()
        payload["jalwe"]["metadata"]["opportunity_evidence"] = {
            "score": 66., "minimum_score": 70., "approved": False}
        self.memory.record(payload, "v1")
        self.memory.record(self.payload(), "legacy")
        audit = self.memory.rejection_audit("2026-10-02")
        self.assertEqual(len(audit["opportunity_score_evidence"]), 1)
        report = self.memory.rejection_report_text(audit)
        self.assertIn("درجة 66.00 / المطلوب 70.00", report)
        self.assertIn("أقل من الحد: 1", report)

    def test_audit_is_read_only_and_summary_contains_it(self):
        self.memory.record(self.payload(), "v1")
        before = self.row()
        summary = self.memory.summary_for_ny_date("2026-10-02")
        self.assertEqual(before, self.row())
        self.assertIn("أسباب قلة الدخول", self.memory.report_text(summary))


    def test_freshness_audit_uses_recorded_age_and_threshold(self):
        payload = self.payload(reason="Feature freshness check failed.")
        payload["jalwe"]["metadata"]["feature_diagnostics"] = {
            "latest_bar_age_minutes": 22., "max_bar_age_minutes": 15.}
        self.memory.record(payload, "v1")
        audit = self.memory.rejection_audit("2026-10-02")
        review = audit["freshness_review"]
        self.assertEqual(review["counts"]["AGE_LIMIT"], 1)
        self.assertEqual(review["periods_with_evidence"]["regular_clock"], 1)
        self.assertEqual(review["samples"][0]["age_minutes"], 22.)
        self.assertIn("22.0 دقيقة / الحد 15.0", self.memory.rejection_report_text(audit))

    def test_freshness_audit_distinguishes_future_missing_and_other_stale_flags(self):
        for version, age in (("future", -1.), ("other", 5.), ("missing", None)):
            payload = self.payload(reason="Feature freshness check failed.")
            payload["jalwe"]["metadata"]["feature_diagnostics"] = {
                "latest_bar_age_minutes": age, "max_bar_age_minutes": 15.}
            self.memory.record(payload, version)
        counts = self.memory.rejection_audit("2026-10-02")["freshness_review"]["counts"]
        self.assertEqual(counts["FUTURE_TIMESTAMP"], 1)
        self.assertEqual(counts["OTHER_STALE_FLAG"], 1)
        self.assertEqual(counts["MISSING_EVIDENCE"], 1)
        self.assertEqual(counts["AGE_LIMIT"], 0)

    def test_freshness_audit_separates_after_close_and_does_not_modify_evidence(self):
        payload = self.payload(reason="Feature freshness check failed.")
        payload["timestamp"] = "2026-10-02T21:00:00+00:00"
        payload["jalwe"]["metadata"]["feature_diagnostics"] = {
            "latest_bar_age_minutes": 65., "max_bar_age_minutes": 15.}
        self.memory.record(payload, "v1")
        before = self.row()
        review = self.memory.rejection_audit("2026-10-02")["freshness_review"]
        self.assertEqual(review["periods_with_evidence"]["outside_regular_clock"], 1)
        self.assertEqual(before, self.row())



if __name__ == "__main__":
    unittest.main()
