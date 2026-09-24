from __future__ import annotations

import logging

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from broker.alpaca_client import (
    get_alpaca_client,
)

from core.models import (
    BrokerOrder,
    FeatureSnapshot,
    OrderStatus,
    RiskDecision,
    SignalAction,
    TradeSide,
)

from intelligence.ai_engine import (
    AIAnalysis,
)


logger = logging.getLogger(__name__)


class ExecutionEngine:
    """
    JALWE AI TRADER V4 - Execution Engine V3.1

    PAPER TRADING ONLY.

    Responsibilities:
    - Submit PAPER entry orders.
    - Submit PAPER exit orders.
    - Accept Central DecisionEngine decisions.
    - Support crash-safe client_order_id.
    - Re-check critical safety gates immediately
      before broker submission.
    - Never assume submitted == filled.
    - Return BrokerOrder objects.
    - Leave fill confirmation to ReconciliationEngine.

    Does NOT:
    - generate signals
    - calculate risk
    - manage trade stages
    - assume execution price
    - mark a trade as filled
    """

    def __init__(
        self,
    ) -> None:

        self.broker = (
            get_alpaca_client()
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        normalized = str(
            symbol or ""
        ).strip().upper()

        if not normalized:

            raise ValueError(
                "Symbol cannot be empty."
            )

        return normalized

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:

        try:

            if value is None:
                return None

            return float(
                value
            )

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
                int(
                    float(
                        value or 0
                    )
                ),
                0,
            )

        except (
            TypeError,
            ValueError,
        ):

            return 0

    @staticmethod
    def _enum_value(
        value: Any,
    ) -> str:

        raw = getattr(
            value,
            "value",
            value,
        )

        return str(
            raw or ""
        ).strip()

    # ========================================================
    # BROKER STATUS MAPPING
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

            return (
                OrderStatus.FILLED
            )

        if value == "partially_filled":

            return (
                OrderStatus.PARTIALLY_FILLED
            )

        if value in {
            "accepted",
            "pending_new",
            "accepted_for_bidding",
            "new",
            "held",
        }:

            return (
                OrderStatus.ACCEPTED
            )

        if value in {
            "canceled",
            "cancelled",
            "expired",
            "done_for_day",
            "replaced",
        }:

            return (
                OrderStatus.CANCELED
            )

        if value in {
            "rejected",
            "suspended",
            "stopped",
        }:

            return (
                OrderStatus.REJECTED
            )

        return (
            OrderStatus.SUBMITTED
        )

    # ========================================================
    # CLIENT ORDER ID
    # ========================================================

    @staticmethod
    def _new_client_order_id(
        prefix: str,
    ) -> str:

        return (
            prefix
            + "-"
            + uuid4().hex[
                :20
            ].upper()
        )

    @staticmethod
    def _validate_client_order_id(
        client_order_id: str,
        required_prefix: Optional[str] = None,
    ) -> str:

        value = str(
            client_order_id or ""
        ).strip()

        if not value:

            raise ValueError(
                "client_order_id cannot be empty."
            )

        if (
            required_prefix is not None
            and not value.startswith(
                required_prefix
            )
        ):

            raise ValueError(
                "client_order_id must begin with "
                f"{required_prefix}."
            )

        return value

    # ========================================================
    # BUILD BROKER ORDER
    # ========================================================

    def _build_broker_order(
        self,

        broker_response: Any,

        symbol: str,

        side: TradeSide,

        quantity: int,

        requested_price: Optional[
            float
        ],

        client_order_id: str,

        metadata: Optional[
            dict[str, Any]
        ] = None,

    ) -> BrokerOrder:

        broker_order_id = str(

            getattr(
                broker_response,
                "id",
                "",
            )

            or ""

        ).strip()

        if not broker_order_id:

            raise RuntimeError(
                "Broker did not return "
                "an order ID."
            )

        raw_status = getattr(
            broker_response,
            "status",
            None,
        )

        status = (
            self._map_status(
                raw_status
            )
        )

        filled_price = (
            self._safe_float(
                getattr(
                    broker_response,
                    "filled_avg_price",
                    None,
                )
            )
        )

        filled_quantity = (
            self._safe_int(
                getattr(
                    broker_response,
                    "filled_qty",
                    0,
                )
            )
        )

        raw_status_value = getattr(
            raw_status,
            "value",
            raw_status,
        )

        returned_client_order_id = str(
            getattr(
                broker_response,
                "client_order_id",
                client_order_id,
            )
            or client_order_id
        ).strip()

        order_metadata = dict(
            metadata or {}
        )

        order_metadata.update(
            {
                "paper_trading": True,

                "broker_raw_status": str(
                    raw_status_value
                    or ""
                ),

                "broker_client_order_id": (
                    returned_client_order_id
                ),
            }
        )

        return BrokerOrder(

            symbol=symbol,

            side=side,

            quantity=quantity,

            order_id=(
                broker_order_id
            ),

            client_order_id=(
                returned_client_order_id
            ),

            status=status,

            requested_price=(
                requested_price
            ),

            filled_price=(
                filled_price
            ),

            filled_quantity=(
                filled_quantity
            ),

            submitted_at=(
                datetime.now(
                    timezone.utc
                )
            ),

            metadata=(
                order_metadata
            ),
        )

    # ========================================================
    # LEGACY ENTRY VALIDATION
    # ========================================================

    @staticmethod
    def _entry_reject_reason(
        features: FeatureSnapshot,
        ai_analysis: AIAnalysis,
        risk_decision: RiskDecision,
    ) -> Optional[str]:

        if not features.data_quality_ok:

            return (
                "Feature data quality failed."
            )

        if features.data_is_stale:

            return (
                "Market data is stale."
            )

        if (
            ai_analysis.action
            != SignalAction.BUY
        ):

            return (
                "AI action is not BUY."
            )

        if not risk_decision.approved:

            return (
                "RiskEngine rejected trade: "
                f"{risk_decision.reason}"
            )

        if (
            risk_decision.quantity
            <= 0
        ):

            return (
                "RiskEngine returned "
                "invalid quantity."
            )

        return None

    # ========================================================
    # LEGACY ENTRY ORDER
    # ========================================================

    def submit_entry(
        self,

        features: FeatureSnapshot,

        ai_analysis: AIAnalysis,

        risk_decision: RiskDecision,

    ) -> BrokerOrder:
        """
        Legacy entry path.

        Kept for compatibility/testing.

        New integrated flow should use:
            submit_final_decision()
        """

        reject_reason = (
            self._entry_reject_reason(
                features,
                ai_analysis,
                risk_decision,
            )
        )

        if reject_reason is not None:

            raise RuntimeError(
                reject_reason
            )

        if not self.broker.market_is_open():

            raise RuntimeError(
                "Market is currently closed. "
                "Entry order was not submitted."
            )

        symbol = (
            self._normalize_symbol(
                features.symbol
            )
        )

        quantity = int(
            risk_decision.quantity
        )

        client_order_id = (
            self._new_client_order_id(
                "JALWE-ENTRY"
            )
        )

        logger.info(
            "Submitting PAPER ENTRY | "
            "symbol=%s qty=%s "
            "client_order_id=%s",

            symbol,
            quantity,
            client_order_id,
        )

        broker_response = (
            self.broker
            .submit_market_order(

                symbol=symbol,

                quantity=quantity,

                side="BUY",

                client_order_id=(
                    client_order_id
                ),
            )
        )

        return (
            self._build_broker_order(

                broker_response=(
                    broker_response
                ),

                symbol=symbol,

                side=TradeSide.BUY,

                quantity=quantity,

                requested_price=(
                    features.price
                ),

                client_order_id=(
                    client_order_id
                ),

                metadata={
                    "order_role": (
                        "ENTRY"
                    ),

                    "execution_path": (
                        "LEGACY"
                    ),

                    "ai_score": (
                        ai_analysis.score
                    ),

                    "risk_amount": (
                        risk_decision
                        .risk_amount
                    ),

                    "risk_pct": (
                        risk_decision
                        .risk_pct
                    ),

                    "stop_price": (
                        risk_decision
                        .stop_price
                    ),

                    "target_1": (
                        risk_decision
                        .target_1
                    ),

                    "target_2": (
                        risk_decision
                        .target_2
                    ),

                    "target_3": (
                        risk_decision
                        .target_3
                    ),
                },
            )
        )

    # ========================================================
    # CENTRAL DECISION VALIDATION
    # ========================================================

    @staticmethod
    def _final_decision_reject_reason(
        decision: Any,
    ) -> Optional[str]:

        if decision is None:

            return (
                "Final trade decision is missing."
            )

        # ----------------------------------------------------
        # READY
        # ----------------------------------------------------

        if not bool(
            getattr(
                decision,
                "ready_for_execution",
                False,
            )
        ):

            return (
                "DecisionEngine has not approved "
                "this trade for execution."
            )

        # ----------------------------------------------------
        # FINAL STATE
        # ----------------------------------------------------

        state = str(
            getattr(
                getattr(
                    decision,
                    "state",
                    None,
                ),
                "value",
                getattr(
                    decision,
                    "state",
                    "",
                ),
            )
            or ""
        ).strip()

        if (
            state
            != "READY_FOR_PAPER_EXECUTION"
        ):

            return (
                "Final decision state is not "
                "READY_FOR_PAPER_EXECUTION."
            )

        # ----------------------------------------------------
        # SYMBOL
        # ----------------------------------------------------

        symbol = str(
            getattr(
                decision,
                "symbol",
                "",
            )
            or ""
        ).strip().upper()

        if not symbol:

            return (
                "Final decision symbol "
                "is missing."
            )

        # ----------------------------------------------------
        # GATES
        # ----------------------------------------------------

        gates = getattr(
            decision,
            "gates",
            None,
        )

        if not isinstance(
            gates,
            dict,
        ):

            return (
                "Final decision gates "
                "are unavailable."
            )

        required_gates = (
            "market_data",
            "features",
            "ai",
            "opportunity",
            "market_regime",
            "strategy_router",
            "session_strategy",
            "trigger",
            "breakout_confirmation",
            "risk",
        )

        failed_gates = [
            gate

            for gate
            in required_gates

            if not bool(
                gates.get(
                    gate,
                    False,
                )
            )
        ]

        if failed_gates:

            return (
                "Mandatory decision gates failed: "
                + ", ".join(
                    failed_gates
                )
            )

        # ----------------------------------------------------
        # QUANTITY
        # ----------------------------------------------------

        try:

            quantity = int(
                getattr(
                    decision,
                    "quantity",
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            quantity = 0

        if quantity <= 0:

            return (
                "Final decision contains "
                "invalid quantity."
            )

        # ----------------------------------------------------
        # PRICE STRUCTURE
        # ----------------------------------------------------

        try:

            entry = float(
                getattr(
                    decision,
                    "entry_price",
                    0,
                )
                or 0
            )

            stop = float(
                getattr(
                    decision,
                    "stop_price",
                    0,
                )
                or 0
            )

            target_1 = float(
                getattr(
                    decision,
                    "target_1",
                    0,
                )
                or 0
            )

            target_2 = float(
                getattr(
                    decision,
                    "target_2",
                    0,
                )
                or 0
            )

            target_3 = float(
                getattr(
                    decision,
                    "target_3",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            return (
                "Final decision contains "
                "invalid price levels."
            )

        if not (
            0
            < stop
            < entry
            < target_1
            < target_2
            < target_3
        ):

            return (
                "Final decision price "
                "structure is invalid."
            )

        # ----------------------------------------------------
        # RISK
        # ----------------------------------------------------

        try:

            risk_pct = float(
                getattr(
                    decision,
                    "risk_pct",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            return (
                "Invalid final risk percentage."
            )

        # Current JALWE hard cap.
        if not (
            0
            < risk_pct
            <= 1.50
        ):

            return (
                "Final risk percentage is "
                "outside JALWE safety limits."
            )

        return None

    # ========================================================
    # CENTRAL DECISION PAPER ENTRY
    # ========================================================

    def submit_final_decision(
        self,
        decision: Any,
        client_order_id: Optional[str] = None,
    ) -> BrokerOrder:
        """
        Crash-safe central entry pathway.

        Safe sequence:

            Orchestrator generates client_order_id
                      ↓
            Database saves EntryIntent
                      ↓
            SQLite COMMIT
                      ↓
            submit_final_decision(
                decision,
                client_order_id=...
            )
                      ↓
            Alpaca PAPER

        If the process crashes after broker submission,
        RecoveryEngine can locate the broker order using
        the already-persisted client_order_id.

        IMPORTANT:
            submitted != filled

        ReconciliationEngine confirms actual execution.
        """

        # ====================================================
        # 1. CENTRAL DECISION SAFETY
        # ====================================================

        reject_reason = (
            self._final_decision_reject_reason(
                decision
            )
        )

        if reject_reason is not None:

            raise RuntimeError(
                reject_reason
            )

        # ====================================================
        # 2. MARKET OPEN
        # ====================================================

        if not self.broker.market_is_open():

            raise RuntimeError(
                "Market is currently closed. "
                "Entry order was not submitted."
            )

        # ====================================================
        # 3. SYMBOL / QUANTITY
        # ====================================================

        symbol = (
            self._normalize_symbol(
                getattr(
                    decision,
                    "symbol",
                    "",
                )
            )
        )

        quantity = int(
            decision.quantity
        )

        if quantity <= 0:

            raise RuntimeError(
                "Final decision quantity "
                "is invalid."
            )

        # ====================================================
        # 4. LAST-SECOND BROKER POSITION CHECK
        # ====================================================

        try:

            positions = (
                self.broker
                .get_all_positions()
            )

        except Exception as exc:

            raise RuntimeError(
                "Unable to verify broker "
                "positions before entry."
            ) from exc

        if positions:

            existing_symbols = {
                str(
                    getattr(
                        position,
                        "symbol",
                        "",
                    )
                )
                .strip()
                .upper()

                for position
                in positions
            }

            if symbol in existing_symbols:

                raise RuntimeError(
                    f"Broker position already exists "
                    f"for {symbol}. "
                    "Duplicate entry blocked."
                )

            raise RuntimeError(
                "Another broker position is "
                "already open. "
                "JALWE small-account mode "
                "allows only one position."
            )

        # ====================================================
        # 5. CLIENT ORDER ID
        # ====================================================

        if client_order_id is None:

            final_client_order_id = (
                self._new_client_order_id(
                    "JALWE-FINAL"
                )
            )

        else:

            final_client_order_id = (
                self._validate_client_order_id(
                    client_order_id,
                    required_prefix=(
                        "JALWE-FINAL"
                    ),
                )
            )

        # ====================================================
        # 6. SUBMIT PAPER BUY
        # ====================================================

        logger.info(
            "Submitting FINAL PAPER ENTRY | "
            "symbol=%s qty=%s "
            "strategy=%s grade=%s "
            "risk=%s%% client_order_id=%s",

            symbol,

            quantity,

            getattr(
                decision,
                "strategy",
                None,
            ),

            getattr(
                decision,
                "setup_grade",
                None,
            ),

            getattr(
                decision,
                "risk_pct",
                None,
            ),

            final_client_order_id,
        )

        broker_response = (
            self.broker
            .submit_market_order(

                symbol=symbol,

                quantity=quantity,

                side="BUY",

                client_order_id=(
                    final_client_order_id
                ),
            )
        )

        # ====================================================
        # 7. RETURN BROKER ORDER
        # ====================================================

        return (
            self._build_broker_order(

                broker_response=(
                    broker_response
                ),

                symbol=symbol,

                side=TradeSide.BUY,

                quantity=quantity,

                requested_price=(
                    decision.entry_price
                ),

                client_order_id=(
                    final_client_order_id
                ),

                metadata={
                    "order_role": (
                        "CENTRAL_ENTRY"
                    ),

                    "execution_path": (
                        "DECISION_ENGINE_V3_1"
                    ),

                    "crash_safe_client_order_id": (
                        True
                    ),

                    "decision_state": (
                        self._enum_value(
                            getattr(
                                decision,
                                "state",
                                "",
                            )
                        )
                    ),

                    "strategy": (
                        getattr(
                            decision,
                            "strategy",
                            None,
                        )
                    ),

                    "setup_grade": (
                        getattr(
                            decision,
                            "setup_grade",
                            None,
                        )
                    ),

                    "market_regime": (
                        getattr(
                            decision,
                            "market_regime",
                            None,
                        )
                    ),

                    "ai_score": (
                        getattr(
                            decision,
                            "ai_score",
                            None,
                        )
                    ),

                    "opportunity_score": (
                        getattr(
                            decision,
                            "opportunity_score",
                            None,
                        )
                    ),

                    "session_strategy_score": (
                        getattr(
                            decision,
                            "session_strategy_score",
                            None,
                        )
                    ),

                    "breakout_score": (
                        getattr(
                            decision,
                            "breakout_score",
                            None,
                        )
                    ),

                    "risk_pct": (
                        getattr(
                            decision,
                            "risk_pct",
                            None,
                        )
                    ),

                    "stop_price": (
                        getattr(
                            decision,
                            "stop_price",
                            None,
                        )
                    ),

                    "target_1": (
                        getattr(
                            decision,
                            "target_1",
                            None,
                        )
                    ),

                    "target_2": (
                        getattr(
                            decision,
                            "target_2",
                            None,
                        )
                    ),

                    "target_3": (
                        getattr(
                            decision,
                            "target_3",
                            None,
                        )
                    ),

                    "decision_reason": (
                        getattr(
                            decision,
                            "reason",
                            None,
                        )
                    ),

                    "decision_gates": dict(
                        getattr(
                            decision,
                            "gates",
                            {},
                        )
                    ),
                },
            )
        )

    # ========================================================
    # EXIT ORDER
    # ========================================================

    def submit_exit(
        self,

        symbol: str,

        quantity: int,

        requested_price: Optional[
            float
        ] = None,

        reason: Optional[
            str
        ] = None,

    ) -> BrokerOrder:
        """
        Submit a partial or full PAPER SELL order.

        This method does NOT change ManagedTrade state.

        Correct flow:

            ExecutionEngine.submit_exit()
                      ↓
            TradeManager.register_exit_order()
                      ↓
            ReconciliationEngine.reconcile_order()
                      ↓
            TradeManager.apply_exit_reconciliation()
        """

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        quantity = int(
            quantity
        )

        if quantity <= 0:

            raise ValueError(
                "Exit quantity must be "
                "greater than zero."
            )

        # ====================================================
        # MARKET OPEN
        # ====================================================

        if not self.broker.market_is_open():

            raise RuntimeError(
                "Market is currently closed. "
                "Exit order was not submitted."
            )

        # ====================================================
        # VERIFY BROKER POSITION
        # ====================================================

        position = (
            self.broker
            .get_position(
                symbol
            )
        )

        if position is None:

            raise RuntimeError(
                f"No broker position "
                f"exists for {symbol}."
            )

        broker_quantity = (
            self._safe_int(
                getattr(
                    position,
                    "qty",
                    0,
                )
            )
        )

        if broker_quantity <= 0:

            raise RuntimeError(
                f"Broker position quantity "
                f"for {symbol} is invalid."
            )

        if (
            quantity
            > broker_quantity
        ):

            raise RuntimeError(
                "Requested exit quantity "
                "exceeds broker position quantity."
            )

        # ====================================================
        # CLIENT ORDER ID
        # ====================================================

        client_order_id = (
            self._new_client_order_id(
                "JALWE-EXIT"
            )
        )

        # ====================================================
        # SUBMIT PAPER SELL
        # ====================================================

        logger.info(
            "Submitting PAPER EXIT | "
            "symbol=%s qty=%s "
            "broker_qty=%s reason=%s "
            "client_order_id=%s",

            symbol,

            quantity,

            broker_quantity,

            reason,

            client_order_id,
        )

        broker_response = (
            self.broker
            .submit_market_order(

                symbol=symbol,

                quantity=quantity,

                side="SELL",

                client_order_id=(
                    client_order_id
                ),
            )
        )

        return (
            self._build_broker_order(

                broker_response=(
                    broker_response
                ),

                symbol=symbol,

                side=TradeSide.SELL,

                quantity=quantity,

                requested_price=(
                    requested_price
                ),

                client_order_id=(
                    client_order_id
                ),

                metadata={
                    "order_role": (
                        "EXIT"
                    ),

                    "exit_reason": (
                        reason
                    ),

                    "broker_position_before_exit": (
                        broker_quantity
                    ),
                },
            )
        )


# ============================================================
# LAZY SINGLETON
# ============================================================

execution_engine: Optional[
    ExecutionEngine
] = None


def get_execution_engine(
) -> ExecutionEngine:

    global execution_engine

    if execution_engine is None:

        execution_engine = (
            ExecutionEngine()
        )

    return execution_engine