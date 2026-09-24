# ============================================================
# JALWE AI TRADER V4
# EXTERNAL RESEARCH BRIDGE V4
#
# PURPOSE:
#   Receive/read external research packets from APEX.
#
# BACKENDS:
#   1) PostgreSQL when DATABASE_URL exists (Railway)
#   2) SQLite fallback for local development
#
# IMPORTANT:
#   - Research only
#   - No order execution
#   - APEX never gets execution authority
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
import threading

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


# ============================================================
# CONSTANTS
# ============================================================

UTC = timezone.utc

DEFAULT_SOURCE = "APEX"

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

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
        min(
            maximum,
            number,
        ),
    )


def clean_symbol(
    symbol: Any,
) -> str:
    return (
        str(symbol or "")
        .strip()
        .upper()
    )


def clean_source(
    source: Any,
) -> str:
    value = (
        str(source or DEFAULT_SOURCE)
        .strip()
        .upper()
    )

    return value or DEFAULT_SOURCE


def json_dumps(
    value: Any,
) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return "{}"


def json_loads(
    value: Any,
    default: Any,
) -> Any:
    if value is None:
        return default

    if isinstance(
        value,
        (dict, list),
    ):
        return value

    try:
        return json.loads(
            str(value)
        )
    except Exception:
        return default


