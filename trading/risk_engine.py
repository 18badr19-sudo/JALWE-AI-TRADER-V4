from __future__ import annotations

import logging
import math

from typing import Optional

from broker.alpaca_client import get_alpaca_client
from core.config import settings

from core.models import (
    FeatureSnapshot,
    RiskDecision,
    SignalAction,
)

from intelligence.ai_engine import AIAnalysis


logger = logging.getLogger(__name__)


class RiskEngine:
    """
    JALWE AI TRADER V4 - Small Account Risk Engine.

    PAPER TRADING ONLY.

    Starting model:
    - Strategy starting capital: $100
    - B setup:    0.50%
    - A setup:    1.00%
    - A+ setup:   1.50%
    - Hard automatic risk cap: 1.50%
    - Daily loss circuit breaker: 3%
    - Maximum open positions: 1
    - Maximum position allocation: 75%
    - No leverage
    - T1 = 2R
    - T2 = 3R
    - T3 = 4R

    Important:
    Alpaca paper account equity is NOT used as the
    strategy capital.

    strategy_equity represents JALWE's virtual account
    balance.

    Example:
        Alpaca paper equity = $100,000
        JALWE strategy equity = $100

        Position sizing uses $100.

    As profits accumulate, strategy_equity can later
    be supplied as $105, $120, $150, etc.
    """

    def __init__(
        self,
        strategy_starting_capital: float = 100.0,
        default_risk_pct: float = 1.0,
        maximum_risk_pct: float = 1.50,
        max_daily_loss_pct: float = 3.0,
        max_open_positions: int = 1,
        max_position_allocation_pct: float = 75.0,
        allocation_b_pct: float = 50.0,
        allocation_a_pct: float = 75.0,
        allocation_a_plus_pct: float = 100.0,
        minimum_signal_score: float = 82.0,
        minimum_price: float = 0.50,
        maximum_price: float = 100.0,
        atr_stop_multiplier: float = 1.5,
        maximum_stop_pct: float = 5.0,
        minimum_stop_pct: float = 0.50,
    ) -> None:

        self.strategy_starting_capital = float(
            strategy_starting_capital
        )

        self.default_risk_pct = float(
            default_risk_pct
        )

        self.maximum_risk_pct = float(
            maximum_risk_pct
        )

        self.max_daily_loss_pct = float(
            max_daily_loss_pct
        )

        self.max_open_positions = int(
            max_open_positions
        )

        self.max_position_allocation_pct = float(
            max_position_allocation_pct
        )

        self.allocation_b_pct = float(
            allocation_b_pct
        )

        self.allocation_a_pct = float(
            allocation_a_pct
        )

        self.allocation_a_plus_pct = float(
            allocation_a_plus_pct
        )

        self.minimum_signal_score = float(
            minimum_signal_score
        )

        self.minimum_price = float(
            minimum_price
        )

        self.maximum_price = float(
            maximum_price
        )

        self.atr_stop_multiplier = float(
            atr_stop_multiplier
        )

        self.maximum_stop_pct = float(
            maximum_stop_pct
        )

        self.minimum_stop_pct = float(
            minimum_stop_pct
        )

        self._validate_configuration()

    # ========================================================
    # CONFIGURATION
    # ========================================================

    def _validate_configuration(
        self,
    ) -> None:

        if self.strategy_starting_capital <= 0:

            raise ValueError(
                "strategy_starting_capital "
                "must be greater than zero."
            )

        if not (
            0
            < self.default_risk_pct
            <= self.maximum_risk_pct
        ):

            raise ValueError(
                "default_risk_pct is invalid."
            )

        if not (
            0
            < self.maximum_risk_pct
            <= 2.0
        ):

            raise ValueError(
                "maximum_risk_pct must be "
                "between 0 and 2."
            )

        if not (
            0
            < self.max_daily_loss_pct
            <= 10
        ):

            raise ValueError(
                "max_daily_loss_pct must be "
                "between 0 and 10."
            )

        if self.max_open_positions <= 0:

            raise ValueError(
                "max_open_positions must be "
                "greater than zero."
            )

        if not (
            0
            < self.max_position_allocation_pct
            <= 100
        ):

            raise ValueError(
                "max_position_allocation_pct "
                "must be between 0 and 100."
            )

        if not (
            0
            <= self.minimum_signal_score
            <= 100
        ):

            raise ValueError(
                "minimum_signal_score must be "
                "between 0 and 100."
            )

        if self.minimum_price <= 0:

            raise ValueError(
                "minimum_price must be "
                "greater than zero."
            )

        if (
            self.maximum_price
            <= self.minimum_price
        ):

            raise ValueError(
                "maximum_price must exceed "
                "minimum_price."
            )

        if not (
            0
            < self.minimum_stop_pct
            < self.maximum_stop_pct
        ):

            raise ValueError(
                "Invalid stop percentage limits."
            )

    # ========================================================
    # REJECTION HELPER
    # ========================================================

    @staticmethod
    def _reject(
        reason: str,
        **metadata,
    ) -> RiskDecision:

        return RiskDecision(
            approved=False,
            reason=reason,
            quantity=0,
            metadata=metadata,
        )

    # ========================================================
    # SETUP GRADE -> RISK
    # ========================================================

    @staticmethod
    def _grade_risk_pct(
        setup_grade: Optional[str],
    ) -> Optional[float]:

        if setup_grade is None:
            return None

        normalized = (
            str(setup_grade)
            .strip()
            .upper()
            .replace(" ", "")
        )

        if normalized in {
            "A+",
            "A_PLUS",
            "APLUS",
        }:
            return 1.50

        if normalized == "A":
            return 1.00

        if normalized == "B":
            return 0.50

        return None

    # ========================================================
    # RESOLVE REQUESTED RISK
    # ========================================================

    def _resolve_risk_pct(
        self,
        recommended_risk_pct: Optional[float],
        setup_grade: Optional[str],
    ) -> tuple[
        Optional[float],
        str,
        Optional[str],
    ]:
        """
        Priority:

        1. StrategyRouter final risk
        2. Setup grade
        3. Default risk

        StrategyRouter may reduce risk below 0.50%.
        It cannot exceed the system hard cap.
        """

        if recommended_risk_pct is not None:

            try:
                requested = float(
                    recommended_risk_pct
                )

            except (
                TypeError,
                ValueError,
            ):

                return (
                    None,
                    "router",
                    "Invalid recommended risk percentage.",
                )

            if requested <= 0:

                return (
                    None,
                    "router",
                    "Recommended risk is zero or negative.",
                )

            if (
                requested
                > self.maximum_risk_pct
            ):

                return (
                    None,
                    "router",
                    (
                        "Recommended risk exceeds "
                        "JALWE hard risk cap."
                    ),
                )

            return (
                requested,
                "strategy_router",
                None,
            )

        grade_risk = (
            self._grade_risk_pct(
                setup_grade
            )
        )

        if grade_risk is not None:

            if (
                grade_risk
                > self.maximum_risk_pct
            ):

                return (
                    None,
                    "grade",
                    "Grade risk exceeds hard cap.",
                )

            return (
                grade_risk,
                "setup_grade",
                None,
            )

        return (
            self.default_risk_pct,
            "default",
            None,
        )

    # ========================================================
    # DYNAMIC POSITION ALLOCATION
    # ========================================================

    def _allocation_pct_for_grade(
        self,
        setup_grade: Optional[str],
    ) -> float:
        normalized = (
            str(setup_grade or "")
            .strip()
            .upper()
            .replace(" ", "")
        )

        if normalized in {
            "A+",
            "A_PLUS",
            "APLUS",
        }:
            return min(
                100.0,
                self.allocation_a_plus_pct,
            )

        if normalized == "A":
            return min(
                100.0,
                self.allocation_a_pct,
            )

        if normalized == "B":
            return min(
                100.0,
                self.allocation_b_pct,
            )

        return min(
            100.0,
            self.max_position_allocation_pct,
        )

    # ========================================================
    # DAILY LOSS CIRCUIT BREAKER
    # ========================================================

    def _check_daily_loss(
        self,
        current_strategy_equity: float,
        daily_start_strategy_equity: float,
    ) -> tuple[
        bool,
        float,
    ]:

        if (
            daily_start_strategy_equity
            <= 0
        ):

            raise ValueError(
                "daily_start_equity must be "
                "greater than zero."
            )

        change_pct = (
            (
                current_strategy_equity
                - daily_start_strategy_equity
            )
            / daily_start_strategy_equity
            * 100.0
        )

        breached = (
            change_pct
            <= -self.max_daily_loss_pct
        )

        return (
            breached,
            change_pct,
        )

    # ========================================================
    # ATR STOP
    # ========================================================

    def _calculate_atr_stop(
        self,
        features: FeatureSnapshot,
        entry: float,
    ) -> Optional[float]:

        if (
            features.atr is None
            or features.atr <= 0
        ):

            return None

        atr_distance = (
            float(features.atr)
            * self.atr_stop_multiplier
        )

        minimum_distance = (
            entry
            * (
                self.minimum_stop_pct
                / 100.0
            )
        )

        maximum_distance = (
            entry
            * (
                self.maximum_stop_pct
                / 100.0
            )
        )

        stop_distance = max(
            atr_distance,
            minimum_distance,
        )

        stop_distance = min(
            stop_distance,
            maximum_distance,
        )

        stop = (
            entry
            - stop_distance
        )

        if (
            stop <= 0
            or stop >= entry
        ):

            return None

        return round(
            stop,
            4,
        )

    # ========================================================
    # EXTERNAL STRATEGY STOP
    # ========================================================

    def _validate_strategy_stop(
        self,
        entry: float,
        stop: float,
    ) -> tuple[
        bool,
        Optional[str],
        Optional[float],
    ]:

        if (
            stop <= 0
            or stop >= entry
        ):

            return (
                False,
                "Invalid strategy stop.",
                None,
            )

        stop_distance_pct = (
            (
                entry - stop
            )
            / entry
            * 100.0
        )

        if (
            stop_distance_pct
            < self.minimum_stop_pct
        ):

            return (
                False,
                (
                    "Strategy stop is too tight "
                    "for JALWE safety rules."
                ),
                stop_distance_pct,
            )

        if (
            stop_distance_pct
            > self.maximum_stop_pct
        ):

            return (
                False,
                (
                    "Strategy stop is too wide "
                    "for JALWE safety rules."
                ),
                stop_distance_pct,
            )

        return (
            True,
            None,
            stop_distance_pct,
        )

    # ========================================================
    # TARGETS
    # ========================================================

    @staticmethod
    def _calculate_targets(
        entry: float,
        stop: float,
    ) -> tuple[
        float,
        float,
        float,
    ]:
        """
        New JALWE target model:

        T1 = 2R
        T2 = 3R
        T3 = 4R

        TradeManager later performs:
        T1 -> stop to entry
        T2 -> stop to T1
        T3 -> stop to T2
        Runner -> trailing
        """

        risk_per_share = (
            entry - stop
        )

        target_1 = (
            entry
            + risk_per_share * 2.0
        )

        target_2 = (
            entry
            + risk_per_share * 3.0
        )

        target_3 = (
            entry
            + risk_per_share * 4.0
        )

        return (
            round(
                target_1,
                4,
            ),
            round(
                target_2,
                4,
            ),
            round(
                target_3,
                4,
            ),
        )

    # ========================================================
    # POSITION SIZING
    # ========================================================

    def _calculate_quantity(
        self,
        entry: float,
        stop: float,
        strategy_equity: float,
        available_cash: float,
        risk_pct: float,
        allocation_pct: float,
    ) -> tuple[
        int,
        float,
        float,
    ]:

        risk_per_share = (
            entry - stop
        )

        if risk_per_share <= 0:

            return (
                0,
                0.0,
                0.0,
            )

        # ----------------------------------------------------
        # RISK BUDGET
        # ----------------------------------------------------

        risk_budget = (
            strategy_equity
            * (
                risk_pct
                / 100.0
            )
        )

        # ----------------------------------------------------
        # MAX POSITION ALLOCATION
        # ----------------------------------------------------

        max_position_value = (
            strategy_equity
            * (
                allocation_pct
                / 100.0
            )
        )

        # ----------------------------------------------------
        # QUANTITY BY RISK
        # ----------------------------------------------------

        quantity_by_risk = (
            math.floor(
                risk_budget
                / risk_per_share
            )
        )

        # ----------------------------------------------------
        # QUANTITY BY ALLOCATION
        # ----------------------------------------------------

        quantity_by_allocation = (
            math.floor(
                max_position_value
                / entry
            )
        )

        # ----------------------------------------------------
        # QUANTITY BY CASH
        # ----------------------------------------------------

        # No leverage.
        quantity_by_cash = (
            math.floor(
                available_cash
                / entry
            )
        )

        quantity = min(
            quantity_by_risk,
            quantity_by_allocation,
            quantity_by_cash,
        )

        quantity = max(
            quantity,
            0,
        )

        actual_risk = (
            quantity
            * risk_per_share
        )

        position_value = (
            quantity
            * entry
        )

        return (
            quantity,
            actual_risk,
            position_value,
        )

    # ========================================================
    # MAIN RISK EVALUATION
    # ========================================================

    def evaluate(
        self,
        features: FeatureSnapshot,
        ai_analysis: AIAnalysis,
        daily_start_equity: Optional[float],
        *,
        strategy_equity: Optional[float] = None,
        recommended_risk_pct: Optional[float] = None,
        setup_grade: Optional[str] = None,
        confirmed_entry_price: Optional[float] = None,
        confirmed_stop_price: Optional[float] = None,
        confirmation_approved: Optional[bool] = None,
    ) -> RiskDecision:
        """
        daily_start_equity:
            JALWE virtual equity at start of day.

            Example:
                $100.00

            NOT Alpaca's $100,000 paper balance.

        strategy_equity:
            Current JALWE virtual equity.

            Examples:
                $100
                $103.20
                $150

        recommended_risk_pct:
            Normally comes from StrategyRouter.

            Examples:
                0.50
                0.70
                1.00
                1.50

        confirmed_entry_price / confirmed_stop_price:
            Use Trigger / Breakout Confirmation levels
            when available.
        """

        # ====================================================
        # DATA QUALITY
        # ====================================================

        if not features.data_quality_ok:

            return self._reject(
                "Feature data quality failed."
            )

        if features.data_is_stale:

            return self._reject(
                "Market data is stale."
            )

        # ====================================================
        # BREAKOUT CONFIRMATION GATE
        # ====================================================

        if confirmation_approved is False:

            return self._reject(
                "Breakout confirmation rejected entry."
            )

        # ====================================================
        # AI SIGNAL
        # ====================================================

        if (
            ai_analysis.action
            != SignalAction.BUY
        ):

            return self._reject(
                "AI signal is not BUY.",

                ai_action=(
                    ai_analysis.action.value
                ),

                ai_score=(
                    ai_analysis.score
                ),
            )

        if (
            ai_analysis.score
            < self.minimum_signal_score
        ):

            return self._reject(
                "AI score is below minimum threshold.",

                ai_score=(
                    ai_analysis.score
                ),

                required_score=(
                    self.minimum_signal_score
                ),
            )

        # ====================================================
        # ENTRY PRICE
        # ====================================================

        if confirmed_entry_price is not None:

            try:
                entry = float(
                    confirmed_entry_price
                )

            except (
                TypeError,
                ValueError,
            ):

                return self._reject(
                    "Invalid confirmed entry price."
                )

        else:

            if (
                features.price is None
                or features.price <= 0
            ):

                return self._reject(
                    "Invalid entry price."
                )

            entry = float(
                features.price
            )

        if entry <= 0:

            return self._reject(
                "Invalid entry price."
            )

        # ====================================================
        # PRICE RANGE
        # ====================================================

        if entry < self.minimum_price:

            return self._reject(
                "Stock price is below allowed minimum.",
                price=entry,
                minimum_price=(
                    self.minimum_price
                ),
            )

        if entry > self.maximum_price:

            return self._reject(
                "Stock price is above allowed maximum.",
                price=entry,
                maximum_price=(
                    self.maximum_price
                ),
            )

        # ====================================================
        # BROKER ACCOUNT
        # ====================================================

        try:

            broker = (
                get_alpaca_client()
            )

            account = (
                broker
                .get_account_snapshot()
            )

            positions = (
                broker
                .get_all_positions()
            )

        except Exception as exc:

            logger.exception(
                "RiskEngine broker check failed."
            )

            return self._reject(
                "Broker account data unavailable.",
                error=str(exc),
            )

        broker_equity = float(
            account["equity"]
        )

        broker_cash = float(
            account["cash"]
        )

        if (
            broker_equity <= 0
            or broker_cash < 0
        ):

            return self._reject(
                "Invalid broker account values."
            )

        # ====================================================
        # JALWE VIRTUAL STRATEGY EQUITY
        # ====================================================

        if strategy_equity is None:

            current_strategy_equity = (
                self.strategy_starting_capital
            )

        else:

            try:
                current_strategy_equity = float(
                    strategy_equity
                )

            except (
                TypeError,
                ValueError,
            ):

                return self._reject(
                    "Invalid strategy equity."
                )

        if current_strategy_equity <= 0:

            return self._reject(
                "Strategy equity is depleted."
            )

        # Broker must actually have enough equity
        # to support JALWE virtual equity.
        effective_equity = min(
            current_strategy_equity,
            broker_equity,
        )

        # No leverage.
        available_cash = min(
            broker_cash,
            effective_equity,
        )

        if available_cash <= 0:

            return self._reject(
                "No available strategy cash."
            )

        # ====================================================
        # MAX OPEN POSITIONS
        # ====================================================

        open_position_count = len(
            positions
        )

        if (
            open_position_count
            >= self.max_open_positions
        ):

            return self._reject(
                "Maximum open positions reached.",

                open_positions=(
                    open_position_count
                ),

                maximum=(
                    self.max_open_positions
                ),
            )

        # ====================================================
        # DUPLICATE SYMBOL
        # ====================================================

        symbol = (
            features.symbol
            .strip()
            .upper()
        )

        for position in positions:

            broker_symbol = str(
                getattr(
                    position,
                    "symbol",
                    "",
                )
            ).upper()

            if broker_symbol == symbol:

                return self._reject(
                    "Position already exists for symbol.",
                    symbol=symbol,
                )

        # ====================================================
        # DAILY LOSS LIMIT
        # ====================================================

        if daily_start_equity is None:

            return self._reject(
                "Daily start strategy equity is "
                "required to enforce daily loss limit."
            )

        try:

            daily_start_equity = float(
                daily_start_equity
            )

        except (
            TypeError,
            ValueError,
        ):

            return self._reject(
                "Invalid daily start strategy equity."
            )

        try:

            (
                daily_loss_breached,
                daily_change_pct,
            ) = self._check_daily_loss(
                current_strategy_equity=(
                    effective_equity
                ),

                daily_start_strategy_equity=(
                    daily_start_equity
                ),
            )

        except ValueError as exc:

            return self._reject(
                str(exc)
            )

        if daily_loss_breached:

            return self._reject(
                "Daily loss circuit breaker triggered.",

                daily_change_pct=round(
                    daily_change_pct,
                    4,
                ),

                max_daily_loss_pct=(
                    self.max_daily_loss_pct
                ),
            )

        # ====================================================
        # RESOLVE RISK %
        # ====================================================

        (
            selected_risk_pct,
            risk_source,
            risk_error,
        ) = self._resolve_risk_pct(
            recommended_risk_pct=(
                recommended_risk_pct
            ),

            setup_grade=(
                setup_grade
            ),
        )

        if (
            risk_error is not None
            or selected_risk_pct is None
        ):

            return self._reject(
                risk_error
                or "Unable to resolve trade risk."
            )

        # ====================================================
        # STOP
        # ====================================================

        stop_source = "ATR"

        if confirmed_stop_price is not None:

            try:
                stop = float(
                    confirmed_stop_price
                )

            except (
                TypeError,
                ValueError,
            ):

                return self._reject(
                    "Invalid confirmed stop price."
                )

            (
                stop_valid,
                stop_error,
                stop_distance_pct,
            ) = self._validate_strategy_stop(
                entry=entry,
                stop=stop,
            )

            if not stop_valid:

                return self._reject(
                    stop_error
                    or "Invalid strategy stop.",

                    entry=entry,
                    stop=stop,

                    stop_distance_pct=(
                        round(
                            stop_distance_pct,
                            4,
                        )
                        if stop_distance_pct
                        is not None
                        else None
                    ),
                )

            stop = round(
                stop,
                4,
            )

            stop_source = (
                "STRATEGY_CONFIRMATION"
            )

        else:

            stop = self._calculate_atr_stop(
                features=features,
                entry=entry,
            )

            if stop is None:

                return self._reject(
                    "Unable to calculate "
                    "valid stop loss."
                )

            stop_distance_pct = (
                (
                    entry - stop
                )
                / entry
                * 100.0
            )

        # ====================================================
        # POSITION SIZE
        # ====================================================

        allocation_pct = (
            self._allocation_pct_for_grade(
                setup_grade
            )
        )

        (
            quantity,
            actual_risk,
            position_value,
        ) = self._calculate_quantity(
            entry=entry,
            stop=stop,

            strategy_equity=(
                effective_equity
            ),

            available_cash=(
                available_cash
            ),

            risk_pct=(
                selected_risk_pct
            ),

            allocation_pct=(
                allocation_pct
            ),
        )

        if quantity <= 0:

            return self._reject(
                "Position size calculated as zero.",

                entry=entry,
                stop=stop,

                strategy_equity=(
                    effective_equity
                ),

                requested_risk_pct=(
                    selected_risk_pct
                ),

                max_position_allocation_pct=(
                    allocation_pct
                ),
            )

        # ====================================================
        # TARGETS
        # ====================================================

        (
            target_1,
            target_2,
            target_3,
        ) = self._calculate_targets(
            entry=entry,
            stop=stop,
        )

        # ====================================================
        # ACTUAL RISK %
        # ====================================================

        actual_risk_pct = (
            (
                actual_risk
                / effective_equity
            )
            * 100.0
        )

        # ====================================================
        # SAFETY ASSERTIONS
        # ====================================================

        if (
            actual_risk_pct
            > self.maximum_risk_pct
            + 0.0001
        ):

            return self._reject(
                "Calculated trade risk exceeded "
                "hard risk limit.",

                actual_risk_pct=(
                    actual_risk_pct
                ),

                maximum_risk_pct=(
                    self.maximum_risk_pct
                ),
            )

        if (
            position_value
            > effective_equity
        ):

            return self._reject(
                "Position would exceed "
                "JALWE strategy equity."
            )

        if (
            position_value
            > available_cash
        ):

            return self._reject(
                "Position would exceed "
                "available cash."
            )

        # ====================================================
        # FINAL APPROVAL
        # ====================================================

        return RiskDecision(
            approved=True,

            reason=(
                "JALWE small-account risk "
                "checks passed."
            ),

            quantity=quantity,

            risk_amount=round(
                actual_risk,
                4,
            ),

            risk_pct=round(
                actual_risk_pct,
                4,
            ),

            position_value=round(
                position_value,
                4,
            ),

            stop_price=stop,

            target_1=target_1,
            target_2=target_2,
            target_3=target_3,

            metadata={
                "symbol": symbol,

                "entry_price": (
                    entry
                ),

                "broker_equity": (
                    broker_equity
                ),

                "broker_cash": (
                    broker_cash
                ),

                "strategy_starting_capital": (
                    self.strategy_starting_capital
                ),

                "strategy_equity": (
                    effective_equity
                ),

                "available_strategy_cash": (
                    available_cash
                ),

                "daily_start_strategy_equity": (
                    daily_start_equity
                ),

                "daily_change_pct": round(
                    daily_change_pct,
                    4,
                ),

                "open_positions": (
                    open_position_count
                ),

                "requested_risk_pct": (
                    selected_risk_pct
                ),

                "risk_source": (
                    risk_source
                ),

                "actual_risk_pct": round(
                    actual_risk_pct,
                    4,
                ),

                "max_risk_pct": (
                    self.maximum_risk_pct
                ),

                "max_daily_loss_pct": (
                    self.max_daily_loss_pct
                ),

                "max_position_allocation_pct": (
                    allocation_pct
                ),

                "allocation_model": (
                    "B=50%,A=75%,A+=100%"
                ),

                "stop_source": (
                    stop_source
                ),

                "stop_distance_pct": round(
                    stop_distance_pct,
                    4,
                ),

                "target_model": (
                    "T1=2R,T2=3R,T3=4R"
                ),

                "paper_trading": True,
            },
        )


