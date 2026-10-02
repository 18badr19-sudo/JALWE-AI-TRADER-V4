from __future__ import annotations

import hashlib
import json
import math
import time

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from core.database import database
from market.market_data import get_market_data


class OpportunityPerformanceTracker:
    """
    Tracks high-quality JALWE opportunities even when no PAPER order is placed.

    A setup is tracked only after every pre-trigger gate has passed:
      market_data, features, ai, opportunity, market_regime,
      strategy_router, and session_strategy.

    The tracker records:
      - price after 5 / 15 / 30 / 60 minutes
      - MFE / MAE
      - Stop / T1 / T2 / T3 touch times
      - whether Stop or T1 was reached first

    It never submits orders and never changes DecisionEngine outcomes.
    """

    CHECKPOINTS = (5, 15, 30, 60)
    PRE_ENTRY_GATES = (
        "market_data",
        "features",
        "ai",
        "opportunity",
        "market_regime",
        "strategy_router",
        "session_strategy",
    )

    def __init__(self) -> None:
        self.market_data = get_market_data()
        self._last_update_epoch = 0.0
        self._ensure_table()

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _ensure_table(self) -> None:
        with database.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS opportunity_performance (
                    signal_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy TEXT,
                    source_state TEXT,
                    started_at TEXT NOT NULL,

                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    target_3 REAL NOT NULL,

                    ai_score REAL,
                    opportunity_score REAL,
                    session_score REAL,
                    market_regime TEXT,

                    mfe_price REAL,
                    mae_price REAL,
                    mfe_pct REAL,
                    mae_pct REAL,

                    price_5m REAL,
                    price_15m REAL,
                    price_30m REAL,
                    price_60m REAL,

                    hit_stop_at TEXT,
                    hit_t1_at TEXT,
                    hit_t2_at TEXT,
                    hit_t3_at TEXT,

                    first_level_hit TEXT,
                    first_level_hit_at TEXT,

                    status TEXT NOT NULL DEFAULT 'TRACKING',
                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _eligible(self, payload: dict[str, Any]) -> bool:
        jalwe = payload.get("jalwe")
        if not isinstance(jalwe, dict):
            return False

        state = str(jalwe.get("state") or "").upper()
        if state not in {"WATCHING", "READY_FOR_PAPER_EXECUTION"}:
            return False

        gates = jalwe.get("gates")
        if not isinstance(gates, dict):
            return False

        if not all(bool(gates.get(name, False)) for name in self.PRE_ENTRY_GATES):
            return False

        entry = self._float(jalwe.get("entry_price"))
        stop = self._float(jalwe.get("stop_price"))

        return bool(
            entry is not None
            and stop is not None
            and entry > stop > 0
        )

    def start_from_payload(
        self,
        payload: dict[str, Any],
        research_version: str,
    ) -> bool:
        if not self._eligible(payload):
            return False

        jalwe = dict(payload.get("jalwe") or {})
        symbol = str(payload.get("symbol") or "").strip().upper()

        if not symbol:
            return False

        entry = float(jalwe["entry_price"])
        stop = float(jalwe["stop_price"])
        risk_per_share = entry - stop

        if risk_per_share <= 0:
            return False

        target_1 = self._float(jalwe.get("target_1"))
        target_2 = self._float(jalwe.get("target_2"))
        target_3 = self._float(jalwe.get("target_3"))

        # WATCHING setups normally do not have RiskEngine targets yet.
        # Use the same 2R / 3R / 4R model for theoretical evaluation.
        target_1 = target_1 if target_1 and target_1 > entry else entry + risk_per_share * 2.0
        target_2 = target_2 if target_2 and target_2 > target_1 else entry + risk_per_share * 3.0
        target_3 = target_3 if target_3 and target_3 > target_2 else entry + risk_per_share * 4.0

        version = str(research_version or payload.get("timestamp") or "").strip()
        raw_key = f"{symbol}|{version}".encode("utf-8")
        signal_key = hashlib.sha256(raw_key).hexdigest()

        metadata = {
            "research_version": version,
            "reason": jalwe.get("reason"),
            "gates": jalwe.get("gates"),
            "theoretical_targets": not all(
                self._float(jalwe.get(name)) is not None
                for name in ("target_1", "target_2", "target_3")
            ),
        }

        started_at = str(payload.get("timestamp") or "").strip()
        if not started_at:
            started_at = datetime.now(timezone.utc).isoformat()

        with database.connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO opportunity_performance (
                    signal_key,
                    symbol,
                    strategy,
                    source_state,
                    started_at,
                    entry_price,
                    stop_price,
                    target_1,
                    target_2,
                    target_3,
                    ai_score,
                    opportunity_score,
                    session_score,
                    market_regime,
                    status,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?)
                """,
                (
                    signal_key,
                    symbol,
                    str(jalwe.get("strategy") or ""),
                    str(jalwe.get("state") or ""),
                    started_at,
                    entry,
                    stop,
                    float(target_1),
                    float(target_2),
                    float(target_3),
                    self._float(jalwe.get("ai_score")),
                    self._float(jalwe.get("opportunity_score")),
                    self._float(jalwe.get("session_strategy_score")),
                    str(jalwe.get("market_regime") or ""),
                    json.dumps(metadata, default=str),
                ),
            )

        return bool(cursor.rowcount and cursor.rowcount > 0)

    @staticmethod
    def _first_touch_time(
        bars: pd.DataFrame,
        *,
        level: float,
        mode: str,
    ) -> Optional[str]:
        if bars is None or bars.empty:
            return None

        if mode == "low":
            matched = bars[bars["low"] <= level]
        else:
            matched = bars[bars["high"] >= level]

        if matched.empty:
            return None

        ts = pd.Timestamp(matched.index[0])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        return ts.to_pydatetime().isoformat()

    @staticmethod
    def _checkpoint_price(
        bars: pd.DataFrame,
        target_time: datetime,
    ) -> Optional[float]:
        if bars is None or bars.empty:
            return None

        idx = pd.DatetimeIndex(bars.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")

        target = pd.Timestamp(target_time)
        eligible = bars.loc[idx < target]

        if eligible.empty:
            return None

        value = float(eligible["close"].iloc[-1])
        return value if math.isfinite(value) and value > 0 else None

    def _update_row(self, row: dict[str, Any], now: datetime) -> None:
        symbol = str(row["symbol"]).upper()
        started_at = self._parse_utc(row["started_at"])
        age_minutes = (now - started_at).total_seconds() / 60.0

        bars = self.market_data.get_bars(
            symbol=symbol,
            timeframe="1m",
            limit=180,
        ).copy()

        idx = pd.DatetimeIndex(bars.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")

        bars.index = idx

        # Do not count the partly elapsed minute in which the signal appeared.
        first_full_minute = pd.Timestamp(started_at).ceil("min")
        segment = bars.loc[bars.index >= first_full_minute]

        if segment.empty:
            return

        entry = float(row["entry_price"])
        stop = float(row["stop_price"])
        t1 = float(row["target_1"])
        t2 = float(row["target_2"])
        t3 = float(row["target_3"])

        mfe_price = float(segment["high"].max())
        mae_price = float(segment["low"].min())

        mfe_pct = (mfe_price - entry) / entry * 100.0
        mae_pct = (mae_price - entry) / entry * 100.0

        values: dict[str, Any] = {
            "mfe_price": mfe_price,
            "mae_price": mae_price,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
        }

        for minutes in self.CHECKPOINTS:
            column = f"price_{minutes}m"
            if row.get(column) is None and age_minutes >= minutes + 1:
                values[column] = self._checkpoint_price(
                    segment,
                    started_at + timedelta(minutes=minutes),
                )

        hit_stop = row.get("hit_stop_at") or self._first_touch_time(
            segment,
            level=stop,
            mode="low",
        )
        hit_t1 = row.get("hit_t1_at") or self._first_touch_time(
            segment,
            level=t1,
            mode="high",
        )
        hit_t2 = row.get("hit_t2_at") or self._first_touch_time(
            segment,
            level=t2,
            mode="high",
        )
        hit_t3 = row.get("hit_t3_at") or self._first_touch_time(
            segment,
            level=t3,
            mode="high",
        )

        values.update(
            {
                "hit_stop_at": hit_stop,
                "hit_t1_at": hit_t1,
                "hit_t2_at": hit_t2,
                "hit_t3_at": hit_t3,
            }
        )

        first_level = row.get("first_level_hit")
        first_level_at = row.get("first_level_hit_at")

        if not first_level:
            stop_dt = self._parse_utc(hit_stop) if hit_stop else None
            t1_dt = self._parse_utc(hit_t1) if hit_t1 else None

            if stop_dt and t1_dt:
                if stop_dt == t1_dt:
                    first_level = "AMBIGUOUS_STOP_T1"
                    first_level_at = hit_stop
                elif stop_dt < t1_dt:
                    first_level = "STOP"
                    first_level_at = hit_stop
                else:
                    first_level = "T1"
                    first_level_at = hit_t1
            elif stop_dt:
                first_level = "STOP"
                first_level_at = hit_stop
            elif t1_dt:
                first_level = "T1"
                first_level_at = hit_t1

        values["first_level_hit"] = first_level
        values["first_level_hit_at"] = first_level_at

        # Give the 60-minute checkpoint a few extra minutes for the final
        # completed 1-minute bar to become available.
        values["status"] = (
            "COMPLETE"
            if age_minutes >= 65.0
            else "TRACKING"
        )

        assignments = []
        params = []

        for name, value in values.items():
            if value is None and name.startswith("price_"):
                continue
            assignments.append(f"{name} = ?")
            params.append(value)

        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(row["signal_key"])

        with database.connection() as conn:
            conn.execute(
                f"""
                UPDATE opportunity_performance
                SET {", ".join(assignments)}
                WHERE signal_key = ?
                """,
                tuple(params),
            )

    def update_open(
        self,
        *,
        minimum_interval_seconds: int = 60,
    ) -> int:
        now_epoch = time.time()

        if (
            now_epoch - self._last_update_epoch
            < max(15, int(minimum_interval_seconds))
        ):
            return 0

        self._last_update_epoch = now_epoch
        now = datetime.now(timezone.utc)

        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM opportunity_performance
                WHERE status = 'TRACKING'
                ORDER BY started_at ASC
                LIMIT 50
                """
            ).fetchall()

        updated = 0

        for raw in rows:
            row = dict(raw)

            try:
                self._update_row(row, now)
                updated += 1
            except Exception as exc:
                try:
                    database.log_event(
                        event_type="OPPORTUNITY_TRACKER_UPDATE_ERROR",
                        severity="WARNING",
                        message=f"{row.get('symbol')}: {exc}",
                        metadata={
                            "signal_key": row.get("signal_key"),
                            "symbol": row.get("symbol"),
                        },
                    )
                except Exception:
                    pass

        return updated

    def summary_for_ny_date(self, date_value: Any) -> dict[str, Any]:
        ny = ZoneInfo("America/New_York")

        if isinstance(date_value, str):
            local_date = datetime.fromisoformat(date_value).date()
        elif hasattr(date_value, "year") and not isinstance(date_value, datetime):
            local_date = date_value
        else:
            local_date = date_value.astimezone(ny).date()

        start_local = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            tzinfo=ny,
        )
        end_local = start_local + timedelta(days=1)

        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()

        with database.connection() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM opportunity_performance
                    WHERE started_at >= ?
                      AND started_at < ?
                    ORDER BY started_at ASC
                    """,
                    (start_utc, end_utc),
                ).fetchall()
            ]

        completed = [
            row for row in rows
            if row.get("status") == "COMPLETE"
        ]

        stop_first = sum(
            1 for row in completed
            if row.get("first_level_hit") == "STOP"
        )
        target_first = sum(
            1 for row in completed
            if row.get("first_level_hit") == "T1"
        )
        ambiguous = sum(
            1 for row in completed
            if row.get("first_level_hit") == "AMBIGUOUS_STOP_T1"
        )
        no_level = sum(
            1 for row in completed
            if not row.get("first_level_hit")
        )

        hit_t1 = sum(1 for row in completed if row.get("hit_t1_at"))
        hit_t2 = sum(1 for row in completed if row.get("hit_t2_at"))
        hit_t3 = sum(1 for row in completed if row.get("hit_t3_at"))

        mfe_values = [
            float(row["mfe_pct"])
            for row in completed
            if row.get("mfe_pct") is not None
        ]
        mae_values = [
            float(row["mae_pct"])
            for row in completed
            if row.get("mae_pct") is not None
        ]

        return {
            "date": local_date.isoformat(),
            "total": len(rows),
            "completed": len(completed),
            "tracking": len(rows) - len(completed),
            "stop_first": stop_first,
            "target_first": target_first,
            "ambiguous": ambiguous,
            "no_level": no_level,
            "hit_t1": hit_t1,
            "hit_t2": hit_t2,
            "hit_t3": hit_t3,
            "avg_mfe_pct": (
                sum(mfe_values) / len(mfe_values)
                if mfe_values else None
            ),
            "avg_mae_pct": (
                sum(mae_values) / len(mae_values)
                if mae_values else None
            ),
            "rows": rows,
        }

    @staticmethod
    def build_daily_report(summary: dict[str, Any]) -> str:
        def fmt(value: Any) -> str:
            try:
                return f"{float(value):+.2f}%"
            except (TypeError, ValueError):
                return "N/A"

        lines = [
            "📊 تقييم فرص JALWE اليوم",
            "",
            f"الفرص المؤهلة للتتبع: {summary.get('total', 0)}",
            f"اكتمل تتبع 60 دقيقة: {summary.get('completed', 0)}",
            f"ما زالت تحت التتبع: {summary.get('tracking', 0)}",
            "",
            f"🎯 وصل T1: {summary.get('hit_t1', 0)}",
            f"🎯 وصل T2: {summary.get('hit_t2', 0)}",
            f"🎯 وصل T3: {summary.get('hit_t3', 0)}",
            f"🛑 Stop قبل T1: {summary.get('stop_first', 0)}",
            f"✅ T1 قبل Stop: {summary.get('target_first', 0)}",
            f"⚖️ نفس شمعة 1m (ترتيب غير محسوم): {summary.get('ambiguous', 0)}",
            f"➖ لم يصل Stop أو T1 خلال 60 دقيقة: {summary.get('no_level', 0)}",
            "",
            f"📈 متوسط أفضل حركة MFE: {fmt(summary.get('avg_mfe_pct'))}",
            f"📉 متوسط أسوأ حركة MAE: {fmt(summary.get('avg_mae_pct'))}",
        ]

        rows = list(summary.get("rows") or [])
        completed = [
            row for row in rows
            if row.get("status") == "COMPLETE"
        ]

        if completed:
            lines.extend(["", "أبرز الفرص:"])

            ranked = sorted(
                completed,
                key=lambda row: float(row.get("mfe_pct") or -999.0),
                reverse=True,
            )

            for row in ranked[:8]:
                symbol = str(row.get("symbol") or "")
                strategy = str(row.get("strategy") or "N/A")
                outcome = str(row.get("first_level_hit") or "NO_LEVEL")
                lines.append(
                    f"• {symbol} | {strategy} | "
                    f"MFE {fmt(row.get('mfe_pct'))} | "
                    f"MAE {fmt(row.get('mae_pct'))} | {outcome}"
                )

        lines.extend(
            [
                "",
                "ملاحظة: هذا تقييم للفرص المؤهلة، وليس صفقات منفذة فعليًا.",
            ]
        )

        return "\n".join(lines)


_tracker: Optional[OpportunityPerformanceTracker] = None


def get_opportunity_performance_tracker() -> OpportunityPerformanceTracker:
    global _tracker

    if _tracker is None:
        _tracker = OpportunityPerformanceTracker()

    return _tracker
