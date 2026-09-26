# ============================================================
# JALWE AI TRADER V4
# EXTERNAL RESEARCH BRIDGE V5
#
# PURPOSE:
#   Read/write Apex research packets using the SAME shared schema
#   used by Apex jalwe_bridge_client.py V5.
#
# SAFETY:
#   - Research only
#   - No broker/order execution
#   - Apex has zero execution authority
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
import threading

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


UTC = timezone.utc
DEFAULT_SOURCE = "APEX"

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

if load_dotenv is not None and ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)

DEFAULT_SQLITE_PATH = BASE_DIR / "data" / "apex_jalwe_bridge.db"


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso() -> str:
    return utc_now().isoformat()


def clean_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def clean_source(value: Any) -> str:
    text = str(value or DEFAULT_SOURCE).strip().upper()
    return text or DEFAULT_SOURCE


def clean_optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
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
    return max(minimum, min(maximum, number))


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

    if isinstance(value, (list, tuple, set)):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    text = str(value).strip()
    return [text] if text else []


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def normalize_created_at(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.isoformat()

    text = str(value or "").strip()
    if not text:
        return utc_iso()

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.isoformat()
    except Exception:
        return utc_iso()


def _resolve_sqlite_path(value: Optional[str | Path]) -> str:
    if value is not None and str(value).strip():
        raw = str(value).strip()
    else:
        raw = (
            os.getenv("JALWE_EXTERNAL_RESEARCH_DB", "").strip()
            or os.getenv("APEX_JALWE_BRIDGE_DB", "").strip()
            or str(DEFAULT_SQLITE_PATH)
        )

    if raw == ":memory:":
        return raw

    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


# ============================================================
# RESEARCH MODEL
# ============================================================

@dataclass
class ExternalResearch:
    symbol: str
    source: str = DEFAULT_SOURCE
    news_score: Optional[float] = None
    sentiment: str = "UNKNOWN"
    catalyst: Optional[str] = None
    confidence: float = 0.0
    summary: str = ""
    market_bias: Optional[str] = None
    technical_notes: List[str] = field(default_factory=list)
    headlines: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbol = clean_symbol(self.symbol)
        self.source = clean_source(self.source)

        if self.news_score is not None:
            self.news_score = clamp(self.news_score, 0.0, 100.0, 50.0)

        self.sentiment = str(self.sentiment or "UNKNOWN").strip().upper()
        self.confidence = clamp(self.confidence, 0.0, 1.0, 0.0)
        self.summary = str(self.summary or "").strip()
        self.market_bias = (
            str(self.market_bias).strip().upper()
            if self.market_bias is not None
            else None
        )
        self.catalyst = clean_optional_text(self.catalyst)
        self.technical_notes = normalize_list(self.technical_notes)
        self.headlines = normalize_list(self.headlines)
        self.risk_flags = normalize_list(self.risk_flags)
        self.created_at = normalize_created_at(self.created_at)
        if not isinstance(self.metadata, dict):
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# BRIDGE
# ============================================================

class ExternalResearchBridge:
    """
    Shared Apex -> JALWE research bridge.

    The schema intentionally matches Apex jalwe_bridge_client.py V5:
        research_latest
        research_history

    This class never submits, cancels, or modifies broker orders.
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        sqlite_path: Optional[str | Path] = None,
        database_path: Optional[str | Path] = None,
    ) -> None:
        explicit_sqlite = sqlite_path if sqlite_path is not None else database_path
        env_sqlite = (
            os.getenv("JALWE_EXTERNAL_RESEARCH_DB", "").strip()
            or os.getenv("APEX_JALWE_BRIDGE_DB", "").strip()
        )
        sqlite_is_explicit = bool(explicit_sqlite or env_sqlite)

        if database_url is not None:
            self.database_url = str(database_url).strip()
        elif sqlite_is_explicit:
            self.database_url = ""
        else:
            self.database_url = os.getenv("DATABASE_URL", "").strip()

        self.backend = "POSTGRES" if self.database_url else "SQLITE"
        self.sqlite_path = _resolve_sqlite_path(explicit_sqlite)

        # Backward-compatible alias used by earlier diagnostics/tests.
        self.database_path = self.sqlite_path

        self.research_only = True
        self.order_execution_enabled = False
        self.execution_authority = False

        self._lock = threading.RLock()
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._closed = False

        self._initialize()

    # ========================================================
    # CONNECTIONS
    # ========================================================

    def _connect_postgres(self):
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL backend requires psycopg2-binary."
            ) from exc

        return psycopg2.connect(
            self.database_url,
            connect_timeout=10,
        )

    def _connect_sqlite(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise RuntimeError("ExternalResearchBridge is closed.")

            if self._sqlite_conn is not None:
                return self._sqlite_conn

            if self.sqlite_path != ":memory:":
                path = Path(self.sqlite_path)
                path.parent.mkdir(parents=True, exist_ok=True)

            conn = sqlite3.connect(
                self.sqlite_path,
                timeout=30,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")

            self._sqlite_conn = conn
            return conn

    # ========================================================
    # SCHEMA
    # ========================================================

    def _initialize(self) -> None:
        if self.backend == "POSTGRES":
            self._initialize_postgres()
        else:
            self._initialize_sqlite()

    def _initialize_postgres(self) -> None:
        conn = self._connect_postgres()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS research_latest (
                            symbol TEXT PRIMARY KEY,
                            source TEXT NOT NULL,
                            news_score DOUBLE PRECISION,
                            sentiment TEXT NOT NULL,
                            catalyst TEXT,
                            confidence DOUBLE PRECISION NOT NULL,
                            summary TEXT NOT NULL,
                            market_bias TEXT,
                            technical_notes_json TEXT NOT NULL,
                            headlines_json TEXT NOT NULL,
                            risk_flags_json TEXT NOT NULL,
                            metadata_json TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS research_history (
                            id BIGSERIAL PRIMARY KEY,
                            symbol TEXT NOT NULL,
                            source TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_research_history_symbol_created
                        ON research_history(symbol, created_at DESC)
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS
                        idx_research_latest_created
                        ON research_latest(created_at DESC)
                        """
                    )
        finally:
            conn.close()

    def _initialize_sqlite(self) -> None:
        conn = self._connect_sqlite()
        with self._lock:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_latest (
                    symbol TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    news_score REAL,
                    sentiment TEXT NOT NULL,
                    catalyst TEXT,
                    confidence REAL NOT NULL,
                    summary TEXT NOT NULL,
                    market_bias TEXT,
                    technical_notes_json TEXT NOT NULL,
                    headlines_json TEXT NOT NULL,
                    risk_flags_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_research_history_symbol_created
                ON research_history(symbol, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_research_latest_created
                ON research_latest(created_at DESC)
                """
            )
            conn.commit()

    # ========================================================
    # SERIALIZATION
    # ========================================================

    @staticmethod
    def _row_to_research(row: Any) -> Optional[ExternalResearch]:
        if row is None:
            return None

        if isinstance(row, sqlite3.Row):
            data = dict(row)
        elif isinstance(row, dict):
            data = row
        else:
            columns = [
                "symbol",
                "source",
                "news_score",
                "sentiment",
                "catalyst",
                "confidence",
                "summary",
                "market_bias",
                "technical_notes_json",
                "headlines_json",
                "risk_flags_json",
                "metadata_json",
                "created_at",
                "updated_at",
            ]
            data = dict(zip(columns, row))

        return ExternalResearch(
            symbol=data.get("symbol", ""),
            source=data.get("source", DEFAULT_SOURCE),
            news_score=data.get("news_score"),
            sentiment=data.get("sentiment") or "UNKNOWN",
            catalyst=data.get("catalyst"),
            confidence=data.get("confidence", 0.0),
            summary=data.get("summary", ""),
            market_bias=data.get("market_bias"),
            technical_notes=normalize_list(
                json_loads(data.get("technical_notes_json"), [])
            ),
            headlines=normalize_list(
                json_loads(data.get("headlines_json"), [])
            ),
            risk_flags=normalize_list(
                json_loads(data.get("risk_flags_json"), [])
            ),
            metadata=json_loads(data.get("metadata_json"), {}),
            created_at=data.get("created_at") or utc_iso(),
        )

    @staticmethod
    def _history_payload_to_research(payload: Any) -> Optional[ExternalResearch]:
        data = json_loads(payload, {})
        if not isinstance(data, dict) or not data:
            return None

        return ExternalResearch(
            symbol=data.get("symbol", ""),
            source=data.get("source", DEFAULT_SOURCE),
            news_score=data.get("news_score"),
            sentiment=data.get("sentiment") or "UNKNOWN",
            catalyst=data.get("catalyst"),
            confidence=data.get("confidence", 0.0),
            summary=data.get("summary", ""),
            market_bias=data.get("market_bias"),
            technical_notes=normalize_list(data.get("technical_notes", [])),
            headlines=normalize_list(data.get("headlines", [])),
            risk_flags=normalize_list(data.get("risk_flags", [])),
            metadata=(data.get("metadata") if isinstance(data.get("metadata"), dict) else {}),
            created_at=data.get("created_at") or utc_iso(),
        )

    # ========================================================
    # PUBLISH (RESEARCH ONLY)
    # ========================================================

    def publish_research(self, research: ExternalResearch) -> bool:
        if not isinstance(research, ExternalResearch):
            raise TypeError("research must be ExternalResearch")
        if not research.symbol:
            raise ValueError("Research symbol is required.")

        research = ExternalResearch(**research.to_dict())
        payload_json = json_dumps(research.to_dict())
        updated_at = utc_iso()

        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO research_history (
                                symbol, source, payload_json, created_at
                            ) VALUES (%s, %s, %s, %s)
                            """,
                            (
                                research.symbol,
                                research.source,
                                payload_json,
                                research.created_at,
                            ),
                        )
                        cur.execute(
                            """
                            INSERT INTO research_latest (
                                symbol, source, news_score, sentiment, catalyst,
                                confidence, summary, market_bias,
                                technical_notes_json, headlines_json,
                                risk_flags_json, metadata_json,
                                created_at, updated_at
                            ) VALUES (
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                            )
                            ON CONFLICT(symbol) DO UPDATE SET
                                source = EXCLUDED.source,
                                news_score = EXCLUDED.news_score,
                                sentiment = EXCLUDED.sentiment,
                                catalyst = EXCLUDED.catalyst,
                                confidence = EXCLUDED.confidence,
                                summary = EXCLUDED.summary,
                                market_bias = EXCLUDED.market_bias,
                                technical_notes_json = EXCLUDED.technical_notes_json,
                                headlines_json = EXCLUDED.headlines_json,
                                risk_flags_json = EXCLUDED.risk_flags_json,
                                metadata_json = EXCLUDED.metadata_json,
                                created_at = EXCLUDED.created_at,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                research.symbol,
                                research.source,
                                research.news_score,
                                research.sentiment,
                                research.catalyst,
                                research.confidence,
                                research.summary,
                                research.market_bias,
                                json_dumps(research.technical_notes),
                                json_dumps(research.headlines),
                                json_dumps(research.risk_flags),
                                json_dumps(research.metadata),
                                research.created_at,
                                updated_at,
                            ),
                        )
                return True
            finally:
                conn.close()

        conn = self._connect_sqlite()
        with self._lock:
            conn.execute(
                """
                INSERT INTO research_history (
                    symbol, source, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    research.symbol,
                    research.source,
                    payload_json,
                    research.created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO research_latest (
                    symbol, source, news_score, sentiment, catalyst,
                    confidence, summary, market_bias,
                    technical_notes_json, headlines_json,
                    risk_flags_json, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    source = excluded.source,
                    news_score = excluded.news_score,
                    sentiment = excluded.sentiment,
                    catalyst = excluded.catalyst,
                    confidence = excluded.confidence,
                    summary = excluded.summary,
                    market_bias = excluded.market_bias,
                    technical_notes_json = excluded.technical_notes_json,
                    headlines_json = excluded.headlines_json,
                    risk_flags_json = excluded.risk_flags_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    research.symbol,
                    research.source,
                    research.news_score,
                    research.sentiment,
                    research.catalyst,
                    research.confidence,
                    research.summary,
                    research.market_bias,
                    json_dumps(research.technical_notes),
                    json_dumps(research.headlines),
                    json_dumps(research.risk_flags),
                    json_dumps(research.metadata),
                    research.created_at,
                    updated_at,
                ),
            )
            conn.commit()
        return True

    def publish(self, research: ExternalResearch) -> bool:
        return self.publish_research(research)

    # ========================================================
    # GET LATEST
    # ========================================================

    def get_latest_research(
        self,
        symbol: str,
        source: Optional[str] = None,
    ) -> Optional[ExternalResearch]:
        symbol = clean_symbol(symbol)
        if not symbol:
            return None

        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    if source:
                        cur.execute(
                            """
                            SELECT symbol,source,news_score,sentiment,catalyst,
                                   confidence,summary,market_bias,
                                   technical_notes_json,headlines_json,
                                   risk_flags_json,metadata_json,
                                   created_at,updated_at
                            FROM research_latest
                            WHERE symbol = %s AND source = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (symbol, clean_source(source)),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT symbol,source,news_score,sentiment,catalyst,
                                   confidence,summary,market_bias,
                                   technical_notes_json,headlines_json,
                                   risk_flags_json,metadata_json,
                                   created_at,updated_at
                            FROM research_latest
                            WHERE symbol = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (symbol,),
                        )
                    return self._row_to_research(cur.fetchone())
            finally:
                conn.close()

        conn = self._connect_sqlite()
        with self._lock:
            if source:
                row = conn.execute(
                    """
                    SELECT * FROM research_latest
                    WHERE symbol = ? AND source = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (symbol, clean_source(source)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM research_latest
                    WHERE symbol = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()

        return self._row_to_research(row)

    # ========================================================
    # FRESHNESS
    # ========================================================

    def get_age_minutes(
        self,
        research: Optional[ExternalResearch],
    ) -> Optional[float]:
        if research is None:
            return None

        try:
            created = datetime.fromisoformat(
                str(research.created_at).replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            else:
                created = created.astimezone(UTC)
            return max(
                0.0,
                (utc_now() - created).total_seconds() / 60.0,
            )
        except Exception:
            return None

    def is_fresh(
        self,
        research: Optional[ExternalResearch],
        *,
        max_age_minutes: float = 30.0,
    ) -> bool:
        age = self.get_age_minutes(research)
        return bool(
            age is not None
            and age <= float(max_age_minutes)
        )

    def get_fresh_research(
        self,
        symbol: str,
        max_age_minutes: float = 30.0,
        source: Optional[str] = None,
    ) -> Optional[ExternalResearch]:
        research = self.get_latest_research(symbol, source=source)
        if research is None:
            return None
        if not self.is_fresh(research, max_age_minutes=max_age_minutes):
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
        limit = max(1, min(int(limit), 500))

        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    if source:
                        cur.execute(
                            """
                            SELECT symbol,source,news_score,sentiment,catalyst,
                                   confidence,summary,market_bias,
                                   technical_notes_json,headlines_json,
                                   risk_flags_json,metadata_json,
                                   created_at,updated_at
                            FROM research_latest
                            WHERE source = %s
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (clean_source(source), limit),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT symbol,source,news_score,sentiment,catalyst,
                                   confidence,summary,market_bias,
                                   technical_notes_json,headlines_json,
                                   risk_flags_json,metadata_json,
                                   created_at,updated_at
                            FROM research_latest
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (limit,),
                        )
                    rows = cur.fetchall()
            finally:
                conn.close()
        else:
            conn = self._connect_sqlite()
            with self._lock:
                if source:
                    rows = conn.execute(
                        """
                        SELECT * FROM research_latest
                        WHERE source = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (clean_source(source), limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM research_latest
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()

        output: List[ExternalResearch] = []
        for row in rows:
            item = self._row_to_research(row)
            if item is not None:
                output.append(item)
        return output

    def list_recent(
        self,
        limit: int = 20,
        source: Optional[str] = None,
    ) -> List[ExternalResearch]:
        return self.list_recent_research(limit=limit, source=source)

    # ========================================================
    # HISTORY
    # ========================================================

    def get_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[ExternalResearch]:
        symbol = clean_symbol(symbol)
        if not symbol:
            return []
        limit = max(1, min(int(limit), 1000))

        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT payload_json
                        FROM research_history
                        WHERE symbol = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (symbol, limit),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            payloads = [row[0] for row in rows]
        else:
            conn = self._connect_sqlite()
            with self._lock:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM research_history
                    WHERE symbol = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (symbol, limit),
                ).fetchall()
            payloads = [row["payload_json"] for row in rows]

        output: List[ExternalResearch] = []
        for payload in payloads:
            item = self._history_payload_to_research(payload)
            if item is not None:
                output.append(item)
        return output

    def get_research_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[ExternalResearch]:
        return self.get_history(symbol=symbol, limit=limit)

    # ========================================================
    # DELETE
    # ========================================================

    def delete_symbol(self, symbol: str) -> bool:
        symbol = clean_symbol(symbol)
        if not symbol:
            return False

        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM research_latest WHERE symbol = %s",
                            (symbol,),
                        )
                        cur.execute(
                            "DELETE FROM research_history WHERE symbol = %s",
                            (symbol,),
                        )
                return True
            finally:
                conn.close()

        conn = self._connect_sqlite()
        with self._lock:
            conn.execute(
                "DELETE FROM research_latest WHERE symbol = ?",
                (symbol,),
            )
            conn.execute(
                "DELETE FROM research_history WHERE symbol = ?",
                (symbol,),
            )
            conn.commit()
        return True

    def delete_research(self, symbol: str) -> bool:
        return self.delete_symbol(symbol)

    # ========================================================
    # COUNT / HEALTH
    # ========================================================

    def count(self) -> int:
        if self.backend == "POSTGRES":
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM research_latest")
                    row = cur.fetchone()
                    return int(row[0] if row else 0)
            finally:
                conn.close()

        conn = self._connect_sqlite()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM research_latest"
            ).fetchone()
        return int(row["total"] if row else 0)

    def count_latest(self) -> int:
        return self.count()

    def health_check(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "backend": self.backend,
            "database_url_present": bool(self.database_url),
            "sqlite_path": self.sqlite_path if self.backend == "SQLITE" else None,
            "database": self.sqlite_path if self.backend == "SQLITE" else "POSTGRES",
            "schema": "JALWE_SHARED_RESEARCH_V2",
            "research_only": True,
            "order_execution_enabled": False,
            "execution_authority": False,
            "latest_research_count": 0,
            "error": None,
        }

        try:
            if self.backend == "POSTGRES":
                conn = self._connect_postgres()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        row = cur.fetchone()
                        result["ok"] = bool(row and row[0] == 1)
                finally:
                    conn.close()
            else:
                conn = self._connect_sqlite()
                with self._lock:
                    row = conn.execute("SELECT 1").fetchone()
                result["ok"] = bool(row and row[0] == 1)

            if result["ok"]:
                result["latest_research_count"] = self.count_latest()
        except Exception as exc:
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def health_details(self) -> Dict[str, Any]:
        details = dict(self.health_check())
        details["healthy"] = bool(details.get("ok"))
        details["postgres_enabled"] = self.backend == "POSTGRES"
        details["mode"] = "RESEARCH_BRIDGE_ONLY"
        return details

    # ========================================================
    # CLOSE
    # ========================================================

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._sqlite_conn is not None:
                try:
                    self._sqlite_conn.close()
                except Exception:
                    pass
                self._sqlite_conn = None
            self._closed = True


# ============================================================
# SINGLETON
# ============================================================

_bridge_instance: Optional[ExternalResearchBridge] = None
_bridge_lock = threading.Lock()


def get_external_research_bridge() -> ExternalResearchBridge:
    global _bridge_instance

    if _bridge_instance is None:
        with _bridge_lock:
            if _bridge_instance is None:
                _bridge_instance = ExternalResearchBridge()

    return _bridge_instance


# ============================================================
# LOCAL TEST
# ============================================================

if __name__ == "__main__":
    bridge = ExternalResearchBridge()

    print("======================================")
    print("JALWE EXTERNAL RESEARCH BRIDGE V5")
    print("======================================")
    print("HEALTH:", bridge.health_check())
    print("COUNT:", bridge.count_latest())
    print("RESEARCH ONLY: True")
    print("EXECUTION AUTHORITY: DISABLED")
    print("======================================")