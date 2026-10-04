"""Durable PAPER preference versions and forward-only research rollback.

This module never changes code, model weights, broker positions or risk limits.
Monitoring uses the same simulated paired returns as strategy experiments.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class StrategyPolicyGuard:
    MIN_PAIRS = 20
    MIN_DAYS = 5
    MIN_TRADES = 10
    MIN_COVERAGE = .90
    WINDOW_DAYS = 30
    COOLDOWN_DAYS = 7

    def __init__(self, db, protocol):
        self.db, self.protocol = db, protocol
        with db.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_policy_versions (
                policy_id TEXT PRIMARY KEY, context TEXT NOT NULL,
                strategy TEXT NOT NULL, protocol TEXT NOT NULL,
                activated_at TEXT NOT NULL, status TEXT NOT NULL,
                retired_at TEXT, cooldown_until TEXT,
                activation_evaluation_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_policy_active
                ON strategy_policy_versions(context) WHERE status = 'ACTIVE'""")
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_policy_observations (
                observation_key TEXT PRIMARY KEY, policy_id TEXT NOT NULL,
                episode_key TEXT NOT NULL, decided_at TEXT NOT NULL,
                selected_strategy TEXT NOT NULL, baseline_strategy TEXT NOT NULL,
                protocol TEXT NOT NULL
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_strategy_policy_observation
                ON strategy_policy_observations(policy_id, decided_at)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS strategy_policy_events (
                event_key TEXT PRIMARY KEY, policy_id TEXT NOT NULL,
                event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )""")

    def _event(self, conn, policy_id, kind, now, evidence):
        day = now.astimezone(NY).date().isoformat()
        key = hashlib.sha256(f"{policy_id}|{kind}|{day}".encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO strategy_policy_events VALUES (?, ?, ?, ?, ?)",
                     (key, policy_id, kind, now.isoformat(), json.dumps(evidence)))

    def cooling_down(self, conn, ctx, strategy, now):
        return conn.execute("""SELECT 1 FROM strategy_policy_versions
            WHERE context = ? AND strategy = ? AND protocol = ?
              AND status = 'ROLLED_BACK' AND datetime(cooldown_until) > datetime(?)
            LIMIT 1""", (ctx, strategy, self.protocol, now.isoformat())).fetchone() is not None

    def validate_selection(self, conn, profile, evaluation, now):
        if self.cooling_down(conn, profile["context"], profile["preferred_strategy"], now):
            return "ROLLBACK_COOLDOWN"
        row = conn.execute("SELECT * FROM strategy_policy_versions WHERE policy_id = ?",
                           (evaluation.get("policy_id"),)).fetchone()
        if (row is None or row["status"] != "ACTIVE" or row["protocol"] != self.protocol
            or row["context"] != profile["context"] or row["strategy"] != profile["preferred_strategy"]
            or datetime.fromisoformat(row["activated_at"]) > now):
            return "POLICY_VERSION_NOT_ACTIVE"
        return None

    def record_selection(self, conn, metadata, episode, decision_key, stamp, ctx, native_ok):
        audit = metadata.get("strategy_selection") or {}
        baseline = metadata.get("session_strategy_baseline")
        selected = audit.get("preferred_strategy")
        if (not native_ok or audit.get("mode") != "VALIDATED_PAPER_PREFERENCE"
            or selected == baseline or not audit.get("policy_id")):
            return
        profile = {"context": ctx, "preferred_strategy": selected}
        if self.validate_selection(conn, profile, audit, stamp):
            return
        # Link only the original frozen episode. A later recheck cannot assign
        # old simulated results to a newly activated policy version.
        trials = conn.execute("""SELECT strategy, decision_key FROM strategy_trials
            WHERE episode_key = ? AND protocol = ? AND eligible = 1
              AND strategy IN (?, ?)""", (episode, self.protocol, selected, baseline)).fetchall()
        if (len(trials) != 2 or any(r["decision_key"] != decision_key for r in trials)):
            return
        key = hashlib.sha256(f"{episode}|{self.protocol}".encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO strategy_policy_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (key, audit["policy_id"], episode, stamp.isoformat(), selected, baseline, self.protocol))

    def _monitor(self, conn, version, lookup, now, cutoff):
        since = max(datetime.fromisoformat(version["activated_at"]), now - timedelta(days=self.WINDOW_DAYS))
        observations = conn.execute("""SELECT * FROM strategy_policy_observations
            WHERE policy_id = ? AND protocol = ? AND datetime(decided_at) >= datetime(?)
              AND datetime(decided_at, '+61 minutes') <= datetime(?)
            ORDER BY decided_at, episode_key""",
            (version["policy_id"], self.protocol, since.isoformat(), cutoff.isoformat())).fetchall()
        pairs, seen = [], set()
        for observation in observations:
            candidate = lookup.get((observation["episode_key"], observation["selected_strategy"]))
            baseline = lookup.get((observation["episode_key"], observation["baseline_strategy"]))
            if candidate is None:
                # Preserve missing evidence in the coverage denominator.
                pairs.append({"candidate": None, "baseline": None})
                continue
            identity = (candidate["symbol"], candidate["ny_date"])
            if identity in seen:
                continue
            seen.add(identity)
            def result(row):
                value = json.loads(row["result_json"] or "{}") if row and row["status"] == "COMPLETE" else {}
                raw = value.get("net_r")
                return value if isinstance(raw, (int, float)) and math.isfinite(raw) else {}
            a, b = result(candidate), result(baseline)
            pairs.append({"day": candidate["ny_date"], "candidate": a.get("net_r"),
                          "baseline": b.get("net_r"), "traded": bool(a.get("traded")),
                          "baseline_traded": bool(b.get("traded"))})
        usable = [p for p in pairs if p["candidate"] is not None and p["baseline"] is not None]
        days = sorted({p["day"] for p in usable})
        evidence = {"state": "WAITING_FORWARD_DATA", "pairs": len(usable), "days": len(days),
                    "coverage": len(usable) / len(pairs) if pairs else 0,
                    "scope": "SIMULATED_NOT_REALIZED_PNL", "data_through": cutoff.isoformat()}
        if (len(usable) < self.MIN_PAIRS or len(days) < self.MIN_DAYS
            or evidence["coverage"] < self.MIN_COVERAGE
            or sum(p["traded"] for p in usable) < self.MIN_TRADES
            or sum(p["baseline_traded"] for p in usable) < self.MIN_TRADES
            or days[-1] < (now.astimezone(NY).date() - timedelta(days=7)).isoformat()):
            return evidence
        daily = [statistics.mean(p["candidate"] - p["baseline"] for p in usable if p["day"] == d) for d in days]
        mean = statistics.mean(daily)
        upper = mean + 2.58 * statistics.stdev(daily) / math.sqrt(len(daily))
        def drawdown(key):
            equity = peak = worst = 0.0
            for p in usable:
                equity += p[key]
                peak = max(peak, equity)
                worst = max(worst, peak - equity)
            return worst
        extra_drawdown = drawdown("candidate") - drawdown("baseline")
        failed = upper < -.10 or (mean < -.10 and extra_drawdown > 3.0)
        evidence.update(state="DEGRADED" if failed else "NO_PROVEN_DEGRADATION",
                        daily_edge_r=mean, daily_edge_upper_r=upper,
                        excess_drawdown_r=extra_drawdown)
        return evidence

    def publish(self, conn, profiles, rows, now, cutoff):
        """Called in the same transaction as profiles and the daily run marker."""
        lookup = {(r["episode_key"], r["strategy"]): r for r in rows}
        versions = {r["context"]: dict(r) for r in conn.execute(
            "SELECT * FROM strategy_policy_versions WHERE status = 'ACTIVE'").fetchall()}
        monitoring = {}
        for ctx, version in versions.items():
            if version["protocol"] != self.protocol:
                continue
            evidence = self._monitor(conn, version, lookup, now, cutoff)
            monitoring[ctx] = evidence
            self._event(conn, version["policy_id"], "MONITORED", now, evidence)
            if evidence["state"] == "DEGRADED":
                conn.execute("""UPDATE strategy_policy_versions SET status = 'ROLLED_BACK',
                    retired_at = ?, cooldown_until = ? WHERE policy_id = ? AND status = 'ACTIVE'""",
                    (now.isoformat(), (now + timedelta(days=self.COOLDOWN_DAYS)).isoformat(), version["policy_id"]))
                version["status"] = "ROLLED_BACK"
                self._event(conn, version["policy_id"], "ROLLED_BACK", now, evidence)
        final = []
        for profile in profiles:
            ctx, preferred, generated, expires, through, protocol, encoded = profile
            evaluation = json.loads(encoded)
            if preferred and (protocol != self.protocol or evaluation.get("state") != "VALIDATED_PAPER_PREFERENCE"):
                evaluation["state"] = "POLICY_NOT_VALIDATED"
                preferred = None
            if monitoring.get(ctx, {}).get("state") == "DEGRADED":
                evaluation.update(state="ROLLED_BACK", blocked_strategy=versions[ctx]["strategy"])
                preferred = None
            elif preferred and self.cooling_down(conn, ctx, preferred, now):
                evaluation.update(state="ROLLBACK_COOLDOWN", blocked_strategy=preferred)
                preferred = None
            old = versions.get(ctx)
            keep = bool(preferred and old and old["status"] == "ACTIVE"
                        and old["strategy"] == preferred and old["protocol"] == protocol)
            if preferred:
                if not keep:
                    if old and old["status"] == "ACTIVE":
                        self._retire(conn, old, now, "SUPERSEDED")
                    policy_id = hashlib.sha256(f"{ctx}|{preferred}|{protocol}|{generated}".encode()).hexdigest()
                    conn.execute("INSERT INTO strategy_policy_versions VALUES (?, ?, ?, ?, ?, 'ACTIVE', NULL, NULL, ?)",
                                 (policy_id, ctx, preferred, protocol, generated, encoded))
                    self._event(conn, policy_id, "ACTIVATED", now, evaluation)
                else:
                    policy_id = old["policy_id"]
                    self._event(conn, policy_id, "REVALIDATED", now, evaluation)
                evaluation["policy_id"] = policy_id
            if ctx in monitoring:
                evaluation["forward_monitor"] = monitoring[ctx]
            final.append((ctx, preferred, generated, expires, through, protocol, json.dumps(evaluation)))
        chosen = {p[0]: p[1] for p in final}
        for ctx, version in versions.items():
            if version["status"] == "ACTIVE" and chosen.get(ctx) != version["strategy"]:
                self._retire(conn, version, now, "NO_VALIDATED_PREFERENCE")
        return final

    def _retire(self, conn, version, now, reason):
        conn.execute("UPDATE strategy_policy_versions SET status = 'RETIRED', retired_at = ? WHERE policy_id = ? AND status = 'ACTIVE'",
                     (now.isoformat(), version["policy_id"]))
        version["status"] = "RETIRED"
        self._event(conn, version["policy_id"], "RETIRED", now, {"reason": reason})

    def status_text(self):
        now = datetime.now(timezone.utc)
        with self.db.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM strategy_policy_versions WHERE protocol = ?", (self.protocol,)).fetchone()[0]
            blocked = conn.execute("""SELECT strategy FROM strategy_policy_versions WHERE protocol = ?
                AND status = 'ROLLED_BACK' AND datetime(cooldown_until) > datetime(?)
                ORDER BY retired_at DESC LIMIT 3""", (self.protocol, now.isoformat())).fetchall()
        return (f"سجل تفضيلات الاستراتيجيات: {total} إصدار؛ مراقبة التراجع مفعلة.\n" +
                ("تفضيلات موقوفة مؤقتًا: " + "، ".join(r["strategy"] for r in blocked)
                 if blocked else "لا توجد تفضيلات موقوفة بسبب تراجع مثبت."))
