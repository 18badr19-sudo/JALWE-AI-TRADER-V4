from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd


class TriggerState(str, Enum):
    REJECTED = "REJECTED"
    SETUP_FOUND = "SETUP_FOUND"
    WAITING_FOR_TRIGGER = "WAITING_FOR_TRIGGER"
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
    TOO_LATE = "TOO_LATE"
    INVALIDATED = "INVALIDATED"


@dataclass
class TriggerDecision:
    symbol: str

    state: TriggerState

    approved_for_entry: bool

    current_price: Optional[float]
    trigger_price: Optional[float]
    stop_price: Optional[float]

    distance_to_trigger_pct: Optional[float] = None
    chase_pct: Optional[float] = None

    bars_since_breakout: Optional[int] = None

    reason: str = ""

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


class TriggerEngine:
    """
    JALWE V4 - Trigger Intelligence Engine.

    Converts a valid strategy setup into a precise
    pre-entry state.

    States:
    - REJECTED
    - SETUP_FOUND
    - WAITING_FOR_TRIGGER
    - ENTRY_CONFIRMED
    - TOO_LATE
    - INVALIDATED

    This engine NEVER places orders.
    """

    def __init__(
        self,
        waiting_zone_pct: float = 2.0,
        max_chase_pct: float = 1.50,
        max_bars_after_breakout: int = 2,
    ) -> None:

        self.waiting_zone_pct = float(
            waiting_zone_pct
        )

        self.max_chase_pct = float(
            max_chase_pct
        )

        self.max_bars_after_breakout = int(
            max_bars_after_breakout
        )

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

            value = float(value)

            if pd.isna(value):
                return None

            return value

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _distance_pct(
        current: float,
        trigger: float,
    ) -> float:

        return (
            (current - trigger)
            / trigger
            * 100.0
        )

    @staticmethod
    def _extract_last_price(
        bars: Optional[pd.DataFrame],
    ) -> Optional[float]:

        if (
            bars is None
            or bars.empty
            or "close" not in bars.columns
        ):
            return None

        closes = pd.to_numeric(
            bars["close"],
            errors="coerce",
        ).dropna()

        if closes.empty:
            return None

        return float(
            closes.iloc[-1]
        )

    # ========================================================
    # BREAKOUT AGE
    # ========================================================

    @staticmethod
    def _bars_since_breakout(
        bars: Optional[pd.DataFrame],
        trigger: float,
    ) -> Optional[int]:

        if (
            bars is None
            or bars.empty
            or "close" not in bars.columns
        ):
            return None

        closes = pd.to_numeric(
            bars["close"],
            errors="coerce",
        ).dropna()

        if len(closes) < 2:
            return None

        values = closes.tolist()

        last_cross_index = None

        for i in range(
            1,
            len(values),
        ):

            previous = float(
                values[i - 1]
            )

            current = float(
                values[i]
            )

            if (
                previous < trigger
                and current >= trigger
            ):
                last_cross_index = i

        if last_cross_index is None:
            return None

        return (
            len(values)
            - 1
            - last_cross_index
        )

    # ========================================================
    # MAIN EVALUATION
    # ========================================================

    def evaluate(
        self,
        analysis: Any,
        current_price: Optional[float] = None,
        bars: Optional[pd.DataFrame] = None,
    ) -> TriggerDecision:

        symbol = str(
            getattr(
                analysis,
                "symbol",
                "",
            )
        ).upper().strip()

        approved = bool(
            getattr(
                analysis,
                "approved",
                False,
            )
        )

        trigger = self._safe_float(
            getattr(
                analysis,
                "trigger_price",
                None,
            )
        )

        stop = self._safe_float(
            getattr(
                analysis,
                "invalidation_price",
                None,
            )
        )

        current = self._safe_float(
            current_price
        )

        if current is None:
            current = (
                self._extract_last_price(
                    bars
                )
            )

        # ====================================================
        # STRATEGY REJECTED
        # ====================================================

        if not approved:

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.REJECTED
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                reason=(
                    "Strategy engine rejected "
                    "the setup."
                ),
            )

        # ====================================================
        # MISSING LEVELS
        # ====================================================

        if (
            current is None
            or trigger is None
            or stop is None
            or trigger <= 0
            or stop <= 0
        ):

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.REJECTED
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                reason=(
                    "Trigger, stop, or current "
                    "price is unavailable."
                ),
            )

        # ====================================================
        # INVALIDATED
        # ====================================================

        if current <= stop:

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.INVALIDATED
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                reason=(
                    "Price reached the setup "
                    "invalidation level before entry."
                ),
            )

        distance = self._distance_pct(
            current,
            trigger,
        )

        # ====================================================
        # BELOW TRIGGER
        # ====================================================

        if current < trigger:

            distance_below = abs(
                distance
            )

            if (
                distance_below
                <= self.waiting_zone_pct
            ):

                return TriggerDecision(
                    symbol=symbol,

                    state=(
                        TriggerState
                        .WAITING_FOR_TRIGGER
                    ),

                    approved_for_entry=False,

                    current_price=current,
                    trigger_price=trigger,
                    stop_price=stop,

                    distance_to_trigger_pct=(
                        round(
                            distance,
                            3,
                        )
                    ),

                    reason=(
                        "Valid setup is close to "
                        "its breakout trigger."
                    ),
                )

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.SETUP_FOUND
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                distance_to_trigger_pct=(
                    round(
                        distance,
                        3,
                    )
                ),

                reason=(
                    "Valid setup found but price "
                    "is not yet near the trigger."
                ),
            )

        # ====================================================
        # ABOVE TRIGGER
        # ====================================================

        chase_pct = max(
            0.0,
            distance,
        )

        if (
            chase_pct
            > self.max_chase_pct
        ):

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.TOO_LATE
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                chase_pct=round(
                    chase_pct,
                    3,
                ),

                reason=(
                    "Price is too extended above "
                    "the trigger. Do not chase."
                ),
            )

        # ====================================================
        # CHECK BREAKOUT RECENCY
        # ====================================================

        bars_since = (
            self._bars_since_breakout(
                bars,
                trigger,
            )
        )

        if (
            bars_since is not None
            and bars_since
            > self.max_bars_after_breakout
        ):

            return TriggerDecision(
                symbol=symbol,

                state=(
                    TriggerState.TOO_LATE
                ),

                approved_for_entry=False,

                current_price=current,
                trigger_price=trigger,
                stop_price=stop,

                chase_pct=round(
                    chase_pct,
                    3,
                ),

                bars_since_breakout=(
                    bars_since
                ),

                reason=(
                    "Breakout happened too many "
                    "bars ago. Entry window expired."
                ),
            )

        # ====================================================
        # ENTRY CONFIRMED
        # ====================================================

        return TriggerDecision(
            symbol=symbol,

            state=(
                TriggerState
                .ENTRY_CONFIRMED
            ),

            approved_for_entry=True,

            current_price=current,
            trigger_price=trigger,
            stop_price=stop,

            distance_to_trigger_pct=(
                round(
                    distance,
                    3,
                )
            ),

            chase_pct=round(
                chase_pct,
                3,
            ),

            bars_since_breakout=(
                bars_since
            ),

            reason=(
                "Trigger crossed inside the "
                "permitted entry zone."
            ),

            metadata={
                "waiting_zone_pct": (
                    self.waiting_zone_pct
                ),

                "max_chase_pct": (
                    self.max_chase_pct
                ),

                "max_bars_after_breakout": (
                    self.max_bars_after_breakout
                ),
            },
        )


_trigger_engine: Optional[
    TriggerEngine
] = None


def get_trigger_engine(
) -> TriggerEngine:

    global _trigger_engine

    if _trigger_engine is None:
        _trigger_engine = (
            TriggerEngine()
        )

    return _trigger_engine