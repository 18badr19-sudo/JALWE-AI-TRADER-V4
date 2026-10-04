import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from decision_outcome_memory import DecisionOutcomeMemory
from strategy_experiments import StrategyExperiments, simulate, context, PROTOCOL, NY
from intelligence.session_strategy_engine import (
    SessionStrategyAnalysis, SessionStrategyCandidate, SessionStrategyName,
)
from intelligence.decision_engine import DecisionEngine, DecisionState
from core.models import FeatureSnapshot, SignalAction
from intelligence.opportunity_engine import OpportunityGrade
from intelligence.trigger_engine import TriggerDecision, TriggerState
from intelligence.breakout_confirmation_engine import BreakoutConfirmation, BreakoutConfirmationState
from market.market_regime import MarketRegime, MarketRegimeResult
from test_decision_outcome_memory import Store


class StrategyExperimentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Store(Path(folder.name) / "experiments.db")
        self.memory = DecisionOutcomeMemory(self.db)
        self.engine = self.memory.experiments
        self.now = datetime(2026, 10, 4, 13, 45, tzinfo=timezone.utc)
        self.ctx = context("BULL_TREND", "iex", self.now.replace(day=2))

    def bars(self):
        return pd.DataFrame({"open": 10., "high": 10.5, "low": 9.5,
                             "close": 10., "volume": 100.},
                            index=pd.date_range("2026-10-02T13:45:00Z", periods=60, freq="min"))

    def analysis(self):
        baseline = SessionStrategyCandidate(SessionStrategyName.OPENING_RANGE_BREAKOUT, 95, True, 10., 9.)
        challenger = SessionStrategyCandidate(SessionStrategyName.VWAP_BOUNCE, 80, True, 10.1, 9.1)
        return SessionStrategyAnalysis("TEST", True, baseline.strategy, baseline.score, 10., 9.,
                                       candidates=[baseline, challenger])

    def payload(self):
        analysis = self.analysis()
        return {"symbol": "TEST", "timestamp": "2026-10-02T13:45:00+00:00", "jalwe": {
            "state": "WATCHING", "strategy": "OPENING_RANGE_BREAKOUT", "reason": "watch",
            "market_regime": "BULL_TREND", "entry_price": 10., "stop_price": 9.,
            "gates": dict.fromkeys(("market_data", "features", "ai", "opportunity", "market_regime", "strategy_router"), True),
            "metadata": {"decision_data_feed": "iex", "session_strategy_baseline": "OPENING_RANGE_BREAKOUT",
                "session_strategy_candidates": DecisionEngine._session_experiment_evidence(analysis, 75),
                "decision_features": {"price": 10., "data_quality_ok": True, "data_is_stale": False,
                                      "timestamp": "2026-10-02T13:40:00+00:00"}}}}

    def choice(self, analysis=None, **changes):
        args = dict(regime="BULL_TREND", feed="iex", now=self.now,
                    paper=True, allow_live=False, minimum_score=75)
        args.update(changes)
        return self.engine.choose(analysis or self.analysis(), **args)

    def profile(self, **changes):
        row = {"context": self.ctx, "preferred_strategy": "VWAP_BOUNCE",
               "generated_at": (self.now - timedelta(hours=1)).isoformat(),
               "expires_at": (self.now + timedelta(hours=23)).isoformat(),
               "data_through": (self.now - timedelta(days=1)).isoformat(),
               "protocol": PROTOCOL, "evaluation_json": '{"state":"VALIDATED_PAPER_PREFERENCE"}'}
        row.update(changes)
        with self.db.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO strategy_preferences VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(row.values()))

    def history(self, *, validation_bad=False, second_challenger=False, repeated=False, missing=False, traded=True):
        days = pd.bdate_range("2026-09-07", "2026-10-02")
        with self.db.connection() as conn:
            for d, stamp in enumerate(days):
                day = stamp.date().isoformat()
                for stock in range(4):
                    for repeat in range(3 if repeated else 1):
                        symbol = "ONE" if repeated else f"STOCK{stock}"
                        episode = f"{day}-{stock}-{repeat}"
                        values = {"OPENING_RANGE_BREAKOUT": .2,
                                  "VWAP_BOUNCE": -.2 if validation_bad and d >= 13 else .5}
                        if second_challenger:
                            values["BULL_FLAG_BREAKOUT"] = .4
                        for name, result in values.items():
                            status = "PARTIAL_DATA" if missing and (stock == 0) else "COMPLETE"
                            conn.execute("INSERT INTO strategy_trials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                                f"{episode}-{name}", episode, episode, symbol, f"{day}T13:45:00+00:00",
                                day, self.ctx, name, "OPENING_RANGE_BREAKOUT", 1, "{}", PROTOCOL, status,
                                json.dumps({"net_r": result, "traded": traded})))

    def saved_profile(self):
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM strategy_preferences WHERE context = ?", (self.ctx,)).fetchone()
            return dict(row) if row else None

    def test_candidates_freeze_together_and_rechecks_do_not_make_new_trials(self):
        payload = self.payload()
        self.assertTrue(self.memory.record(payload, "report1"))
        later = copy.deepcopy(payload)
        later["timestamp"] = "2026-10-02T13:45:45+00:00"
        later["jalwe"]["reason"] = "new recheck"
        later["jalwe"]["metadata"]["session_strategy_candidates"][1]["trigger_price"] = 99.
        self.memory.record(later, "report1")
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM strategy_trials").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["episode_key"] for r in rows}), 1)
        challenger = next(r for r in rows if r["strategy"] == "VWAP_BOUNCE")
        self.assertEqual(json.loads(challenger["candidate_json"])["trigger_price"], 10.1)

    def test_failed_native_gates_are_saved_as_ineligible(self):
        payload = self.payload()
        payload["jalwe"]["gates"]["ai"] = False
        self.memory.record(payload, "v")
        with self.db.connection() as conn:
            self.assertEqual({r[0] for r in conn.execute("SELECT status FROM strategy_trials")}, {"INELIGIBLE"})

    def test_all_six_candidates_include_below_threshold_without_trading_them(self):
        analysis = self.analysis()
        analysis.candidates = [SessionStrategyCandidate(s, 70 if i == 0 else 80, True, 10, 9)
                               for i, s in enumerate(SessionStrategyName)]
        evidence = DecisionEngine._session_experiment_evidence(analysis, 75)
        self.assertEqual(len(evidence), 6)
        self.assertFalse(evidence[0]["eligible_at_decision"])
        self.assertTrue(all(e["eligible_at_decision"] for e in evidence[1:]))

    def test_partial_paths_are_not_simulated_or_promoted(self):
        self.memory.record(self.payload(), "v")
        with self.db.connection() as conn:
            key = conn.execute("SELECT decision_key FROM strategy_trials LIMIT 1").fetchone()[0]
            self.engine.observe(conn, key, self.bars(), "PARTIAL_DATA")
        with self.db.connection() as conn:
            for row in conn.execute("SELECT * FROM strategy_trials"):
                self.assertEqual(row["status"], "PARTIAL_DATA")
                self.assertIsNone(json.loads(row["result_json"])["net_r"])

    def test_simulation_uses_next_open_and_both_costs(self):
        bars = self.bars()
        bars.iloc[0, bars.columns.get_loc("high")] = 12.5  # Before entry, ignored.
        bars.iloc[2, bars.columns.get_loc("high")] = 12.5
        result = simulate(bars, 10., 9.)
        self.assertEqual(result["outcome"], "TARGET")
        self.assertAlmostEqual(result["entry_fill"], 10.01)
        self.assertAlmostEqual(result["exit_fill"], 11.988)
        self.assertAlmostEqual(result["net_r"], 1.978)
        self.assertEqual(result["entry_at"], bars.index[1].isoformat())

    def test_stop_gap_uses_worse_open_and_same_bar_order_stays_ambiguous(self):
        bars = self.bars()
        bars.iloc[2] = [8.5, 8.8, 8.2, 8.7, 100.]
        result = simulate(bars, 10., 9.)
        self.assertEqual(result["outcome"], "STOP")
        self.assertLess(result["net_r"], -1.5)
        bars.iloc[2] = [10., 12.5, 8.5, 10., 100.]
        self.assertIsNone(simulate(bars, 10., 9.)["net_r"])

    def test_invalidation_before_activation_does_not_become_a_post_entry_loss(self):
        bars = self.bars()
        bars.iloc[0] = [9.2, 9.5, 8.8, 9.3, 100.]
        result = simulate(bars, 10., 9.)
        self.assertEqual(result["outcome"], "INVALIDATED_BEFORE_ENTRY")
        self.assertFalse(result["traded"])
        self.assertEqual(result["net_r"], 0)

    def test_outside_plan_entry_and_end_of_window_trigger_do_not_fake_fills(self):
        bars = self.bars()
        bars.iloc[1] = [12.1, 12.2, 12., 12.1, 100.]
        self.assertEqual(simulate(bars, 10., 9.)["outcome"], "ENTRY_OUTSIDE_PLAN")
        bars = self.bars()
        bars["close"] = 9.8
        bars.iloc[-1, bars.columns.get_loc("close")] = 10.
        self.assertFalse(simulate(bars, 10., 9.)["traded"])

    def test_no_samples_preserves_original_selection(self):
        analysis = self.analysis()
        selected, audit = self.choice(analysis)
        self.assertIs(selected, analysis)
        self.assertEqual(audit["mode"], "BASELINE")

    def test_validated_preference_selects_only_an_already_eligible_candidate(self):
        self.profile()
        analysis = self.analysis()
        selected, audit = self.choice(analysis)
        self.assertEqual(selected.strategy, SessionStrategyName.VWAP_BOUNCE)
        self.assertEqual(selected.trigger_price, 10.1)
        self.assertEqual(selected.invalidation_price, 9.1)
        self.assertEqual(selected.score, 80)
        self.assertEqual(analysis.strategy, SessionStrategyName.OPENING_RANGE_BREAKOUT)
        self.assertEqual(audit["mode"], "VALIDATED_PAPER_PREFERENCE")

    def test_preference_cannot_revive_reject_or_override_score_levels_or_paper_lock(self):
        self.profile()
        for options in ({"paper": False}, {"allow_live": True}, {"regime": "PANIC"}, {"feed": "unknown"}):
            selected, audit = self.choice(**options)
            self.assertEqual(audit["mode"], "BASELINE")
        for field, value in (("score", 74), ("score", float("nan")), ("valid", False),
                             ("trigger_price", float("inf")), ("invalidation_price", 11)):
            analysis = self.analysis()
            setattr(analysis.candidates[1], field, value)
            self.assertIs(self.choice(analysis)[0], analysis)
        analysis = self.analysis()
        analysis.approved = False
        self.assertIs(self.choice(analysis)[0], analysis)

    def test_expired_future_wrong_protocol_or_other_context_profiles_are_ignored(self):
        for changes in ({"expires_at": (self.now - timedelta(seconds=1)).isoformat()},
                        {"generated_at": (self.now + timedelta(seconds=1)).isoformat()},
                        {"data_through": self.now.isoformat()}, {"protocol": "old"},
                        {"context": "SIDEWAYS|iex|OPEN"}):
            with self.db.connection() as conn:
                conn.execute("DELETE FROM strategy_preferences")
            self.profile(**changes)
            self.assertEqual(self.choice()[1]["mode"], "BASELINE")

    def test_whole_day_validation_promotes_training_winner_with_positive_later_edge(self):
        self.history()
        self.assertTrue(self.engine.refresh_if_due(self.now))
        profile = self.saved_profile()
        self.assertEqual(profile["preferred_strategy"], "VWAP_BOUNCE")
        details = json.loads(profile["evaluation_json"])
        self.assertGreaterEqual(details["train_pairs"], 40)
        self.assertGreaterEqual(details["validation_pairs"], 20)
        self.assertGreater(details["validation_edge_lower_r"], 0)
        self.assertFalse(self.engine.refresh_if_due(self.now))

    def test_failed_holdout_does_not_search_validation_for_another_winner(self):
        self.history(validation_bad=True, second_challenger=True)
        self.engine.refresh_if_due(self.now)
        profile = self.saved_profile()
        self.assertIsNone(profile["preferred_strategy"])
        details = json.loads(profile["evaluation_json"])
        self.assertEqual(details["training_winner"], "VWAP_BOUNCE")
        self.assertEqual(details["state"], "VALIDATION_FAILED")

    def test_training_labels_cannot_overlap_first_validation_day(self):
        self.history()
        # First validation day is September 24 NY; September 23 23:30
        # observations finish after that boundary and must be purged.
        with self.db.connection() as conn:
            conn.execute("UPDATE strategy_trials SET decided_at = ? WHERE ny_date = ?",
                         ("2026-09-24T03:30:00+00:00", "2026-09-23"))
        self.engine.refresh_if_due(self.now)
        details = json.loads(self.saved_profile()["evaluation_json"])
        self.assertEqual(details["split_day"], "2026-09-24")
        self.assertEqual(details["train_pairs"], 48)

    def test_rechecks_missing_paths_or_no_actual_activations_cannot_inflate_evidence(self):
        for options in ({"repeated": True}, {"missing": True}, {"traded": False}):
            with self.db.connection() as conn:
                conn.execute("DELETE FROM strategy_trials")
                conn.execute("DELETE FROM strategy_evaluation_runs")
            self.history(**options)
            self.engine.refresh_if_due(self.now)
            self.assertIsNone(self.saved_profile()["preferred_strategy"])

    def test_current_and_future_days_do_not_enter_training(self):
        self.history()
        with self.db.connection() as conn:
            conn.execute("UPDATE strategy_trials SET ny_date = ?, decided_at = ?",
                         (self.now.astimezone(NY).date().isoformat(), self.now.isoformat()))
        self.engine.refresh_if_due(self.now)
        self.assertIsNone(self.saved_profile())

    def native_engine(self, waiting=False):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine._breakout_latches = {}
        engine._load_external_research = Mock(return_value={})
        bars = self.bars()
        bars.attrs["data_feed"] = "iex"
        engine.market_data = SimpleNamespace(get_bars=Mock(return_value=bars), get_last_price=Mock(return_value=10.2))
        features = FeatureSnapshot(symbol="TEST", price=10., data_quality_ok=True,
                                   timestamp=self.now - timedelta(minutes=5))
        diagnostics = {"valid_rows": 60, "required_rows": 51, "missing_columns": []}
        engine.feature_engine = SimpleNamespace(diagnose_input=Mock(return_value=diagnostics), build=Mock(return_value=features))
        engine._smart_backfill_bars = Mock(return_value=(bars, diagnostics, None))
        engine._apply_news = Mock()
        engine._apply_options = Mock()
        engine.ai_engine = SimpleNamespace(evaluate=Mock(return_value=SimpleNamespace(action=SignalAction.BUY, score=95)))
        engine.opportunity_engine = SimpleNamespace(evaluate=Mock(return_value=SimpleNamespace(approved=True, score=95,
                                                       grade=OpportunityGrade.A, warnings=[])))
        engine.market_context_engine = SimpleNamespace(get_regime_result=Mock(return_value=MarketRegimeResult(MarketRegime.BULL_TREND, .9)))
        engine.strategy_router = SimpleNamespace(route=Mock(return_value=SimpleNamespace(approved=True, grade=OpportunityGrade.A,
                                                          routed_score=95, final_risk_pct=1., warnings=[])))
        engine.session_feature_engine = SimpleNamespace(build=Mock(return_value=SimpleNamespace(current_price=10.2)))
        engine.session_strategy_engine = SimpleNamespace(minimum_score=75, evaluate=Mock(return_value=self.analysis()))
        engine.trigger_engine = SimpleNamespace(evaluate=Mock(return_value=TriggerDecision("TEST", TriggerState.ENTRY_CONFIRMED,
                                                         True, 10.2, 10.1, 9.1)))
        breakout = BreakoutConfirmation("TEST", BreakoutConfirmationState.WAITING if waiting else BreakoutConfirmationState.CONFIRMED,
                                        not waiting, 10.1, 9.1, close_price=10.2, score=95, reason="await candle" if waiting else "confirmed")
        engine.breakout_engine = SimpleNamespace(evaluate=Mock(return_value=breakout))
        engine.risk_engine = SimpleNamespace(evaluate=Mock(return_value=SimpleNamespace(approved=False, reason="Risk test rejected", metadata={})))
        return engine

    def test_native_risk_still_rejects_after_validated_strategy_is_selected(self):
        self.profile()
        engine = self.native_engine()
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch("intelligence.decision_engine.datetime", Clock), \
             patch("intelligence.decision_engine.database.save_feature_snapshot"), \
             patch("strategy_experiments.get_strategy_experiments", return_value=self.engine):
            decision = engine.analyze("TEST")
        self.assertEqual(decision.strategy, "VWAP_BOUNCE")
        self.assertEqual(decision.state, DecisionState.REJECTED)
        self.assertFalse(decision.ready_for_execution)
        self.assertFalse(decision.gates["risk"])
        selected = engine.trigger_engine.evaluate.call_args.args[0]
        self.assertEqual(selected.strategy, SessionStrategyName.VWAP_BOUNCE)
        self.assertEqual(engine.risk_engine.evaluate.call_args.kwargs["recommended_risk_pct"], 1.)

    def test_waiting_latch_keeps_selected_strategy_and_original_deadline(self):
        self.profile()
        engine = self.native_engine(waiting=True)
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch("intelligence.decision_engine.datetime", Clock), \
             patch("intelligence.decision_engine.database.save_feature_snapshot"), \
             patch("strategy_experiments.get_strategy_experiments", return_value=self.engine):
            decision = engine.analyze("TEST")
            self.assertEqual(decision.state, DecisionState.WATCHING)
            before = copy.deepcopy(engine._breakout_latches["TEST"])
            self.assertEqual(before["strategy"], "VWAP_BOUNCE")
            now += timedelta(minutes=1)
            decision = engine.analyze("TEST")
            self.assertEqual(decision.state, DecisionState.WATCHING)
            self.assertEqual(decision.strategy, "VWAP_BOUNCE")
            self.assertEqual(engine._breakout_latches["TEST"], before)
            self.assertEqual(engine.session_strategy_engine.evaluate.call_count, 1)
            engine.risk_engine.evaluate.assert_not_called()

    def test_database_preference_failure_keeps_native_score_selection(self):
        engine = self.native_engine()
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch("intelligence.decision_engine.datetime", Clock), \
             patch("intelligence.decision_engine.database.save_feature_snapshot"), \
             patch("strategy_experiments.get_strategy_experiments", side_effect=RuntimeError("test unavailable")):
            decision = engine.analyze("TEST")
        self.assertEqual(decision.strategy, "OPENING_RANGE_BREAKOUT")
        self.assertEqual(decision.state, DecisionState.REJECTED)
        self.assertEqual(engine.trigger_engine.evaluate.call_args.args[0].strategy,
                         SessionStrategyName.OPENING_RANGE_BREAKOUT)

    def test_failed_ai_gate_cannot_become_a_trusted_latch(self):
        engine = self.native_engine(waiting=True)
        engine.ai_engine.evaluate.return_value.action = SignalAction.SELL
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch("intelligence.decision_engine.datetime", Clock), \
             patch("intelligence.decision_engine.database.save_feature_snapshot"), \
             patch("strategy_experiments.get_strategy_experiments", return_value=self.engine):
            decision = engine.analyze("TEST")
        self.assertFalse(decision.gates["ai"])
        self.assertFalse(decision.ready_for_execution)
        self.assertEqual(engine._breakout_latches, {})

    def test_untrusted_or_nonfinite_latch_is_removed_before_gate_approval(self):
        for extra in ({"pre_entry_gates_passed": False}, {"expires_at": float("nan")},
                      {"stop_price": float("inf")}):
            engine = self.native_engine()
            engine._breakout_latches["TEST"] = {"pre_entry_gates_passed": True,
                "expires_at": datetime.now(timezone.utc).timestamp() + 60,
                "trigger_price": 10., "stop_price": 9., **extra}
            gates = {}
            result = engine._resume_breakout_latch(symbol="TEST", bars=self.bars(), features=None,
                gates=gates, warnings=[], base_metadata={}, strategy_equity=100, daily_start_equity=100)
            self.assertIsNone(result)
            self.assertEqual(gates, {})
            self.assertEqual(engine._breakout_latches, {})

    def test_memory_observes_all_candidates_from_one_shared_provider_window(self):
        self.memory.record(self.payload(), "v1")
        fetch = Mock(return_value=self.bars())
        self.memory.market_data = SimpleNamespace(get_observation_bars=fetch)
        self.memory.update_due(now=datetime(2026, 10, 2, 14, 56, tzinfo=timezone.utc))
        fetch.assert_called_once()
        with self.db.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM strategy_trials")]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"COMPLETE"})
        for row in rows:
            result = json.loads(row["result_json"])
            self.assertEqual(result["protocol"], PROTOCOL)
            self.assertIsNotNone(result["net_r"])

    def test_cross_midnight_windows_are_purged_at_evaluation_cutoff(self):
        self.history()
        with self.db.connection() as conn:
            conn.execute("UPDATE strategy_trials SET ny_date = ?, decided_at = ?",
                         ("2026-10-03", "2026-10-04T03:30:00+00:00"))
        self.engine.refresh_if_due(self.now)
        self.assertIsNone(self.saved_profile())


if __name__ == "__main__":
    unittest.main()
