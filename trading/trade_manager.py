from __future__ import annotations

import logging
import math

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from core.models import (
    BrokerOrder,
    OrderStatus,
)


logger = logging.getLogger(__name__)


# ============================================================
# TRADE STAGE
# ============================================================

class TradeStage(str, Enum):

    OPEN = "OPEN"

    T1_HIT = "T1_HIT"
    T2_HIT = "T2_HIT"
    T3_HIT = "T3_HIT"

    RUNNER = "RUNNER"

    # Kept for compatibility with older saved trades.
    TRAILING = "TRAILING"

    CLOSED = "CLOSED"


# ============================================================
# TRADE ACTION
# ============================================================

class TradeAction(str, Enum):

    HOLD = "HOLD"

    EXIT_STOP = "EXIT_STOP"

    TAKE_PROFIT_1 = "TAKE_PROFIT_1"
    TAKE_PROFIT_2 = "TAKE_PROFIT_2"
    TAKE_PROFIT_3 = "TAKE_PROFIT_3"

    # Non-broker actions used when the position is too small
    # to take another whole-share partial.
    LOCK_T1 = "LOCK_T1"
    LOCK_T2 = "LOCK_T2"

    START_RUNNER = "START_RUNNER"

    # Legacy compatibility.
    START_TRAILING = "START_TRAILING"

    EXIT_TRAILING = "EXIT_TRAILING"

    # Legacy compatibility with old pending orders.
    EXIT_TARGET_3 = "EXIT_TARGET_3"


# ============================================================
# ACTIONS THAT REQUIRE A BROKER SELL ORDER
# ============================================================

EXIT_ACTIONS = {

    TradeAction.EXIT_STOP,

    TradeAction.TAKE_PROFIT_1,
    TradeAction.TAKE_PROFIT_2,
    TradeAction.TAKE_PROFIT_3,

    TradeAction.EXIT_TRAILING,

    # Legacy.
    TradeAction.EXIT_TARGET_3,
}


# ============================================================
# MANAGED TRADE
# ============================================================

@dataclass
class ManagedTrade:
    """
    Confirmed local state of one trade.

    Critical rule:

    remaining_quantity changes ONLY after
    broker reconciliation confirms fills.

    T1/T2/T3 partial exits also become completed
    only after broker-confirmed fills.

    Small-position exception:

    If the position is too small to sell a partial
    while preserving a runner, JALWE may move the
    protective stop at a target without submitting
    a sell order.
    """

    symbol: str

    entry_price: float

    initial_quantity: int
    remaining_quantity: int

    initial_stop: float
    current_stop: float

    target_1: float
    target_2: float
    target_3: float

    stage: TradeStage = (
        TradeStage.OPEN
    )

    # --------------------------------------------------------
    # TARGET COMPLETION
    # --------------------------------------------------------

    t1_completed: bool = False
    t2_completed: bool = False
    t3_completed: bool = False

    # --------------------------------------------------------
    # REALIZED QUANTITY BY TARGET
    # --------------------------------------------------------

    t1_realized_quantity: int = 0
    t2_realized_quantity: int = 0
    t3_realized_quantity: int = 0

    realized_quantity: int = 0

    # --------------------------------------------------------
    # ORIGINAL PROFIT-TAKING PLAN
    # --------------------------------------------------------

    planned_t1_quantity: int = 0
    planned_t2_quantity: int = 0
    planned_t3_quantity: int = 0
    planned_runner_quantity: int = 0

    # --------------------------------------------------------
    # RUNNER / TRAILING
    # --------------------------------------------------------

    trailing_active: bool = False

    highest_price: Optional[
        float
    ] = None

    # --------------------------------------------------------
    # PENDING BROKER EXIT
    # --------------------------------------------------------

    pending_action: Optional[
        TradeAction
    ] = None

    pending_order_id: Optional[
        str
    ] = None

    pending_requested_quantity: int = 0
    pending_filled_quantity: int = 0

    pending_status: Optional[
        str
    ] = None

    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    # --------------------------------------------------------
    # RESTORE EXTRA V2 STATE FROM METADATA
    # --------------------------------------------------------

    def __post_init__(
        self,
    ) -> None:

        metadata = (
            self.metadata
            if isinstance(
                self.metadata,
                dict,
            )
            else {}
        )

        self.metadata = metadata

        self.t3_completed = bool(
            metadata.get(
                "t3_completed",
                self.t3_completed,
            )
        )

        self.t3_realized_quantity = int(
            metadata.get(
                "t3_realized_quantity",
                self.t3_realized_quantity,
            )
            or 0
        )

        self.planned_t1_quantity = int(
            metadata.get(
                "planned_t1_quantity",
                self.planned_t1_quantity,
            )
            or 0
        )

        self.planned_t2_quantity = int(
            metadata.get(
                "planned_t2_quantity",
                self.planned_t2_quantity,
            )
            or 0
        )

        self.planned_t3_quantity = int(
            metadata.get(
                "planned_t3_quantity",
                self.planned_t3_quantity,
            )
            or 0
        )

        self.planned_runner_quantity = int(
            metadata.get(
                "planned_runner_quantity",
                self.planned_runner_quantity,
            )
            or 0
        )

    @property
    def has_pending_exit(
        self,
    ) -> bool:

        return (
            self.pending_action
            is not None
        )


