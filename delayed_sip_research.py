"""Independent retrospective SIP evidence. No orders, gates or strategy preferences."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from market.research_diagnostics import (
    SIP_RESEARCH_DELAY_MINUTES, coverage_diagnostics, diagnostic_counts,
    diagnostic_text, error_diagnostics,
)

logger = logging.getLogger(__name__)


def _utc(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Research timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


def _price(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


class DelayedSipResearch:
    PROTOCOL = "independent-delayed-sip-60m-v1"
    BATCH_SIZE = 4
    GRACE_MINUTES = 10
    BACKFILL_DAYS = 7

    def __init__(self, db, market_data=None):
        self.db, self.market_data = db, market_data
        with self.db.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS delayed_sip_research (
                observation_key TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
                source_key TEXT NOT NULL, symbol TEXT NOT NULL, decided_at TEXT NOT NULL,
                window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                decision_feed TEXT NOT NULL, reference_price REAL,
                decision_json TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, last_checked_at TEXT, check_count INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_delayed_sip_source
                ON delayed_sip_research(source_kind, source_key)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_delayed_sip_due
                ON delayed_sip_research(status, window_end, last_checked_at)""")
        logger.info("DELAYED_SIP_RESEARCH_INIT delay_minutes=%s research_only=true",
                    SIP_RESEARCH_DELAY_MINUTES)

    def _sync_sources(self, now):
        cutoff = (now - timedelta(days=self.BACKFILL_DAYS)).isoformat()
        with self.db.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            specs = [("DECISION", "decision_outcome_memory", "decision_key", "decided_at"),
                     ("OPPORTUNITY", "opportunity_performance", "signal_key", "started_at")]
            for kind, table, key, timestamp in specs:
                if table not in tables:
                    continue
                # Table/column names are internal constants, never user input.
                rows = conn.execute(f"""SELECT s.* FROM {table} s
                    LEFT JOIN delayed_sip_research r ON r.source_kind = ? AND r.source_key = s.{key}
                    WHERE r.observation_key IS NULL AND s.{timestamp} >= ?
                    ORDER BY s.{timestamp} DESC LIMIT 100""", (kind, cutoff)).fetchall()
                for raw in rows:
                    row = dict(raw)
                    try:
                        decided = _utc(row[timestamp])
                        if kind == "DECISION":
                            decision = json.loads(row["payload_json"])["jalwe"]
                            reference = _price(row["reference_price"])
                            feed = row.get("data_feed") or "unknown"
                        else:
                            meta = json.loads(row.get("metadata_json") or "{}")
                            decision = {name: row.get(name) for name in
                                        ("entry_price", "stop_price", "target_1", "target_2", "target_3")}
                            reference = _price(row["entry_price"])
                            feed = meta.get("decision_data_feed") or "unknown"
                    except (ValueError, TypeError, KeyError):
                        logger.warning("Delayed SIP source invalid | kind=%s key=%s", kind, row[key])
                        continue
                    start = pd.Timestamp(decided).ceil("min").to_pydatetime()
                    end = start + timedelta(minutes=60)
                    obs_key = hashlib.sha256(f"{kind}|{row[key]}".encode()).hexdigest()
                    conn.execute("""INSERT OR IGNORE INTO delayed_sip_research
                        (observation_key, source_kind, source_key, symbol, decided_at,
                         window_start, window_end, decision_feed, reference_price,
                         decision_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (obs_key, kind, row[key], row["symbol"], decided.isoformat(),
                         start.isoformat(), end.isoformat(), feed, reference,
                         json.dumps(decision, default=str),
                         "PENDING" if reference is not None else "UNOBSERVABLE"))

    def update_due(self, *, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._sync_sources(now)
        cutoff = (now - timedelta(minutes=SIP_RESEARCH_DELAY_MINUTES)).isoformat()
        with self.db.connection() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT * FROM delayed_sip_research
                WHERE status = 'PENDING' AND window_end <= ?
                ORDER BY last_checked_at IS NOT NULL, source_kind = 'OPPORTUNITY' DESC,
                         last_checked_at, window_end DESC
                LIMIT ?""", (cutoff, self.BATCH_SIZE)).fetchall()]
        if not rows:
            return 0
        if self.market_data is None:
            from market.market_data import get_market_data
            self.market_data = get_market_data()
        from decision_outcome_memory import DecisionOutcomeMemory
        for row in rows:
            start, end = _utc(row["window_start"]), _utc(row["window_end"])
            terminal = now >= end + timedelta(minutes=SIP_RESEARCH_DELAY_MINUTES + self.GRACE_MINUTES)
            result = {"protocol": self.PROTOCOL, "scope": "HISTORICAL_RESEARCH_NOT_EXECUTED_PNL",
                      "decision_feed": row["decision_feed"], "observation_feed": "sip",
                      "window_start": start.isoformat(), "window_end": end.isoformat(),
                      "minimum_delay_minutes": SIP_RESEARCH_DELAY_MINUTES}
            try:
                frame = self.market_data.get_research_observation_bars(
                    row["symbol"], start, end, as_of=now)
                if frame.attrs.get("data_feed") != "sip":
                    raise ValueError("Research observation feed mismatch")
                bars = DecisionOutcomeMemory._valid_bars(frame, start, end)
                diagnostics = coverage_diagnostics(frame, bars, start, end)
                complete = diagnostics["missing_minutes"] == 0 and len(bars) == 60
                baseline = float(row["reference_price"])
                result.update(data_diagnostics=diagnostics, complete_path=complete,
                              observed_minutes=len(bars),
                              mfe_pct=None if bars.empty else max(0., (float(bars["high"].max()) / baseline - 1) * 100),
                              mae_pct=None if bars.empty else min(0., (float(bars["low"].min()) / baseline - 1) * 100),
                              levels=DecisionOutcomeMemory._level_observation(
                                  bars, json.loads(row["decision_json"]), complete))
                status = "COMPLETE" if complete else "PARTIAL_DATA" if terminal else "PENDING"
            except Exception as exc:
                diagnostics = error_diagnostics(exc)
                result.update(data_diagnostics=diagnostics, complete_path=False)
                status = "DATA_UNAVAILABLE" if terminal else "PENDING"
            with self.db.connection() as conn:
                conn.execute("""UPDATE delayed_sip_research SET status = ?, result_json = ?,
                    last_checked_at = ?, check_count = check_count + 1
                    WHERE observation_key = ? AND status = 'PENDING'""",
                    (status, json.dumps(result), now.isoformat(), row["observation_key"]))
            previous = json.loads(row.get("result_json") or "{}")
            if status != "PENDING" or previous.get("data_diagnostics") != diagnostics:
                logger.info("DELAYED_SIP_RESEARCH_DATA %s", json.dumps({
                    "symbol": row["symbol"], "source_kind": row["source_kind"],
                    "source_key": row["source_key"], "status": status, "feed": "sip",
                    "decision_feed": row["decision_feed"], **diagnostics}))
        return len(rows)

    def report_for_ny_date(self, date_value):
        day = datetime.fromisoformat(str(date_value)).date()
        start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York"))
        end = start + timedelta(days=1)
        with self.db.connection() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT * FROM delayed_sip_research
                WHERE decided_at >= ? AND decided_at < ?""",
                (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()]
        lines = ["🔬 تقييم تاريخي مستقل — SIP متأخر", "ينتظر 16 دقيقة بعد نافذة الـ60 دقيقة؛ مصدر قرار الدخول محفوظ منفصلًا."]
        feeds = Counter(row["decision_feed"] for row in rows)
        if feeds:
            lines.append("مصادر القرار المسجلة: " + " | ".join(f"{feed}: {count}" for feed, count in sorted(feeds.items())))
        for kind, label in (("OPPORTUNITY", "فرص التتبع"), ("DECISION", "القرارات")):
            group = [row for row in rows if row["source_kind"] == kind]
            counts = {s: sum(row["status"] == s for row in group) for s in
                      ("COMPLETE", "PENDING", "PARTIAL_DATA", "DATA_UNAVAILABLE", "UNOBSERVABLE")}
            lines.append(f"{label}: مكتملة {counts['COMPLETE']} | تنتظر {counts['PENDING']} | ناقصة/غير متاحة {counts['PARTIAL_DATA'] + counts['DATA_UNAVAILABLE']} | دون مرجع موثوق {counts['UNOBSERVABLE']}")
            completed = [json.loads(row["result_json"]) for row in group if row["status"] == "COMPLETE"]
            if completed:
                first = lambda name: sum((r.get("levels") or {}).get("outcome") == name for r in completed)
                lines.append(f"  هدف أولًا {first('TARGET_FIRST')} | وقف أولًا {first('STOP_FIRST')} | ترتيب غير محسوم {first('AMBIGUOUS_STOP_TARGET')} | لم تتفعل {first('NOT_ACTIVATED')}")
                lines.append(f"  MFE {sum(r['mfe_pct'] for r in completed) / len(completed):+.2f}% | MAE {sum(r['mae_pct'] for r in completed) / len(completed):+.2f}%")
        reasons = diagnostic_text(diagnostic_counts(rows, "result_json"))
        if reasons:
            lines.append("أسباب نقص SIP: " + reasons)
        lines.append("بحث إضافي فقط؛ لا يغيّر قرار الدخول أو تفضيلات الاستراتيجيات، ولا يثبت بيعًا لحظيًا خارج الجلسة.")
        lines.append("الحركة من المرجع الأصلي؛ هدف 2R افتراضي عند غياب T1. قد يتكرر السهم بين السجلات.")
        return "\n".join(lines)
