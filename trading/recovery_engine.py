from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from broker.alpaca_client import get_alpaca_client
from core.database import database
from core.models import BrokerOrder, OrderStatus, TradeSide
from trading.reconciliation_engine import get_reconciliation_engine
from trading.trade_manager import get_trade_manager


logger = logging.getLogger(__name__)


class RecoveryEngine:
    """
    JALWE AI TRADER V4 - Startup Recovery Engine V3.

    MUST run before new entries are allowed.

    Responsibilities:
    - Recover crash-safe entry intents.
    - Find broker entries by order_id OR client_order_id.
    - Preserve cumulative fill state.
    - Convert fully-filled or terminal-partially-filled entries
      into ManagedTrade state.
    - Recover pending exits.
    - Verify local managed quantity against Alpaca.
    - Detect orphan positions and unresolved JALWE orders.
    - Never silently repair broker/database mismatches.

    Important:
    - This engine never submits a new BUY or SELL order.
    - Active entry orders remain unresolved and block new entries.
    - A partially filled order that is still active remains blocked
      until it becomes terminal or is otherwise resolved.
    """

    JALWE_ENTRY_PREFIXES = (
        "JALWE-ENTRY",
        "JALWE-FINAL",
    )

    JALWE_EXIT_PREFIXES = (
        "JALWE-EXIT",
    )

    ACTIVE_ORDER_STATUSES = {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "accepted_for_bidding",
        "held",
        "pending_replace",
        "pending_cancel",
    }

    def __init__(self) -> None:
        self.broker = get_alpaca_client()
        self.reconciliation = get_reconciliation_engine()
        self.trade_manager = get_trade_manager()

    # ========================================================
    # GENERIC HELPERS
    # ========================================================

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(int(float(value or 0)), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _value(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw or "").strip()

    @staticmethod
    def _normalize_symbol(symbol: Any) -> str:
        return str(symbol or "").strip().upper()

    # ========================================================
    # ORDER STATUS MAPPING
    # ========================================================

    @staticmethod
    def _map_order_status(status: Any) -> OrderStatus:
        value = str(
            getattr(status, "value", status) or ""
        ).strip().lower()

        if value == "filled":
            return OrderStatus.FILLED

        if value == "partially_filled":
            return OrderStatus.PARTIALLY_FILLED

        if value in {
            "accepted",
            "pending_new",
            "accepted_for_bidding",
            "new",
            "held",
            "pending_replace",
            "pending_cancel",
        }:
            return OrderStatus.ACCEPTED

        if value in {
            "canceled",
            "cancelled",
            "expired",
            "done_for_day",
            "replaced",
        }:
            return OrderStatus.CANCELED

        if value in {
            "rejected",
            "suspended",
            "stopped",
        }:
            return OrderStatus.REJECTED

        return OrderStatus.SUBMITTED

    # ========================================================
    # BUILD BUY BROKER ORDER FROM RAW ALPACA RESPONSE
    # ========================================================

    def _raw_entry_to_broker_order(
        self,
        raw_order: Any,
        intent: dict[str, Any],
    ) -> BrokerOrder:
        order_id = str(
            getattr(raw_order, "id", "") or ""
        ).strip()

        if not order_id:
            raise RuntimeError(
                "Recovered broker entry has no order ID."
            )

        client_order_id = str(
            getattr(raw_order, "client_order_id", "")
            or intent.get("client_order_id")
            or ""
        ).strip()

        symbol = self._normalize_symbol(
            getattr(raw_order, "symbol", "")
            or intent.get("symbol")
        )

        raw_side = self._value(
            getattr(raw_order, "side", "BUY")
        ).upper()

        if raw_side and raw_side != "BUY":
            raise RuntimeError(
                "Recovered entry intent points to a non-BUY broker order."
            )

        quantity = self._safe_int(
            getattr(raw_order, "qty", None)
        )

        if quantity <= 0:
            quantity = self._safe_int(
                intent.get("quantity")
            )

        filled_quantity = self._safe_int(
            getattr(raw_order, "filled_qty", 0)
        )

        filled_price = self._safe_float(
            getattr(raw_order, "filled_avg_price", None)
        )

        submitted_at = getattr(
            raw_order,
            "submitted_at",
            None,
        )

        if submitted_at is None:
            submitted_at = datetime.now(timezone.utc)

        return BrokerOrder(
            symbol=symbol,
            side=TradeSide.BUY,
            quantity=quantity,
            order_id=order_id,
            client_order_id=(
                client_order_id or None
            ),
            status=self._map_order_status(
                getattr(raw_order, "status", None)
            ),
            requested_price=self._safe_float(
                intent.get("planned_entry")
            ),
            filled_price=filled_price,
            filled_quantity=filled_quantity,
            submitted_at=submitted_at,
            metadata={
                "recovery": True,
                "recovery_role": "ENTRY",
                "broker_raw_status": self._value(
                    getattr(raw_order, "status", "")
                ),
            },
        )

    # ========================================================
    # PENDING EXIT PLACEHOLDER
    # ========================================================

    @staticmethod
    def _build_pending_exit_order(
        trade: Any,
    ) -> BrokerOrder:
        order_id = str(
            trade.pending_order_id or ""
        ).strip()

        if not order_id:
            raise RuntimeError(
                "Pending trade has no broker order ID."
            )

        quantity = int(
            trade.pending_requested_quantity
        )

        if quantity <= 0:
            raise RuntimeError(
                "Pending trade has invalid requested exit quantity."
            )

        return BrokerOrder(
            symbol=trade.symbol,
            side=TradeSide.SELL,
            quantity=quantity,
            order_id=order_id,
            client_order_id=None,
            status=OrderStatus.SUBMITTED,
            requested_price=None,
            filled_price=None,
            filled_quantity=int(
                trade.pending_filled_quantity
            ),
            submitted_at=datetime.now(timezone.utc),
            metadata={
                "recovery": True,
                "recovery_role": "PENDING_EXIT",
            },
        )

    # ========================================================
    # ENTRY ORDER LOOKUP
    # ========================================================

    def _lookup_entry_order(
        self,
        intent: dict[str, Any],
    ) -> Any:
        """
        Locate an entry broker order without ever submitting
        another order.

        Preference:
        1. broker_order_id when already persisted.
        2. client_order_id for crash recovery.
        """

        broker_order_id = str(
            intent.get("broker_order_id") or ""
        ).strip()

        client_order_id = str(
            intent.get("client_order_id") or ""
        ).strip()

        id_error: Optional[Exception] = None

        if broker_order_id:
            try:
                return self.broker.get_order(
                    broker_order_id
                )
            except Exception as exc:
                id_error = exc
                logger.warning(
                    "Entry order lookup by broker ID failed | "
                    "intent_id=%s order_id=%s error=%s",
                    intent.get("intent_id"),
                    broker_order_id,
                    exc,
                )

        if client_order_id:
            try:
                return self.broker.get_order_by_client_id(
                    client_order_id
                )
            except Exception as exc:
                if id_error is not None:
                    raise RuntimeError(
                        "Entry order lookup failed by both broker "
                        "order ID and client_order_id. "
                        f"broker_id_error={id_error}; "
                        f"client_id_error={exc}"
                    ) from exc
                raise

        if id_error is not None:
            raise id_error

        raise RuntimeError(
            "Entry intent has neither broker_order_id "
            "nor client_order_id."
        )

    # ========================================================
    # TARGETS FROM ACTUAL FILL
    # ========================================================

    @staticmethod
    def _targets_from_actual_fill(
        entry_price: float,
        stop_price: float,
    ) -> tuple[float, float, float]:
        if not (
            0 < stop_price < entry_price
        ):
            raise RuntimeError(
                "Recovered fill invalidates planned stop structure."
            )

        risk_per_share = entry_price - stop_price

        return (
            round(entry_price + 2.0 * risk_per_share, 4),
            round(entry_price + 3.0 * risk_per_share, 4),
            round(entry_price + 4.0 * risk_per_share, 4),
        )

    # ========================================================
    # CONVERT CONFIRMED ENTRY TO MANAGED TRADE
    # ========================================================

    def _manage_confirmed_entry(
        self,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        intent_id = str(
            intent.get("intent_id") or ""
        ).strip()

        symbol = self._normalize_symbol(
            intent.get("symbol")
        )

        broker_order_id = str(
            intent.get("broker_order_id") or ""
        ).strip()

        filled_quantity = self._safe_int(
            intent.get("filled_quantity")
        )

        if not broker_order_id:
            raise RuntimeError(
                "Confirmed entry has no broker_order_id."
            )

        if filled_quantity <= 0:
            raise RuntimeError(
                "Confirmed entry has no filled shares to manage."
            )

        # ----------------------------------------------------
        # IDEMPOTENCY: CRASH MAY HAVE OCCURRED AFTER
        # ManagedTrade was saved but before intent was marked.
        # ----------------------------------------------------

        existing_managed = (
            database.get_managed_trade_row_by_entry_order_id(
                broker_order_id
            )
        )

        if existing_managed is not None:
            managed_trade_id = str(
                existing_managed["trade_id"]
            )

            database.mark_entry_intent_managed(
                intent_id,
                managed_trade_id,
            )

            return {
                "managed": True,
                "managed_trade_id": managed_trade_id,
                "created": False,
                "reason": "EXISTING_MANAGED_TRADE_REUSED",
            }

        # ----------------------------------------------------
        # VERIFY ACTUAL BROKER POSITION
        # ----------------------------------------------------

        position = self.reconciliation.verify_position(
            symbol
        )

        if not bool(
            position.get("exists", False)
        ):
            raise RuntimeError(
                "Entry has confirmed fills but broker position "
                "does not exist."
            )

        broker_quantity = self._safe_int(
            position.get("quantity")
        )

        if broker_quantity != filled_quantity:
            raise RuntimeError(
                "Recovered entry fill quantity does not match "
                "broker position quantity. "
                f"filled={filled_quantity} broker={broker_quantity}"
            )

        actual_entry_price = self._safe_float(
            position.get("average_entry_price")
        )

        if actual_entry_price is None:
            actual_entry_price = self._safe_float(
                intent.get("filled_price")
            )

        if (
            actual_entry_price is None
            or actual_entry_price <= 0
        ):
            raise RuntimeError(
                "Recovered entry has no valid actual fill price."
            )

        stop_price = self._safe_float(
            intent.get("stop_price")
        )

        if stop_price is None:
            raise RuntimeError(
                "Recovered entry intent has no stop price."
            )

        target_1, target_2, target_3 = (
            self._targets_from_actual_fill(
                actual_entry_price,
                stop_price,
            )
        )

        managed_trade = self.trade_manager.create_trade(
            symbol=symbol,
            entry_price=actual_entry_price,
            quantity=filled_quantity,
            stop_price=stop_price,
            target_1=target_1,
            target_2=target_2,
            target_3=target_3,
        )

        managed_trade.metadata.update(
            {
                "recovered_from_entry_intent": True,
                "entry_intent_id": intent_id,
                "entry_order_id": broker_order_id,
                "client_order_id": intent.get(
                    "client_order_id"
                ),
                "strategy": intent.get("strategy"),
                "setup_grade": intent.get("setup_grade"),
                "risk_pct": intent.get("risk_pct"),
                "requested_entry_price": intent.get(
                    "planned_entry"
                ),
                "actual_entry_price": actual_entry_price,
                "entry_intent_terminal_state": intent.get(
                    "state"
                ),
                "entry_requested_quantity": intent.get(
                    "quantity"
                ),
                "entry_filled_quantity": filled_quantity,
            }
        )

        self.trade_manager._sync_metadata(
            managed_trade
        )

        managed_trade_id = (
            "TRADE-" + intent_id
        )

        # Save trade first. If a crash happens immediately
        # afterwards, next recovery reuses this row by entry_order_id.
        database.save_managed_trade(
            managed_trade_id,
            managed_trade,
            entry_order_id=broker_order_id,
            entry_fill_price=actual_entry_price,
        )

        database.mark_entry_intent_managed(
            intent_id,
            managed_trade_id,
        )

        database.log_event(
            event_type="ENTRY_RECOVERY_MANAGED",
            severity="INFO",
            message=(
                "Recovered entry was converted into a ManagedTrade."
            ),
            metadata={
                "intent_id": intent_id,
                "trade_id": managed_trade_id,
                "symbol": symbol,
                "filled_quantity": filled_quantity,
                "entry_price": actual_entry_price,
                "stop_price": stop_price,
                "target_1": target_1,
                "target_2": target_2,
                "target_3": target_3,
            },
        )

        return {
            "managed": True,
            "managed_trade_id": managed_trade_id,
            "created": True,
            "reason": "MANAGED_TRADE_CREATED",
        }

    # ========================================================
    # RECOVER ONE ENTRY INTENT
    # ========================================================

    def recover_entry_intent(
        self,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        intent_id = str(
            intent.get("intent_id") or ""
        ).strip()

        symbol = self._normalize_symbol(
            intent.get("symbol")
        )

        result: dict[str, Any] = {
            "intent_id": intent_id,
            "symbol": symbol,
            "resolved": False,
            "managed": False,
            "state_before": intent.get("state"),
            "state_after": intent.get("state"),
            "broker_order_id": intent.get("broker_order_id"),
            "filled_quantity": self._safe_int(
                intent.get("filled_quantity")
            ),
            "warning": None,
        }

        try:
            raw_order = self._lookup_entry_order(
                intent
            )

            broker_order = self._raw_entry_to_broker_order(
                raw_order,
                intent,
            )

            updated_intent = (
                database.update_entry_intent_from_broker_order(
                    intent_id,
                    broker_order,
                )
            )

            result["state_after"] = updated_intent[
                "state"
            ]
            result["broker_order_id"] = updated_intent[
                "broker_order_id"
            ]
            result["filled_quantity"] = self._safe_int(
                updated_intent.get("filled_quantity")
            )

        except Exception as exc:
            logger.exception(
                "Pending entry recovery lookup failed | "
                "intent_id=%s symbol=%s",
                intent_id,
                symbol,
            )

            database.mark_entry_intent_error(
                intent_id,
                (
                    "Entry broker lookup/reconciliation failed: "
                    + str(exc)
                ),
                terminal=False,
            )

            database.log_event(
                event_type="RECOVERY_ENTRY_LOOKUP_ERROR",
                severity="CRITICAL",
                message=(
                    "Unable to determine broker state for a "
                    "persisted entry intent."
                ),
                metadata={
                    "intent_id": intent_id,
                    "symbol": symbol,
                    "client_order_id": intent.get(
                        "client_order_id"
                    ),
                    "broker_order_id": intent.get(
                        "broker_order_id"
                    ),
                    "error": str(exc),
                },
            )

            result["warning"] = (
                "ENTRY_ORDER_LOOKUP_FAILED"
            )
            result["error"] = str(exc)
            result["state_after"] = "ERROR"
            return result

        state = str(
            updated_intent.get("state") or ""
        ).upper()

        filled_quantity = self._safe_int(
            updated_intent.get("filled_quantity")
        )

        # ----------------------------------------------------
        # ZERO-FILL TERMINAL ENTRY: SAFE RESOLUTION
        # ----------------------------------------------------

        if state in {
            "CANCELED",
            "REJECTED",
            "FAILED",
        }:
            if filled_quantity > 0:
                result["warning"] = (
                    "TERMINAL_ENTRY_STATE_WITH_UNEXPECTED_FILL"
                )
                return result

            result["resolved"] = True
            result["managed"] = False

            database.log_event(
                event_type="ENTRY_RECOVERY_NO_FILL_TERMINAL",
                severity="INFO",
                message=(
                    "Entry intent terminated without any broker fill."
                ),
                metadata={
                    "intent_id": intent_id,
                    "symbol": symbol,
                    "state": state,
                },
            )

            return result

        # ----------------------------------------------------
        # FULL FILL OR TERMINAL PARTIAL FILL -> MANAGE SHARES
        # ----------------------------------------------------

        if state in {
            "FILLED",
            "PARTIALLY_CANCELED",
            "PARTIALLY_REJECTED",
        }:
            try:
                management = self._manage_confirmed_entry(
                    updated_intent
                )
            except Exception as exc:
                logger.exception(
                    "Recovered entry management failed | "
                    "intent_id=%s symbol=%s",
                    intent_id,
                    symbol,
                )

                database.mark_entry_intent_error(
                    intent_id,
                    (
                        "Entry fill exists but ManagedTrade "
                        "recovery failed: "
                        + str(exc)
                    ),
                    terminal=False,
                )

                database.log_event(
                    event_type="RECOVERY_ENTRY_MANAGEMENT_ERROR",
                    severity="CRITICAL",
                    message=(
                        "Confirmed entry shares could not be safely "
                        "converted into ManagedTrade state."
                    ),
                    metadata={
                        "intent_id": intent_id,
                        "symbol": symbol,
                        "state": state,
                        "filled_quantity": filled_quantity,
                        "error": str(exc),
                    },
                )

                result["warning"] = (
                    "ENTRY_MANAGEMENT_FAILED"
                )
                result["error"] = str(exc)
                result["state_after"] = "ERROR"
                return result

            result["resolved"] = True
            result["managed"] = True
            result["managed_trade_id"] = management[
                "managed_trade_id"
            ]
            result["management_created"] = management[
                "created"
            ]
            result["state_after"] = "MANAGED"
            return result

        # ----------------------------------------------------
        # ACTIVE ENTRY ORDER - KEEP BLOCKED
        # ----------------------------------------------------

        if state in {
            "PREPARED",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "ERROR",
        }:
            result["warning"] = (
                "ENTRY_STILL_UNRESOLVED"
            )
            return result

        result["warning"] = (
            "UNKNOWN_ENTRY_INTENT_STATE"
        )
        return result

    # ========================================================
    # RECOVER ONE MANAGED TRADE / PENDING EXIT
    # ========================================================

    def recover_trade(
        self,
        trade_id: str,
        trade: Any,
    ) -> dict[str, Any]:
        symbol = self._normalize_symbol(
            trade.symbol
        )

        result: dict[str, Any] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "recovered": False,
            "pending_reconciled": False,
            "broker_match": False,
            "local_quantity": int(
                trade.remaining_quantity
            ),
            "broker_quantity": None,
            "warning": None,
        }

        # ----------------------------------------------------
        # PENDING EXIT ORDER
        # ----------------------------------------------------

        if trade.has_pending_exit:
            try:
                local_order = self._build_pending_exit_order(
                    trade
                )

                broker_order = self.reconciliation.reconcile_order(
                    local_order
                )

                reconciliation_result = (
                    self.trade_manager.apply_exit_reconciliation(
                        trade,
                        broker_order,
                    )
                )

                database.save_broker_order(
                    broker_order
                )

                database.save_managed_trade(
                    trade_id,
                    trade,
                )

                result["pending_reconciled"] = True
                result["pending_reconciliation"] = (
                    reconciliation_result
                )

            except Exception as exc:
                logger.exception(
                    "Pending exit recovery failed | "
                    "trade_id=%s symbol=%s",
                    trade_id,
                    symbol,
                )

                result["warning"] = (
                    "PENDING_EXIT_RECONCILIATION_FAILED"
                )
                result["error"] = str(exc)

                database.log_event(
                    event_type="RECOVERY_PENDING_ERROR",
                    severity="ERROR",
                    message=(
                        "Unable to reconcile pending broker exit "
                        "during startup."
                    ),
                    metadata={
                        "trade_id": trade_id,
                        "symbol": symbol,
                        "pending_order_id": (
                            trade.pending_order_id
                        ),
                        "pending_filled_quantity": (
                            trade.pending_filled_quantity
                        ),
                        "error": str(exc),
                    },
                )

                return result

        # ----------------------------------------------------
        # VERIFY BROKER POSITION
        # ----------------------------------------------------

        try:
            position = self.reconciliation.verify_position(
                symbol
            )
        except Exception as exc:
            logger.exception(
                "Position recovery failed | "
                "trade_id=%s symbol=%s",
                trade_id,
                symbol,
            )

            result["warning"] = (
                "BROKER_POSITION_UNAVAILABLE"
            )
            result["error"] = str(exc)
            return result

        broker_exists = bool(
            position.get("exists", False)
        )
        broker_quantity = self._safe_int(
            position.get("quantity")
        )
        local_quantity = int(
            trade.remaining_quantity
        )

        result["broker_quantity"] = broker_quantity

        if (
            broker_exists
            and broker_quantity == local_quantity
            and local_quantity > 0
        ):
            result["broker_match"] = True
            result["recovered"] = True

            database.log_event(
                event_type="RECOVERY_OK",
                severity="INFO",
                message=(
                    "Managed trade successfully matched with "
                    "broker position."
                ),
                metadata={
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "quantity": local_quantity,
                },
            )
            return result

        if (
            not broker_exists
            and local_quantity == 0
        ):
            result["broker_match"] = True
            result["recovered"] = True
            return result

        result["warning"] = (
            "BROKER_DATABASE_QUANTITY_MISMATCH"
        )

        database.log_event(
            event_type="RECOVERY_POSITION_MISMATCH",
            severity="CRITICAL",
            message=(
                "Local managed trade quantity does not match "
                "Alpaca broker position."
            ),
            metadata={
                "trade_id": trade_id,
                "symbol": symbol,
                "local_quantity": local_quantity,
                "broker_exists": broker_exists,
                "broker_quantity": broker_quantity,
            },
        )

        return result

    # ========================================================
    # BROKER POSITIONS
    # ========================================================

    def _broker_positions(
        self,
    ) -> dict[str, dict[str, Any]]:
        raw_positions = self.broker.get_all_positions()

        positions: dict[str, dict[str, Any]] = {}

        for position in raw_positions:
            symbol = self._normalize_symbol(
                getattr(position, "symbol", "")
            )

            if not symbol:
                continue

            positions[symbol] = {
                "symbol": symbol,
                "quantity": self._safe_int(
                    getattr(position, "qty", 0)
                ),
                "average_entry_price": self._safe_float(
                    getattr(position, "avg_entry_price", None)
                ),
                "market_value": self._safe_float(
                    getattr(position, "market_value", None)
                ),
                "unrealized_pl": self._safe_float(
                    getattr(position, "unrealized_pl", None)
                ),
            }

        return positions

    # ========================================================
    # ORPHAN POSITIONS
    # ========================================================

    def _check_orphan_positions(
        self,
        local_trades: dict[str, Any],
        broker_positions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        managed_symbols = {
            self._normalize_symbol(trade.symbol)
            for trade in local_trades.values()
            if int(trade.remaining_quantity) > 0
        }

        orphan_positions: list[dict[str, Any]] = []

        for symbol, position in broker_positions.items():
            if symbol in managed_symbols:
                continue

            orphan = {
                "symbol": symbol,
                "quantity": position["quantity"],
                "average_entry_price": position[
                    "average_entry_price"
                ],
            }

            orphan_positions.append(orphan)

            database.log_event(
                event_type="RECOVERY_ORPHAN_POSITION",
                severity="CRITICAL",
                message=(
                    "Broker position exists without an active "
                    "local ManagedTrade."
                ),
                metadata=orphan,
            )

        return orphan_positions

    # ========================================================
    # BROKER ORDERS
    # ========================================================

    def _get_broker_orders(self) -> list[Any]:
        orders = self.broker.get_orders()
        if orders is None:
            return []
        return list(orders)

    # ========================================================
    # UNRESOLVED JALWE ORDERS
    # ========================================================

    def _check_unresolved_jalwe_orders(
        self,
        local_trades: dict[str, Any],
        entry_intents: list[dict[str, Any]],
        broker_orders: list[Any],
    ) -> list[dict[str, Any]]:
        known_pending_exit_ids = {
            str(trade.pending_order_id).strip()
            for trade in local_trades.values()
            if getattr(trade, "pending_order_id", None)
        }

        known_entry_order_ids = {
            str(row.get("broker_order_id") or "").strip()
            for row in entry_intents
            if row.get("broker_order_id")
        }

        known_entry_client_ids = {
            str(row.get("client_order_id") or "").strip()
            for row in entry_intents
            if row.get("client_order_id")
        }

        unresolved: list[dict[str, Any]] = []

        for order in broker_orders:
            order_id = str(
                getattr(order, "id", "") or ""
            ).strip()
            client_order_id = str(
                getattr(order, "client_order_id", "") or ""
            ).strip()

            if not client_order_id:
                continue

            is_jalwe_entry = any(
                client_order_id.startswith(prefix)
                for prefix in self.JALWE_ENTRY_PREFIXES
            )
            is_jalwe_exit = any(
                client_order_id.startswith(prefix)
                for prefix in self.JALWE_EXIT_PREFIXES
            )

            if not (is_jalwe_entry or is_jalwe_exit):
                continue

            status = self._value(
                getattr(order, "status", "")
            ).lower()

            if status not in self.ACTIVE_ORDER_STATUSES:
                continue

            if (
                is_jalwe_exit
                and order_id in known_pending_exit_ids
            ):
                continue

            if is_jalwe_entry and (
                order_id in known_entry_order_ids
                or client_order_id in known_entry_client_ids
            ):
                continue

            record = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "symbol": self._normalize_symbol(
                    getattr(order, "symbol", "")
                ),
                "side": self._value(
                    getattr(order, "side", "")
                ),
                "status": status,
                "quantity": self._safe_int(
                    getattr(order, "qty", 0)
                ),
                "filled_quantity": self._safe_int(
                    getattr(order, "filled_qty", 0)
                ),
                "role": (
                    "ENTRY" if is_jalwe_entry else "EXIT"
                ),
            }

            unresolved.append(record)

            database.log_event(
                event_type="RECOVERY_UNRESOLVED_ORDER",
                severity="CRITICAL",
                message=(
                    "Unresolved JALWE broker order detected "
                    "during startup."
                ),
                metadata=record,
            )

        return unresolved

    # ========================================================
    # DUPLICATE LOCAL SYMBOLS
    # ========================================================

    def _check_duplicate_local_symbols(
        self,
        trades: dict[str, Any],
    ) -> list[str]:
        counts: dict[str, int] = {}

        for trade in trades.values():
            if int(trade.remaining_quantity) <= 0:
                continue

            symbol = self._normalize_symbol(
                trade.symbol
            )
            counts[symbol] = counts.get(symbol, 0) + 1

        duplicates = [
            symbol
            for symbol, count in counts.items()
            if count > 1
        ]

        for symbol in duplicates:
            database.log_event(
                event_type="RECOVERY_DUPLICATE_LOCAL_TRADE",
                severity="CRITICAL",
                message=(
                    "Multiple active ManagedTrade records exist "
                    "for the same symbol."
                ),
                metadata={
                    "symbol": symbol,
                    "count": counts[symbol],
                },
            )

        return duplicates

    # ========================================================
    # STARTUP RECOVERY
    # ========================================================

    def recover_all(self) -> dict[str, Any]:
        entry_results: list[dict[str, Any]] = []
        trade_results: list[dict[str, Any]] = []
        errors: list[str] = []
        safe_to_trade = True

        # ====================================================
        # 1. RECOVER ENTRY INTENTS FIRST
        # ====================================================

        try:
            entry_intents_before = (
                database.get_unresolved_entry_intent_rows()
            )
        except Exception as exc:
            return {
                "safe_to_trade": False,
                "active_trade_count": 0,
                "broker_position_count": None,
                "entry_intent_count": None,
                "unresolved_entry_intent_count": None,
                "entry_results": [],
                "results": [],
                "orphan_positions": [],
                "unresolved_orders": [],
                "duplicate_local_symbols": [],
                "errors": [str(exc)],
                "reason": "ENTRY_INTENT_DATABASE_UNAVAILABLE",
                "startup_reconciliation_ready": True,
                "pending_exit_recovery_ready": True,
                "pending_entry_recovery_ready": True,
            }

        for intent in entry_intents_before:
            result = self.recover_entry_intent(
                intent
            )
            entry_results.append(result)

            if not result.get("resolved", False):
                safe_to_trade = False

        # Re-read after recovery because some may now be MANAGED.
        try:
            unresolved_entry_intents = (
                database.get_unresolved_entry_intent_rows()
            )
        except Exception as exc:
            unresolved_entry_intents = []
            safe_to_trade = False
            errors.append(
                "ENTRY_INTENT_RELOAD_FAILED: " + str(exc)
            )

        if unresolved_entry_intents:
            safe_to_trade = False

        # ====================================================
        # 2. LOAD MANAGED TRADES (INCLUDING NEWLY RECOVERED)
        # ====================================================

        try:
            trades = database.load_active_managed_trades()
        except Exception as exc:
            return {
                "safe_to_trade": False,
                "active_trade_count": 0,
                "broker_position_count": None,
                "entry_intent_count": len(entry_intents_before),
                "unresolved_entry_intent_count": len(
                    unresolved_entry_intents
                ),
                "entry_results": entry_results,
                "results": [],
                "orphan_positions": [],
                "unresolved_orders": [],
                "duplicate_local_symbols": [],
                "errors": [str(exc)],
                "reason": "LOCAL_DATABASE_UNAVAILABLE",
                "startup_reconciliation_ready": True,
                "pending_exit_recovery_ready": True,
                "pending_entry_recovery_ready": True,
            }

        duplicate_local_symbols = (
            self._check_duplicate_local_symbols(trades)
        )

        if duplicate_local_symbols:
            safe_to_trade = False

        # ====================================================
        # 3. RECOVER MANAGED TRADES / PENDING EXITS
        # ====================================================

        for trade_id, trade in trades.items():
            try:
                result = self.recover_trade(
                    trade_id,
                    trade,
                )
            except Exception as exc:
                logger.exception(
                    "Unexpected managed trade recovery failure | "
                    "trade_id=%s",
                    trade_id,
                )
                result = {
                    "trade_id": trade_id,
                    "symbol": self._normalize_symbol(
                        getattr(trade, "symbol", "")
                    ),
                    "recovered": False,
                    "pending_reconciled": False,
                    "broker_match": False,
                    "warning": "UNEXPECTED_RECOVERY_ERROR",
                    "error": str(exc),
                }

            trade_results.append(result)

            if not result.get("recovered", False):
                safe_to_trade = False

        # Reload after pending exit reconciliation may have closed trades.
        try:
            trades = database.load_active_managed_trades()
        except Exception as exc:
            safe_to_trade = False
            errors.append(
                "MANAGED_TRADE_RELOAD_FAILED: " + str(exc)
            )

        # ====================================================
        # 4. BROKER POSITIONS
        # ====================================================

        try:
            broker_positions = self._broker_positions()
        except Exception as exc:
            logger.exception(
                "Unable to load Alpaca positions during recovery."
            )
            broker_positions = {}
            safe_to_trade = False
            errors.append(
                "BROKER_POSITIONS_UNAVAILABLE: " + str(exc)
            )

        orphan_positions = self._check_orphan_positions(
            trades,
            broker_positions,
        )

        if orphan_positions:
            safe_to_trade = False

        # ====================================================
        # 5. BROKER ORDERS
        # ====================================================

        try:
            broker_orders = self._get_broker_orders()
        except Exception as exc:
            logger.exception(
                "Unable to load broker orders during startup recovery."
            )
            broker_orders = []
            safe_to_trade = False
            errors.append(
                "BROKER_ORDERS_UNAVAILABLE: " + str(exc)
            )

        # Include currently unresolved entry intents in the known set.
        unresolved_orders = self._check_unresolved_jalwe_orders(
            trades,
            unresolved_entry_intents,
            broker_orders,
        )

        if unresolved_orders:
            safe_to_trade = False

        # ====================================================
        # 6. GLOBAL EVENT
        # ====================================================

        database.log_event(
            event_type="STARTUP_RECOVERY_COMPLETE",
            severity=(
                "INFO" if safe_to_trade else "CRITICAL"
            ),
            message=(
                "Startup recovery completed successfully."
                if safe_to_trade
                else "Startup recovery found unresolved safety issues."
            ),
            metadata={
                "safe_to_trade": safe_to_trade,
                "entry_intent_count": len(
                    entry_intents_before
                ),
                "unresolved_entry_intent_count": len(
                    unresolved_entry_intents
                ),
                "active_trade_count": len(trades),
                "broker_position_count": len(
                    broker_positions
                ),
                "orphan_position_count": len(
                    orphan_positions
                ),
                "unresolved_order_count": len(
                    unresolved_orders
                ),
                "duplicate_local_symbols": (
                    duplicate_local_symbols
                ),
                "errors": errors,
            },
        )

        return {
            "safe_to_trade": safe_to_trade,
            "entry_intent_count": len(
                entry_intents_before
            ),
            "unresolved_entry_intent_count": len(
                unresolved_entry_intents
            ),
            "active_trade_count": len(trades),
            "broker_position_count": len(
                broker_positions
            ),
            "entry_results": entry_results,
            "results": trade_results,
            "orphan_positions": orphan_positions,
            "unresolved_orders": unresolved_orders,
            "duplicate_local_symbols": (
                duplicate_local_symbols
            ),
            "errors": errors,
            "startup_reconciliation_ready": True,
            "pending_exit_recovery_ready": True,
            "pending_entry_recovery_ready": True,
        }


# ============================================================
# LAZY SINGLETON
# ============================================================

recovery_engine: Optional[RecoveryEngine] = None


def get_recovery_engine() -> RecoveryEngine:
    global recovery_engine

    if recovery_engine is None:
        recovery_engine = RecoveryEngine()

    return recovery_engine