def normalize_created_at(
    value: Any,
) -> str:
    if isinstance(
        value,
        datetime,
    ):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=UTC
            )
        else:
            dt = dt.astimezone(
                UTC
            )

        return dt.isoformat()

    text = str(
        value or ""
    ).strip()

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
            dt = dt.replace(
                tzinfo=UTC
            )
        else:
            dt = dt.astimezone(
                UTC
            )

        return dt.isoformat()

    except Exception:
        return utc_iso()


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class ExternalResearch:
    symbol: str

    source: str = DEFAULT_SOURCE

    news_score: Optional[float] = None

    sentiment: Optional[str] = None

    catalyst: Optional[str] = None

    confidence: float = 0.0

    summary: str = ""

    market_bias: Optional[str] = None

    technical_notes: str = ""

    headlines: List[str] = field(
        default_factory=list
    )

    risk_flags: List[str] = field(
        default_factory=list
    )

    created_at: str = field(
        default_factory=utc_iso
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.symbol = clean_symbol(
            self.symbol
        )

        self.source = clean_source(
            self.source
        )

        self.confidence = clamp(
            self.confidence,
            0.0,
            1.0,
            0.0,
        )

        if self.news_score is not None:
            self.news_score = clamp(
                self.news_score,
                0.0,
                100.0,
                50.0,
            )

        self.sentiment = (
            str(self.sentiment).strip().upper()
            if self.sentiment is not None
            else None
        )

        self.market_bias = (
            str(self.market_bias).strip().upper()
            if self.market_bias is not None
            else None
        )

        self.catalyst = (
            str(self.catalyst).strip()
            if self.catalyst is not None
            else None
        )

        self.summary = str(
            self.summary or ""
        ).strip()

        self.technical_notes = str(
            self.technical_notes or ""
        ).strip()

        self.headlines = [
            str(item).strip()
            for item in (
                self.headlines
                or []
            )
            if str(item).strip()
        ]

        self.risk_flags = [
            str(item).strip()
            for item in (
                self.risk_flags
                or []
            )
            if str(item).strip()
        ]

        self.created_at = (
            normalize_created_at(
                self.created_at
            )
        )

        if not isinstance(
            self.metadata,
            dict,
        ):
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(
            self
        )


# ============================================================
# BRIDGE
# ============================================================

class ExternalResearchBridge:
    """
    Shared research bridge.

    Railway:
        DATABASE_URL present
        -> PostgreSQL

    Local development:
        DATABASE_URL absent
        -> SQLite
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
                "JALWE_EXTERNAL_RESEARCH_DB",
                str(
                    DEFAULT_SQLITE_PATH
                ),
            )
        )

        self._lock = (
            threading.RLock()
        )

        self._sqlite_conn: Optional[
            sqlite3.Connection
        ] = None

        self._initialize()


    # ========================================================
    # CONNECTIONS
    # ========================================================

    def _connect_postgres(self):
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL backend requires "
                "'psycopg2-binary'. "
                "Add psycopg2-binary to requirements.txt."
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
                return (
                    self._sqlite_conn
                )

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

            conn.execute(
                "PRAGMA foreign_keys=ON"
            )

            self._sqlite_conn = conn

            return conn


    # ========================================================
    # INITIALIZATION
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
                            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                            summary TEXT NOT NULL DEFAULT '',
                            market_bias TEXT,
                            technical_notes TEXT NOT NULL DEFAULT '',
                            headlines_json TEXT NOT NULL DEFAULT '[]',
                            risk_flags_json TEXT NOT NULL DEFAULT '[]',
                            created_at TEXT NOT NULL,
                            metadata_json TEXT NOT NULL DEFAULT '{}',
                            PRIMARY KEY (symbol, source)
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
                            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                            summary TEXT NOT NULL DEFAULT '',
                            market_bias TEXT,
                            technical_notes TEXT NOT NULL DEFAULT '',
                            headlines_json TEXT NOT NULL DEFAULT '[]',
                            risk_flags_json TEXT NOT NULL DEFAULT '[]',
                            created_at TEXT NOT NULL,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_external_research_history_symbol
                        ON external_research_history
                        (symbol, created_at DESC)
                        """
                    )

                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_external_research_latest_created
                        ON external_research_latest
                        (created_at DESC)
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
                    confidence REAL NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    market_bias TEXT,
                    technical_notes TEXT NOT NULL DEFAULT '',
                    headlines_json TEXT NOT NULL DEFAULT '[]',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (symbol, source)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                external_research_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    news_score REAL,
                    sentiment TEXT,
                    catalyst TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    market_bias TEXT,
                    technical_notes TEXT NOT NULL DEFAULT '',
                    headlines_json TEXT NOT NULL DEFAULT '[]',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_external_research_history_symbol
                ON external_research_history
                (symbol, created_at DESC)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_external_research_latest_created
                ON external_research_latest
                (created_at DESC)
                """
            )

            conn.commit()


    # ========================================================
    # SERIALIZATION
    # ========================================================

    @staticmethod
    def _record_values(
        research: ExternalResearch,
    ) -> tuple:

        return (
            research.symbol,
            research.source,
            research.news_score,
            research.sentiment,
            research.catalyst,
            research.confidence,
            research.summary,
            research.market_bias,
            research.technical_notes,
            json_dumps(
                research.headlines
            ),
            json_dumps(
                research.risk_flags
            ),
            research.created_at,
            json_dumps(
                research.metadata
            ),
        )


    @staticmethod
    def _row_to_research(
        row: Any,
    ) -> Optional[ExternalResearch]:

        if row is None:
            return None

        if isinstance(
            row,
            sqlite3.Row,
        ):
            data = dict(
                row
            )

        elif isinstance(
            row,
            dict,
        ):
            data = row

        else:
            # PostgreSQL tuple
            columns = [
                "symbol",
                "source",
                "news_score",
                "sentiment",
                "catalyst",
                "confidence",
                "summary",
                "market_bias",
                "technical_notes",
                "headlines_json",
                "risk_flags_json",
                "created_at",
                "metadata_json",
            ]

            data = dict(
                zip(
                    columns,
                    row,
                )
            )

        return ExternalResearch(
            symbol=data.get(
                "symbol",
                "",
            ),
            source=data.get(
                "source",
                DEFAULT_SOURCE,
            ),
            news_score=data.get(
                "news_score"
            ),
            sentiment=data.get(
                "sentiment"
            ),
            catalyst=data.get(
                "catalyst"
            ),
            confidence=data.get(
                "confidence",
                0.0,
            ),
            summary=data.get(
                "summary",
                "",
            ),
            market_bias=data.get(
                "market_bias"
            ),
            technical_notes=data.get(
                "technical_notes",
                "",
            ),
            headlines=json_loads(
                data.get(
                    "headlines_json"
                ),
                [],
            ),
            risk_flags=json_loads(
                data.get(
                    "risk_flags_json"
                ),
                [],
            ),
            created_at=data.get(
                "created_at",
                utc_iso(),
            ),
            metadata=json_loads(
                data.get(
                    "metadata_json"
                ),
                {},
            ),
        )


    # ========================================================
    # PUBLISH
    # ========================================================

    def publish_research(
        self,
        research: ExternalResearch,
    ) -> bool:

        if not isinstance(
            research,
            ExternalResearch,
        ):
            raise TypeError(
                "research must be ExternalResearch"
            )

        if not research.symbol:
            raise ValueError(
                "Research symbol is required."
            )

        values = (
            self._record_values(
                research
            )
        )

        if (
            self.backend
            ==
            "POSTGRES"
        ):
            return (
                self._publish_postgres(
                    values
                )
            )

        return (
            self._publish_sqlite(
                values
            )
        )


    def publish(
        self,
        research: ExternalResearch,
    ) -> bool:
        return self.publish_research(
            research
        )


    def _publish_postgres(
        self,
        values: tuple,
    ) -> bool:

        conn = (
            self._connect_postgres()
        )

        try:
            with conn:
                with conn.cursor() as cur:

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
                        (symbol, source)
                        DO UPDATE SET
                            news_score = EXCLUDED.news_score,
                            sentiment = EXCLUDED.sentiment,
                            catalyst = EXCLUDED.catalyst,
                            confidence = EXCLUDED.confidence,
                            summary = EXCLUDED.summary,
                            market_bias = EXCLUDED.market_bias,
                            technical_notes = EXCLUDED.technical_notes,
                            headlines_json = EXCLUDED.headlines_json,
                            risk_flags_json = EXCLUDED.risk_flags_json,
                            created_at = EXCLUDED.created_at,
                            metadata_json = EXCLUDED.metadata_json
                        """,
                        values,
                    )

            return True

        finally:
            conn.close()


    def _publish_sqlite(
        self,
        values: tuple,
    ) -> bool:

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
                ON CONFLICT(symbol, source)
                DO UPDATE SET
                    news_score = excluded.news_score,
                    sentiment = excluded.sentiment,
                    catalyst = excluded.catalyst,
                    confidence = excluded.confidence,
                    summary = excluded.summary,
                    market_bias = excluded.market_bias,
                    technical_notes = excluded.technical_notes,
                    headlines_json = excluded.headlines_json,
                    risk_flags_json = excluded.risk_flags_json,
                    created_at = excluded.created_at,
                    metadata_json = excluded.metadata_json
                """,
                values,
            )

            conn.commit()

        return True


    # ========================================================
    # GET LATEST
    # ========================================================

    def get_latest_research(
        self,
        symbol: str,
        source: Optional[str] = None,
    ) -> Optional[ExternalResearch]:

        symbol = clean_symbol(
            symbol
        )

        if not symbol:
            return None

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

                    if source:

                        cur.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            WHERE symbol = %s
                              AND source = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (
                                symbol,
                                clean_source(
                                    source
                                ),
                            ),
                        )

                    else:

                        cur.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            WHERE symbol = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (
                                symbol,
                            ),
                        )

                    row = (
                        cur.fetchone()
                    )

                    return (
                        self._row_to_research(
                            row
                        )
                    )

            finally:
                conn.close()

        conn = (
            self._connect_sqlite()
        )

        with self._lock:

            if source:

                row = (
                    conn.execute(
                        """
                        SELECT
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
                        FROM external_research_latest
                        WHERE symbol = ?
                          AND source = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (
                            symbol,
                            clean_source(
                                source
                            ),
                        ),
                    )
                    .fetchone()
                )

            else:

                row = (
                    conn.execute(
                        """
                        SELECT
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
                        FROM external_research_latest
                        WHERE symbol = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (
                            symbol,
                        ),
                    )
                    .fetchone()
                )

        return (
            self._row_to_research(
                row
            )
        )


    # ========================================================
    # FRESHNESS
    # ========================================================

    def get_age_minutes(
        self,
        research: Optional[
            ExternalResearch
        ],
    ) -> Optional[float]:

        if research is None:
            return None

        try:
            created = (
                datetime.fromisoformat(
                    research.created_at.replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            if created.tzinfo is None:
                created = (
                    created.replace(
                        tzinfo=UTC
                    )
                )

            created = (
                created.astimezone(
                    UTC
                )
            )

            delta = (
                utc_now()
                -
                created
            )

            return max(
                0.0,
                delta.total_seconds()
                /
                60.0,
            )

        except Exception:
            return None


    def get_fresh_research(
        self,
        symbol: str,
        max_age_minutes: float = 30.0,
        source: Optional[str] = None,
    ) -> Optional[ExternalResearch]:

        research = (
            self.get_latest_research(
                symbol,
                source=source,
            )
        )

        if research is None:
            return None

        age = (
            self.get_age_minutes(
                research
            )
        )

        if age is None:
            return None

        if (
            age
            >
            float(
                max_age_minutes
            )
        ):
            return None

        return research


    # ========================================================
    # LIST RECENT
    # ========================================================

    def list_recent_research(
        self,
        limit: int = 20,
        source: Optional[str] = None,
    ) -> List[ExternalResearch]:

        limit = max(
            1,
            min(
                int(limit),
                500,
            ),
        )

        rows: Iterable[Any]

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

                    if source:

                        cur.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            WHERE source = %s
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (
                                clean_source(
                                    source
                                ),
                                limit,
                            ),
                        )

                    else:

                        cur.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (
                                limit,
                            ),
                        )

                    rows = (
                        cur.fetchall()
                    )

            finally:
                conn.close()

        else:
            conn = (
                self._connect_sqlite()
            )

            with self._lock:

                if source:

                    rows = (
                        conn.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            WHERE source = ?
                            ORDER BY created_at DESC
                            LIMIT ?
                            """,
                            (
                                clean_source(
                                    source
                                ),
                                limit,
                            ),
                        )
                        .fetchall()
                    )

                else:

                    rows = (
                        conn.execute(
                            """
                            SELECT
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
                            FROM external_research_latest
                            ORDER BY created_at DESC
                            LIMIT ?
                            """,
                            (
                                limit,
                            ),
                        )
                        .fetchall()
                    )

        result: List[
            ExternalResearch
        ] = []

        for row in rows:
            item = (
                self._row_to_research(
                    row
                )
            )

            if item is not None:
                result.append(
                    item
                )

        return result


    def list_recent(
        self,
        limit: int = 20,
        source: Optional[str] = None,
    ) -> List[ExternalResearch]:
        return self.list_recent_research(
            limit=limit,
            source=source,
        )


    # ========================================================
    # HISTORY
    # ========================================================

    def get_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[ExternalResearch]:

        symbol = clean_symbol(
            symbol
        )

        if not symbol:
            return []

        limit = max(
            1,
            min(
                int(limit),
                1000,
            ),
        )

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
                        SELECT
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
                        FROM external_research_history
                        WHERE symbol = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (
                            symbol,
                            limit,
                        ),
                    )

                    rows = (
                        cur.fetchall()
                    )

            finally:
                conn.close()

        else:
            conn = (
                self._connect_sqlite()
            )

            with self._lock:

                rows = (
                    conn.execute(
                        """
                        SELECT
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
                        FROM external_research_history
                        WHERE symbol = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (
                            symbol,
                            limit,
                        ),
                    )
                    .fetchall()
                )

        result: List[
            ExternalResearch
        ] = []

        for row in rows:

            item = (
                self._row_to_research(
                    row
                )
            )

            if item is not None:
                result.append(
                    item
                )

        return result


    def get_research_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[ExternalResearch]:
        return self.get_history(
            symbol=symbol,
            limit=limit,
        )


    # ========================================================
    # DELETE
    # ========================================================

    def delete_symbol(
        self,
        symbol: str,
    ) -> bool:

        symbol = clean_symbol(
            symbol
        )

        if not symbol:
            return False

        if (
            self.backend
            ==
            "POSTGRES"
        ):
            conn = (
                self._connect_postgres()
            )

            try:
                with conn:
                    with conn.cursor() as cur:

                        cur.execute(
                            """
                            DELETE FROM
                            external_research_latest
                            WHERE symbol = %s
                            """,
                            (
                                symbol,
                            ),
                        )

                        cur.execute(
                            """
                            DELETE FROM
                            external_research_history
                            WHERE symbol = %s
                            """,
                            (
                                symbol,
                            ),
                        )

            finally:
                conn.close()

            return True

        conn = (
            self._connect_sqlite()
        )

        with self._lock:

            conn.execute(
                """
                DELETE FROM
                external_research_latest
                WHERE symbol = ?
                """,
                (
                    symbol,
                ),
            )

            conn.execute(
                """
                DELETE FROM
                external_research_history
                WHERE symbol = ?
                """,
                (
                    symbol,
                ),
            )

            conn.commit()

        return True


    def delete_research(
        self,
        symbol: str,
    ) -> bool:
        return self.delete_symbol(
            symbol
        )


    # ========================================================
    # COUNT
    # ========================================================

    def count(
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


    def count_latest(
        self,
    ) -> int:
        return self.count()


    # ========================================================
    # HEALTH
    # ========================================================

    def health_check(
        self,
    ) -> bool:

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

                        result = (
                            cur.fetchone()
                        )

                        return bool(
                            result
                            and
                            result[0] == 1
                        )

                finally:
                    conn.close()

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

            return bool(
                row
                and
                row[0] == 1
            )

        except Exception:
            return False


    def health_details(
        self,
    ) -> Dict[str, Any]:

        return {
            "healthy": self.health_check(),
            "backend": self.backend,
            "postgres_enabled": (
                self.backend
                ==
                "POSTGRES"
            ),
            "sqlite_path": (
                None
                if self.backend
                ==
                "POSTGRES"
                else
                self.sqlite_path
            ),
            "database_url_present": bool(
                self.database_url
            ),
            "execution_authority": False,
            "mode": "RESEARCH_BRIDGE_ONLY",
        }


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
# SINGLETON
# ============================================================

_bridge_instance: Optional[
    ExternalResearchBridge
] = None

_bridge_lock = (
    threading.Lock()
)


def get_external_research_bridge(
) -> ExternalResearchBridge:

    global _bridge_instance

    if (
        _bridge_instance
        is None
    ):

        with _bridge_lock:

            if (
                _bridge_instance
                is None
            ):

                _bridge_instance = (
                    ExternalResearchBridge()
                )

    return _bridge_instance


# ============================================================
# LOCAL TEST
# ============================================================

if __name__ == "__main__":

    bridge = (
        ExternalResearchBridge()
    )

    print(
        "======================================"
    )

    print(
        "JALWE EXTERNAL RESEARCH BRIDGE V4"
    )

    print(
        "======================================"
    )

    print(
        "BACKEND:",
        bridge.backend,
    )

    print(
        "HEALTH:",
        bridge.health_check(),
    )

    print(
        "COUNT:",
        bridge.count(),
    )

    print(
        "EXECUTION AUTHORITY: DISABLED"
    )

    print(
        "======================================"
    )