from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from market.session_features import SessionFeatures


class SessionStrategyName(str, Enum):
    PREMARKET_HIGH_BREAK = "PREMARKET_HIGH_BREAK"
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"
    GAP_AND_GO = "GAP_AND_GO"
    BULL_FLAG_BREAKOUT = "BULL_FLAG_BREAKOUT"
    VWAP_BOUNCE = "VWAP_BOUNCE"
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

            number = float(value)
            return number if math.isfinite(number) else default

        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _clamp(
        value: float,
    ) -> float:

        if not math.isfinite(value):
            return 0.0

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

        feature_metadata = getattr(
            features,
            "metadata",
            {},
        )
        if not isinstance(feature_metadata, dict):
            feature_metadata = {}

        rvol = max(
            self._num(
                getattr(
                    features,
                    "rvol",
                    None,
                )
            ),
            self._num(
                feature_metadata.get(
                    "scoring_rvol"
                )
            ),
        )

        liquidity = self._num(
            getattr(
                features,
                "liquidity_score",
                None,
            )
        )

        volume_acceleration = max(
            self._num(
                getattr(
                    features,
                    "volume_acceleration",
                    None,
                )
            ),
            self._num(
                feature_metadata.get(
                    "scoring_volume_acceleration"
                )
            ),
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
    # BULL FLAG BREAKOUT
    # ========================================================

    def _bull_flag_breakout(
        self,
        features: Any,
        bars: Optional[pd.DataFrame],
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(features)
        )

        if bars is None or len(bars) < 20:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=0.0,
                valid=False,
                warnings=["Not enough bars for bull-flag analysis."],
            )

        data = bars.copy().tail(20)

        for column in ("open", "high", "low", "close", "volume"):
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=["open", "high", "low", "close", "volume"]
        )

        if len(data) < 12:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=0.0,
                valid=False,
                warnings=["Bull-flag data is incomplete."],
            )

        pattern = data.tail(12)
        impulse = pattern.iloc[:6]
        flag = pattern.iloc[6:]

        impulse_start = float(impulse["open"].iloc[0])
        impulse_high = float(impulse["high"].max())
        impulse_low = float(impulse["low"].min())

        if impulse_start <= 0 or impulse_high <= impulse_low:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=0.0,
                valid=False,
            )

        impulse_move_pct = (
            (impulse_high - impulse_start)
            / impulse_start
            * 100.0
        )

        impulse_range = impulse_high - impulse_low
        flag_high = float(flag["high"].max())
        flag_low = float(flag["low"].min())
        flag_range = flag_high - flag_low

        retracement_ratio = (
            (impulse_high - flag_low)
            / impulse_range
            if impulse_range > 0
            else 999.0
        )

        contraction_ratio = (
            flag_range / impulse_range
            if impulse_range > 0
            else 999.0
        )

        impulse_volume = float(
            impulse["volume"].mean()
        )
        flag_volume = float(
            flag["volume"].mean()
        )

        volume_contraction_ratio = (
            flag_volume / impulse_volume
            if impulse_volume > 0
            else None
        )

        latest = flag.iloc[-1]
        latest_close = float(latest["close"])
        latest_open = float(latest["open"])

        flag_location = (
            (latest_close - flag_low)
            / flag_range
            if flag_range > 0
            else 0.0
        )

        if impulse_move_pct < 2.0:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=self._clamp(score - 20.0),
                valid=False,
                warnings=warnings + [
                    "No strong impulse leg before the flag."
                ],
                metadata={
                    "impulse_move_pct": impulse_move_pct,
                },
            )

        if not (0.0 <= retracement_ratio <= 0.55):
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=self._clamp(score - 15.0),
                valid=False,
                warnings=warnings + [
                    "Bull-flag pullback is too deep."
                ],
                metadata={
                    "retracement_ratio": retracement_ratio,
                },
            )

        score += 20.0
        reasons.append("Strong impulse leg before consolidation.")

        if retracement_ratio <= 0.35:
            score += 18.0
            reasons.append("Bull-flag retracement is shallow.")
        else:
            score += 10.0

        if contraction_ratio <= 0.55:
            score += 15.0
            reasons.append("Flag range is contracting.")
        elif contraction_ratio <= 0.75:
            score += 8.0
        else:
            score -= 12.0
            warnings.append("Flag range is too loose.")

        if (
            volume_contraction_ratio is not None
            and volume_contraction_ratio <= 0.85
        ):
            score += 10.0
            reasons.append("Volume contracted during the flag.")

        if flag_location >= 0.65:
            score += 10.0
            reasons.append("Price is holding near the flag high.")

        if latest_close > latest_open:
            score += 5.0

        trigger = round(
            flag_high * 1.001,
            4,
        )

        stop = round(
            flag_low * 0.998,
            4,
        )

        if stop <= 0 or stop >= trigger:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
                score=0.0,
                valid=False,
                warnings=["Invalid bull-flag price structure."],
            )

        return SessionStrategyCandidate(
            strategy=SessionStrategyName.BULL_FLAG_BREAKOUT,
            score=self._clamp(score),
            valid=True,
            trigger_price=trigger,
            invalidation_price=stop,
            reasons=reasons,
            warnings=warnings,
            metadata={
                "impulse_move_pct": impulse_move_pct,
                "retracement_ratio": retracement_ratio,
                "contraction_ratio": contraction_ratio,
                "volume_contraction_ratio": volume_contraction_ratio,
                "flag_high": flag_high,
                "flag_low": flag_low,
            },
        )

    # ========================================================
    # VWAP BOUNCE
    # ========================================================

    def _vwap_bounce(
        self,
        features: Any,
        bars: Optional[pd.DataFrame],
    ) -> SessionStrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(features)
        )

        vwap = self._num(
            getattr(
                features,
                "vwap",
                None,
            )
        )

        ema_9 = self._num(
            getattr(
                features,
                "ema_9",
                None,
            )
        )

        ema_20 = self._num(
            getattr(
                features,
                "ema_20",
                None,
            )
        )

        if (
            vwap <= 0
            or bars is None
            or len(bars) < 10
        ):
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.VWAP_BOUNCE,
                score=0.0,
                valid=False,
                warnings=["VWAP-bounce inputs are unavailable."],
            )

        data = bars.copy().tail(6)

        for column in ("open", "high", "low", "close", "volume"):
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=["open", "high", "low", "close", "volume"]
        )

        if len(data) < 4:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.VWAP_BOUNCE,
                score=0.0,
                valid=False,
                warnings=["VWAP-bounce bars are incomplete."],
            )

        recent = data.tail(4)
        recent_low = float(recent["low"].min())
        latest = recent.iloc[-1]

        latest_open = float(latest["open"])
        latest_high = float(latest["high"])
        latest_close = float(latest["close"])

        low_distance_pct = abs(
            (recent_low - vwap)
            / vwap
            * 100.0
        )

        recovery_pct = (
            (latest_close - vwap)
            / vwap
            * 100.0
        )

        touched_vwap = (
            recent_low <= vwap * 1.004
            and recent_low >= vwap * 0.985
        )

        recovered_vwap = (
            latest_close >= vwap * 1.001
        )

        if not touched_vwap or not recovered_vwap:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.VWAP_BOUNCE,
                score=self._clamp(score - 12.0),
                valid=False,
                warnings=warnings + [
                    "No clean VWAP test-and-recovery pattern."
                ],
                metadata={
                    "recent_low": recent_low,
                    "vwap": vwap,
                    "recovery_pct": recovery_pct,
                },
            )

        if low_distance_pct <= 0.35:
            score += 24.0
            reasons.append("Price tested VWAP precisely.")
        else:
            score += 14.0

        score += 18.0
        reasons.append("Price recovered and closed back above VWAP.")

        if latest_close > latest_open:
            score += 10.0
            reasons.append("Bounce candle closed bullish.")

        if (
            ema_9 > 0
            and ema_20 > 0
            and ema_9 > ema_20
        ):
            score += 12.0
            reasons.append("EMA structure supports the VWAP bounce.")

        if recovery_pct > 2.0:
            score -= 10.0
            warnings.append("VWAP bounce is becoming extended.")

        trigger = round(
            latest_high * 1.001,
            4,
        )

        stop = round(
            min(
                recent_low * 0.998,
                vwap * 0.995,
            ),
            4,
        )

        if stop <= 0 or stop >= trigger:
            return SessionStrategyCandidate(
                strategy=SessionStrategyName.VWAP_BOUNCE,
                score=0.0,
                valid=False,
                warnings=["Invalid VWAP-bounce price structure."],
            )

        return SessionStrategyCandidate(
            strategy=SessionStrategyName.VWAP_BOUNCE,
            score=self._clamp(score),
            valid=True,
            trigger_price=trigger,
            invalidation_price=stop,
            reasons=reasons,
            warnings=warnings,
            metadata={
                "vwap": vwap,
                "recent_low": recent_low,
                "low_distance_pct": low_distance_pct,
                "recovery_pct": recovery_pct,
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

        # Invalidation must sit below the compression structure.
        # Using the higher of compression_low and the ATR stop made the
        # stop too tight on low-priced names and could place it inside
        # the normal bid/ask noise. RiskEngine will reduce quantity when
        # the structural stop is wider.
        stop = round(
            min(
                compression_low,
                atr_stop,
            ),
            4,
        )

        if stop <= 0 or stop >= trigger:
            stop = round(
                min(
                    compression_low,
                    trigger * 0.99,
                ),
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

            self._bull_flag_breakout(
                features,
                bars,
            ),

            self._vwap_bounce(
                features,
                bars,
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
            and math.isfinite(candidate.score)
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
        from core.config import settings

        _session_strategy_engine = (
            SessionStrategyEngine(
                minimum_score=settings.MIN_SESSION_STRATEGY_SCORE,
            )
        )

    return _session_strategy_engine