# ============================================================
# LAZY SINGLETON
# ============================================================

risk_engine: Optional[
    RiskEngine
] = None


def get_risk_engine() -> RiskEngine:

    global risk_engine

    if risk_engine is None:

        risk_engine = RiskEngine(
            strategy_starting_capital=(
                settings.STRATEGY_STARTING_CAPITAL
            ),
            default_risk_pct=(
                settings.RISK_PER_TRADE_PCT
            ),
            maximum_risk_pct=(
                settings.MAX_RISK_PER_TRADE_PCT
            ),
            max_daily_loss_pct=(
                settings.MAX_DAILY_LOSS_PCT
            ),
            max_open_positions=(
                settings.MAX_OPEN_POSITIONS
            ),
            max_position_allocation_pct=(
                settings.MAX_POSITION_ALLOCATION_PCT
            ),
            allocation_b_pct=(
                settings.ALLOCATION_B_PCT
            ),
            allocation_a_pct=(
                settings.ALLOCATION_A_PCT
            ),
            allocation_a_plus_pct=(
                settings.ALLOCATION_A_PLUS_PCT
            ),
            minimum_signal_score=(
                settings.MIN_SIGNAL_SCORE
            ),
            minimum_price=(
                settings.MIN_STOCK_PRICE
            ),
            maximum_price=(
                settings.MAX_STOCK_PRICE
            ),
            atr_stop_multiplier=(
                settings.DEFAULT_STOP_ATR_MULTIPLIER
            ),
        )

    return risk_engine