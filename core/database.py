from __future__ import annotations

import json
import sqlite3

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from core.config import settings
from core.models import (
    BrokerOrder,
    Trade,
    TradeStatus,
)


class Database:
    """
    Central SQLite database for JALWE AI TRADER V4.

    This is the only database layer other modules should use.

    Responsibilities:
    - Trades
    - Managed trade state
    - Broker orders
    - Crash-safe entry intents
    - Signals
    - Feature snapshots
    - System events

    ManagedTrade state is persisted separately so JALWE
    can recover safely after restart.
    """

    def __init__(
        self,
        database_path: Optional[str] = None,
    ) -> None:

        self.database_path = Path(
            database_path
            or settings.DATABASE_PATH
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.initialize()

    # ========================================================
    # CONNECTION
    # ========================================================

    @contextmanager
    def connection(
        self,
    ) -> Iterator[sqlite3.Connection]:

        conn = sqlite3.connect(
            self.database_path,
            timeout=30,
        )

        conn.row_factory = sqlite3.Row

        try:
            conn.execute(
                "PRAGMA journal_mode=WAL;"
            )

            conn.execute(
                "PRAGMA foreign_keys=ON;"
            )

            conn.execute(
                "PRAGMA busy_timeout=30000;"
            )

            yield conn

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    # ========================================================
    # MIGRATION HELPERS
    # ========================================================

    @staticmethod
    def _table_columns(
        conn: sqlite3.Connection,
        table: str,
    ) -> set[str]:

        rows = conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()

        return {
            str(row["name"])
            for row in rows
        }

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:

        columns = self._table_columns(
            conn,
            table,
        )

        if column not in columns:
            conn.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN {column} {definition}
                """
            )

    # ========================================================
    # INITIALIZE DATABASE
    # ========================================================

    def initialize(self) -> None:

        with self.connection() as conn:

            # =================================================
            # TRADES
            # =================================================

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

            # =================================================
            # MANAGED TRADE STATE
            # =================================================

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_trades (
                    trade_id TEXT PRIMARY KEY,

                    symbol TEXT NOT NULL,

                    entry_order_id TEXT,
                    entry_fill_price REAL,

                    entry_price REAL NOT NULL,

                    initial_quantity INTEGER NOT NULL,
                    remaining_quantity INTEGER NOT NULL,

                    initial_stop REAL NOT NULL,
                    current_stop REAL NOT NULL,

                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    target_3 REAL NOT NULL,

                    stage TEXT NOT NULL,

                    t1_completed INTEGER NOT NULL DEFAULT 0,
                    t2_completed INTEGER NOT NULL DEFAULT 0,

                    t1_realized_quantity INTEGER NOT NULL DEFAULT 0,
                    t2_realized_quantity INTEGER NOT NULL DEFAULT 0,

                    realized_quantity INTEGER NOT NULL DEFAULT 0,

                    trailing_active INTEGER NOT NULL DEFAULT 0,
                    highest_price REAL,

                    pending_action TEXT,
                    pending_order_id TEXT,

                    pending_requested_quantity INTEGER NOT NULL DEFAULT 0,
                    pending_filled_quantity INTEGER NOT NULL DEFAULT 0,

                    pending_status TEXT,

                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_managed_trades_symbol
                ON managed_trades(symbol)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_managed_trades_stage
                ON managed_trades(stage)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_managed_trades_pending_order
                ON managed_trades(pending_order_id)
                """
            )

            # =================================================
            # SIGNALS
            # =================================================

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

            # =================================================
            # BROKER ORDERS
            # =================================================

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_orders (
                    order_id TEXT PRIMARY KEY,

                    client_order_id TEXT,

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

            # Existing V4 database may have been created
            # before client_order_id was added.
            self._ensure_column(
                conn,
                "broker_orders",
                "client_order_id",
                "TEXT",
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_broker_orders_symbol
                ON broker_orders(symbol)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_broker_orders_status
                ON broker_orders(status)
                """
            )

            # =================================================
            # CRASH-SAFE ENTRY INTENTS
            # =================================================

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entry_intents (
                    intent_id TEXT PRIMARY KEY,

                    client_order_id TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT UNIQUE,

                    symbol TEXT NOT NULL,
                    strategy TEXT,
                    setup_grade TEXT,

                    quantity INTEGER NOT NULL,

                    planned_entry REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_1 REAL NOT NULL,
                    target_2 REAL NOT NULL,
                    target_3 REAL NOT NULL,

                    risk_pct REAL NOT NULL,

                    decision_state TEXT,
                    decision_gates_json TEXT,

                    broker_status TEXT,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    filled_price REAL,

                    state TEXT NOT NULL,
                    managed_trade_id TEXT,

                    error_message TEXT,
                    metadata_json TEXT,

                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_entry_intents_symbol
                ON entry_intents(symbol)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_entry_intents_state
                ON entry_intents(state)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_entry_intents_broker_order
                ON entry_intents(broker_order_id)
                """
            )

            # =================================================
            # FEATURE SNAPSHOTS
            # =================================================

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

            # =================================================
            # STRATEGY REALIZED PNL LEDGER
            # =================================================

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_pnl_ledger (
                    event_key TEXT PRIMARY KEY,
                    trade_id TEXT,
                    order_id TEXT,
                    symbol TEXT NOT NULL,
                    action TEXT,
                    quantity INTEGER NOT NULL,
                    fill_price REAL,
                    entry_price REAL,
                    realized_pnl REAL NOT NULL,
                    event_time TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_strategy_pnl_event_time
                ON strategy_pnl_ledger(event_time)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_strategy_pnl_symbol
                ON strategy_pnl_ledger(symbol)
                """
            )

            # =================================================
            # SYSTEM EVENTS
            # =================================================

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

    # ========================================================
    # EXISTING TRADE MODEL
    # ========================================================

    def save_trade(
        self,
        trade: Trade,
    ) -> None:

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

    def get_open_trades(
        self,
    ) -> list[dict[str, Any]]:

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

        return [
            dict(row)
            for row in rows
        ]

    # ========================================================
    # MANAGED TRADE PERSISTENCE
    # ========================================================

    def save_managed_trade(
        self,
        trade_id: str,
        trade: Any,
        entry_order_id: Optional[str] = None,
        entry_fill_price: Optional[float] = None,
    ) -> None:
        """
        Persist the TradeManager state.

        This is the important restart-safety layer.
        """

        trade_id = str(
            trade_id or ""
        ).strip()

        if not trade_id:
            raise ValueError(
                "trade_id cannot be empty."
            )

        metadata_json = json.dumps(
            trade.metadata or {},
            default=str,
        )

        stage = getattr(
            trade.stage,
            "value",
            str(trade.stage),
        )

        pending_action = None

        if trade.pending_action is not None:
            pending_action = getattr(
                trade.pending_action,
                "value",
                str(trade.pending_action),
            )

        closed = (
            stage == "CLOSED"
        )

        with self.connection() as conn:

            conn.execute(
                """
                INSERT INTO managed_trades (
                    trade_id,
                    symbol,
                    entry_order_id,
                    entry_fill_price,
                    entry_price,
                    initial_quantity,
                    remaining_quantity,
                    initial_stop,
                    current_stop,
                    target_1,
                    target_2,
                    target_3,
                    stage,
                    t1_completed,
                    t2_completed,
                    t1_realized_quantity,
                    t2_realized_quantity,
                    realized_quantity,
                    trailing_active,
                    highest_price,
                    pending_action,
                    pending_order_id,
                    pending_requested_quantity,
                    pending_filled_quantity,
                    pending_status,
                    metadata_json,
                    closed_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, CASE
                        WHEN ? = 1
                        THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END
                )

                ON CONFLICT(trade_id)
                DO UPDATE SET
                    symbol = excluded.symbol,

                    entry_order_id =
                        COALESCE(
                            excluded.entry_order_id,
                            managed_trades.entry_order_id
                        ),

                    entry_fill_price =
                        COALESCE(
                            excluded.entry_fill_price,
                            managed_trades.entry_fill_price
                        ),

                    entry_price =
                        excluded.entry_price,

                    initial_quantity =
                        excluded.initial_quantity,

                    remaining_quantity =
                        excluded.remaining_quantity,

                    initial_stop =
                        excluded.initial_stop,

                    current_stop =
                        excluded.current_stop,

                    target_1 =
                        excluded.target_1,

                    target_2 =
                        excluded.target_2,

                    target_3 =
                        excluded.target_3,

                    stage =
                        excluded.stage,

                    t1_completed =
                        excluded.t1_completed,

                    t2_completed =
                        excluded.t2_completed,

                    t1_realized_quantity =
                        excluded.t1_realized_quantity,

                    t2_realized_quantity =
                        excluded.t2_realized_quantity,

                    realized_quantity =
                        excluded.realized_quantity,

                    trailing_active =
                        excluded.trailing_active,

                    highest_price =
                        excluded.highest_price,

                    pending_action =
                        excluded.pending_action,

                    pending_order_id =
                        excluded.pending_order_id,

                    pending_requested_quantity =
                        excluded.pending_requested_quantity,

                    pending_filled_quantity =
                        excluded.pending_filled_quantity,

                    pending_status =
                        excluded.pending_status,

                    metadata_json =
                        excluded.metadata_json,

                    closed_at =
                        CASE
                            WHEN excluded.stage = 'CLOSED'
                            THEN COALESCE(
                                managed_trades.closed_at,
                                CURRENT_TIMESTAMP
                            )
                            ELSE NULL
                        END,

                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    trade_id,

                    trade.symbol,

                    entry_order_id,
                    entry_fill_price,

                    trade.entry_price,

                    trade.initial_quantity,
                    trade.remaining_quantity,

                    trade.initial_stop,
                    trade.current_stop,

                    trade.target_1,
                    trade.target_2,
                    trade.target_3,

                    stage,

                    int(
                        bool(
                            trade.t1_completed
                        )
                    ),

                    int(
                        bool(
                            trade.t2_completed
                        )
                    ),

                    trade.t1_realized_quantity,
                    trade.t2_realized_quantity,

                    trade.realized_quantity,

                    int(
                        bool(
                            trade.trailing_active
                        )
                    ),

                    trade.highest_price,

                    pending_action,
                    trade.pending_order_id,

                    trade.pending_requested_quantity,
                    trade.pending_filled_quantity,

                    trade.pending_status,

                    metadata_json,

                    int(closed),
                ),
            )

    def get_managed_trade_row(
        self,
        trade_id: str,
    ) -> Optional[dict[str, Any]]:

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM managed_trades
                WHERE trade_id = ?
                """,
                (
                    trade_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    def get_managed_trade_row_by_entry_order_id(
        self,
        entry_order_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return an existing ManagedTrade row for an entry order."""

        entry_order_id = str(
            entry_order_id or ""
        ).strip()

        if not entry_order_id:
            return None

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM managed_trades
                WHERE entry_order_id = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    entry_order_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    def load_managed_trade(
        self,
        trade_id: str,
    ) -> Optional[Any]:
        """
        Rebuild a ManagedTrade object from SQLite.

        Lazy import prevents database.py from creating
        unnecessary module coupling at startup.
        """

        row = self.get_managed_trade_row(
            trade_id
        )

        if row is None:
            return None

        from trading.trade_manager import (
            ManagedTrade,
            TradeAction,
            TradeStage,
        )

        metadata: dict[str, Any]

        try:
            metadata = json.loads(
                row["metadata_json"] or "{}"
            )

        except json.JSONDecodeError:
            metadata = {}

        pending_action = None

        if row["pending_action"]:
            pending_action = TradeAction(
                row["pending_action"]
            )

        return ManagedTrade(
            symbol=row["symbol"],

            entry_price=float(
                row["entry_price"]
            ),

            initial_quantity=int(
                row["initial_quantity"]
            ),

            remaining_quantity=int(
                row["remaining_quantity"]
            ),

            initial_stop=float(
                row["initial_stop"]
            ),

            current_stop=float(
                row["current_stop"]
            ),

            target_1=float(
                row["target_1"]
            ),

            target_2=float(
                row["target_2"]
            ),

            target_3=float(
                row["target_3"]
            ),

            stage=TradeStage(
                row["stage"]
            ),

            t1_completed=bool(
                row["t1_completed"]
            ),

            t2_completed=bool(
                row["t2_completed"]
            ),

            t1_realized_quantity=int(
                row[
                    "t1_realized_quantity"
                ]
            ),

            t2_realized_quantity=int(
                row[
                    "t2_realized_quantity"
                ]
            ),

            realized_quantity=int(
                row["realized_quantity"]
            ),

            trailing_active=bool(
                row["trailing_active"]
            ),

            highest_price=(
                float(
                    row["highest_price"]
                )
                if row["highest_price"]
                is not None
                else None
            ),

            pending_action=(
                pending_action
            ),

            pending_order_id=(
                row["pending_order_id"]
            ),

            pending_requested_quantity=int(
                row[
                    "pending_requested_quantity"
                ]
            ),

            pending_filled_quantity=int(
                row[
                    "pending_filled_quantity"
                ]
            ),

            pending_status=(
                row["pending_status"]
            ),

            metadata=metadata,
        )

    def get_active_managed_trade_rows(
        self,
    ) -> list[dict[str, Any]]:

        with self.connection() as conn:

            rows = conn.execute(
                """
                SELECT *
                FROM managed_trades
                WHERE stage != 'CLOSED'
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def load_active_managed_trades(
        self,
    ) -> dict[str, Any]:

        rows = (
            self.get_active_managed_trade_rows()
        )

        result: dict[str, Any] = {}

        for row in rows:

            trade = self.load_managed_trade(
                row["trade_id"]
            )

            if trade is not None:
                result[
                    row["trade_id"]
                ] = trade

        return result

    # ========================================================
    # BROKER ORDER PERSISTENCE
    # ========================================================

    def save_broker_order(
        self,
        order: BrokerOrder,
    ) -> None:

        metadata_json = json.dumps(
            order.metadata or {},
            default=str,
        )

        side = getattr(
            order.side,
            "value",
            str(order.side),
        )

        status = getattr(
            order.status,
            "value",
            str(order.status),
        )

        submitted_at = getattr(
            order,
            "submitted_at",
            None,
        )

        filled_at = getattr(
            order,
            "filled_at",
            None,
        )

        with self.connection() as conn:

            conn.execute(
                """
                INSERT INTO broker_orders (
                    order_id,
                    client_order_id,
                    symbol,
                    side,
                    quantity,
                    filled_quantity,
                    status,
                    requested_price,
                    filled_price,
                    submitted_at,
                    filled_at,
                    metadata_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )

                ON CONFLICT(order_id)
                DO UPDATE SET
                    client_order_id =
                        excluded.client_order_id,

                    symbol =
                        excluded.symbol,

                    side =
                        excluded.side,

                    quantity =
                        excluded.quantity,

                    filled_quantity =
                        excluded.filled_quantity,

                    status =
                        excluded.status,

                    requested_price =
                        excluded.requested_price,

                    filled_price =
                        excluded.filled_price,

                    submitted_at =
                        excluded.submitted_at,

                    filled_at =
                        excluded.filled_at,

                    metadata_json =
                        excluded.metadata_json,

                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    order.order_id,

                    order.client_order_id,

                    order.symbol,

                    side,

                    order.quantity,

                    order.filled_quantity,

                    status,

                    order.requested_price,

                    order.filled_price,

                    (
                        submitted_at.isoformat()
                        if submitted_at
                        else None
                    ),

                    (
                        filled_at.isoformat()
                        if filled_at
                        else None
                    ),

                    metadata_json,
                ),
            )

    def get_broker_order_row(
        self,
        order_id: str,
    ) -> Optional[dict[str, Any]]:

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM broker_orders
                WHERE order_id = ?
                """,
                (
                    order_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    # ========================================================
    # ENTRY INTENT PERSISTENCE
    # ========================================================

    @staticmethod
    def _entry_intent_state_from_status(
        status: Any,
        filled_quantity: int = 0,
        requested_quantity: Optional[int] = None,
    ) -> str:
        """
        Convert broker order status into the persisted
        entry-intent lifecycle state.

        Important safety behavior:

        - FILLED stays unresolved until a ManagedTrade exists.
        - A terminal broker order with ZERO fills can resolve as
          CANCELED / REJECTED.
        - A terminal broker order with PARTIAL fills must remain
          unresolved as PARTIALLY_CANCELED / PARTIALLY_REJECTED
          until the filled shares are converted into a ManagedTrade.
        """

        raw = getattr(
            status,
            "value",
            status,
        )

        value = str(
            raw or ""
        ).strip().upper()

        try:
            filled = max(
                int(
                    filled_quantity or 0
                ),
                0,
            )
        except (TypeError, ValueError):
            filled = 0

        requested: Optional[int] = None

        if requested_quantity is not None:
            try:
                requested = max(
                    int(requested_quantity),
                    0,
                )
            except (TypeError, ValueError):
                requested = None

        if (
            requested is not None
            and requested > 0
            and filled >= requested
        ):
            return "FILLED"

        if value == "FILLED":
            return "FILLED"

        if value == "PARTIALLY_FILLED":
            return "PARTIALLY_FILLED"

        if value in {
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "DONE_FOR_DAY",
            "REPLACED",
        }:
            if filled > 0:
                return "PARTIALLY_CANCELED"
            return "CANCELED"

        if value in {
            "REJECTED",
            "SUSPENDED",
            "STOPPED",
        }:
            if filled > 0:
                return "PARTIALLY_REJECTED"
            return "REJECTED"

        return "SUBMITTED"

    def create_entry_intent(
        self,
        intent_id: str,
        client_order_id: str,
        symbol: str,
        quantity: int,
        planned_entry: float,
        stop_price: float,
        target_1: float,
        target_2: float,
        target_3: float,
        risk_pct: float,
        strategy: Optional[str] = None,
        setup_grade: Optional[str] = None,
        decision_state: Optional[str] = None,
        decision_gates: Optional[
            dict[str, Any]
        ] = None,
        metadata: Optional[
            dict[str, Any]
        ] = None,
    ) -> None:
        """
        Persist the entry intent BEFORE broker submission.

        This commit is the crash-safety anchor for entries.
        The caller must not submit an order until this method
        returns successfully.
        """

        intent_id = str(
            intent_id or ""
        ).strip()

        client_order_id = str(
            client_order_id or ""
        ).strip()

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not intent_id:
            raise ValueError(
                "intent_id cannot be empty."
            )

        if not client_order_id:
            raise ValueError(
                "client_order_id cannot be empty."
            )

        if not symbol:
            raise ValueError(
                "symbol cannot be empty."
            )

        try:
            quantity = int(
                quantity
            )

            planned_entry = float(
                planned_entry
            )

            stop_price = float(
                stop_price
            )

            target_1 = float(
                target_1
            )

            target_2 = float(
                target_2
            )

            target_3 = float(
                target_3
            )

            risk_pct = float(
                risk_pct
            )

        except (
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                "Entry intent contains invalid numeric values."
            ) from exc

        if quantity <= 0:
            raise ValueError(
                "Entry intent quantity must be positive."
            )

        if not (
            0
            < stop_price
            < planned_entry
            < target_1
            < target_2
            < target_3
        ):
            raise ValueError(
                "Entry intent price structure is invalid."
            )

        if not (
            0
            < risk_pct
            <= 1.50
        ):
            raise ValueError(
                "Entry intent risk exceeds JALWE safety limits."
            )

        gates_json = json.dumps(
            decision_gates or {},
            default=str,
        )

        metadata_json = json.dumps(
            metadata or {},
            default=str,
        )

        with self.connection() as conn:

            existing_symbol = conn.execute(
                """
                SELECT intent_id
                FROM entry_intents
                WHERE symbol = ?
                  AND state NOT IN (
                      'MANAGED',
                      'CANCELED',
                      'REJECTED',
                      'FAILED'
                  )
                LIMIT 1
                """,
                (
                    symbol,
                ),
            ).fetchone()

            if existing_symbol is not None:
                raise RuntimeError(
                    "An unresolved entry intent already exists "
                    f"for {symbol}."
                )

            conn.execute(
                """
                INSERT INTO entry_intents (
                    intent_id,
                    client_order_id,
                    broker_order_id,
                    symbol,
                    strategy,
                    setup_grade,
                    quantity,
                    planned_entry,
                    stop_price,
                    target_1,
                    target_2,
                    target_3,
                    risk_pct,
                    decision_state,
                    decision_gates_json,
                    broker_status,
                    filled_quantity,
                    filled_price,
                    state,
                    managed_trade_id,
                    error_message,
                    metadata_json,
                    completed_at
                )
                VALUES (
                    ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, NULL, 0, NULL, 'PREPARED', NULL, NULL,
                    ?, NULL
                )
                """,
                (
                    intent_id,
                    client_order_id,
                    symbol,
                    strategy,
                    setup_grade,
                    quantity,
                    planned_entry,
                    stop_price,
                    target_1,
                    target_2,
                    target_3,
                    risk_pct,
                    decision_state,
                    gates_json,
                    metadata_json,
                ),
            )

    def save_entry_order_snapshot(
        self,
        intent_id: str,
        order: BrokerOrder,
    ) -> dict[str, Any]:
        """
        Atomically persist a broker entry-order snapshot and
        advance the matching entry intent.

        Use this immediately after broker submission and again
        after each reconciliation snapshot.
        """

        intent_id = str(
            intent_id or ""
        ).strip()

        if not intent_id:
            raise ValueError(
                "intent_id cannot be empty."
            )

        side = getattr(
            order.side,
            "value",
            str(order.side),
        )

        if str(
            side or ""
        ).strip().upper() != "BUY":
            raise ValueError(
                "Entry intent can only persist BUY orders."
            )

        order_id = str(
            order.order_id or ""
        ).strip()

        if not order_id:
            raise ValueError(
                "Broker entry order ID is missing."
            )

        order_client_id = str(
            order.client_order_id or ""
        ).strip()

        symbol = str(
            order.symbol or ""
        ).strip().upper()

        status = getattr(
            order.status,
            "value",
            str(order.status),
        )

        submitted_at = getattr(
            order,
            "submitted_at",
            None,
        )

        filled_at = getattr(
            order,
            "filled_at",
            None,
        )

        order_metadata_json = json.dumps(
            order.metadata or {},
            default=str,
        )

        with self.connection() as conn:

            intent = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE intent_id = ?
                """,
                (
                    intent_id,
                ),
            ).fetchone()

            if intent is None:
                raise KeyError(
                    f"Entry intent not found: {intent_id}"
                )

            if str(
                intent["state"] or ""
            ).upper() == "MANAGED":
                raise RuntimeError(
                    "Managed entry intent cannot be modified."
                )

            expected_client_id = str(
                intent["client_order_id"] or ""
            ).strip()

            if (
                order_client_id
                and order_client_id
                != expected_client_id
            ):
                raise ValueError(
                    "Broker client_order_id does not match "
                    "the persisted entry intent."
                )

            if symbol != str(
                intent["symbol"] or ""
            ).strip().upper():
                raise ValueError(
                    "Broker order symbol does not match "
                    "the persisted entry intent."
                )

            if int(
                order.quantity
            ) != int(
                intent["quantity"]
            ):
                raise ValueError(
                    "Broker order quantity does not match "
                    "the persisted entry intent."
                )

            existing_filled = int(
                intent["filled_quantity"] or 0
            )

            incoming_filled = max(
                int(
                    order.filled_quantity or 0
                ),
                0,
            )

            persisted_filled = max(
                existing_filled,
                incoming_filled,
            )

            new_state = (
                self._entry_intent_state_from_status(
                    status=status,
                    filled_quantity=persisted_filled,
                    requested_quantity=int(
                        intent["quantity"]
                    ),
                )
            )

            conn.execute(
                """
                INSERT INTO broker_orders (
                    order_id,
                    client_order_id,
                    symbol,
                    side,
                    quantity,
                    filled_quantity,
                    status,
                    requested_price,
                    filled_price,
                    submitted_at,
                    filled_at,
                    metadata_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )

                ON CONFLICT(order_id)
                DO UPDATE SET
                    client_order_id = excluded.client_order_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    quantity = excluded.quantity,
                    filled_quantity =
                        CASE
                            WHEN excluded.filled_quantity
                                 > broker_orders.filled_quantity
                            THEN excluded.filled_quantity
                            ELSE broker_orders.filled_quantity
                        END,
                    status = excluded.status,
                    requested_price = excluded.requested_price,
                    filled_price = COALESCE(
                        excluded.filled_price,
                        broker_orders.filled_price
                    ),
                    submitted_at = COALESCE(
                        broker_orders.submitted_at,
                        excluded.submitted_at
                    ),
                    filled_at = COALESCE(
                        excluded.filled_at,
                        broker_orders.filled_at
                    ),
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    order_id,
                    (
                        order_client_id
                        or expected_client_id
                    ),
                    symbol,
                    side,
                    int(order.quantity),
                    persisted_filled,
                    status,
                    order.requested_price,
                    order.filled_price,
                    (
                        submitted_at.isoformat()
                        if submitted_at
                        else None
                    ),
                    (
                        filled_at.isoformat()
                        if filled_at
                        else None
                    ),
                    order_metadata_json,
                ),
            )

            completed_at_sql = (
                "CURRENT_TIMESTAMP"
                if new_state in {
                    "CANCELED",
                    "REJECTED",
                }
                else "NULL"
            )

            conn.execute(
                f"""
                UPDATE entry_intents
                SET broker_order_id = ?,
                    broker_status = ?,
                    filled_quantity = ?,
                    filled_price = COALESCE(
                        ?,
                        filled_price
                    ),
                    state = ?,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP,
                    completed_at = {completed_at_sql}
                WHERE intent_id = ?
                """,
                (
                    order_id,
                    status,
                    persisted_filled,
                    order.filled_price,
                    new_state,
                    intent_id,
                ),
            )

            row = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE intent_id = ?
                """,
                (
                    intent_id,
                ),
            ).fetchone()

        return dict(
            row
        )

    def register_entry_submission(
        self,
        intent_id: str,
        order: BrokerOrder,
    ) -> dict[str, Any]:
        """Compatibility name for first broker snapshot."""

        return self.save_entry_order_snapshot(
            intent_id,
            order,
        )

    def update_entry_intent_from_broker_order(
        self,
        intent_id: str,
        order: BrokerOrder,
    ) -> dict[str, Any]:
        """Persist a later reconciliation snapshot."""

        return self.save_entry_order_snapshot(
            intent_id,
            order,
        )

    def mark_entry_intent_managed(
        self,
        intent_id: str,
        managed_trade_id: str,
    ) -> None:
        """
        Mark a fully-filled or terminal-partially-filled entry
        intent as safely converted into a persisted ManagedTrade.
        """

        intent_id = str(
            intent_id or ""
        ).strip()

        managed_trade_id = str(
            managed_trade_id or ""
        ).strip()

        if not intent_id:
            raise ValueError(
                "intent_id cannot be empty."
            )

        if not managed_trade_id:
            raise ValueError(
                "managed_trade_id cannot be empty."
            )

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT state,
                       broker_order_id,
                       filled_quantity,
                       quantity
                FROM entry_intents
                WHERE intent_id = ?
                """,
                (
                    intent_id,
                ),
            ).fetchone()

            if row is None:
                raise KeyError(
                    f"Entry intent not found: {intent_id}"
                )

            state = str(
                row["state"] or ""
            ).upper()

            manageable_states = {
                "FILLED",
                "PARTIALLY_CANCELED",
                "PARTIALLY_REJECTED",
            }

            if state not in manageable_states:
                raise RuntimeError(
                    "Entry intent cannot be marked MANAGED "
                    f"from state {state}."
                )

            if not row["broker_order_id"]:
                raise RuntimeError(
                    "Entry intent has no broker order ID."
                )

            filled_quantity = int(
                row["filled_quantity"] or 0
            )

            requested_quantity = int(
                row["quantity"] or 0
            )

            if filled_quantity <= 0:
                raise RuntimeError(
                    "Entry intent has no confirmed fill to manage."
                )

            if (
                state == "FILLED"
                and filled_quantity < requested_quantity
            ):
                raise RuntimeError(
                    "FILLED entry intent is not fully filled."
                )

            managed_row = conn.execute(
                """
                SELECT trade_id
                FROM managed_trades
                WHERE trade_id = ?
                """,
                (
                    managed_trade_id,
                ),
            ).fetchone()

            if managed_row is None:
                raise RuntimeError(
                    "ManagedTrade must be persisted before "
                    "completing the entry intent."
                )

            conn.execute(
                """
                UPDATE entry_intents
                SET state = 'MANAGED',
                    managed_trade_id = ?,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP,
                    completed_at = CURRENT_TIMESTAMP
                WHERE intent_id = ?
                """,
                (
                    managed_trade_id,
                    intent_id,
                ),
            )

    def mark_entry_intent_error(
        self,
        intent_id: str,
        error_message: str,
        terminal: bool = False,
    ) -> None:
        """
        Record an entry-lifecycle failure.

        terminal=False keeps the intent unresolved so startup
        recovery continues to block new trading.
        """

        intent_id = str(
            intent_id or ""
        ).strip()

        if not intent_id:
            raise ValueError(
                "intent_id cannot be empty."
            )

        state = (
            "FAILED"
            if terminal
            else "ERROR"
        )

        completed_at_sql = (
            "CURRENT_TIMESTAMP"
            if terminal
            else "NULL"
        )

        with self.connection() as conn:

            cursor = conn.execute(
                f"""
                UPDATE entry_intents
                SET state = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    completed_at = {completed_at_sql}
                WHERE intent_id = ?
                """,
                (
                    state,
                    str(
                        error_message or ""
                    ),
                    intent_id,
                ),
            )

            if cursor.rowcount == 0:
                raise KeyError(
                    f"Entry intent not found: {intent_id}"
                )

    def get_entry_intent_row(
        self,
        intent_id: str,
    ) -> Optional[dict[str, Any]]:

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE intent_id = ?
                """,
                (
                    intent_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(
            row
        )

    def get_entry_intent_by_client_order_id(
        self,
        client_order_id: str,
    ) -> Optional[dict[str, Any]]:

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE client_order_id = ?
                """,
                (
                    client_order_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(
            row
        )

    def get_unresolved_entry_intent_rows(
        self,
    ) -> list[dict[str, Any]]:
        """
        Return every entry lifecycle that still requires
        recovery or management.
        """

        with self.connection() as conn:

            rows = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE state NOT IN (
                    'MANAGED',
                    'CANCELED',
                    'REJECTED',
                    'FAILED'
                )
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def get_unresolved_entry_intent_for_symbol(
        self,
        symbol: str,
    ) -> Optional[dict[str, Any]]:

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not symbol:
            return None

        with self.connection() as conn:

            row = conn.execute(
                """
                SELECT *
                FROM entry_intents
                WHERE symbol = ?
                  AND state NOT IN (
                      'MANAGED',
                      'CANCELED',
                      'REJECTED',
                      'FAILED'
                  )
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    symbol,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(
            row
        )

    # ========================================================
    # FEATURE SNAPSHOT
    # ========================================================

    def save_feature_snapshot(
        self,
        symbol: str,
        snapshot: Any,
        signal_id: Optional[str] = None,
    ) -> None:

        if hasattr(
            snapshot,
            "to_dict",
        ):
            payload = snapshot.to_dict()

        elif isinstance(
            snapshot,
            dict,
        ):
            payload = snapshot

        else:
            payload = vars(
                snapshot
            )

        snapshot_json = json.dumps(
            payload,
            default=str,
        )

        with self.connection() as conn:

            conn.execute(
                """
                INSERT INTO feature_snapshots (
                    signal_id,
                    symbol,
                    snapshot_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    signal_id,
                    symbol,
                    snapshot_json,
                ),
            )

    # ========================================================
    # STRATEGY REALIZED PNL LEDGER
    # ========================================================

    def record_strategy_pnl_event(
        self,
        *,
        event_key: str,
        trade_id: Optional[str],
        order_id: Optional[str],
        symbol: str,
        action: Optional[str],
        quantity: int,
        fill_price: Optional[float],
        entry_price: Optional[float],
        realized_pnl: float,
        event_time: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Persist one broker-confirmed realized PnL increment.

        event_key must be idempotent so retries/restarts cannot
        count the same fill twice.
        """

        event_key = str(event_key or "").strip()
        symbol = str(symbol or "").strip().upper()
        event_time = str(event_time or "").strip()

        if not event_key:
            raise ValueError("event_key cannot be empty.")

        if not symbol:
            raise ValueError("symbol cannot be empty.")

        if not event_time:
            raise ValueError("event_time cannot be empty.")

        quantity = int(quantity)

        if quantity <= 0:
            raise ValueError("quantity must be positive.")

        metadata_json = json.dumps(
            metadata or {},
            default=str,
        )

        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO strategy_pnl_ledger (
                    event_key,
                    trade_id,
                    order_id,
                    symbol,
                    action,
                    quantity,
                    fill_price,
                    entry_price,
                    realized_pnl,
                    event_time,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    trade_id,
                    order_id,
                    symbol,
                    action,
                    quantity,
                    fill_price,
                    entry_price,
                    float(realized_pnl),
                    event_time,
                    metadata_json,
                ),
            )

            return bool(
                cursor.rowcount
                and cursor.rowcount > 0
            )

    def get_strategy_realized_pnl_total(
        self,
    ) -> float:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(
                    SUM(realized_pnl),
                    0.0
                ) AS total
                FROM strategy_pnl_ledger
                """
            ).fetchone()

        return float(
            row["total"]
            if row is not None
            else 0.0
        )

    def get_strategy_realized_pnl_since(
        self,
        since_utc: str,
    ) -> float:
        since_utc = str(
            since_utc or ""
        ).strip()

        if not since_utc:
            raise ValueError(
                "since_utc cannot be empty."
            )

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(
                    SUM(realized_pnl),
                    0.0
                ) AS total
                FROM strategy_pnl_ledger
                WHERE event_time >= ?
                """,
                (
                    since_utc,
                ),
            ).fetchone()

        return float(
            row["total"]
            if row is not None
            else 0.0
        )

    # ========================================================
    # SYSTEM EVENTS
    # ========================================================

    def log_event(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        metadata: Optional[
            dict[str, Any]
        ] = None,
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


# ============================================================
# SINGLETON
# ============================================================

database = Database()