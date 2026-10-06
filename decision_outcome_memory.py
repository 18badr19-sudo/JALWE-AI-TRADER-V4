"""Immutable decision evidence and subsequent price observations; research only.

Includes rejected decisions. Measurements are NOT executed trades, profit labels,
or proof that a rejection was wrong. No model weights or trading gates are changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from strategy_experiments import StrategyExperiments

logger = logging.getLogger(__name__)


def utc(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Decision timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


def price(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


class DecisionOutcomeMemory:
    HORIZON_MINUTES = 60
    DATA_GRACE_MINUTES = 10
    BATCH_SIZE = 4

    def __init__(self, db=None, market_data=None):
        if db is None:
            from core.database import database
            db = database
        self.db = db
        self.experiments = StrategyExperiments(db)
        self.market_data = market_data
        self._worker_lock = threading.Lock()
        self._next_update = 0.0
        with self.db.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_outcome_memory (
                    decision_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    research_version TEXT NOT NULL,
                    source_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    strategy TEXT,
                    market_regime TEXT,
                    decided_at TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    reference_price REAL,
                    data_feed TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observed_bars INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    last_checked_at TEXT,
                    check_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_decision_outcomes_due
                ON decision_outcome_memory(status, last_checked_at, window_end)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_decision_outcomes_date
                ON decision_outcome_memory(decided_at)""")

    def record(self, payload: dict, research_version: str) -> bool:
        decision = payload.get("jalwe") or {}
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or decision.get("state") not in {
            "REJECTED", "WATCHING", "READY_FOR_PAPER_EXECUTION",
        }:
            return False
        decided_at = utc(payload["timestamp"])
        metadata = decision.get("metadata") or {}
        features = metadata.get("decision_features") or {}
        reference = price(features.get("price"))
        if not features.get("data_quality_ok") or features.get("data_is_stale"):
            reference = None
        # Deduplicate identical rechecks of one report, but preserve changed
        # decisions/evidence. Timestamps and execution attempts are not features.
        fingerprint = json.dumps(decision, sort_keys=True, default=str, ensure_ascii=False)
        key = hashlib.sha256(
            f"{symbol}|{research_version}|{fingerprint}".encode()
        ).hexdigest()
        window_start = pd.Timestamp(decided_at).ceil("min").to_pydatetime()
        window_end = window_start + timedelta(minutes=self.HORIZON_MINUTES)
        feed = metadata.get("decision_data_feed") or (metadata.get("quality_inputs") or {}).get("feed")
        with self.db.connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO decision_outcome_memory (
                    decision_key, symbol, research_version, source_state, reason,
                    strategy, market_regime, decided_at, window_start, window_end,
                    reference_price, data_feed, payload_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (key, symbol, str(research_version), decision["state"],
                  str(decision.get("reason") or ""), decision.get("strategy"),
                  decision.get("market_regime"), decided_at.isoformat(),
                  window_start.isoformat(), window_end.isoformat(), reference, feed,
                  json.dumps(payload, default=str, ensure_ascii=False),
                  "PENDING" if reference is not None else "UNOBSERVABLE"))
            if cursor.rowcount > 0:
                self.experiments.record_candidates(conn, payload, key, research_version)
        return cursor.rowcount > 0

    @staticmethod
    def _valid_bars(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
        columns = ["open", "high", "low", "close", "volume"]
        if frame is None or frame.empty or not set(columns).issubset(frame.columns):
            return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
        frame = frame[columns].copy()
        frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
        for column in columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        finite = frame.apply(lambda col: col.map(lambda x: pd.notna(x) and math.isfinite(x))).all(axis=1)
        valid = (finite & (frame["low"] > 0) & (frame["volume"] > 0)
                 & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
                 & (frame["low"] <= frame[["open", "close"]].min(axis=1)))
        frame = frame.loc[valid & (frame.index >= start)
                          & (frame.index + pd.Timedelta(minutes=1) <= end)]
        # Only genuine one-minute boundaries are accepted; no gap filling.
        frame = frame.loc[frame.index == frame.index.floor("min")]
        return frame.loc[~frame.index.duplicated(keep="first")].sort_index()

    @staticmethod
    def _level_observation(bars: pd.DataFrame, decision: dict, complete: bool) -> dict:
        entry, stop, target = (price(decision.get(name))
                               for name in ("entry_price", "stop_price", "target_1"))
        theoretical = target is None
        if entry is None or stop is None or not entry > stop:
            return {"outcome": "NO_VALID_SETUP_LEVELS"}
        if theoretical:
            target = entry + 2 * (entry - stop)
        if target <= entry:
            return {"outcome": "NO_VALID_SETUP_LEVELS"}
        result = {"trigger_price": entry, "stop_price": stop, "target_1": target,
                  "theoretical_target": theoretical,
                  "activation_rule": "FIRST_COMPLETED_1M_CLOSE_AT_OR_ABOVE_TRIGGER"}
        activated = bars.loc[bars["close"] >= entry]
        if activated.empty:
            result["outcome"] = "NOT_ACTIVATED" if complete else "INCOMPLETE_PATH"
            return result
        first = activated.index[0]
        result["activated_at"] = (first + pd.Timedelta(minutes=1)).isoformat()
        result["activation_close"] = float(activated["close"].iloc[0])
        if not complete:
            result["outcome"] = "INCOMPLETE_PATH"
            return result
        if result["activation_close"] >= target:
            result["outcome"] = "ACTIVATION_PAST_TARGET"
            return result
        # Intrabar levels before the activation close cannot be post-entry hits.
        after = bars.loc[bars.index > first]
        hit_stop = after.loc[after["low"] <= stop]
        hit_target = after.loc[after["high"] >= target]
        stop_at = None if hit_stop.empty else hit_stop.index[0]
        target_at = None if hit_target.empty else hit_target.index[0]
        result.update(stop_touch_at=stop_at.isoformat() if stop_at is not None else None,
                      target_touch_at=target_at.isoformat() if target_at is not None else None)
        if stop_at is not None and target_at is not None and stop_at == target_at:
            result["outcome"] = "AMBIGUOUS_STOP_TARGET"
        elif stop_at is not None and (target_at is None or stop_at < target_at):
            result["outcome"] = "STOP_FIRST"
        elif target_at is not None:
            result["outcome"] = "TARGET_FIRST"
        else:
            result["outcome"] = "NO_LEVEL"
        return result

    def update_due(self, *, now: datetime | None = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.db.connection() as conn:
            rows = [dict(row) for row in conn.execute("""
                SELECT * FROM decision_outcome_memory
                WHERE status = 'PENDING' AND window_end <= ?
                ORDER BY last_checked_at IS NOT NULL, last_checked_at, window_end
                LIMIT ?
            """, (now.isoformat(), self.BATCH_SIZE)).fetchall()]
        if not rows:
            return 0
        if self.market_data is None:
            from market.market_data import get_market_data
            self.market_data = get_market_data()
        updated = 0
        for row in rows:
            start, end = utc(row["window_start"]), utc(row["window_end"])
            terminal = now >= end + timedelta(minutes=self.DATA_GRACE_MINUTES)
            try:
                frame = self.market_data.get_observation_bars(row["symbol"], start, end)
                observed_feed = frame.attrs.get("data_feed")
                if observed_feed and row["data_feed"] and observed_feed != row["data_feed"]:
                    raise ValueError("Observation feed differs from decision feed")
                bars = self._valid_bars(frame, start, end)
                complete = len(bars) == self.HORIZON_MINUTES
                baseline = float(row["reference_price"])
                payload = json.loads(row["payload_json"])
                result = {"scope": "OBSERVED_PRICE_PATH_NOT_EXECUTED_PNL",
                          "expected_bars": self.HORIZON_MINUTES,
                          "observed_bars": len(bars), "complete_path": complete,
                          "feed": observed_feed or row["data_feed"],
                          "max_observed_return_pct": None if bars.empty else
                              (float(bars["high"].max()) / baseline - 1) * 100,
                          "min_observed_return_pct": None if bars.empty else
                              (float(bars["low"].min()) / baseline - 1) * 100,
                          "levels": self._level_observation(bars, payload["jalwe"], complete)}
                checkpoints = {}
                for minute in (5, 15, 30, 60):
                    stamp = pd.Timestamp(start + timedelta(minutes=minute - 1))
                    checkpoints[str(minute)] = (float(bars.at[stamp, "close"])
                                                if stamp in bars.index else None)
                result["checkpoint_close_prices"] = checkpoints
                status = "COMPLETE" if complete else "PARTIAL_DATA" if terminal else "PENDING"
            except Exception as exc:
                # Provider failures are data failures, never losing/no-opportunity labels.
                result = {"scope": "OBSERVED_PRICE_PATH_NOT_EXECUTED_PNL",
                          "error_type": type(exc).__name__, "complete_path": False}
                bars = []
                status = "DATA_UNAVAILABLE" if terminal else "PENDING"
                logger.warning("Decision outcome data unavailable for %s (%s)",
                               row["symbol"], type(exc).__name__)
            with self.db.connection() as conn:
                conn.execute("""UPDATE decision_outcome_memory SET status = ?,
                    observed_bars = ?, result_json = ?, last_checked_at = ?,
                    check_count = check_count + 1 WHERE decision_key = ? AND status = 'PENDING'
                """, (status, len(bars), json.dumps(result), now.isoformat(), row["decision_key"]))
                self.experiments.observe(conn, row["decision_key"], bars, status)
            updated += 1
        return updated

    def schedule_update(self) -> bool:
        """Single bounded background worker so research does not hold up trade management."""
        if time.monotonic() < self._next_update or not self._worker_lock.acquire(blocking=False):
            return False
        self._next_update = time.monotonic() + 60

        def run():
            try:
                self.update_due()
                self.experiments.refresh_if_due()
            except Exception:
                logger.exception("Decision outcome refresh failed")
            finally:
                self._worker_lock.release()
        try:
            threading.Thread(target=run, name="decision-outcomes", daemon=True).start()
        except Exception:
            self._worker_lock.release()
            raise
        return True

    @staticmethod
    def _freshness_review(rows: list[dict]) -> dict:
        counts = {"AGE_LIMIT": 0, "FUTURE_TIMESTAMP": 0,
                  "MISSING_EVIDENCE": 0, "OTHER_STALE_FLAG": 0}
        periods = {"regular_clock": 0, "outside_regular_clock": 0}
        samples = []
        for row in rows:
            if row["reason"] != "Feature freshness check failed.":
                continue
            try:
                metadata = (json.loads(row["payload_json"]).get("jalwe") or {}).get("metadata") or {}
                diagnostics = metadata.get("feature_diagnostics") or {}
                age = float(diagnostics["latest_bar_age_minutes"])
                limit = float(diagnostics["max_bar_age_minutes"])
                if not math.isfinite(age) or not math.isfinite(limit):
                    raise ValueError("Non-finite evidence")
                cause = "FUTURE_TIMESTAMP" if age < 0 else "AGE_LIMIT" if age > limit else "OTHER_STALE_FLAG"
                local = utc(row["decided_at"]).astimezone(ZoneInfo("America/New_York"))
                regular = 570 <= local.hour * 60 + local.minute < 960
                periods["regular_clock" if regular else "outside_regular_clock"] += 1
                samples.append({"symbol": row["symbol"], "decided_at": row["decided_at"],
                                "age_minutes": age, "max_age_minutes": limit,
                                "feed": metadata.get("decision_data_feed") or "unknown",
                                "bar_time": (metadata.get("decision_features") or {}).get("timestamp"),
                                "regular_clock": regular, "cause": cause})
            except (TypeError, ValueError, KeyError, AttributeError):
                cause = "MISSING_EVIDENCE"
            counts[cause] += 1
        # Show near-threshold regular-session cases first, not just overnight extremes.
        samples.sort(key=lambda row: (not row["regular_clock"], abs(row["age_minutes"] - row["max_age_minutes"])))
        return {"counts": counts, "periods_with_evidence": periods, "samples": samples[:5]}

    def rejection_audit(self, date_value) -> dict:
        """Explain recorded decisions, never infer evaluation from default-false gates."""
        day = datetime.fromisoformat(str(date_value)).date()
        start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York"))
        end = start + timedelta(days=1)
        with self.db.connection() as conn:
            rows = [dict(row) for row in conn.execute("""
                SELECT symbol, research_version, source_state, reason, status, decided_at,
                       payload_json, result_json
                FROM decision_outcome_memory WHERE decided_at >= ? AND decided_at < ?
                ORDER BY decided_at ASC
            """, (start.astimezone(timezone.utc).isoformat(),
                  end.astimezone(timezone.utc).isoformat())).fetchall()]
        states = {}
        reviews = []
        score_evidence = []
        for state in ("REJECTED", "WATCHING", "READY_FOR_PAPER_EXECUTION"):
            selected = [row for row in rows if row["source_state"] == state]
            reasons = {}
            for row in selected:
                reason = row["reason"] or "No recorded reason"
                group = reasons.setdefault(reason, {"reason": reason, "count": 0,
                                                     "symbols": set(), "episodes": set()})
                group["count"] += 1
                group["symbols"].add(row["symbol"])
                group["episodes"].add((row["symbol"], row["research_version"]))
            groups = []
            for group in reasons.values():
                groups.append({**group, "symbols": sorted(group["symbols"]),
                               "episodes": len(group["episodes"])})
            states[state] = {"count": len(selected),
                             "symbols": len({row["symbol"] for row in selected}),
                             "episodes": len({(row["symbol"], row["research_version"]) for row in selected}),
                             "reasons": sorted(groups, key=lambda g: (-g["count"], g["reason"]))}
        rejected = [row for row in rows if row["source_state"] == "REJECTED"]
        for row in rejected:
            if row["reason"] != "OpportunityEngine rejected the setup.":
                continue
            try:
                payload = json.loads(row["payload_json"])
                evidence = ((payload.get("jalwe") or {}).get("metadata") or {}).get("opportunity_evidence") or {}
                score, threshold = float(evidence["score"]), float(evidence["minimum_score"])
                if math.isfinite(score) and math.isfinite(threshold) and evidence.get("approved") is False:
                    score_evidence.append({"symbol": row["symbol"], "score": score,
                                           "minimum_score": threshold})
            except (TypeError, ValueError, KeyError):
                continue
        verified = [row for row in rejected if row["status"] == "COMPLETE"]
        for row in verified:
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if (result.get("levels") or {}).get("outcome") == "TARGET_FIRST":
                reviews.append({"symbol": row["symbol"], "reason": row["reason"],
                                "decided_at": row["decided_at"],
                                "research_version": row["research_version"]})
        return {"date": day.isoformat(), "states": states,
                "rejected_complete": len(verified),
                "rejected_incomplete": len(rejected) - len(verified),
                "review_candidates": reviews, "opportunity_score_evidence": score_evidence,
                "freshness_review": self._freshness_review(rows)}

    @staticmethod
    def rejection_report_text(audit: dict, *, compact: bool = False) -> str:
        labels = {"REJECTED": "مرفوضة", "WATCHING": "انتظار", "READY_FOR_PAPER_EXECUTION": "جاهزة"}
        translations = {
            "OpportunityEngine rejected the setup.": "جودة الفرصة لم تجتز OpportunityEngine",
            "Market regime / strategy router rejected the setup.": "حالة السوق أو اختيار الاستراتيجية",
            "No session strategy reached the required quality threshold.": "جودة استراتيجية الجلسة أقل من المطلوب",
            "Valid setup is close to its breakout trigger.": "ينتظر الوصول إلى مستوى الاختراق",
            "Valid setup found but price is not yet near the trigger.": "السعر بعيد عن مستوى الاختراق",
            "Price is too extended above the trigger. Do not chase.": "السعر ممتد؛ منع مطاردة الدخول",
            "Breakout happened too many bars ago. Entry window expired.": "انتهت نافذة الدخول بعد الاختراق",
            "Price reached the setup invalidation level before entry.": "وصل السعر إلى إلغاء الفرصة قبل الدخول",
            "All native JALWE decision gates passed.": "اجتازت جميع بوابات القرار",
        }
        lines = [f"🔎 أسباب قلة الدخول — {audit['date']} NY"]
        for state, label in labels.items():
            data = audit["states"][state]
            lines.append(f"{label}: {data['count']} قرار | {data['episodes']} تقرير سهم | {data['symbols']} سهم مختلف")
            if state == "READY_FOR_PAPER_EXECUTION":
                continue
            for group in data["reasons"][:2 if compact else 4]:
                reason = translations.get(group["reason"], group["reason"])
                examples = ", ".join(group["symbols"][:3])
                lines.append(f"• {reason[:150]}: {group['count']} | تقارير {group['episodes']} | {examples}")
        freshness = audit.get("freshness_review", {})
        causes = freshness.get("counts", {})
        if sum(causes.values()):
            lines.append(f"تقادم البيانات: تجاوز العمر {causes.get('AGE_LIMIT', 0)} | وقت مستقبلي {causes.get('FUTURE_TIMESTAMP', 0)} | دليل ناقص {causes.get('MISSING_EVIDENCE', 0)} | علامة تقادم أخرى {causes.get('OTHER_STALE_FLAG', 0)}")
            if not compact:
                for sample in freshness.get("samples", [])[:2]:
                    lines.append(f"• {sample['symbol']}: عمر الشمعة {sample['age_minutes']:.1f} دقيقة / الحد {sample['max_age_minutes']:.1f} | {sample['feed']}")
        scores = audit.get("opportunity_score_evidence", [])
        if scores:
            below = sum(row["score"] < row["minimum_score"] for row in scores)
            lines.append(f"رفض جودة بدرجة وحد مسجلين: {len(scores)} | أقل من الحد: {below}")
            if not compact:
                for row in scores[:3]:
                    lines.append(f"• {row['symbol']}: درجة {row['score']:.2f} / المطلوب {row['minimum_score']:.2f}")
        lines.extend([
            f"مرفوضة ببيانات 60m مكتملة: {audit['rejected_complete']} | غير مكتملة: {audit['rejected_incomplete']}",
            f"مرشحة للمراجعة (هدف قبل الوقف لاحقًا): {len(audit['review_candidates'])}",
        ])
        if not compact:
            for row in audit["review_candidates"][:5]:
                stamp = row.get("decided_at")
                clock = datetime.fromisoformat(stamp).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S NY") if stamp else ""
                lines.append(f"• مراجعة {row['symbol']} {clock}: {row['reason'][:120]}")
        lines.extend([
            "الأعداد قرارات بحثية؛ تغير القرار لنفس السهم قد يتكرر، والجاهزية لا تثبت تنفيذ شراء.",
            "سبب الرفض مأخوذ من القرار؛ البوابات غير المفحوصة لا تُحسب أسبابًا إضافية.",
            "الوصول لاحقًا إلى هدف لا يثبت خطأ الرفض؛ البيانات الناقصة لا تحسم النتيجة.",
        ])
        return "\n".join(lines)[:3900]

    def summary_for_ny_date(self, date_value) -> dict:
        day = datetime.fromisoformat(str(date_value)).date()
        start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York"))
        end = start + timedelta(days=1)
        with self.db.connection() as conn:
            groups = [dict(row) for row in conn.execute("""
                SELECT source_state, COALESCE(strategy, 'UNSPECIFIED') AS strategy,
                       status, COUNT(*) AS count
                FROM decision_outcome_memory WHERE decided_at >= ? AND decided_at < ?
                GROUP BY source_state, strategy, status
            """, (start.astimezone(timezone.utc).isoformat(),
                  end.astimezone(timezone.utc).isoformat())).fetchall()]
            outcomes = [dict(row) for row in conn.execute("""
                SELECT source_state, COALESCE(strategy, 'UNSPECIFIED') AS strategy,
                       json_extract(result_json, '$.levels.outcome') AS outcome,
                       COUNT(*) AS count
                FROM decision_outcome_memory
                WHERE decided_at >= ? AND decided_at < ? AND status = 'COMPLETE'
                GROUP BY source_state, strategy, outcome
            """, (start.astimezone(timezone.utc).isoformat(),
                  end.astimezone(timezone.utc).isoformat())).fetchall()]
        counts = {state: sum(g["count"] for g in groups if g["source_state"] == state)
                  for state in ("REJECTED", "WATCHING", "READY_FOR_PAPER_EXECUTION")}
        statuses = {state: sum(g["count"] for g in groups if g["status"] == state)
                    for state in ("COMPLETE", "PENDING", "PARTIAL_DATA", "DATA_UNAVAILABLE", "UNOBSERVABLE")}
        return {"decisions": counts, "statuses": statuses, "groups": groups, "outcomes": outcomes,
                "strategy_experiments": self.experiments.status_text(),
                "rejection_audit": self.rejection_audit(day)}

    @staticmethod
    def report_text(summary: dict) -> str:
        decisions, statuses = summary["decisions"], summary["statuses"]
        counts = {outcome: sum(row["count"] for row in summary["outcomes"]
                              if row["outcome"] == outcome)
                  for outcome in ("TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_STOP_TARGET", "NOT_ACTIVATED")}
        return "\n".join([
            "🧠 ذاكرة نتائج القرارات — نافذة 60 دقيقة",
            f"مرفوضة: {decisions['REJECTED']} | متابعة: {decisions['WATCHING']} | جاهزة: {decisions['READY_FOR_PAPER_EXECUTION']}",
            f"اكتملت بياناتها: {statuses['COMPLETE']} | تنتظر: {statuses['PENDING']}",
            f"بيانات ناقصة/غير متاحة: {statuses['PARTIAL_DATA'] + statuses['DATA_UNAVAILABLE']}",
            f"بدون سعر مرجعي موثوق: {statuses['UNOBSERVABLE']}",
            f"المسارات المكتملة: هدف أولًا {counts['TARGET_FIRST']} | وقف أولًا {counts['STOP_FIRST']} | ترتيب غير محسوم {counts['AMBIGUOUS_STOP_TARGET']}",
            f"لم تتأكد فوق مستوى التفعيل: {counts['NOT_ACTIVATED']}",
            "القياس من شموع 1m وبحسب تغطية مزوّد البيانات؛ أهداف 2R افتراضية عند غياب الهدف الأصلي.",
            "نتائج بحثية؛ لا تُحسب أرباحًا فعلية ولا تغيّر شروط الدخول.",
            summary.get("strategy_experiments", ""),
            (DecisionOutcomeMemory.rejection_report_text(summary["rejection_audit"], compact=True)
             if summary.get("rejection_audit") else ""),
        ])


_memory = None


def get_decision_outcome_memory() -> DecisionOutcomeMemory:
    global _memory
    if _memory is None:
        _memory = DecisionOutcomeMemory()
    return _memory
