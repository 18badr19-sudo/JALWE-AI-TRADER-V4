from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from intelligence.trigger_engine import (
    TriggerDecision,
    TriggerState,
)


class BreakoutConfirmationState(str, Enum):
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"
    CONFIRMED = "CONFIRMED"
    FAKE_BREAKOUT = "FAKE_BREAKOUT"
    WEAK_BREAKOUT = "WEAK_BREAKOUT"
    TOO_EXTENDED = "TOO_EXTENDED"


@dataclass
class BreakoutConfirmation:
    symbol: str

    state: BreakoutConfirmationState

    approved_for_entry: bool

    trigger_price: Optional[float]
    stop_price: Optional[float]

    close_price: Optional[float] = None

    volume_ratio: Optional[float] = None

    candle_body_ratio: Optional[float] = None

    upper_wick_ratio: Optional[float] = None

    close_position_ratio: Optional[float] = None

    extension_pct: Optional[float] = None

    score: float = 0.0

    reason: str = ""

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


class BreakoutConfirmationEngine:
    """
    JALWE V4 - Breakout Confirmation Engine

    Confirms whether a breakout is strong enough
    for entry.

    Checks:
    - Strategy/Trigger already approved
    - Candle closes above trigger
    - Volume expansion
    - Candle body strength
    - Upper wick rejection
    - Close location inside candle
    - Chase / extension distance

    This engine NEVER places orders.
    """

    def __init__(
        self,
        minimum_score: float = 70.0,
        minimum_volume_ratio: float = 1.20,
        minimum_body_ratio: float = 0.40,
        maximum_upper_wick_ratio: float = 0.35,
        minimum_close_position: float = 0.60,
        confirmation_buffer_pct: float = 0.05,
        maximum_extension_pct: float = 1.50,
        volume_lookback: int = 20,
    ) -> None:

        self.minimum_score = float(
            minimum_score
        )

        self.minimum_volume_ratio = float(
            minimum_volume_ratio
        )

        self.minimum_body_ratio = float(
            minimum_body_ratio
        )

        self.maximum_upper_wick_ratio = float(
            maximum_upper_wick_ratio
        )

        self.minimum_close_position = float(
            minimum_close_position
        )

        self.confirmation_buffer_pct = float(
            confirmation_buffer_pct
        )

        self.maximum_extension_pct = float(
            maximum_extension_pct
        )

        self.volume_lookback = int(
            volume_lookback
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
    def _clamp(
        value: float,
    ) -> float:

        return max(
            0.0,
            min(
                100.0,
                float(value),
            ),
        )

    # ========================================================
    # PREPARE BARS
    # ========================================================

    def _prepare_bars(
        self,
        bars: pd.DataFrame,
    ) -> pd.DataFrame:

        if bars is None or bars.empty:

            raise ValueError(
                "Breakout confirmation bars are empty."
            )

        required = {
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        missing = required - set(
            bars.columns
        )

        if missing:

            raise ValueError(
                "Missing required columns: "
                + ", ".join(
                    sorted(missing)
                )
            )

        data = bars.copy()

        for column in required:

            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        if len(data) < 5:

            raise ValueError(
                "Not enough bars for breakout confirmation."
            )

        return data.sort_index()

    # ========================================================
    # VOLUME RATIO
    # ========================================================

    def _volume_ratio(
        self,
        data: pd.DataFrame,
    ) -> Optional[float]:

        if len(data) < 3:
            return None

        current_volume = float(
            data["volume"].iloc[-1]
        )

        history = (
            data["volume"]
            .iloc[:-1]
            .tail(
                self.volume_lookback
            )
        )

        history = history[
            history > 0
        ]

        if history.empty:
            return None

        average_volume = float(
            history.mean()
        )

        if average_volume <= 0:
            return None

        return (
            current_volume
            / average_volume
        )

    # ========================================================
    # CANDLE STRUCTURE
    # ========================================================

    @staticmethod
    def _candle_metrics(
        row: pd.Series,
    ) -> dict[str, float]:

        open_price = float(
            row["open"]
        )

        high = float(
            row["high"]
        )

        low = float(
            row["low"]
        )

        close = float(
            row["close"]
        )

        candle_range = (
            high - low
        )

        if candle_range <= 0:

            return {
                "body_ratio": 0.0,
                "upper_wick_ratio": 1.0,
                "close_position_ratio": 0.0,
            }

        body = abs(
            close - open_price
        )

        upper_wick = (
            high
            - max(
                open_price,
                close,
            )
        )

        close_position = (
            (close - low)
            / candle_range
        )

        return {
            "body_ratio": (
                body
                / candle_range
            ),

            "upper_wick_ratio": (
                max(
                    0.0,
                    upper_wick,
                )
                / candle_range
            ),

            "close_position_ratio": (
                close_position
            ),
        }

    # ========================================================
    # MAIN EVALUATION
    # ========================================================

    def evaluate(
        self,
        trigger: TriggerDecision,
        bars: pd.DataFrame,
        features: Optional[Any] = None,
    ) -> BreakoutConfirmation:

        symbol = trigger.symbol

        trigger_price = (
            self._safe_float(
                trigger.trigger_price
            )
        )

        stop_price = (
            self._safe_float(
                trigger.stop_price
            )
        )

        # ----------------------------------------------------
        # TRIGGER MUST ALREADY BE ENTRY CONFIRMED
        # ----------------------------------------------------

        if (
            trigger.state
            != TriggerState.ENTRY_CONFIRMED
            or not trigger.approved_for_entry
        ):

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState.BLOCKED
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                reason=(
                    "TriggerEngine has not approved "
                    "this setup for entry."
                ),
            )

        if (
            trigger_price is None
            or trigger_price <= 0
        ):

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState.BLOCKED
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                reason=(
                    "Invalid breakout trigger price."
                ),
            )

        # ----------------------------------------------------
        # LOAD BARS
        # ----------------------------------------------------

        try:
            data = self._prepare_bars(
                bars
            )

        except Exception as exc:

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState.BLOCKED
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                reason=str(exc),
            )

        last = data.iloc[-1]

        open_price = float(
            last["open"]
        )

        high = float(
            last["high"]
        )

        low = float(
            last["low"]
        )

        close = float(
            last["close"]
        )

        metrics = (
            self._candle_metrics(
                last
            )
        )

        body_ratio = metrics[
            "body_ratio"
        ]

        upper_wick_ratio = metrics[
            "upper_wick_ratio"
        ]

        close_position = metrics[
            "close_position_ratio"
        ]

        volume_ratio = (
            self._volume_ratio(
                data
            )
        )

        extension_pct = (
            (close - trigger_price)
            / trigger_price
            * 100.0
        )

        warnings: list[str] = []
        reasons: list[str] = []

        # ====================================================
        # FAKE BREAKOUT
        # ====================================================

        if (
            high >= trigger_price
            and close < trigger_price
        ):

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState
                    .FAKE_BREAKOUT
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                close_price=close,

                volume_ratio=volume_ratio,

                candle_body_ratio=(
                    body_ratio
                ),

                upper_wick_ratio=(
                    upper_wick_ratio
                ),

                close_position_ratio=(
                    close_position
                ),

                extension_pct=(
                    extension_pct
                ),

                score=0.0,

                reason=(
                    "Price traded above the trigger "
                    "but closed back below it."
                ),
            )

        # ====================================================
        # WAITING FOR CANDLE CLOSE ABOVE TRIGGER
        # ====================================================

        required_close = (
            trigger_price
            * (
                1.0
                + self.confirmation_buffer_pct
                / 100.0
            )
        )

        if close < required_close:

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState.WAITING
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                close_price=close,

                volume_ratio=volume_ratio,

                candle_body_ratio=(
                    body_ratio
                ),

                upper_wick_ratio=(
                    upper_wick_ratio
                ),

                close_position_ratio=(
                    close_position
                ),

                extension_pct=(
                    extension_pct
                ),

                reason=(
                    "Breakout candle has not closed "
                    "far enough above the trigger."
                ),
            )

        # ====================================================
        # TOO EXTENDED
        # ====================================================

        if (
            extension_pct
            > self.maximum_extension_pct
        ):

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState
                    .TOO_EXTENDED
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                close_price=close,

                volume_ratio=volume_ratio,

                candle_body_ratio=(
                    body_ratio
                ),

                upper_wick_ratio=(
                    upper_wick_ratio
                ),

                close_position_ratio=(
                    close_position
                ),

                extension_pct=(
                    extension_pct
                ),

                reason=(
                    "Breakout candle is too extended "
                    "above the trigger."
                ),
            )

        # ====================================================
        # SCORE BREAKOUT QUALITY
        # ====================================================

        score = 0.0

        # ----------------------------------------------------
        # CLOSE ABOVE TRIGGER
        # ----------------------------------------------------

        score += 25.0

        reasons.append(
            "Candle closed above breakout trigger."
        )

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        if volume_ratio is None:

            warnings.append(
                "Volume ratio unavailable."
            )

        elif volume_ratio >= 2.0:

            score += 25.0

            reasons.append(
                "Exceptional breakout volume."
            )

        elif volume_ratio >= 1.50:

            score += 20.0

            reasons.append(
                "Strong breakout volume."
            )

        elif volume_ratio >= (
            self.minimum_volume_ratio
        ):

            score += 15.0

        else:

            warnings.append(
                "Breakout volume is weak."
            )

        # ----------------------------------------------------
        # BODY STRENGTH
        # ----------------------------------------------------

        if body_ratio >= 0.65:

            score += 20.0

            reasons.append(
                "Strong breakout candle body."
            )

        elif body_ratio >= (
            self.minimum_body_ratio
        ):

            score += 14.0

        else:

            warnings.append(
                "Breakout candle body is weak."
            )

        # ----------------------------------------------------
        # UPPER WICK
        # ----------------------------------------------------

        if upper_wick_ratio <= 0.15:

            score += 15.0

            reasons.append(
                "Minimal upper-wick rejection."
            )

        elif upper_wick_ratio <= (
            self.maximum_upper_wick_ratio
        ):

            score += 9.0

        else:

            warnings.append(
                "Large upper wick indicates "
                "possible rejection."
            )

        # ----------------------------------------------------
        # CLOSE LOCATION
        # ----------------------------------------------------

        if close_position >= 0.80:

            score += 15.0

            reasons.append(
                "Candle closed near its high."
            )

        elif close_position >= (
            self.minimum_close_position
        ):

            score += 10.0

        else:

            warnings.append(
                "Candle did not close strongly "
                "near its high."
            )

        # ====================================================
        # FEATURE CONFIRMATION
        # ====================================================

        if features is not None:

            above_vwap = bool(
                getattr(
                    features,
                    "above_vwap",
                    False,
                )
            )

            rvol = self._safe_float(
                getattr(
                    features,
                    "rvol",
                    None,
                )
            )

            if above_vwap:

                score += 5.0

                reasons.append(
                    "Price structure remains "
                    "above VWAP."
                )

            else:

                score -= 10.0

                warnings.append(
                    "Price is below VWAP."
                )

            if (
                rvol is not None
                and rvol >= 3.0
            ):

                score += 5.0

        score = self._clamp(
            score
        )

        # ====================================================
        # HARD QUALITY CONDITIONS
        # ====================================================

        strong_volume = (
            volume_ratio is not None
            and volume_ratio
            >= self.minimum_volume_ratio
        )

        strong_body = (
            body_ratio
            >= self.minimum_body_ratio
        )

        acceptable_wick = (
            upper_wick_ratio
            <= self.maximum_upper_wick_ratio
        )

        strong_close = (
            close_position
            >= self.minimum_close_position
        )

        approved = (
            score >= self.minimum_score
            and strong_volume
            and strong_body
            and acceptable_wick
            and strong_close
        )

        if not approved:

            return BreakoutConfirmation(
                symbol=symbol,

                state=(
                    BreakoutConfirmationState
                    .WEAK_BREAKOUT
                ),

                approved_for_entry=False,

                trigger_price=trigger_price,
                stop_price=stop_price,

                close_price=close,

                volume_ratio=(
                    round(
                        volume_ratio,
                        3,
                    )
                    if volume_ratio
                    is not None
                    else None
                ),

                candle_body_ratio=(
                    round(
                        body_ratio,
                        3,
                    )
                ),

                upper_wick_ratio=(
                    round(
                        upper_wick_ratio,
                        3,
                    )
                ),

                close_position_ratio=(
                    round(
                        close_position,
                        3,
                    )
                ),

                extension_pct=(
                    round(
                        extension_pct,
                        3,
                    )
                ),

                score=round(
                    score,
                    2,
                ),

                reason=(
                    "Breakout crossed the trigger "
                    "but confirmation quality is "
                    "not strong enough."
                ),

                warnings=warnings,

                metadata={
                    "open": open_price,
                    "high": high,
                    "low": low,
                },
            )

        # ====================================================
        # CONFIRMED BREAKOUT
        # ====================================================

        return BreakoutConfirmation(
            symbol=symbol,

            state=(
                BreakoutConfirmationState.CONFIRMED
            ),

            approved_for_entry=True,

            trigger_price=trigger_price,
            stop_price=stop_price,

            close_price=close,

            volume_ratio=(
                round(
                    volume_ratio,
                    3,
                )
                if volume_ratio
                is not None
                else None
            ),

            candle_body_ratio=(
                round(
                    body_ratio,
                    3,
                )
            ),

            upper_wick_ratio=(
                round(
                    upper_wick_ratio,
                    3,
                )
            ),

            close_position_ratio=(
                round(
                    close_position,
                    3,
                )
            ),

            extension_pct=(
                round(
                    extension_pct,
                    3,
                )
            ),

            score=round(
                score,
                2,
            ),

            reason=(
                "Breakout confirmed by price, "
                "volume, and candle structure."
            ),

            warnings=warnings,

            metadata={
                "open": open_price,
                "high": high,
                "low": low,

                "confirmation_buffer_pct": (
                    self.confirmation_buffer_pct
                ),

                "minimum_score": (
                    self.minimum_score
                ),
            },
        )


_breakout_confirmation_engine: Optional[
    BreakoutConfirmationEngine
] = None


def get_breakout_confirmation_engine(
) -> BreakoutConfirmationEngine:

    global _breakout_confirmation_engine

    if _breakout_confirmation_engine is None:

        _breakout_confirmation_engine = (
            BreakoutConfirmationEngine()
        )

    return _breakout_confirmation_engine