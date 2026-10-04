"""Paired session-strategy PAPER research. No broker access or risk overrides.

All candidates share a frozen observation window and a versioned cost model.
A challenger is chosen on training days, then checked once on later whole days.
Only native eligible candidates can receive a validated preference.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from strategy_policy_guard import StrategyPolicyGuard

NY = ZoneInfo("America/New_York")
PROTOCOL = "session-2r-next-open-cost10bps-v1"
ONE_WAY_COST = .001  # Assumed spread + slippage, 10 bps on each side, not live quotes.
STRATEGIES = {"PREMARKET_HIGH_BREAK", "OPENING_RANGE_BREAKOUT", "GAP_AND_GO",
              "BULL_FLAG_BREAKOUT", "VWAP_BOUNCE", "COMPRESSION_BREAKOUT"}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def session_bucket(stamp):
    local = stamp.astimezone(NY)
    minute = local.hour * 60 + local.minute
    return ("OPEN" if 570 <= minute < 630 else "MID" if 630 <= minute < 840
            else "LATE" if 840 <= minute < 960 else "EXTENDED")


def context(regime, feed, stamp):
    return f"{regime}|{feed}|{session_bucket(stamp)}"


def simulate(bars, trigger, stop):
    """Conservative one-minute proxy, 2R target, next open entry, 60m time exit."""
    trigger, stop = number(trigger), number(stop)
    base = {"protocol": PROTOCOL, "one_way_cost_pct": ONE_WAY_COST * 100,
            "traded": False, "net_r": None, "scope": "SIMULATED_NOT_REALIZED_PNL"}
    if trigger is None or stop is None or not trigger > stop > 0 or bars.empty:
        return {**base, "outcome": "INVALID_PLAN"}
    target = trigger + 2 * (trigger - stop)
    base.update(trigger=trigger, stop=stop, target=target)
    activation = None
    for i, (_, bar) in enumerate(bars.iterrows()):
        if bar["low"] <= stop:
            if bar["close"] >= trigger:
                return {**base, "outcome": "AMBIGUOUS_ACTIVATION"}
            return {**base, "outcome": "INVALIDATED_BEFORE_ENTRY", "net_r": 0.0}
        if bar["close"] >= trigger:
            activation = i
            break
    if activation is None or activation + 1 >= len(bars):
        return {**base, "outcome": "NO_ENTRY", "net_r": 0.0}
    entry_bar = bars.iloc[activation + 1]
    fill = float(entry_bar["open"]) * (1 + ONE_WAY_COST)
    if fill >= target or fill <= stop:
        return {**base, "outcome": "ENTRY_OUTSIDE_PLAN", "net_r": 0.0}
    base.update(traded=True, entry_fill=fill,
                entry_at=bars.index[activation + 1].isoformat())
    exit_raw, outcome = float(bars["close"].iloc[-1]), "TIME_EXIT"
    for stamp, bar in bars.iloc[activation + 1:].iterrows():
        if bar["low"] <= stop and bar["high"] >= target:
            return {**base, "outcome": "AMBIGUOUS_STOP_TARGET", "net_r": None}
        if bar["low"] <= stop:
            exit_raw, outcome = min(stop, float(bar["open"])), "STOP"
            base["exit_at"] = stamp.isoformat()
            break
        if bar["high"] >= target:
            exit_raw, outcome = target, "TARGET"
            base["exit_at"] = stamp.isoformat()
            break
    exit_fill = exit_raw * (1 - ONE_WAY_COST)
    return {**base, "outcome": outcome, "exit_fill": exit_fill,
            "net_r": (exit_fill - fill) / (trigger - stop)}


def metrics(values):
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"mean_r": statistics.mean(values), "drawdown_r": drawdown,
            "worst_r": min(values), "positive_rate": sum(v > 0 for v in values) / len(values)}


class StrategyExperiments:
    MIN_TRAIN = 40
    MIN_VALIDATION = 20
    MIN_TRAIN_DAYS = 10
    MIN_VALIDATION_DAYS = 5
    MIN_COVERAGE = .90

    def __init__(self, db=None):
        if db is None:
            from core.database import database
            db = database
        self.db = db
        with db.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_trials (
                trial_key TEXT PRIMARY KEY, episode_key TEXT NOT NULL,
                decision_key TEXT NOT NULL, symbol TEXT NOT NULL,
                decided_at TEXT NOT NULL, ny_date TEXT NOT NULL,
                context TEXT NOT NULL, strategy TEXT NOT NULL,
                baseline_strategy TEXT, eligible INTEGER NOT NULL,
                candidate_json TEXT NOT NULL, protocol TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_strategy_trial_parent
                ON strategy_trials(decision_key, status)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_strategy_trial_evaluation
                ON strategy_trials(protocol, ny_date, context)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_preferences (
                context TEXT PRIMARY KEY, preferred_strategy TEXT,
                generated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                data_through TEXT NOT NULL, protocol TEXT NOT NULL,
                evaluation_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_evaluation_runs (
                ny_date TEXT PRIMARY KEY, generated_at TEXT NOT NULL
            )""")
        self.guard = StrategyPolicyGuard(db, PROTOCOL)

    def record_candidates(self, conn, payload, decision_key, research_version):
        jalwe = payload["jalwe"]
        metadata = jalwe.get("metadata") or {}
        candidates = metadata.get("session_strategy_candidates") or []
        if not candidates or metadata.get("breakout_latch"):
            return
        stamp = datetime.fromisoformat(payload["timestamp"]).astimezone(timezone.utc)
        features = metadata.get("decision_features") or {}
        feed = metadata.get("decision_data_feed") or (metadata.get("quality_inputs") or {}).get("feed")
        ctx = context(jalwe.get("market_regime"), feed, stamp)
        # One report/bar episode shared by every candidate. Never count repeated
        # watcher decisions as independent strategy evidence.
        episode = hashlib.sha256(f"{payload['symbol']}|{research_version}|{features.get('timestamp')}".encode()).hexdigest()
        baseline = metadata.get("session_strategy_baseline")
        gates = jalwe.get("gates") or {}
        native_ok = all(gates.get(g) for g in (
            "market_data", "features", "ai", "opportunity", "market_regime", "strategy_router"))
        native_ok = bool(native_ok and features.get("data_quality_ok") and not features.get("data_is_stale")
                         and number(features.get("price")) is not None
                         and number(features.get("price")) > 0 and features.get("timestamp"))
        for candidate in candidates:
            name = candidate.get("strategy")
            if name not in STRATEGIES:
                continue
            key = hashlib.sha256(f"{episode}|{name}|{PROTOCOL}".encode()).hexdigest()
            eligible = bool(native_ok and candidate.get("eligible_at_decision"))
            conn.execute("""INSERT OR IGNORE INTO strategy_trials VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """, (key, episode, decision_key, payload["symbol"], stamp.isoformat(),
                  stamp.astimezone(NY).date().isoformat(), ctx, name, baseline,
                  int(eligible), json.dumps(candidate, default=str), PROTOCOL,
                  "PENDING" if eligible else "INELIGIBLE"))
        self.guard.record_selection(conn, metadata, episode, decision_key, stamp, ctx, native_ok)

    def observe(self, conn, decision_key, bars, status):
        rows = conn.execute("""SELECT * FROM strategy_trials
            WHERE decision_key = ? AND status = 'PENDING'""", (decision_key,)).fetchall()
        for row in rows:
            if status == "PENDING":
                continue
            if status == "COMPLETE":
                candidate = json.loads(row["candidate_json"])
                result = simulate(bars, candidate.get("trigger_price"), candidate.get("invalidation_price"))
                trial_status = "COMPLETE" if number(result.get("net_r")) is not None else "AMBIGUOUS"
            else:
                result, trial_status = {"protocol": PROTOCOL, "net_r": None}, status
            conn.execute("""UPDATE strategy_trials SET status = ?, result_json = ?
                WHERE trial_key = ? AND status = 'PENDING'""",
                (trial_status, json.dumps(result), row["trial_key"]))

    def _assess(self, pairs):
        usable = [p for p in pairs if p["candidate"] is not None and p["baseline"] is not None]
        days = sorted({p["day"] for p in pairs})
        if len(days) < self.MIN_TRAIN_DAYS + self.MIN_VALIDATION_DAYS:
            return None
        boundary = days[len(days) * 2 // 3]
        boundary_time = datetime.fromisoformat(boundary).replace(tzinfo=NY).astimezone(timezone.utc)
        def training_pair(p):
            # The label window must end before the first validation day.
            return (p["day"] < boundary
                    and datetime.fromisoformat(p["decided_at"]) + timedelta(minutes=61) <= boundary_time)
        train = [p for p in usable if training_pair(p)]
        validation = [p for p in usable if p["day"] >= boundary]
        if (len(train) < self.MIN_TRAIN or len(validation) < self.MIN_VALIDATION
            or len({p["day"] for p in train}) < self.MIN_TRAIN_DAYS
            or len({p["day"] for p in validation}) < self.MIN_VALIDATION_DAYS):
            return None
        # Require coverage in each split, including missing and ambiguous paths.
        for part, predicate in ((train, training_pair),
                                (validation, lambda p: p["day"] >= boundary)):
            total = sum(predicate(p) for p in pairs)
            if len(part) / total < self.MIN_COVERAGE:
                return None
        return {"train": train, "validation": validation, "boundary": boundary}

    def refresh_if_due(self, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = now.astimezone(NY).date().isoformat()
        cutoff = datetime.fromisoformat(day).replace(tzinfo=NY).astimezone(timezone.utc)
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM strategy_evaluation_runs WHERE ny_date = ?", (day,)).fetchone():
                return False
            # Yesterday and older NY days only. Whole-day splits also purge the
            # overlapping one-hour observation windows from train/validation.
            oldest = (now.astimezone(NY).date() - timedelta(days=90)).isoformat()
            rows = [dict(r) for r in conn.execute("""SELECT * FROM strategy_trials
                WHERE eligible = 1 AND protocol = ? AND ny_date >= ? AND ny_date < ?
                  AND datetime(decided_at, '+61 minutes') <= datetime(?)
                ORDER BY decided_at, episode_key, strategy""", (PROTOCOL, oldest, day, cutoff.isoformat())).fetchall()]
        lookup = {(r["episode_key"], r["strategy"]): r for r in rows}
        contexts = {}
        seen = set()
        for row in rows:
            # At most one paired observation per symbol/NY day/strategy/context.
            identity = (row["context"], row["symbol"], row["ny_date"], row["strategy"])
            if identity in seen:
                continue
            seen.add(identity)
            baseline = lookup.get((row["episode_key"], row["baseline_strategy"]))
            if baseline is None:
                continue
            def outcome(item):
                return json.loads(item["result_json"] or "{}") if item["status"] == "COMPLETE" else {}
            result, original = outcome(row), outcome(baseline)
            contexts.setdefault(row["context"], {}).setdefault(row["strategy"], []).append({
                "day": row["ny_date"], "decided_at": row["decided_at"],
                "candidate": number(result.get("net_r")),
                "baseline": number(original.get("net_r")),
                "traded": bool(result.get("traded")), "baseline_traded": bool(original.get("traded"))})
        profiles = []
        for ctx, strategies in contexts.items():
            experiments = []
            for name, pairs in strategies.items():
                assessed = self._assess(pairs)
                if assessed is not None:
                    train = assessed["train"]
                    edge = statistics.mean(p["candidate"] - p["baseline"] for p in train)
                    experiments.append((edge, name, assessed))
            preferred = None
            evaluation = {"state": "INSUFFICIENT_DATA", "assumed_one_way_cost_pct": ONE_WAY_COST * 100}
            if experiments:
                # Winner chosen exclusively on training. A failed validation
                # never causes us to search the holdout for another winner.
                edge, name, assessed = sorted(experiments, key=lambda x: (-x[0], x[1]))[0]
                train, val = assessed["train"], assessed["validation"]
                # Stocks on the same day share market shocks. Assess uncertainty
                # over daily paired edges rather than pretending each stock is independent.
                deltas = [statistics.mean(p["candidate"] - p["baseline"] for p in val if p["day"] == d)
                          for d in sorted({p["day"] for p in val})]
                lower = statistics.mean(deltas) - 2.58 * statistics.stdev(deltas) / math.sqrt(len(deltas))
                candidate_stats = metrics([p["candidate"] for p in val])
                baseline_stats = metrics([p["baseline"] for p in val])
                trades_ok = (sum(p["traded"] for p in train) >= 20
                             and sum(p["baseline_traded"] for p in train) >= 20
                             and sum(p["traded"] for p in val) >= 10
                             and sum(p["baseline_traded"] for p in val) >= 10)
                fresh = max(p["day"] for p in val) >= (now.astimezone(NY).date() - timedelta(days=7)).isoformat()
                passed = (edge >= .10 and lower > 0 and candidate_stats["mean_r"] > 0 and trades_ok
                          and fresh and candidate_stats["drawdown_r"] <= baseline_stats["drawdown_r"] + 1
                          and candidate_stats["worst_r"] >= baseline_stats["worst_r"] - .5)
                if passed:
                    preferred = name
                evaluation.update(state="VALIDATED_PAPER_PREFERENCE" if passed else "VALIDATION_FAILED",
                    training_winner=name, training_edge_r=edge, train_pairs=len(train), validation_pairs=len(val),
                    validation_edge_lower_r=lower, candidate_validation=candidate_stats,
                    baseline_validation=baseline_stats, split_day=assessed["boundary"])
            profiles.append((ctx, preferred, now.isoformat(), (now + timedelta(hours=24)).isoformat(),
                             cutoff.isoformat(), PROTOCOL, json.dumps(evaluation)))
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM strategy_evaluation_runs WHERE ny_date = ?", (day,)).fetchone():
                return False
            profiles = self.guard.publish(conn, profiles, rows, now, cutoff)
            conn.execute("DELETE FROM strategy_preferences")
            conn.executemany("INSERT INTO strategy_preferences VALUES (?, ?, ?, ?, ?, ?, ?)", profiles)
            conn.execute("INSERT OR IGNORE INTO strategy_evaluation_runs VALUES (?, ?)", (day, now.isoformat()))
        return True

    def choose(self, analysis, *, regime, feed, now, paper, allow_live, minimum_score):
        if (not paper or allow_live or not analysis.approved or regime in {"UNKNOWN", "PANIC"}
            or feed not in {"iex", "sip"}):
            return analysis, {"mode": "BASELINE", "reason": "PREFERENCE_NOT_PERMITTED"}
        ctx = context(regime, feed, now)
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM strategy_preferences WHERE context = ?", (ctx,)).fetchone()
        if (row is None or row["protocol"] != PROTOCOL or not row["preferred_strategy"]
            or not datetime.fromisoformat(row["generated_at"]) <= now < datetime.fromisoformat(row["expires_at"])
            or datetime.fromisoformat(row["data_through"]) >= now):
            return analysis, {"mode": "BASELINE", "reason": "NO_CURRENT_VALIDATED_PREFERENCE"}
        evaluation = json.loads(row["evaluation_json"])
        if evaluation.get("state") != "VALIDATED_PAPER_PREFERENCE":
            return analysis, {"mode": "BASELINE", "reason": "PREFERENCE_NOT_VALIDATED"}
        with self.db.connection() as conn:
            blocked = self.guard.validate_selection(conn, row, evaluation, now)
        if blocked:
            return analysis, {"mode": "BASELINE", "reason": blocked}
        for candidate in analysis.candidates:
            score, trigger, stop = map(number, (candidate.score, candidate.trigger_price, candidate.invalidation_price))
            name = getattr(candidate.strategy, "value", candidate.strategy)
            if (name == row["preferred_strategy"] and name in STRATEGIES and candidate.valid
                and score is not None and score >= minimum_score
                and trigger is not None and stop is not None and trigger > stop > 0):
                selected = replace(analysis, strategy=candidate.strategy, score=score,
                    trigger_price=trigger, invalidation_price=stop,
                    reasons=list(candidate.reasons), warnings=list(candidate.warnings))
                return selected, {"mode": "VALIDATED_PAPER_PREFERENCE", "context": ctx,
                    "preferred_strategy": name, "policy_id": evaluation["policy_id"], "evaluation": evaluation}
        return analysis, {"mode": "BASELINE", "reason": "PREFERRED_CANDIDATE_NOT_NATIVE_ELIGIBLE"}

    def status_text(self):
        with self.db.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM strategy_trials WHERE eligible = 1 AND protocol = ?", (PROTOCOL,)).fetchone()[0]
            completed = conn.execute("SELECT COUNT(*) FROM strategy_trials WHERE status = 'COMPLETE' AND protocol = ?", (PROTOCOL,)).fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM strategy_preferences WHERE preferred_strategy IS NOT NULL AND expires_at > ? AND protocol = ?",
                                  (datetime.now(timezone.utc).isoformat(), PROTOCOL)).fetchone()[0]
            preferred = conn.execute("""SELECT preferred_strategy, context FROM strategy_preferences
                WHERE preferred_strategy IS NOT NULL AND expires_at > ? AND protocol = ?
                ORDER BY context LIMIT 3""", (datetime.now(timezone.utc).isoformat(), PROTOCOL)).fetchall()
        return (f"🧪 تجارب الاستراتيجيات الافتراضية: {completed}/{count}\n"
                f"تفضيلات PAPER المجتازة للتقييم: {active}\n"
                "تكلفة مفترضة 0.10% لكل جانب؛ التقييم لا يمثل أرباحًا فعلية.\n" +
                ("\n".join(f"• {r['preferred_strategy']} | {r['context']}" for r in preferred)
                 if preferred else "الاختيار الأصلي مستمر حتى اجتياز المقارنة.") +
                "\n" + self.guard.status_text())


_experiments = None


def get_strategy_experiments():
    global _experiments
    if _experiments is None:
        _experiments = StrategyExperiments()
    return _experiments
