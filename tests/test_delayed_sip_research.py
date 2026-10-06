import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from alpaca.data.enums import DataFeed

from decision_outcome_memory import DecisionOutcomeMemory
from delayed_sip_research import DelayedSipResearch
from market.market_data import MarketData, MarketDataError
from market.research_diagnostics import coverage_diagnostics, error_diagnostics


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


class DelayedSipResearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Store(Path(temp.name) / "research.sqlite")
        self.market = Mock()
        self.memory = DecisionOutcomeMemory(self.db, self.market)
        self.research = self.memory.delayed_sip_research
        self.start = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=60)
        self.payload = {"timestamp": self.start.isoformat(), "symbol": "TEST", "jalwe": {
            "state": "WATCHING", "reason": "waiting", "strategy": "VWAP_BOUNCE",
            "entry_price": 10., "stop_price": 9., "target_1": 12.,
            "metadata": {"decision_data_feed": "iex", "decision_features": {
                "price": 10., "data_quality_ok": True, "data_is_stale": False}}}}
        self.memory.record(self.payload, "v1")

    def frame(self, periods=60):
        frame = pd.DataFrame({"open": 10., "high": 10.5, "low": 9.5,
                              "close": 10., "volume": 100.},
                             index=pd.date_range(self.start, periods=periods, freq="min"))
        frame.attrs["data_feed"] = "sip"
        return frame

    def row(self):
        with self.db.connection() as conn:
            return dict(conn.execute("SELECT * FROM delayed_sip_research").fetchone())

    def observe(self, frame, minutes=30):
        self.market.get_research_observation_bars.return_value = frame
        self.research.update_due(now=self.end + timedelta(minutes=minutes))
        row = self.row()
        return row, json.loads(row["result_json"] or "{}")

    def test_waits_16_minutes_without_making_recent_sip_request(self):
        self.research.update_due(now=self.end + timedelta(minutes=15, seconds=59))
        self.market.get_research_observation_bars.assert_not_called()
        row, result = self.observe(self.frame(), 16)
        self.assertEqual(row["status"], "COMPLETE")
        self.market.get_research_observation_bars.assert_called_once_with(
            "TEST", self.start, self.end, as_of=self.end + timedelta(minutes=16))
        self.assertEqual(result["decision_feed"], "iex")
        self.assertEqual(result["observation_feed"], "sip")

    def test_keeps_original_evidence_and_preferences_untouched(self):
        with self.db.connection() as conn:
            before = dict(conn.execute("SELECT * FROM decision_outcome_memory").fetchone())
        self.memory.experiments.observe = Mock(side_effect=AssertionError("preferences changed"))
        self.observe(self.frame())
        with self.db.connection() as conn:
            after = dict(conn.execute("SELECT * FROM decision_outcome_memory").fetchone())
        self.assertEqual(before, after)
        self.memory.experiments.observe.assert_not_called()
        self.market.get_observation_bars.assert_not_called()

    def test_eligible_opportunities_are_separate_from_decisions_and_prioritized(self):
        import opportunity_performance_tracker as module
        with patch.object(module, "database", self.db), patch.object(module, "get_market_data", return_value=self.market):
            tracker = module.OpportunityPerformanceTracker()
            payload = copy.deepcopy(self.payload)
            payload["jalwe"]["gates"] = {gate: True for gate in tracker.PRE_ENTRY_GATES}
            self.assertTrue(tracker.start_from_payload(payload, "v1"))
        with self.db.connection() as conn:
            before = dict(conn.execute("SELECT * FROM opportunity_performance").fetchone())
        self.observe(self.frame())
        with self.db.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM delayed_sip_research").fetchall()]
            after = dict(conn.execute("SELECT * FROM opportunity_performance").fetchone())
        self.assertEqual(before, after)
        self.assertEqual({r["source_kind"] for r in rows}, {"OPPORTUNITY", "DECISION"})
        self.assertTrue(all(r["status"] == "COMPLETE" for r in rows))
        self.assertTrue(all(r["decision_feed"] == "iex" for r in rows))
        report = self.research.report_for_ny_date("2026-10-02")
        self.assertIn("مصادر القرار المسجلة", report)
        self.assertEqual(self.market.get_research_observation_bars.call_count, 2)

    def test_missing_minute_is_not_completed_or_a_profit_label(self):
        frame = self.frame().drop(self.frame().index[20])
        row, result = self.observe(frame)
        self.assertEqual(row["status"], "PARTIAL_DATA")
        self.assertFalse(result["complete_path"])
        self.assertEqual(result["data_diagnostics"]["missing_minutes"], 1)
        self.assertEqual(result["data_diagnostics"]["reason"], "MISSING_PROVIDER_MINUTES")
        self.assertEqual(result["levels"]["outcome"], "INCOMPLETE_PATH")

    def test_empty_window_and_invalid_bars_have_distinct_diagnostics(self):
        row, result = self.observe(self.frame(0))
        self.assertEqual(row["status"], "PARTIAL_DATA")
        self.assertEqual(result["data_diagnostics"]["reason"], "EMPTY_PROVIDER_WINDOW")
        frame = self.frame()
        frame.iloc[10, frame.columns.get_loc("volume")] = 0
        valid = DecisionOutcomeMemory._valid_bars(frame, self.start, self.end)
        self.assertEqual(coverage_diagnostics(frame, valid, self.start, self.end)["reason"],
                         "INVALID_OR_DUPLICATE_BARS")

    def test_wrong_observation_feed_is_rejected_instead_of_silently_mixed(self):
        frame = self.frame()
        frame.attrs["data_feed"] = "iex"
        row, result = self.observe(frame)
        self.assertEqual(row["status"], "DATA_UNAVAILABLE")
        self.assertEqual(result["data_diagnostics"]["reason"], "OBSERVATION_FEED_MISMATCH")

    def test_provider_errors_retry_during_grace_then_keep_root_cause(self):
        root = RuntimeError('subscription does not permit querying recent SIP data SECRET')
        root.response = SimpleNamespace(status_code=403)
        wrapped = MarketDataError("failed to read window")
        wrapped.__cause__ = root
        self.market.get_research_observation_bars.side_effect = wrapped
        self.research.update_due(now=self.end + timedelta(minutes=17))
        self.assertEqual(self.row()["status"], "PENDING")
        self.research.update_due(now=self.end + timedelta(minutes=27))
        row = self.row()
        result = json.loads(row["result_json"])
        self.assertEqual(row["status"], "DATA_UNAVAILABLE")
        self.assertEqual(result["data_diagnostics"]["reason"], "SUBSCRIPTION_REQUIRED")
        self.assertEqual(result["data_diagnostics"]["http_status"], 403)
        self.assertNotIn("SECRET", row["result_json"])

    def test_restart_deduplicates_source_and_retains_measurement(self):
        self.observe(self.frame())
        restarted = DelayedSipResearch(self.db, self.market)
        self.assertEqual(restarted.update_due(now=self.end + timedelta(minutes=31)), 0)
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM delayed_sip_research").fetchone()[0], 1)
        self.assertEqual(self.market.get_research_observation_bars.call_count, 1)

    def test_stale_decision_reference_remains_unobservable(self):
        stale = copy.deepcopy(self.payload)
        stale["symbol"] = "STALE"
        stale["jalwe"]["metadata"]["decision_features"]["data_is_stale"] = True
        self.memory.record(stale, "v2")
        self.observe(self.frame())
        with self.db.connection() as conn:
            row = dict(conn.execute("SELECT * FROM delayed_sip_research WHERE symbol='STALE'").fetchone())
        self.assertEqual(row["status"], "UNOBSERVABLE")
        self.assertIsNone(row["reference_price"])
        self.assertIsNone(row["result_json"])

    def test_report_excludes_incomplete_paths_and_preserves_ny_date(self):
        self.observe(self.frame().drop(self.frame().index[20]))
        report = self.research.report_for_ny_date("2026-10-02")
        self.assertIn("SIP", report)
        self.assertIn("MISSING_PROVIDER_MINUTES", self.row()["result_json"])
        self.assertNotIn("MFE", report)
        empty = self.research.report_for_ny_date("2026-10-01")
        self.assertNotIn("MFE", empty)

    def test_batch_is_bounded_and_old_window_never_uses_later_target(self):
        for index in range(6):
            payload = copy.deepcopy(self.payload)
            payload["symbol"] = "TEST" + str(index)
            self.memory.record(payload, "v" + str(index))
        frame = self.frame(80)
        frame.loc[frame.index[60]:, ["open", "high", "low", "close"]] = 15.
        self.observe(frame)
        self.assertEqual(self.market.get_research_observation_bars.call_count, 4)
        with self.db.connection() as conn:
            complete = conn.execute("SELECT result_json FROM delayed_sip_research WHERE status='COMPLETE'").fetchall()
        self.assertEqual(len(complete), 4)
        self.assertTrue(all(json.loads(r[0])["mfe_pct"] < 6. for r in complete))


