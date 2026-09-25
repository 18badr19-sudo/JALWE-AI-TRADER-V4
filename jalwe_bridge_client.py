# ============================================================
# APEX -> JALWE V4 RESEARCH BRIDGE CLIENT V4
#
# PURPOSE:
#   Apex publishes research packets for JALWE V4.
#
# BACKENDS:
#   Railway  -> PostgreSQL via DATABASE_URL
#   Local    -> SQLite fallback
#
# SAFETY:
#   RESEARCH ONLY
#   NO ORDER EXECUTION
#   APEX HAS ZERO BROKER AUTHORITY
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
import threading

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


UTC = timezone.utc
SOURCE_NAME = "APEX"

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_SQLITE_PATH = (
    BASE_DIR
    / "data"
    / "apex_jalwe_bridge.db"
)


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso() -> str:
    return utc_now().isoformat()


def clean_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_optional_text(value: Any) -> Optional[str]:
    text = clean_text(value)
    return text if text else None


def clamp(
    value: Any,
    minimum: float,
    maximum: float,
    default: float = 0.0,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)

    return max(
        minimum,
        min(maximum, number),
    )


def json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return "{}"


def normalize_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return []

        try:
            decoded = json.loads(text)

            if isinstance(decoded, list):
                return [
                    str(item).strip()
                    for item in decoded
                    if str(item).strip()
                ]
        except Exception:
            pass

        return [text]

    if isinstance(
        value,
        (tuple, list, set),
    ):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    return [
        str(value).strip()
    ]


