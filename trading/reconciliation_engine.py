from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from broker.alpaca_client import get_alpaca_client
from core.models import (
    BrokerOrder,
    OrderStatus,
)


logger = logging.getLogger(__name__)


class ReconciliationError(RuntimeError):
    pass


class ReconciliationEngine:
    """
    JALWE AI TRADER V4 - Broker Reconciliation Engine.

    PAPER TRADING ONLY.

    Responsibilities:
    - Read the real order state from Alpaca.
    - Confirm partial/full fills.
    - Read actual fill price.
    - Read actual filled quantity.
    - Detect rejected/cancelled orders.
    - Never assume SUBMITTED means FILLED.

    This engine does NOT:
    - generate signals
    - calculate risk
    - submit new entry orders
    """

    TERMINAL_STATUSES = {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    }

    def __init__(self) -> None:
        self.broker = get_alpaca_client()

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:

        try:
            if value is None:
                return None

            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int:

        try:
            return max(
                int(float(value or 0)),
                0,
            )

        except (
            TypeError,
            ValueError,
        ):
            return 0

    @staticmethod
    def _safe_datetime(
        value: Any,
    ) -> Optional[datetime]:

        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value

        try:
            return datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00",
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

    # ========================================================
    # STATUS MAPPING
    # ========================================================

    @staticmethod
    def _map_status(
        broker_status: Any,
    ) -> OrderStatus:

        raw = getattr(
            broker_status,
            "value",
            broker_status,
        )

        value = str(
            raw or ""
        ).strip().lower()

        if value == "filled":
            return OrderStatus.FILLED

        if value == "partially_filled":
            return (
                OrderStatus.PARTIALLY_FILLED
            )

        if value in {
            "accepted",
            "pending_new",
            "accepted_for_bidding",
        }:
            return OrderStatus.ACCEPTED

        if value in {
            "new",
            "pending_replace",
            "pending_cancel",
        }:
            return OrderStatus.SUBMITTED

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
    # ORDER RECONCILIATION
    # ========================================================

    def reconcile_order(
        self,
        order: BrokerOrder,
    ) -> BrokerOrder:
        """
        Refresh a local BrokerOrder using Alpaca's
        current broker-side order state.
        """

        if not order.order_id:
            raise ReconciliationError(
                "BrokerOrder has no Alpaca order ID."
            )

        try:
            broker_order = (
                self.broker.get_order(
                    order.order_id
                )
            )

        except Exception as exc:

            logger.exception(
                "Unable to retrieve broker order | order_id=%s",
                order.order_id,
            )

            raise ReconciliationError(
                "Unable to retrieve broker order."
            ) from exc

        raw_status = getattr(
            broker_order,
            "status",
            None,
        )

        order.status = self._map_status(
            raw_status
        )

        filled_quantity = self._safe_int(
            getattr(
                broker_order,
                "filled_qty",
                0,
            )
        )

        filled_price = self._safe_float(
            getattr(
                broker_order,
                "filled_avg_price",
                None,
            )
        )

        filled_at = self._safe_datetime(
            getattr(
                broker_order,
                "filled_at",
                None,
            )
        )

        order.filled_quantity = (
            filled_quantity
        )

        order.filled_price = (
            filled_price
        )

        if filled_at is not None:
            order.filled_at = filled_at

        raw_value = getattr(
            raw_status,
            "value",
            raw_status,
        )

        order.metadata[
            "broker_raw_status"
        ] = str(
            raw_value or ""
        )

        order.metadata[
            "reconciled"
        ] = True

        order.metadata[
            "broker_filled_qty"
        ] = filled_quantity

        order.metadata[
            "broker_filled_avg_price"
        ] = filled_price

        logger.info(
            "Order reconciled | "
            "order_id=%s symbol=%s status=%s "
            "filled_qty=%s fill_price=%s",
            order.order_id,
            order.symbol,
            order.status.value,
            order.filled_quantity,
            order.filled_price,
        )

        return order

    # ========================================================
    # WAIT FOR TERMINAL STATE
    # ========================================================

    def wait_for_terminal_state(
        self,
        order: BrokerOrder,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> BrokerOrder:
        """
        Poll Alpaca until the order reaches a terminal
        state or timeout is reached.

        Terminal:
        - FILLED
        - CANCELED
        - REJECTED

        This method does not submit or cancel orders.
        """

        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be positive."
            )

        if poll_interval_seconds <= 0:
            raise ValueError(
                "poll_interval_seconds must be positive."
            )

        deadline = (
            time.monotonic()
            + timeout_seconds
        )

        latest = order

        while True:

            latest = self.reconcile_order(
                latest
            )

            if (
                latest.status
                in self.TERMINAL_STATUSES
            ):
                return latest

            if (
                time.monotonic()
                >= deadline
            ):
                latest.metadata[
                    "reconciliation_timeout"
                ] = True

                return latest

            time.sleep(
                poll_interval_seconds
            )

    # ========================================================
    # POSITION VERIFICATION
    # ========================================================

    def verify_position(
        self,
        symbol: str,
    ) -> dict[str, Any]:
        """
        Check whether Alpaca currently has
        a real broker-side position for symbol.
        """

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not symbol:
            raise ValueError(
                "Symbol cannot be empty."
            )

        position = (
            self.broker.get_position(
                symbol
            )
        )

        if position is None:

            return {
                "exists": False,
                "symbol": symbol,
                "quantity": 0.0,
                "average_entry_price": None,
                "market_value": None,
                "unrealized_pl": None,
            }

        return {
            "exists": True,

            "symbol": symbol,

            "quantity": self._safe_float(
                getattr(
                    position,
                    "qty",
                    None,
                )
            ),

            "average_entry_price": (
                self._safe_float(
                    getattr(
                        position,
                        "avg_entry_price",
                        None,
                    )
                )
            ),

            "market_value": (
                self._safe_float(
                    getattr(
                        position,
                        "market_value",
                        None,
                    )
                )
            ),

            "unrealized_pl": (
                self._safe_float(
                    getattr(
                        position,
                        "unrealized_pl",
                        None,
                    )
                )
            ),
        }


# ============================================================
# LAZY SINGLETON
# ============================================================

reconciliation_engine: Optional[
    ReconciliationEngine
] = None


def get_reconciliation_engine(
) -> ReconciliationEngine:

    global reconciliation_engine

    if reconciliation_engine is None:

        reconciliation_engine = (
            ReconciliationEngine()
        )

    return reconciliation_engine