class ResearchReaderTests(unittest.TestCase):
    def test_sip_reader_does_not_change_execution_feed_and_retains_empty_feed(self):
        reader = MarketData.__new__(MarketData)
        reader.feed = DataFeed.IEX
        reader.client = Mock()
        reader.client.get_stock_bars.return_value.df = pd.DataFrame()
        start = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)
        end = start + timedelta(minutes=60)
        with self.assertRaises(ValueError):
            reader.get_research_observation_bars("TEST", start, end, as_of=end + timedelta(minutes=15))
        reader.client.get_stock_bars.assert_not_called()
        frame = reader.get_research_observation_bars("TEST", start, end,
                                                   as_of=end + timedelta(minutes=16))
        request = reader.client.get_stock_bars.call_args.args[0]
        self.assertEqual(request.feed, DataFeed.SIP)
        self.assertEqual(frame.attrs["data_feed"], "sip")
        self.assertEqual(reader.feed, DataFeed.IEX)
        native = reader.get_observation_bars("TEST", start, end)
        self.assertEqual(reader.client.get_stock_bars.call_args.args[0].feed, DataFeed.IEX)
        self.assertEqual(native.attrs["data_feed"], "iex")

    def test_rate_limit_and_wrapped_timeout_are_diagnosed_without_raw_secrets(self):
        exc = RuntimeError("token=SECRET")
        exc.response = SimpleNamespace(status_code=429)
        self.assertEqual(error_diagnostics(exc)["reason"], "RATE_LIMITED")
        wrapped = MarketDataError("token=SECRET")
        wrapped.__cause__ = TimeoutError("timeout")
        self.assertEqual(error_diagnostics(wrapped)["reason"], "PROVIDER_TIMEOUT")
        self.assertNotIn("SECRET", json.dumps(error_diagnostics(wrapped)))


if __name__ == "__main__":
    unittest.main()