def normalize_created_at(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)

        return dt.isoformat()

    text = clean_text(value)

    if not text:
        return utc_iso()

    try:
        dt = datetime.fromisoformat(
            text.replace(
                "Z",
                "+00:00",
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)

        return dt.isoformat()

    except Exception:
        return utc_iso()


# ============================================================
# BRIDGE CLIENT
# ============================================================

class JalweBridgeClient:
    """
    Apex research publisher.

    Apex is advisory/research only.

    It can:
        - write research packets
        - check bridge health

    It cannot:
        - submit trades
        - cancel trades
        - modify JALWE risk
        - call execution engine
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        sqlite_path: Optional[str] = None,
    ) -> None:

        self.database_url = (
            database_url
            or
            os.getenv(
                "DATABASE_URL",
                "",
            ).strip()
        )

        self.backend = (
            "POSTGRES"
            if self.database_url
            else
            "SQLITE"
        )

        self.sqlite_path = (
            sqlite_path
            or
            os.getenv(
                "APEX_JALWE_BRIDGE_DB",
                str(DEFAULT_SQLITE_PATH),
            )
        )

        self.research_only = True

        self.order_execution_enabled = False

        self.execution_authority = False

        self._lock = threading.RLock()

        self._sqlite_conn: Optional[
            sqlite3.Connection
        ] = None

        self._initialize()


    # ========================================================
    # GENERIC PACKET ACCESS
    # ========================================================

    @staticmethod
    def _get(
        packet: Any,
        *names: str,
        default: Any = None,
    ) -> Any:

        if packet is None:
            return default

        if isinstance(packet, dict):
            for name in names:
                if name in packet:
                    value = packet.get(name)

                    if value is not None:
                        return value

            return default

        for name in names:
            if hasattr(packet, name):
                try:
                    value = getattr(
                        packet,
                        name,
                    )

                    if value is not None:
                        return value

                except Exception:
                    continue

        return default


    # ========================================================
    # NORMALIZE APEX PACKET
    # ========================================================

    def _packet_dict(
        self,
        packet: Any,
    ) -> Dict[str, Any]:

        symbol = clean_symbol(
            self._get(
                packet,
                "symbol",
                "ticker",
                default="",
            )
        )

        if not symbol:
            raise ValueError(
                "APEX research packet has no symbol."
            )

        score = clamp(
            self._get(
                packet,
                "score",
                "final_score",
                "research_score",
                default=0.0,
            ),
            0.0,
            100.0,
            0.0,
        )

        confidence_raw = self._get(
            packet,
            "confidence",
            "conf",
            "ai_confidence",
            default=0.0,
        )

        confidence = clamp(
            confidence_raw,
            0.0,
            100.0,
            0.0,
        )

        # JALWE bridge stores confidence 0.0 -> 1.0
        if confidence > 1.0:
            confidence = (
                confidence
                /
                100.0
            )

        confidence = clamp(
            confidence,
            0.0,
            1.0,
            0.0,
        )

        news_score_raw = self._get(
            packet,
            "news_score",
            "news",
            default=None,
        )

        if news_score_raw is None:
            news_score = None
        else:
            news_score = clamp(
                news_score_raw,
                0.0,
                100.0,
                50.0,
            )

        sentiment = clean_optional_text(
            self._get(
                packet,
                "sentiment",
                "news_sentiment",
                default=None,
            )
        )

        if sentiment:
            sentiment = sentiment.upper()

        market_bias = clean_optional_text(
            self._get(
                packet,
                "bias",
                "market_bias",
                "direction",
                default=None,
            )
        )

        if market_bias:
            market_bias = (
                market_bias.upper()
            )

        verdict = clean_optional_text(
            self._get(
                packet,
                "verdict",
                "decision",
                "status",
                default=None,
            )
        )

        if verdict:
            verdict = verdict.upper()

        catalyst_value = self._get(
            packet,
            "catalyst",
            "catalysts",
            "news_catalyst",
            default=None,
        )

        catalysts = normalize_list(
            catalyst_value
        )

        catalyst = (
            ", ".join(catalysts)
            if catalysts
            else None
        )

        headlines = normalize_list(
            self._get(
                packet,
                "headlines",
                "news_headlines",
                default=[],
            )
        )

        risk_flags = normalize_list(
            self._get(
                packet,
                "risk_flags",
                "risks",
                "warnings",
                default=[],
            )
        )

        summary = clean_text(
            self._get(
                packet,
                "summary",
                "research_summary",
                "reason",
                "explanation",
                default="",
            )
        )

        technical_notes = clean_text(
            self._get(
                packet,
                "technical_notes",
                "technical_summary",
                "notes",
                default="",
            )
        )

        created_at = normalize_created_at(
            self._get(
                packet,
                "created_at",
                "timestamp",
                "generated_at",
                default=utc_iso(),
            )
        )

        # ----------------------------------------------------
        # Preserve Apex-specific data for JALWE advisory use
        # ----------------------------------------------------

        metadata = {
            "bridge_version": "APEX_JALWE_V4",
            "publisher": "APEX",
            "research_only": True,
            "order_execution_enabled": False,

            "score": score,
            "verdict": verdict,

            "pre_breakout_score": self._get(
                packet,
                "pre_breakout_score",
                "pre_score",
                "pre",
                default=None,
            ),

            "liquidity_score": self._get(
                packet,
                "liquidity_score",
                "liquidity",
                "liq",
                default=None,
            ),

            "ai_score": self._get(
                packet,
                "ai_score",
                default=None,
            ),

            "scanner_score": self._get(
                packet,
                "scanner_score",
                "radar_score",
                default=None,
            ),

            "session_score": self._get(
                packet,
                "session_score",
                default=None,
            ),

            "breakout_score": self._get(
                packet,
                "breakout_score",
                default=None,
            ),

            "rvol": self._get(
                packet,
                "rvol",
                default=None,
            ),

            "price": self._get(
                packet,
                "price",
                "last_price",
                default=None,
            ),

            "entry": self._get(
                packet,
                "entry",
                "entry_price",
                default=None,
            ),

            "stop": self._get(
                packet,
                "stop",
                "stop_price",
                "stop_loss",
                default=None,
            ),

            "target_1": self._get(
                packet,
                "target1",
                "target_1",
                "t1",
                default=None,
            ),

            "target_2": self._get(
                packet,
                "target2",
                "target_2",
                "t2",
                default=None,
            ),

            "target_3": self._get(
                packet,
                "target3",
                "target_3",
                "t3",
                default=None,
            ),

            "strategy": self._get(
                packet,
                "strategy",
                "strategy_name",
                default=None,
            ),

            "timeframe": self._get(
                packet,
                "timeframe",
                default=None,
            ),

            "data_status": self._get(
                packet,
                "data_status",
                default=None,
            ),

            "raw_source": type(
                packet
            ).__name__,
        }

        existing_metadata = self._get(
            packet,
            "metadata",
            default=None,
        )

        if isinstance(
            existing_metadata,
            dict,
        ):
            metadata.update(
                existing_metadata
            )

        return {
            "symbol": symbol,
            "source": SOURCE_NAME,
            "news_score": news_score,
            "sentiment": sentiment,
            "catalyst": catalyst,
            "confidence": confidence,
            "summary": summary,
            "market_bias": market_bias,
            "technical_notes": technical_notes,
            "headlines": headlines,
            "risk_flags": risk_flags,
            "created_at": created_at,
            "metadata": metadata,
        }


    # ========================================================
    # CONNECTIONS
    # ========================================================

    def _connect_postgres(self):
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "psycopg2-binary is required "
                "for PostgreSQL bridge."
            ) from exc

        return psycopg2.connect(
            self.database_url,
            connect_timeout=10,
        )


    def _connect_sqlite(
        self,
    ) -> sqlite3.Connection:

        with self._lock:

            if (
                self._sqlite_conn
                is not None
            ):
                return self._sqlite_conn

            if (
                self.sqlite_path
                !=
                ":memory:"
            ):
                path = Path(
                    self.sqlite_path
                )

                path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            conn = sqlite3.connect(
                self.sqlite_path,
                timeout=30,
                check_same_thread=False,
            )

            conn.row_factory = (
                sqlite3.Row
            )

            conn.execute(
                "PRAGMA journal_mode=WAL"
            )

            conn.execute(
                "PRAGMA synchronous=NORMAL"
            )

            self._sqlite_conn = conn

            return conn


    # ========================================================
    # SCHEMA
    # ========================================================

    def _initialize(
        self,
    ) -> None:

        if (
            self.backend
            ==
            "POSTGRES"
        ):
            self._initialize_postgres()

        else:
            self._initialize_sqlite()


    def _initialize_postgres(
        self,
    ) -> None:

        conn = (
            self._connect_postgres()
        )

        try:
            with conn:
                with conn.cursor() as cur:

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS
                        external_research_latest (
                            symbol TEXT NOT NULL,
                            source TEXT NOT NULL,
                            news_score DOUBLE PRECISION,
                            sentiment TEXT,
                            catalyst TEXT,
                            confidence DOUBLE PRECISION
                                NOT NULL DEFAULT 0,
                            summary TEXT
                                NOT NULL DEFAULT '',
                            market_bias TEXT,
                            technical_notes TEXT
                                NOT NULL DEFAULT '',
                            headlines_json TEXT
                                NOT NULL DEFAULT '[]',
                            risk_flags_json TEXT
                                NOT NULL DEFAULT '[]',
                            created_at TEXT NOT NULL,
                            metadata_json TEXT
                                NOT NULL DEFAULT '{}',
                            PRIMARY KEY (
                                symbol,
                                source
                            )
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS
                        external_research_history (
                            id BIGSERIAL PRIMARY KEY,
                            symbol TEXT NOT NULL,
                            source TEXT NOT NULL,
                            news_score DOUBLE PRECISION,
                            sentiment TEXT,
                            catalyst TEXT,
                            confidence DOUBLE PRECISION
                                NOT NULL DEFAULT 0,
                            summary TEXT
                                NOT NULL DEFAULT '',
                            market_bias TEXT,
                            technical_notes TEXT
                                NOT NULL DEFAULT '',
                            headlines_json TEXT
                                NOT NULL DEFAULT '[]',
                            risk_flags_json TEXT
                                NOT NULL DEFAULT '[]',
                            created_at TEXT NOT NULL,
                            metadata_json TEXT
                                NOT NULL DEFAULT '{}'
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_external_research_history_symbol
                        ON external_research_history
                        (
                            symbol,
                            created_at DESC
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_external_research_latest_created
                        ON external_research_latest
                        (
                            created_at DESC
                        )
                        """
                    )

        finally:
            conn.close()


    def _initialize_sqlite(
        self,
    ) -> None:

        conn = (
            self._connect_sqlite()
        )

        with self._lock:

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                external_research_latest (
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    news_score REAL,
                    sentiment TEXT,
                    catalyst TEXT,
                    confidence REAL
                        NOT NULL DEFAULT 0,
                    summary TEXT
                        NOT NULL DEFAULT '',
                    market_bias TEXT,
                    technical_notes TEXT
                        NOT NULL DEFAULT '',
                    headlines_json TEXT
                        NOT NULL DEFAULT '[]',
                    risk_flags_json TEXT
                        NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT
                        NOT NULL DEFAULT '{}',
                    PRIMARY KEY (
                        symbol,
                        source
                    )
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                external_research_history (
                    id INTEGER PRIMARY KEY
                        AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    news_score REAL,
                    sentiment TEXT,
                    catalyst TEXT,
                    confidence REAL
                        NOT NULL DEFAULT 0,
                    summary TEXT
                        NOT NULL DEFAULT '',
                    market_bias TEXT,
                    technical_notes TEXT
                        NOT NULL DEFAULT '',
                    headlines_json TEXT
                        NOT NULL DEFAULT '[]',
                    risk_flags_json TEXT
                        NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT
                        NOT NULL DEFAULT '{}'
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_external_research_history_symbol
                ON external_research_history
                (
                    symbol,
                    created_at DESC
                )
                """
            )

            conn.commit()


    # ========================================================
    # DATABASE VALUES
    # ========================================================

    @staticmethod
    def _db_values(
        data: Dict[str, Any],
    ) -> tuple:

        return (
            data["symbol"],
            data["source"],
            data["news_score"],
            data["sentiment"],
            data["catalyst"],
            data["confidence"],
            data["summary"],
            data["market_bias"],
            data["technical_notes"],
            json_dumps(
                data["headlines"]
            ),
            json_dumps(
                data["risk_flags"]
            ),
            data["created_at"],
            json_dumps(
                data["metadata"]
            ),
        )


    # ========================================================
    # PUBLISH ONE PACKET
    # ========================================================

    def publish_packet(
        self,
        packet: Any,
    ) -> bool:

        data = self._packet_dict(
            packet
        )

        values = self._db_values(
            data
        )

        if (
            self.backend
            ==
            "POSTGRES"
        ):
            self._publish_postgres(
                values
            )

        else:
            self._publish_sqlite(
                values
            )

        return True


    def _publish_postgres(
        self,
        values: tuple,
    ) -> None:

        conn = (
            self._connect_postgres()
        )

        try:
            with conn:
                with conn.cursor() as cur:

                    # Full immutable history
                    cur.execute(
                        """
                        INSERT INTO
                        external_research_history (
                            symbol,
                            source,
                            news_score,
                            sentiment,
                            catalyst,
                            confidence,
                            summary,
                            market_bias,
                            technical_notes,
                            headlines_json,
                            risk_flags_json,
                            created_at,
                            metadata_json
                        )
                        VALUES (
                            %s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s
                        )
                        """,
                        values,
                    )

                    # Current/latest research
                    cur.execute(
                        """
                        INSERT INTO
                        external_research_latest (
                            symbol,
                            source,
                            news_score,
                            sentiment,
                            catalyst,
                            confidence,
                            summary,
                            market_bias,
                            technical_notes,
                            headlines_json,
                            risk_flags_json,
                            created_at,
                            metadata_json
                        )
                        VALUES (
                            %s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s
                        )
                        ON CONFLICT
                        (
                            symbol,
                            source
                        )
                        DO UPDATE SET
                            news_score =
                                EXCLUDED.news_score,
                            sentiment =
                                EXCLUDED.sentiment,
                            catalyst =
                                EXCLUDED.catalyst,
                            confidence =
                                EXCLUDED.confidence,
                            summary =
                                EXCLUDED.summary,
                            market_bias =
                                EXCLUDED.market_bias,
                            technical_notes =
                                EXCLUDED.technical_notes,
                            headlines_json =
                                EXCLUDED.headlines_json,
                            risk_flags_json =
                                EXCLUDED.risk_flags_json,
                            created_at =
                                EXCLUDED.created_at,
                            metadata_json =
                                EXCLUDED.metadata_json
                        """,
                        values,
                    )

        finally:
            conn.close()


    def _publish_sqlite(
        self,
        values: tuple,
    ) -> None:

        conn = (
            self._connect_sqlite()
        )

        with self._lock:

            conn.execute(
                """
                INSERT INTO
                external_research_history (
                    symbol,
                    source,
                    news_score,
                    sentiment,
                    catalyst,
                    confidence,
                    summary,
                    market_bias,
                    technical_notes,
                    headlines_json,
                    risk_flags_json,
                    created_at,
                    metadata_json
                )
                VALUES (
                    ?,?,?,?,?,?,?,
                    ?,?,?,?,?,?
                )
                """,
                values,
            )

            conn.execute(
                """
                INSERT INTO
                external_research_latest (
                    symbol,
                    source,
                    news_score,
                    sentiment,
                    catalyst,
                    confidence,
                    summary,
                    market_bias,
                    technical_notes,
                    headlines_json,
                    risk_flags_json,
                    created_at,
                    metadata_json
                )
                VALUES (
                    ?,?,?,?,?,?,?,
                    ?,?,?,?,?,?
                )
                ON CONFLICT
                (
                    symbol,
                    source
                )
                DO UPDATE SET
                    news_score =
                        excluded.news_score,
                    sentiment =
                        excluded.sentiment,
                    catalyst =
                        excluded.catalyst,
                    confidence =
                        excluded.confidence,
                    summary =
                        excluded.summary,
                    market_bias =
                        excluded.market_bias,
                    technical_notes =
                        excluded.technical_notes,
                    headlines_json =
                        excluded.headlines_json,
                    risk_flags_json =
                        excluded.risk_flags_json,
                    created_at =
                        excluded.created_at,
                    metadata_json =
                        excluded.metadata_json
                """,
                values,
            )

            conn.commit()


    # ========================================================
    # PUBLISH SHORTLIST
    # ========================================================

    def publish_shortlist(
        self,
        packets: Iterable[Any],
    ) -> List[str]:

        published: List[str] = []

        if packets is None:
            return published

        for packet in packets:

            try:
                data = (
                    self._packet_dict(
                        packet
                    )
                )

                symbol = data[
                    "symbol"
                ]

                if self.publish_packet(
                    packet
                ):
                    published.append(
                        symbol
                    )

            except Exception as exc:

                symbol = clean_symbol(
                    self._get(
                        packet,
                        "symbol",
                        "ticker",
                        default="UNKNOWN",
                    )
                )

                print(
                    "[JALWE BRIDGE] "
                    f"Publish failed "
                    f"for {symbol}: {exc}"
                )

        return published


    # ========================================================
    # COUNT
    # ========================================================

    def _count_latest(
        self,
    ) -> int:

        if (
            self.backend
            ==
            "POSTGRES"
        ):
            conn = (
                self._connect_postgres()
            )

            try:
                with conn.cursor() as cur:

                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM external_research_latest
                        """
                    )

                    row = (
                        cur.fetchone()
                    )

                    return int(
                        row[0]
                        if row
                        else 0
                    )

            finally:
                conn.close()

        conn = (
            self._connect_sqlite()
        )

        with self._lock:

            row = (
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM external_research_latest
                    """
                )
                .fetchone()
            )

        return int(
            row[0]
            if row
            else 0
        )


    # ========================================================
    # HEALTH CHECK
    # ========================================================

    def health_check(
        self,
    ) -> Dict[str, Any]:

        health: Dict[
            str,
            Any
        ] = {
            "ok": False,
            "backend": self.backend,
            "database_url_present": bool(
                self.database_url
            ),
            "research_only": True,
            "order_execution_enabled": False,
            "execution_authority": False,
            "latest_research_count": 0,
            "error": None,
        }

        try:

            if (
                self.backend
                ==
                "POSTGRES"
            ):
                conn = (
                    self._connect_postgres()
                )

                try:
                    with conn.cursor() as cur:

                        cur.execute(
                            "SELECT 1"
                        )

                        row = (
                            cur.fetchone()
                        )

                        health["ok"] = bool(
                            row
                            and
                            row[0] == 1
                        )

                finally:
                    conn.close()

            else:

                conn = (
                    self._connect_sqlite()
                )

                with self._lock:

                    row = (
                        conn.execute(
                            "SELECT 1"
                        )
                        .fetchone()
                    )

                health["ok"] = bool(
                    row
                    and
                    row[0] == 1
                )

            if health["ok"]:

                health[
                    "latest_research_count"
                ] = (
                    self._count_latest()
                )

        except Exception as exc:

            health["ok"] = False

            health["error"] = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

        return health


    # ========================================================
    # CLOSE
    # ========================================================

    def close(
        self,
    ) -> None:

        with self._lock:

            if (
                self._sqlite_conn
                is not None
            ):

                try:
                    self._sqlite_conn.close()
                except Exception:
                    pass

                self._sqlite_conn = None


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":

    client = (
        JalweBridgeClient()
    )

    print(
        "======================================"
    )

    print(
        "APEX -> JALWE BRIDGE CLIENT V4"
    )

    print(
        "======================================"
    )

    print(
        client.health_check()
    )

    print(
        "RESEARCH ONLY: True"
    )

    print(
        "ORDER EXECUTION: DISABLED"
    )

    print(
        "======================================"
    )