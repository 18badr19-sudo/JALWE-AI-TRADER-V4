from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from core.config import settings
from core.models import Trade, TradeStatus


class Database:
    """
    Central SQLite database for JALWE AI TRADER V4.

    This is the only database layer other modules should use.
    """

    def __init__(self, database_path: Optional[str] = None):
        self.database_path = Path(
            database_path or settings.DATABASE_PATH
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.database_path,
            timeout=30,
        )

        conn.row_factory = sqlite3.Row

        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")

            yield conn

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    signal_id TEXT,
                    broker_order_id TEXT,

                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,

                    quantity INTEGER NOT NULL,

                    entry_price REAL NOT NULL,
                    exit_price REAL,

                    stop_price REAL NOT NULL,

                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    target_3 REAL NOT NULL,

                    opened_at TEXT,
                    closed_at TEXT,

                    exit_reason TEXT,

                    realized_pnl REAL DEFAULT 0,
                    realized_pnl_pct REAL DEFAULT 0,

                    max_favorable_excursion REAL,
                    max_adverse_excursion REAL,

                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_trades_symbol
                ON trades(symbol)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_trades_status
                ON trades(status)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,

                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    action TEXT NOT NULL,

                    score REAL NOT NULL,
                    ai_probability REAL,

                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,

                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    target_3 REAL NOT NULL,

                    reason TEXT,

                    features_json TEXT,

                    created_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_signals_symbol
                ON signals(symbol)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_orders (
                    order_id TEXT PRIMARY KEY,

                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,

                    quantity INTEGER NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,

                    status TEXT NOT NULL,

                    requested_price REAL,
                    filled_price REAL,

                    submitted_at TEXT,
                    filled_at TEXT,

                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feature_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    signal_id TEXT,
                    symbol TEXT NOT NULL,

                    snapshot_json TEXT NOT NULL,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_features_symbol
                ON feature_snapshots(symbol)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,

                    message TEXT NOT NULL,

                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def save_trade(self, trade: Trade) -> None:
        metadata = json.dumps(
            trade.metadata,
            default=str,
        )

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id,
                    signal_id,
                    broker_order_id,
                    symbol,
                    strategy,
                    status,
                    quantity,
                    entry_price,
                    exit_price,
                    stop_price,
                    target_1,
                    target_2,
                    target_3,
                    opened_at,
                    closed_at,
                    exit_reason,
                    realized_pnl,
                    realized_pnl_pct,
                    max_favorable_excursion,
                    max_adverse_excursion,
                    metadata_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(trade_id)
                DO UPDATE SET
                    signal_id = excluded.signal_id,
                    broker_order_id = excluded.broker_order_id,
                    status = excluded.status,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    exit_price = excluded.exit_price,
                    stop_price = excluded.stop_price,
                    target_1 = excluded.target_1,
                    target_2 = excluded.target_2,
                    target_3 = excluded.target_3,
                    opened_at = excluded.opened_at,
                    closed_at = excluded.closed_at,
                    exit_reason = excluded.exit_reason,
                    realized_pnl = excluded.realized_pnl,
                    realized_pnl_pct = excluded.realized_pnl_pct,
                    max_favorable_excursion =
                        excluded.max_favorable_excursion,
                    max_adverse_excursion =
                        excluded.max_adverse_excursion,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    trade.trade_id,
                    trade.signal_id,
                    trade.broker_order_id,
                    trade.symbol,
                    trade.strategy.value,
                    trade.status.value,
                    trade.quantity,
                    trade.entry_price,
                    trade.exit_price,
                    trade.stop_price,
                    trade.target_1,
                    trade.target_2,
                    trade.target_3,
                    (
                        trade.opened_at.isoformat()
                        if trade.opened_at
                        else None
                    ),
                    (
                        trade.closed_at.isoformat()
                        if trade.closed_at
                        else None
                    ),
                    trade.exit_reason,
                    trade.realized_pnl,
                    trade.realized_pnl_pct,
                    trade.max_favorable_excursion,
                    trade.max_adverse_excursion,
                    metadata,
                ),
            )

    def get_open_trades(self) -> list[dict[str, Any]]:
        open_statuses = (
            TradeStatus.PENDING.value,
            TradeStatus.OPEN.value,
            TradeStatus.PARTIALLY_CLOSED.value,
        )

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM trades
                WHERE status IN (?, ?, ?)
                ORDER BY created_at ASC
                """,
                open_statuses,
            ).fetchall()

        return [dict(row) for row in rows]

    def log_event(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:

        metadata_json = json.dumps(
            metadata or {},
            default=str,
        )

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO system_events (
                    event_type,
                    severity,
                    message,
                    metadata_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_type,
                    severity,
                    message,
                    metadata_json,
                ),
            )


database = Database()
