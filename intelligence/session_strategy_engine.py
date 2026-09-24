from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from market.session_features import SessionFeatures


class SessionStrategyName(str, Enum):
    PREMARKET_HIGH_BREAK = "PREMARKET_HIGH_BREAK"
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"
    GAP_AND_GO = "GAP_AND_GO"
    COMPRESSION_BREAKOUT = "COMPRESSION_BREAKOUT"


@dataclass
class SessionStrategyCandidate:
    strategy: SessionStrategyName

    score: float
    valid: bool

    trigger_price: Optional[float] = None
    invalidation_price: Optional[float] = None

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


@dataclass
class SessionStrategyAnalysis:
    symbol: str

    approved: bool

    strategy: Optional[
        SessionStrategyName
    ]

    score: float

    trigger_price: Optional[float]
    invalidation_price: Optional[float]

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    candidates: list[
        SessionStrategyCandidate
    ] = field(
        default_factory=list
    )


class SessionStrategyEngine:
    """
    JALWE V4 - Intraday Session Strategy Engine.

    Detects:
    - Premarket High Break
    - Opening Range Breakout
    - Gap & Go
    - Compression Breakout

    This engine only analyzes.
    It NEVER places orders.
    """

    def __init__(
        self,
        minimum_score: float = 75.0,
    ) -> None:

        self.minimum_score = float(
            minimum_score
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _num(
        value: Any,
        default: float = 0.0,
    ) -> float:

        try:
            if value is None:
                return default

            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return default

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

    @staticmethod
    def _stop_below_level(
        level: float,
        atr: float,
        minimum_pct: float = 0.005,
    ) -> Optional[float]:

        if level <= 0:
            return None

        distance = max(
            atr * 0.75
            if atr > 0
            else 0.0,

            level * minimum_pct,
        )

        return round(
            level - distance,
            4,
        )

    # ========================================================
    # COMMON QUALITY
    # ========================================================

    def _common_quality(
        self,
        features: Any,
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []
        warnings: list[str] = []

        rvol = self._num(
            getattr(
                features,
                "rvol",
                None,
            )
        )

        liquidity = self._num(
            getattr(
                features,
                "liquidity_score",
                None,
            )
        )

        volume_acceleration = self._num(
            getattr(
                features,
                "volume_acceleration",
                None,
            )
        )

        momentum = self._num(
            getattr(
                features,
                "momentum_score",
                None,
            )
        )

        above_vwap = bool(
            getattr(
                features,
                "above_vwap",
                False,
            )
        )

        # RVOL
        if rvol >= 5:
            score += 24
            reasons.append(
                "Exceptional relative volume."
            )

        elif rvol >= 3:
            score += 20

        elif rvol >= 2:
            score += 14

        elif rvol >= 1.5:
            score += 8

        else:
            warnings.append(
                "Relative volume is weak."
            )

        # Liquidity
        if liquidity >= 85:
            score += 18
            reasons.append(
                "Excellent liquidity."
            )

        elif liquidity >= 70:
            score += 14

        elif liquidity >= 55:
            score += 8

        else:
            warnings.append(
                "Liquidity is weak."
            )

        # Volume acceleration
        if volume_acceleration >= 2:
            score += 14
            reasons.append(
                "Volume acceleration is strong."
            )

        elif volume_acceleration >= 1.4:
            score += 9

        elif volume_acceleration >= 1.1:
            score += 4

        # Momentum
        if momentum >= 80:
            score += 12

        elif momentum >= 65:
            score += 9

        elif momentum >= 55:
            score += 5

        # VWAP
        if above_vwap:
            score += 12
            reasons.append(
                "Price is above VWAP."
            )

        else:
            score -= 12
            warnings.append(
                "Price is below VWAP."
            )

        return (
            score,
            reasons,
            warnings,
        )

    # ========================================================
    # PREMARKET HIGH BREAK
    # ========================================================

    def _premarket_high_break(
        self,
        features: Any,
        session: SessionFeatures,
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(
                features
            )
        )

        pm_high = session.premarket_high

        atr = self._num(
            getattr(
                features,
                "atr",
                None,
            )
        )

        if pm_high is None:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .PREMARKET_HIGH_BREAK
                ),
                score=0.0,
                valid=False,
                warnings=[
                    "Premarket high unavailable."
                ],
            )

        distance = (
            session
            .distance_to_premarket_high_pct
        )

        if distance is None:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .PREMARKET_HIGH_BREAK
                ),
                score=0.0,
                valid=False,
                warnings=[
                    "Unable to calculate distance "
                    "to premarket high."
                ],
            )

        # Ideal: just below / just above PM high.
        if -0.75 <= distance <= 0.75:
            score += 24
            reasons.append(
                "Price is at the premarket "
                "breakout zone."
            )

        elif -1.50 <= distance < -0.75:
            score += 12

        elif distance > 2:
            score -= 20
            warnings.append(
                "Premarket breakout is already "
                "extended."
            )

        else:
            score -= 8

        if (
            session.above_premarket_high
            is True
        ):
            score += 8
            reasons.append(
                "Premarket high has been cleared."
            )

        trigger = round(
            pm_high * 1.001,
            4,
        )

        stop = self._stop_below_level(
            pm_high,
            atr,
        )

        return SessionStrategyCandidate(
            strategy=(
                SessionStrategyName
                .PREMARKET_HIGH_BREAK
            ),

            score=self._clamp(
                score
            ),

            valid=True,

            trigger_price=trigger,
            invalidation_price=stop,

            reasons=reasons,
            warnings=warnings,

            metadata={
                "premarket_high": pm_high,
                "distance_pct": distance,
            },
        )

    # ========================================================
    # OPENING RANGE BREAKOUT
    # ========================================================

    def _opening_range_breakout(
        self,
        features: Any,
        session: SessionFeatures,
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(
                features
            )
        )

        if (
            not session.opening_range_ready
            or session.opening_range_high
            is None
            or session.opening_range_low
            is None
        ):

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .OPENING_RANGE_BREAKOUT
                ),

                score=0.0,
                valid=False,

                warnings=[
                    "Opening range is not ready."
                ],
            )

        or_high = float(
            session.opening_range_high
        )

        or_low = float(
            session.opening_range_low
        )

        distance = (
            session
            .distance_to_opening_range_high_pct
        )

        atr = self._num(
            getattr(
                features,
                "atr",
                None,
            )
        )

        if distance is not None:

            if -1.0 <= distance <= 0.75:
                score += 24
                reasons.append(
                    "Price is near opening-range "
                    "breakout level."
                )

            elif -2.0 <= distance < -1:
                score += 10

            elif distance > 2:
                score -= 20
                warnings.append(
                    "Opening-range breakout "
                    "is already extended."
                )

            else:
                score -= 10

        if (
            session
            .above_opening_range_high
            is True
        ):
            score += 8

        trigger = round(
            or_high * 1.001,
            4,
        )

        atr_stop = (
            or_high - atr * 1.5
            if atr > 0
            else or_high * 0.99
        )

        # Avoid using a huge full-range stop
        # if the opening candle range is large.
        stop = round(
            max(
                or_low,
                atr_stop,
            ),
            4,
        )

        if stop >= trigger:
            stop = round(
                trigger * 0.99,
                4,
            )

        return SessionStrategyCandidate(
            strategy=(
                SessionStrategyName
                .OPENING_RANGE_BREAKOUT
            ),

            score=self._clamp(
                score
            ),

            valid=True,

            trigger_price=trigger,
            invalidation_price=stop,

            reasons=reasons,
            warnings=warnings,

            metadata={
                "opening_range_high": or_high,
                "opening_range_low": or_low,
                "distance_pct": distance,
            },
        )

    # ========================================================
    # GAP AND GO
    # ========================================================

    def _gap_and_go(
        self,
        features: Any,
        session: SessionFeatures,
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(
                features
            )
        )

        gap = session.gap_pct

        if gap is None:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .GAP_AND_GO
                ),

                score=0.0,
                valid=False,

                warnings=[
                    "Gap percentage unavailable."
                ],
            )

        # JALWE long Gap & Go.
        if gap >= 8:
            score += 26
            reasons.append(
                "Very strong positive gap."
            )

        elif gap >= 5:
            score += 22

        elif gap >= 3:
            score += 17

        elif gap >= 2:
            score += 10

        else:
            score -= 25
            warnings.append(
                "Gap is too small for Gap & Go."
            )

        pm_high = (
            session.premarket_high
        )

        regular_open = (
            session.regular_open
        )

        levels = [
            value
            for value in (
                pm_high,
                regular_open,
            )
            if value is not None
            and value > 0
        ]

        if not levels:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .GAP_AND_GO
                ),

                score=self._clamp(
                    score
                ),

                valid=False,

                warnings=(
                    warnings
                    + [
                        "No valid Gap & Go "
                        "trigger level."
                    ]
                ),
            )

        trigger_level = max(
            levels
        )

        trigger = round(
            trigger_level * 1.001,
            4,
        )

        atr = self._num(
            getattr(
                features,
                "atr",
                None,
            )
        )

        stop = (
            self._stop_below_level(
                trigger_level,
                atr,
            )
        )

        return SessionStrategyCandidate(
            strategy=(
                SessionStrategyName
                .GAP_AND_GO
            ),

            score=self._clamp(
                score
            ),

            valid=True,

            trigger_price=trigger,
            invalidation_price=stop,

            reasons=reasons,
            warnings=warnings,

            metadata={
                "gap_pct": gap,
            },
        )

    # ========================================================
    # COMPRESSION BREAKOUT
    # ========================================================

    def _compression_breakout(
        self,
        features: Any,
        bars: Optional[
            pd.DataFrame
        ],
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(
                features
            )
        )

        if (
            bars is None
            or len(bars) < 30
        ):

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .COMPRESSION_BREAKOUT
                ),

                score=0.0,
                valid=False,

                warnings=[
                    "Not enough bars for "
                    "compression analysis."
                ],
            )

        data = bars.copy().tail(
            30
        )

        for column in (
            "high",
            "low",
            "close",
        ):

            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=[
                "high",
                "low",
                "close",
            ]
        )

        if len(data) < 25:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .COMPRESSION_BREAKOUT
                ),

                score=0.0,
                valid=False,

                warnings=[
                    "Compression data incomplete."
                ],
            )

        recent = data.tail(
            6
        )

        prior = data.iloc[
            -26:-6
        ]

        recent_range = (
            float(
                recent["high"].max()
            )
            - float(
                recent["low"].min()
            )
        )

        prior_range = (
            float(
                prior["high"].max()
            )
            - float(
                prior["low"].min()
            )
        )

        if prior_range <= 0:

            return SessionStrategyCandidate(
                strategy=(
                    SessionStrategyName
                    .COMPRESSION_BREAKOUT
                ),

                score=0.0,
                valid=False,
            )

        compression_ratio = (
            recent_range
            / prior_range
        )

        if compression_ratio <= 0.30:
            score += 28
            reasons.append(
                "Very strong price compression."
            )

        elif compression_ratio <= 0.45:
            score += 22

        elif compression_ratio <= 0.60:
            score += 14

        elif compression_ratio <= 0.75:
            score += 6

        else:
            score -= 15
            warnings.append(
                "No meaningful price compression."
            )

        compression_high = float(
            recent["high"].max()
        )

        compression_low = float(
            recent["low"].min()
        )

        trigger = round(
            compression_high * 1.001,
            4,
        )

        atr = self._num(
            getattr(
                features,
                "atr",
                None,
            )
        )

        atr_stop = (
            compression_high
            - atr * 1.25
            if atr > 0
            else compression_high * 0.99
        )

        stop = round(
            max(
                compression_low,
                atr_stop,
            ),
            4,
        )

        if stop >= trigger:
            stop = round(
                trigger * 0.99,
                4,
            )

        return SessionStrategyCandidate(
            strategy=(
                SessionStrategyName
                .COMPRESSION_BREAKOUT
            ),

            score=self._clamp(
                score
            ),

            valid=True,

            trigger_price=trigger,
            invalidation_price=stop,

            reasons=reasons,
            warnings=warnings,

            metadata={
                "compression_ratio": (
                    compression_ratio
                ),

                "compression_high": (
                    compression_high
                ),

                "compression_low": (
                    compression_low
                ),
            },
        )

    # ========================================================
    # MAIN EVALUATION
    # ========================================================

    def evaluate(
        self,
        features: Any,
        session: SessionFeatures,
        bars: Optional[
            pd.DataFrame
        ] = None,
    ) -> SessionStrategyAnalysis:

        symbol = str(
            getattr(
                features,
                "symbol",
                session.symbol,
            )
        ).upper().strip()

        candidates = [
            self._premarket_high_break(
                features,
                session,
            ),

            self._opening_range_breakout(
                features,
                session,
            ),

            self._gap_and_go(
                features,
                session,
            ),

            self._compression_breakout(
                features,
                bars,
            ),
        ]

        valid_candidates = [
            candidate
            for candidate in candidates
            if candidate.valid
        ]

        valid_candidates.sort(
            key=lambda candidate:
            candidate.score,
            reverse=True,
        )

        if not valid_candidates:

            return SessionStrategyAnalysis(
                symbol=symbol,

                approved=False,

                strategy=None,

                score=0.0,

                trigger_price=None,
                invalidation_price=None,

                warnings=[
                    "No valid intraday "
                    "session strategy found."
                ],

                candidates=candidates,
            )

        best = valid_candidates[0]

        approved = (
            best.score
            >= self.minimum_score
        )

        return SessionStrategyAnalysis(
            symbol=symbol,

            approved=approved,

            strategy=(
                best.strategy
                if approved
                else None
            ),

            score=round(
                best.score,
                2,
            ),

            trigger_price=(
                best.trigger_price
            ),

            invalidation_price=(
                best.invalidation_price
            ),

            reasons=list(
                best.reasons
            ),

            warnings=list(
                best.warnings
            ),

            candidates=candidates,
        )


_session_strategy_engine: Optional[
    SessionStrategyEngine
] = None


def get_session_strategy_engine(
) -> SessionStrategyEngine:

    global _session_strategy_engine

    if _session_strategy_engine is None:

        _session_strategy_engine = (
            SessionStrategyEngine()
        )

    return _session_strategy_engine