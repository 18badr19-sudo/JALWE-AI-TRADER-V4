import json
import unittest

import pandas as pd

from research_entry_variants import entry_challengers, count_results, PROTOCOL, RULES
from strategy_experiments import simulate
import test_strategy_experiments as fixtures


class EntryVariantTests(unittest.TestCase):
    def bars(self):
        return pd.DataFrame(dict(open=10.1, high=10.2, low=10.0, close=10.1, volume=100.),
                            index=pd.date_range("2026-10-16T14:00Z", periods=60, freq="min"))

    def test_hold_needs_two_closes_volume_and_next_open(self):
        bars = self.bars()
        bars.loc[bars.index[5], "volume"] = 150.
        bars.loc[bars.index[6], ["open", "high", "low", "close"]] = [10.3, 10.4, 10.2, 10.3]
        result = entry_challengers(bars, 10., 9.)
        rule = result["rules"][RULES[0]]
        self.assertEqual(rule["signal_at"], bars.index[5].isoformat())
        self.assertEqual(rule["entry_at"], bars.index[6].isoformat())
        self.assertAlmostEqual(rule["entry_fill"], 10.3 * 1.001)
        self.assertAlmostEqual(rule["exit_fill"], 10.1 * .999)
        self.assertAlmostEqual(rule["net_r"], rule["exit_fill"] - rule["entry_fill"])
        self.assertFalse(result["auto_apply"])

    def test_signal_volume_excludes_itself_and_future_volume(self):
        bars = self.bars()
        bars.loc[bars.index[5], "volume"] = 150.
        bars.loc[bars.index[6]:, "volume"] = 100000.
        rule = entry_challengers(bars, 10., 9.)["rules"][RULES[0]]
        self.assertEqual(rule["signal_at"], bars.index[5].isoformat())
        self.assertEqual(rule["prior_mean_volume"], 100.)

    def test_low_or_zero_volume_never_confirms(self):
        for volume in (0., 100., 149.):
            with self.subTest(volume=volume):
                result = entry_challengers(self.bars().assign(volume=volume), 10., 9.)
                self.assertTrue(all(not r["traded"] for r in result["rules"].values()))

    def test_reclaim_requires_earlier_closed_sweep_above_stop(self):
        bars = self.bars()
        bars.loc[bars.index[3], ["open", "high", "low", "close"]] = [9.8, 10., 9.6, 9.8]
        bars.loc[bars.index[5], "volume"] = 200.
        rule = entry_challengers(bars, 10., 9.)["rules"][RULES[1]]
        self.assertTrue(rule["traded"])
        self.assertEqual(rule["entry_at"], bars.index[6].isoformat())
        unswept = entry_challengers(self.bars().assign(volume=200.), 10., 9.)
        self.assertFalse(unswept["rules"][RULES[1]]["traded"])

    def test_stop_before_confirmation_cannot_be_resurrected(self):
        bars = self.bars()
        bars.loc[bars.index[2], ["open", "high", "low", "close"]] = [9.5, 9.8, 8.9, 9.6]
        bars.loc[bars.index[5], "volume"] = 200.
        results = entry_challengers(bars, 10., 9.)["rules"]
        self.assertTrue(all(r["outcome"] == "INVALIDATED_BEFORE_ENTRY" for r in results.values()))

    def test_ambiguous_activation_and_exit_are_unknown(self):
        bars = self.bars()
        bars.loc[bars.index[2], "low"] = 8.9
        result = entry_challengers(bars, 10., 9.)
        self.assertTrue(all(r["net_r"] is None for r in result["rules"].values()))
        bars = self.bars()
        bars.loc[bars.index[5], "volume"] = 200.
        bars.loc[bars.index[6], ["high", "low"]] = [12.5, 8.5]
        rule = entry_challengers(bars, 10., 9.)["rules"][RULES[0]]
        self.assertEqual(rule["outcome"], "AMBIGUOUS_STOP_TARGET")
        self.assertIsNone(rule["net_r"])

    def test_full_window_and_valid_prices_are_required(self):
        original = self.bars()
        cases = [original.iloc[:59], original.drop(original.index[10]),
                 pd.concat([original.iloc[:30], original.iloc[29:59]]), original.iloc[::-1],
                 original.set_axis(original.index.tz_localize(None))]
        for column, value in (("close", float("nan")), ("volume", -1.),
                              ("high", 8.), ("open", float("inf")), ("volume", True)):
            bad = original.copy().astype(object)
            bad.loc[bad.index[10], column] = value
            cases.append(bad)
        for bars in cases:
            self.assertEqual(entry_challengers(bars, 10., 9.)["outcome"], "INVALID_WINDOW")

    def test_plan_validation(self):
        for trigger, stop in ((True, .5), (10., True), (9., 10.), (10., 0.), (float("nan"), 9.)):
            self.assertEqual(entry_challengers(self.bars(), trigger, stop)["outcome"], "INVALID_PLAN")

    def test_last_signal_needs_next_bar_and_gap_cannot_chase_target(self):
        bars = self.bars()
        bars.loc[bars.index[-1], "volume"] = 200.
        self.assertEqual(entry_challengers(bars, 10., 9.)["rules"][RULES[0]]["outcome"], "NO_NEXT_BAR")
        bars.loc[bars.index[5], "volume"] = 200.
        bars.loc[bars.index[6], ["open", "high", "low", "close"]] = [12.1, 12.2, 12., 12.1]
        rule = entry_challengers(bars, 10., 9.)["rules"][RULES[0]]
        self.assertEqual(rule["outcome"], "ENTRY_OUTSIDE_PLAN")
        self.assertFalse(rule["traded"])

    def test_counts_do_not_confuse_no_entry_with_fills_or_unknown(self):
        comparison = entry_challengers(self.bars(), 10., 9.)
        bad = entry_challengers(self.bars().iloc[:59], 10., 9.)
        counts = count_results([comparison, bad, {"protocol": "old"}])
        self.assertEqual(counts[RULES[0]], dict(entries=0, no_entry=1, unknown=1))


class EntryVariantIntegrationTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting and rerunning the parent suite.
    setUp = fixtures.StrategyExperimentTests.setUp
    payload = fixtures.StrategyExperimentTests.payload
    analysis = fixtures.StrategyExperimentTests.analysis
    bars = fixtures.StrategyExperimentTests.bars

    def test_new_protocol_is_frozen_and_native_results_unchanged(self):
        payload = self.payload()
        self.memory.record(payload, "new-entry-rules")
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM strategy_trials LIMIT 1").fetchone()
            frozen = json.loads(row["candidate_json"])
            self.assertEqual(frozen["entry_variant_protocol"], PROTOCOL)
            self.engine.observe(conn, row["decision_key"], self.bars(), "COMPLETE")
            row = conn.execute("SELECT * FROM strategy_trials WHERE strategy='OPENING_RANGE_BREAKOUT'").fetchone()
            result = json.loads(row["result_json"])
            self.assertEqual(result["net_r"], simulate(self.bars(), 10., 9.)["net_r"])
            self.assertIn("entry_challengers", result)
            self.assertFalse(result["entry_challengers"]["auto_apply"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_preferences").fetchone()[0], 0)
        self.assertIn("ثبات الاختراق مع الحجم", self.engine.status_text())

    def test_partial_and_old_unfrozen_trials_are_not_evaluated(self):
        self.memory.record(self.payload(), "old-entry-rules")
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM strategy_trials").fetchall()
            for row in rows:
                candidate = json.loads(row["candidate_json"])
                candidate.pop("entry_variant_protocol")
                conn.execute("UPDATE strategy_trials SET candidate_json=? WHERE trial_key=?",
                             (json.dumps(candidate), row["trial_key"]))
            self.engine.observe(conn, rows[0]["decision_key"], self.bars(), "COMPLETE")
            for row in conn.execute("SELECT result_json FROM strategy_trials"):
                self.assertNotIn("entry_challengers", json.loads(row[0]))
        self.memory.record(self.payload(), "partial-entry-rules")
        with self.db.connection() as conn:
            key = conn.execute("SELECT decision_key FROM strategy_trials WHERE status='PENDING' LIMIT 1").fetchone()[0]
            self.engine.observe(conn, key, self.bars(), "PARTIAL_DATA")
            for row in conn.execute("SELECT result_json FROM strategy_trials WHERE status='PARTIAL_DATA'"):
                self.assertNotIn("entry_challengers", json.loads(row[0]))