# ============================================================
# MANAGEMENT DECISION
# ============================================================

@dataclass
class TradeManagementDecision:

    symbol: str

    action: TradeAction

    quantity: int

    current_price: float

    stage_before: TradeStage
    stage_after: TradeStage

    stop_before: float
    stop_after: float

    reason: str

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )


# ============================================================
# TRADE MANAGER
# ============================================================

class TradeManager:
    """
    JALWE V4 - Profit Lock + Runner State Machine.

    Profit model:

        Entry
          ↓
        T1
          ↓
        Partial exit
          ↓
        Stop -> Entry
          ↓
        T2
          ↓
        Partial exit
          ↓
        Stop -> T1
          ↓
        T3
          ↓
        Partial exit
          ↓
        Stop -> T2
          ↓
        RUNNER
          ↓
        Ratcheting trailing stop

    Default allocation:

        T1 = 40%
        T2 = 30%
        T3 = 20%
        Runner ≈ 10%

    Whole-share logic adapts automatically for
    small $100-account positions.

    Important:

    A price touching a target does NOT confirm
    that shares were sold.

    Exit quantity/state is changed only after
    ReconciliationEngine confirms broker fills.
    """

    def __init__(
        self,

        t1_exit_pct: float = 40.0,
        t2_exit_pct: float = 30.0,
        t3_exit_pct: float = 20.0,

        trailing_distance_pct: float = 3.0,

        atr_trailing_multiplier: float = 2.0,
    ) -> None:

        for name, value in (
            (
                "t1_exit_pct",
                t1_exit_pct,
            ),
            (
                "t2_exit_pct",
                t2_exit_pct,
            ),
            (
                "t3_exit_pct",
                t3_exit_pct,
            ),
        ):

            if not (
                0
                < float(value)
                < 100
            ):

                raise ValueError(
                    f"{name} must be "
                    "between 0 and 100."
                )

        if (
            t1_exit_pct
            + t2_exit_pct
            + t3_exit_pct
            >= 100
        ):

            raise ValueError(
                "T1 + T2 + T3 must "
                "leave a runner position."
            )

        if trailing_distance_pct <= 0:

            raise ValueError(
                "trailing_distance_pct "
                "must be positive."
            )

        if atr_trailing_multiplier <= 0:

            raise ValueError(
                "atr_trailing_multiplier "
                "must be positive."
            )

        self.t1_exit_pct = float(
            t1_exit_pct
        )

        self.t2_exit_pct = float(
            t2_exit_pct
        )

        self.t3_exit_pct = float(
            t3_exit_pct
        )

        self.trailing_distance_pct = float(
            trailing_distance_pct
        )

        self.atr_trailing_multiplier = float(
            atr_trailing_multiplier
        )

    # ========================================================
    # METADATA SYNC
    # ========================================================

    @staticmethod
    def _sync_metadata(
        trade: ManagedTrade,
    ) -> None:

        trade.metadata[
            "trade_manager_version"
        ] = "PROFIT_LOCK_V2"

        trade.metadata[
            "t3_completed"
        ] = trade.t3_completed

        trade.metadata[
            "t3_realized_quantity"
        ] = (
            trade.t3_realized_quantity
        )

        trade.metadata[
            "planned_t1_quantity"
        ] = (
            trade.planned_t1_quantity
        )

        trade.metadata[
            "planned_t2_quantity"
        ] = (
            trade.planned_t2_quantity
        )

        trade.metadata[
            "planned_t3_quantity"
        ] = (
            trade.planned_t3_quantity
        )

        trade.metadata[
            "planned_runner_quantity"
        ] = (
            trade.planned_runner_quantity
        )

        trade.metadata[
            "runner_active"
        ] = bool(
            trade.trailing_active
            and trade.t3_completed
        )

    # ========================================================
    # QUANTITY PLAN
    # ========================================================

    def _build_quantity_plan(
        self,
        initial_quantity: int,
    ) -> tuple[
        int,
        int,
        int,
        int,
    ]:
        """
        Create a whole-share profit-taking plan.

        Examples:

        1 share:
            T1=0 T2=0 T3=0 Runner=1

        2 shares:
            T1=1 T2=0 T3=0 Runner=1

        3 shares:
            T1=1 T2=1 T3=0 Runner=1

        Larger positions approximate:
            40 / 30 / 20 / 10
        """

        quantity = int(
            initial_quantity
        )

        if quantity <= 0:

            raise ValueError(
                "initial_quantity must "
                "be positive."
            )

        if quantity == 1:

            return (
                0,
                0,
                0,
                1,
            )

        if quantity == 2:

            return (
                1,
                0,
                0,
                1,
            )

        if quantity == 3:

            return (
                1,
                1,
                0,
                1,
            )

        # Reserve at least one whole share
        # for the runner.
        maximum_exit_quantity = (
            quantity - 1
        )

        t1 = max(
            math.floor(
                quantity
                * self.t1_exit_pct
                / 100.0
            ),
            1,
        )

        t2 = max(
            math.floor(
                quantity
                * self.t2_exit_pct
                / 100.0
            ),
            1,
        )

        t3 = max(
            math.floor(
                quantity
                * self.t3_exit_pct
                / 100.0
            ),
            1,
        )

        # If integer rounding consumes too many
        # shares, reduce the largest allocation
        # while keeping each target >= 1.
        while (
            t1 + t2 + t3
            > maximum_exit_quantity
        ):

            allocations = [
                (
                    "t1",
                    t1,
                ),
                (
                    "t2",
                    t2,
                ),
                (
                    "t3",
                    t3,
                ),
            ]

            allocations.sort(
                key=lambda item:
                item[1],
                reverse=True,
            )

            reduced = False

            for name, value in allocations:

                if value <= 1:
                    continue

                if name == "t1":
                    t1 -= 1

                elif name == "t2":
                    t2 -= 1

                else:
                    t3 -= 1

                reduced = True
                break

            if not reduced:
                break

        runner = (
            quantity
            - t1
            - t2
            - t3
        )

        if runner < 1:

            raise RuntimeError(
                "Unable to reserve runner quantity."
            )

        return (
            t1,
            t2,
            t3,
            runner,
        )

    # ========================================================
    # ENSURE PLAN EXISTS
    # ========================================================

    def _ensure_quantity_plan(
        self,
        trade: ManagedTrade,
    ) -> None:

        planned_total = (
            trade.planned_t1_quantity
            + trade.planned_t2_quantity
            + trade.planned_t3_quantity
            + trade.planned_runner_quantity
        )

        if (
            planned_total
            == trade.initial_quantity
            and trade.planned_runner_quantity
            >= 1
        ):

            return

        (
            t1,
            t2,
            t3,
            runner,
        ) = self._build_quantity_plan(
            trade.initial_quantity
        )

        trade.planned_t1_quantity = t1
        trade.planned_t2_quantity = t2
        trade.planned_t3_quantity = t3
        trade.planned_runner_quantity = (
            runner
        )

        self._sync_metadata(
            trade
        )

    # ========================================================
    # CREATE TRADE
    # ========================================================

    def create_trade(
        self,

        symbol: str,

        entry_price: float,

        quantity: int,

        stop_price: float,

        target_1: float,
        target_2: float,
        target_3: float,

    ) -> ManagedTrade:

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not symbol:

            raise ValueError(
                "Symbol cannot be empty."
            )

        if entry_price <= 0:

            raise ValueError(
                "entry_price must be positive."
            )

        if quantity <= 0:

            raise ValueError(
                "quantity must be positive."
            )

        if not (
            0
            < stop_price
            < entry_price
        ):

            raise ValueError(
                "stop_price must be "
                "below entry_price."
            )

        if not (
            entry_price
            < target_1
            < target_2
            < target_3
        ):

            raise ValueError(
                "Targets must satisfy "
                "entry < T1 < T2 < T3."
            )

        (
            planned_t1,
            planned_t2,
            planned_t3,
            planned_runner,
        ) = self._build_quantity_plan(
            int(quantity)
        )

        trade = ManagedTrade(

            symbol=symbol,

            entry_price=float(
                entry_price
            ),

            initial_quantity=int(
                quantity
            ),

            remaining_quantity=int(
                quantity
            ),

            initial_stop=float(
                stop_price
            ),

            current_stop=float(
                stop_price
            ),

            target_1=float(
                target_1
            ),

            target_2=float(
                target_2
            ),

            target_3=float(
                target_3
            ),

            highest_price=float(
                entry_price
            ),

            planned_t1_quantity=(
                planned_t1
            ),

            planned_t2_quantity=(
                planned_t2
            ),

            planned_t3_quantity=(
                planned_t3
            ),

            planned_runner_quantity=(
                planned_runner
            ),
        )

        self._sync_metadata(
            trade
        )

        return trade

    # ========================================================
    # REMAINING TARGET QUANTITIES
    # ========================================================

    @staticmethod
    def _remaining_planned_quantity(
        planned: int,
        realized: int,
        remaining_position: int,
    ) -> int:

        quantity = (
            int(planned)
            - int(realized)
        )

        return max(
            min(
                quantity,
                remaining_position,
            ),
            0,
        )

    def _remaining_t1_quantity(
        self,
        trade: ManagedTrade,
    ) -> int:

        self._ensure_quantity_plan(
            trade
        )

        return (
            self._remaining_planned_quantity(
                trade.planned_t1_quantity,
                trade.t1_realized_quantity,
                trade.remaining_quantity,
            )
        )

    def _remaining_t2_quantity(
        self,
        trade: ManagedTrade,
    ) -> int:

        self._ensure_quantity_plan(
            trade
        )

        return (
            self._remaining_planned_quantity(
                trade.planned_t2_quantity,
                trade.t2_realized_quantity,
                trade.remaining_quantity,
            )
        )

    def _remaining_t3_quantity(
        self,
        trade: ManagedTrade,
    ) -> int:

        self._ensure_quantity_plan(
            trade
        )

        return (
            self._remaining_planned_quantity(
                trade.planned_t3_quantity,
                trade.t3_realized_quantity,
                trade.remaining_quantity,
            )
        )

    # ========================================================
    # TRAILING STOP
    # ========================================================

    def _percentage_trailing_stop(
        self,
        highest_price: float,
    ) -> float:

        return round(
            highest_price
            * (
                1.0
                - self.trailing_distance_pct
                / 100.0
            ),
            4,
        )

    def _smart_trailing_stop(
        self,

        trade: ManagedTrade,

        current_price: float,

        atr: Optional[
            float
        ] = None,

        structure_stop: Optional[
            float
        ] = None,

    ) -> float:
        """
        Ratcheting trailing stop.

        Candidate sources:
        - Percentage trail
        - ATR trail
        - Market-structure stop

        Stop can only move upward.
        """

        if trade.highest_price is None:

            return trade.current_stop

        candidates = [
            trade.current_stop,

            self._percentage_trailing_stop(
                trade.highest_price
            ),
        ]

        if (
            atr is not None
            and atr > 0
        ):

            atr_stop = (
                trade.highest_price
                - (
                    float(atr)
                    * self.atr_trailing_multiplier
                )
            )

            if (
                0
                < atr_stop
                < current_price
            ):

                candidates.append(
                    atr_stop
                )

        if (
            structure_stop is not None
            and 0
            < float(structure_stop)
            < current_price
        ):

            candidates.append(
                float(
                    structure_stop
                )
            )

        return round(
            max(
                candidates
            ),
            4,
        )

    # ========================================================
    # DECISION BUILDER
    # ========================================================

    @staticmethod
    def _decision(
        trade: ManagedTrade,

        action: TradeAction,

        quantity: int,

        current_price: float,

        stage_before: TradeStage,

        stop_before: float,

        reason: str,

        **metadata: Any,

    ) -> TradeManagementDecision:

        return TradeManagementDecision(

            symbol=trade.symbol,

            action=action,

            quantity=int(
                quantity
            ),

            current_price=float(
                current_price
            ),

            stage_before=(
                stage_before
            ),

            stage_after=(
                trade.stage
            ),

            stop_before=float(
                stop_before
            ),

            stop_after=float(
                trade.current_stop
            ),

            reason=reason,

            metadata=metadata,
        )

    # ========================================================
    # COMPLETE ZERO-QUANTITY T1
    # ========================================================

    def _complete_t1_without_exit(
        self,
        trade: ManagedTrade,
    ) -> None:

        trade.t1_completed = True

        trade.stage = (
            TradeStage.T1_HIT
        )

        trade.current_stop = max(
            trade.current_stop,
            trade.entry_price,
        )

        self._sync_metadata(
            trade
        )

    # ========================================================
    # COMPLETE ZERO-QUANTITY T2
    # ========================================================

    def _complete_t2_without_exit(
        self,
        trade: ManagedTrade,
    ) -> None:

        trade.t2_completed = True

        trade.stage = (
            TradeStage.T2_HIT
        )

        trade.current_stop = max(
            trade.current_stop,
            trade.target_1,
        )

        self._sync_metadata(
            trade
        )

    # ========================================================
    # ACTIVATE RUNNER
    # ========================================================

    def _activate_runner(
        self,
        trade: ManagedTrade,
    ) -> None:

        trade.t3_completed = True

        trade.stage = (
            TradeStage.RUNNER
        )

        # T3 reached:
        # lock T2 as the new minimum stop.
        trade.current_stop = max(
            trade.current_stop,
            trade.target_2,
        )

        trade.trailing_active = True

        self._sync_metadata(
            trade
        )

    # ========================================================
    # MAIN STATE MACHINE
    # ========================================================

    def evaluate(
        self,

        trade: ManagedTrade,

        current_price: float,

        *,

        atr: Optional[
            float
        ] = None,

        structure_stop: Optional[
            float
        ] = None,

    ) -> TradeManagementDecision:

        if current_price <= 0:

            raise ValueError(
                "current_price must be positive."
            )

        self._ensure_quantity_plan(
            trade
        )

        stage_before = (
            trade.stage
        )

        stop_before = (
            trade.current_stop
        )

        # ----------------------------------------------------
        # CLOSED
        # ----------------------------------------------------

        if (
            trade.stage
            == TradeStage.CLOSED
        ):

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.HOLD
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Trade is already closed."
                ),
            )

        # ----------------------------------------------------
        # HIGH-WATER MARK
        # ----------------------------------------------------

        if (
            trade.highest_price
            is None
            or current_price
            > trade.highest_price
        ):

            trade.highest_price = float(
                current_price
            )

        # ----------------------------------------------------
        # PENDING BROKER EXIT
        # ----------------------------------------------------

        if trade.has_pending_exit:

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.HOLD
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Broker exit order is "
                    "pending reconciliation."
                ),

                pending_action=(
                    trade.pending_action.value
                    if trade.pending_action
                    else None
                ),

                pending_order_id=(
                    trade.pending_order_id
                ),

                pending_quantity=(
                    trade.pending_requested_quantity
                ),

                pending_filled=(
                    trade.pending_filled_quantity
                ),
            )

        # ----------------------------------------------------
        # RUNNER TRAILING RATCHET
        # ----------------------------------------------------

        if (
            trade.trailing_active
            and trade.highest_price
            is not None
        ):

            trailing_stop = (
                self._smart_trailing_stop(

                    trade=trade,

                    current_price=(
                        current_price
                    ),

                    atr=atr,

                    structure_stop=(
                        structure_stop
                    ),
                )
            )

            # Never reduce the stop.
            trade.current_stop = max(
                trade.current_stop,
                trailing_stop,
            )

        # ====================================================
        # 1. ACTIVE STOP
        # ====================================================

        if (
            current_price
            <= trade.current_stop
        ):

            action = (
                TradeAction.EXIT_TRAILING
                if trade.trailing_active
                else TradeAction.EXIT_STOP
            )

            return self._decision(

                trade=trade,

                action=action,

                quantity=(
                    trade.remaining_quantity
                ),

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Active stop level reached."
                ),
            )

        # ====================================================
        # 2. TARGET 1
        # ====================================================

        if (
            not trade.t1_completed
            and current_price
            >= trade.target_1
        ):

            quantity = (
                self._remaining_t1_quantity(
                    trade
                )
            )

            if quantity > 0:

                return self._decision(

                    trade=trade,

                    action=(
                        TradeAction
                        .TAKE_PROFIT_1
                    ),

                    quantity=quantity,

                    current_price=(
                        current_price
                    ),

                    stage_before=(
                        stage_before
                    ),

                    stop_before=(
                        stop_before
                    ),

                    reason=(
                        "Target 1 reached. "
                        "Waiting for broker fill "
                        "before moving stop "
                        "to break-even."
                    ),

                    planned_target_quantity=(
                        trade
                        .planned_t1_quantity
                    ),
                )

            # Small account / whole-share mode:
            # no share can be sold while preserving
            # the runner, so just lock break-even.
            self._complete_t1_without_exit(
                trade
            )

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.LOCK_T1
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Target 1 reached. "
                    "Position is too small for "
                    "another whole-share partial, "
                    "so stop moved to break-even."
                ),
            )

        # ====================================================
        # 3. TARGET 2
        # ====================================================

        if (
            trade.t1_completed
            and not trade.t2_completed
            and current_price
            >= trade.target_2
        ):

            quantity = (
                self._remaining_t2_quantity(
                    trade
                )
            )

            if quantity > 0:

                return self._decision(

                    trade=trade,

                    action=(
                        TradeAction
                        .TAKE_PROFIT_2
                    ),

                    quantity=quantity,

                    current_price=(
                        current_price
                    ),

                    stage_before=(
                        stage_before
                    ),

                    stop_before=(
                        stop_before
                    ),

                    reason=(
                        "Target 2 reached. "
                        "Waiting for broker fill "
                        "before locking Target 1."
                    ),

                    planned_target_quantity=(
                        trade
                        .planned_t2_quantity
                    ),
                )

            self._complete_t2_without_exit(
                trade
            )

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.LOCK_T2
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Target 2 reached. "
                    "No additional whole-share "
                    "partial is available, "
                    "so stop moved to Target 1."
                ),
            )

        # ====================================================
        # 4. TARGET 3
        # ====================================================

        if (
            trade.t1_completed
            and trade.t2_completed
            and not trade.t3_completed
            and current_price
            >= trade.target_3
        ):

            quantity = (
                self._remaining_t3_quantity(
                    trade
                )
            )

            if quantity > 0:

                return self._decision(

                    trade=trade,

                    action=(
                        TradeAction
                        .TAKE_PROFIT_3
                    ),

                    quantity=quantity,

                    current_price=(
                        current_price
                    ),

                    stage_before=(
                        stage_before
                    ),

                    stop_before=(
                        stop_before
                    ),

                    reason=(
                        "Target 3 reached. "
                        "Waiting for broker fill "
                        "before locking Target 2 "
                        "and activating runner."
                    ),

                    planned_target_quantity=(
                        trade
                        .planned_t3_quantity
                    ),
                )

            # No whole-share T3 exit available.
            # Preserve the final runner.
            self._activate_runner(
                trade
            )

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.START_RUNNER
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Target 3 reached. "
                    "Stop moved to Target 2 "
                    "and runner trailing activated."
                ),

                runner_quantity=(
                    trade.remaining_quantity
                ),
            )

        # ====================================================
        # 5. RUNNER
        # ====================================================

        if (
            trade.t3_completed
            and trade.trailing_active
        ):

            return self._decision(

                trade=trade,

                action=(
                    TradeAction.HOLD
                ),

                quantity=0,

                current_price=current_price,

                stage_before=stage_before,

                stop_before=stop_before,

                reason=(
                    "Runner active. "
                    "Trailing stop ratchets upward."
                ),

                runner_quantity=(
                    trade.remaining_quantity
                ),

                highest_price=(
                    trade.highest_price
                ),

                active_stop=(
                    trade.current_stop
                ),
            )

        # ====================================================
        # HOLD
        # ====================================================

        return self._decision(

            trade=trade,

            action=(
                TradeAction.HOLD
            ),

            quantity=0,

            current_price=current_price,

            stage_before=stage_before,

            stop_before=stop_before,

            reason=(
                "No trade-management trigger."
            ),
        )

    # ========================================================
    # REGISTER BROKER EXIT ORDER
    # ========================================================

    def register_exit_order(
        self,

        trade: ManagedTrade,

        decision: TradeManagementDecision,

        broker_order: BrokerOrder,

    ) -> None:
        """
        Call immediately AFTER ExecutionEngine
        successfully submits an exit order.

        Still does NOT change confirmed quantity
        or target-completion state.
        """

        if (
            decision.action
            not in EXIT_ACTIONS
        ):

            raise ValueError(
                "Decision does not require "
                "a broker exit order."
            )

        if decision.quantity <= 0:

            raise ValueError(
                "Exit quantity must be positive."
            )

        if trade.has_pending_exit:

            raise RuntimeError(
                "Trade already has a "
                "pending exit order."
            )

        order_id = str(
            broker_order.order_id
            or ""
        ).strip()

        if not order_id:

            raise ValueError(
                "Broker order ID is missing."
            )

        if (
            decision.quantity
            > trade.remaining_quantity
        ):

            raise ValueError(
                "Exit quantity exceeds "
                "remaining trade quantity."
            )

        trade.pending_action = (
            decision.action
        )

        trade.pending_order_id = (
            order_id
        )

        trade.pending_requested_quantity = (
            int(
                decision.quantity
            )
        )

        trade.pending_filled_quantity = 0

        trade.pending_status = (
            broker_order.status.value
        )

        self._sync_metadata(
            trade
        )

        logger.info(
            "Exit registered as pending | "
            "symbol=%s action=%s qty=%s "
            "order_id=%s",

            trade.symbol,

            decision.action.value,

            decision.quantity,

            order_id,
        )

    # ========================================================
    # APPLY BROKER RECONCILIATION
    # ========================================================

    def apply_exit_reconciliation(
        self,

        trade: ManagedTrade,

        broker_order: BrokerOrder,

    ) -> dict[str, Any]:
        """
        Update local trade state from a reconciled
        broker order.

        broker_order.filled_quantity must represent
        cumulative broker fill quantity.

        Local position quantity changes ONLY here.
        """

        if not trade.has_pending_exit:

            raise RuntimeError(
                "Trade has no pending exit order."
            )

        broker_order_id = str(
            broker_order.order_id
            or ""
        ).strip()

        if (
            broker_order_id
            != trade.pending_order_id
        ):

            raise ValueError(
                "Broker order does not match "
                "pending exit."
            )

        requested = int(
            trade.pending_requested_quantity
        )

        cumulative_filled = max(
            int(
                broker_order.filled_quantity
                or 0
            ),
            0,
        )

        if (
            cumulative_filled
            > requested
        ):

            raise ValueError(
                "Broker filled quantity exceeds "
                "requested exit quantity."
            )

        # ----------------------------------------------------
        # STALE BROKER SNAPSHOT
        # ----------------------------------------------------

        if (
            cumulative_filled
            < trade.pending_filled_quantity
        ):

            return {

                "updated": False,

                "reason": (
                    "Stale broker "
                    "reconciliation snapshot."
                ),
            }

        new_fill_quantity = (
            cumulative_filled
            - trade.pending_filled_quantity
        )

        action = (
            trade.pending_action
        )

        # ----------------------------------------------------
        # APPLY ONLY NEW FILLS
        # ----------------------------------------------------

        if new_fill_quantity > 0:

            if (
                new_fill_quantity
                > trade.remaining_quantity
            ):

                raise ValueError(
                    "Confirmed broker fill exceeds "
                    "remaining position."
                )

            fill_price = (
                float(
                    broker_order.filled_price
                )
                if broker_order.filled_price
                is not None
                else None
            )

            trade.remaining_quantity -= (
                new_fill_quantity
            )

            trade.realized_quantity += (
                new_fill_quantity
            )

            if fill_price is not None:
                realized_pnl_increment = (
                    (
                        fill_price
                        - trade.entry_price
                    )
                    * new_fill_quantity
                )

                trade.metadata[
                    "realized_pnl"
                ] = float(
                    trade.metadata.get(
                        "realized_pnl",
                        0.0,
                    )
                    or 0.0
                ) + realized_pnl_increment

                trade.metadata[
                    "realized_exit_value"
                ] = float(
                    trade.metadata.get(
                        "realized_exit_value",
                        0.0,
                    )
                    or 0.0
                ) + (
                    fill_price
                    * new_fill_quantity
                )

                trade.metadata[
                    "last_exit_fill_price"
                ] = fill_price

                trade.metadata[
                    "last_exit_fill_quantity"
                ] = new_fill_quantity

                entry_notional = (
                    trade.entry_price
                    * trade.initial_quantity
                )

                if entry_notional > 0:
                    trade.metadata[
                        "realized_pnl_pct"
                    ] = (
                        float(
                            trade.metadata[
                                "realized_pnl"
                            ]
                        )
                        / entry_notional
                        * 100.0
                    )

            if (
                action
                == TradeAction
                .TAKE_PROFIT_1
            ):

                trade.t1_realized_quantity += (
                    new_fill_quantity
                )

            elif (
                action
                == TradeAction
                .TAKE_PROFIT_2
            ):

                trade.t2_realized_quantity += (
                    new_fill_quantity
                )

            elif (
                action
                == TradeAction
                .TAKE_PROFIT_3
            ):

                trade.t3_realized_quantity += (
                    new_fill_quantity
                )

            trade.pending_filled_quantity = (
                cumulative_filled
            )

        trade.pending_status = (
            broker_order.status.value
        )

        # ----------------------------------------------------
        # TERMINAL?
        # ----------------------------------------------------

        terminal = (
            broker_order.status
            in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            }
        )

        fully_filled = (
            cumulative_filled
            >= requested
        )

        # ----------------------------------------------------
        # FINALIZE CONFIRMED TARGET ACTION
        # ----------------------------------------------------

        if (
            terminal
            and fully_filled
        ):

            # ================================================
            # T1
            # ================================================

            if (
                action
                == TradeAction
                .TAKE_PROFIT_1
            ):

                trade.t1_completed = True

                trade.stage = (
                    TradeStage.T1_HIT
                )

                # Profit Lock 1:
                # stop -> entry.
                trade.current_stop = max(
                    trade.current_stop,
                    trade.entry_price,
                )

            # ================================================
            # T2
            # ================================================

            elif (
                action
                == TradeAction
                .TAKE_PROFIT_2
            ):

                trade.t2_completed = True

                trade.stage = (
                    TradeStage.T2_HIT
                )

                # Profit Lock 2:
                # stop -> Target 1.
                trade.current_stop = max(
                    trade.current_stop,
                    trade.target_1,
                )

            # ================================================
            # T3
            # ================================================

            elif (
                action
                == TradeAction
                .TAKE_PROFIT_3
            ):

                trade.t3_completed = True

                trade.stage = (
                    TradeStage.T3_HIT
                )

                # Profit Lock 3:
                # stop -> Target 2.
                trade.current_stop = max(
                    trade.current_stop,
                    trade.target_2,
                )

                if (
                    trade.remaining_quantity
                    > 0
                ):

                    trade.trailing_active = True

                    trade.stage = (
                        TradeStage.RUNNER
                    )

                else:

                    trade.stage = (
                        TradeStage.CLOSED
                    )

            # ================================================
            # FULL POSITION EXIT
            # ================================================

            elif action in {

                TradeAction.EXIT_STOP,

                TradeAction.EXIT_TRAILING,

                # Handle old pending T3-close orders
                # created before Profit Lock V2.
                TradeAction.EXIT_TARGET_3,
            }:

                if (
                    trade.remaining_quantity
                    == 0
                ):

                    trade.stage = (
                        TradeStage.CLOSED
                    )

        # ----------------------------------------------------
        # IF NO POSITION REMAINS, ALWAYS CLOSED
        # ----------------------------------------------------

        if (
            trade.remaining_quantity
            == 0
        ):

            trade.trailing_active = False

            trade.stage = (
                TradeStage.CLOSED
            )

        # ----------------------------------------------------
        # SAVE V2 STATE INTO METADATA
        # ----------------------------------------------------

        self._sync_metadata(
            trade
        )

        # ----------------------------------------------------
        # CLEAR PENDING ONLY WHEN TERMINAL
        # ----------------------------------------------------

        if terminal:

            previous_action = (
                action
            )

            trade.metadata[
                "last_exit_action"
            ] = (
                previous_action.value
                if previous_action
                else None
            )

            trade.metadata[
                "last_exit_order_id"
            ] = (
                broker_order_id
            )

            trade.metadata[
                "last_exit_status"
            ] = (
                broker_order.status.value
            )

            trade.metadata[
                "last_exit_filled_quantity"
            ] = (
                cumulative_filled
            )

            self._clear_pending(
                trade
            )

            self._sync_metadata(
                trade
            )

        logger.info(
            "Exit reconciliation applied | "
            "symbol=%s status=%s "
            "new_fill=%s remaining=%s "
            "stage=%s stop=%s",

            trade.symbol,

            broker_order.status.value,

            new_fill_quantity,

            trade.remaining_quantity,

            trade.stage.value,

            trade.current_stop,
        )

        return {

            "updated": True,

            "new_fill_quantity": (
                new_fill_quantity
            ),

            "cumulative_filled": (
                cumulative_filled
            ),

            "remaining_quantity": (
                trade.remaining_quantity
            ),

            "terminal": (
                terminal
            ),

            "fully_filled": (
                fully_filled
            ),

            "stage": (
                trade.stage.value
            ),

            "t1_completed": (
                trade.t1_completed
            ),

            "t2_completed": (
                trade.t2_completed
            ),

            "t3_completed": (
                trade.t3_completed
            ),

            "t1_realized_quantity": (
                trade.t1_realized_quantity
            ),

            "t2_realized_quantity": (
                trade.t2_realized_quantity
            ),

            "t3_realized_quantity": (
                trade.t3_realized_quantity
            ),

            "current_stop": (
                trade.current_stop
            ),

            "trailing_active": (
                trade.trailing_active
            ),

            "runner_quantity": (
                trade.remaining_quantity
                if trade.trailing_active
                else 0
            ),
        }

    # ========================================================
    # CLEAR PENDING
    # ========================================================

    @staticmethod
    def _clear_pending(
        trade: ManagedTrade,
    ) -> None:

        trade.pending_action = None

        trade.pending_order_id = None

        trade.pending_requested_quantity = 0

        trade.pending_filled_quantity = 0

        trade.pending_status = None


# ============================================================
# LAZY SINGLETON
# ============================================================

trade_manager: Optional[
    TradeManager
] = None


def get_trade_manager(
) -> TradeManager:

    global trade_manager

    if trade_manager is None:

        trade_manager = (
            TradeManager()
        )

    return trade_manager