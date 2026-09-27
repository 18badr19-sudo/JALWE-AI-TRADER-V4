from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class StrategyName(str, Enum):
    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    CATALYST_BREAKOUT = "CATALYST_BREAKOUT"


class OpportunityGrade(str, Enum):
    REJECT = "REJECT"
    B = "B"
    A = "A"
    A_PLUS = "A+"


@dataclass
class StrategyCandidate:
    strategy: StrategyName
    score: float

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
class OpportunityAnalysis:
    symbol: str

    approved: bool

    strategy: Optional[
        StrategyName
    ]

    score: float
    grade: OpportunityGrade

    recommended_risk_pct: float

    trigger_price: Optional[float]
    invalidation_price: Optional[float]

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    candidates: list[
        StrategyCandidate
    ] = field(
        default_factory=list
    )


class OpportunityEngine:
    """
    JALWE V4 - Multi Strategy Opportunity Engine.

    This engine does NOT place orders.

    It determines:
    - Which setup currently exists.
    - Setup quality.
    - Whether price is late / extended.
    - Recommended strategy grade.
    - Suggested risk tier.

    RiskEngine remains the final authority.
    """

    def __init__(
        self,
        minimum_score: float = 74.0,
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
    def _get(
        obj: Any,
        name: str,
        default: Any = None,
    ) -> Any:

        return getattr(
            obj,
            name,
            default,
        )

    @staticmethod
    def _grade(
        score: float,
    ) -> OpportunityGrade:

        if score >= 90:
            return OpportunityGrade.A_PLUS

        if score >= 82:
            return OpportunityGrade.A

        if score >= 74:
            return OpportunityGrade.B

        return OpportunityGrade.REJECT

    @staticmethod
    def _risk_for_grade(
        grade: OpportunityGrade,
    ) -> float:
        """
        Risk % of JALWE strategy capital.

        $100 account example:
        B   = $0.50
        A   = $1.00
        A+  = $1.50

        2% remains a hard-cap option later,
        but is not automatically used.
        """

        if grade == OpportunityGrade.A_PLUS:
            return 1.50

        if grade == OpportunityGrade.A:
            return 1.00

        if grade == OpportunityGrade.B:
            return 0.50

        return 0.0

    @staticmethod
    def _distance_pct(
        price: float,
        level: float,
    ) -> Optional[float]:

        if (
            price <= 0
            or level <= 0
        ):
            return None

        return (
            (price - level)
            / level
            * 100.0
        )

    # ========================================================
    # COMMON QUALITY
    # ========================================================

    def _common_quality(
        self,
        f: Any,
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []
        warnings: list[str] = []

        rvol = self._num(
            self._get(
                f,
                "rvol",
            )
        )

        liquidity = self._num(
            self._get(
                f,
                "liquidity_score",
            )
        )

        volume_acceleration = self._num(
            self._get(
                f,
                "volume_acceleration",
            )
        )

        vwap_distance = self._num(
            self._get(
                f,
                "distance_from_vwap_pct",
            )
        )

        # RVOL
        if rvol >= 5:
            score += 25
            reasons.append(
                "Exceptional RVOL."
            )

        elif rvol >= 3:
            score += 21
            reasons.append(
                "Strong RVOL."
            )

        elif rvol >= 2:
            score += 16

        elif rvol >= 1.5:
            score += 10

        else:
            warnings.append(
                "Weak relative volume."
            )

        # Liquidity
        if liquidity >= 85:
            score += 20
            reasons.append(
                "Excellent liquidity."
            )

        elif liquidity >= 70:
            score += 16

        elif liquidity >= 55:
            score += 10

        else:
            warnings.append(
                "Liquidity quality is weak."
            )

        # Volume acceleration
        if volume_acceleration >= 2:
            score += 15
            reasons.append(
                "Volume is accelerating strongly."
            )

        elif volume_acceleration >= 1.4:
            score += 10

        elif volume_acceleration >= 1.1:
            score += 5

        # Avoid chasing extreme extension
        if vwap_distance > 8:
            score -= 25
            warnings.append(
                "Price is extremely extended "
                "above VWAP."
            )

        elif vwap_distance > 5:
            score -= 12
            warnings.append(
                "Price may be too extended "
                "for a fresh entry."
            )

        return (
            score,
            reasons,
            warnings,
        )

    # ========================================================
    # MOMENTUM BREAKOUT
    # ========================================================

    def _momentum_breakout(
        self,
        f: Any,
    ) -> StrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(f)
        )

        price = self._num(
            self._get(
                f,
                "price",
            )
        )

        high_20 = self._num(
            self._get(
                f,
                "high_20",
            )
        )

        momentum = self._num(
            self._get(
                f,
                "momentum_score",
            )
        )

        above_vwap = bool(
            self._get(
                f,
                "above_vwap",
                False,
            )
        )

        atr = self._num(
            self._get(
                f,
                "atr",
            )
        )

        # VWAP confirmation
        if above_vwap:
            score += 15
            reasons.append(
                "Price is above VWAP."
            )

        else:
            score -= 15
            warnings.append(
                "Breakout is below VWAP."
            )

        # Momentum
        if momentum >= 80:
            score += 15
            reasons.append(
                "Very strong momentum."
            )

        elif momentum >= 65:
            score += 12

        elif momentum >= 55:
            score += 7

        else:
            score -= 8

        # Near breakout level
        if (
            high_20 > 0
            and price > 0
        ):

            below_high_pct = (
                (high_20 - price)
                / price
                * 100
            )

            if -0.50 <= below_high_pct <= 1.25:
                score += 20
                reasons.append(
                    "Price is at the breakout zone."
                )

            elif 1.25 < below_high_pct <= 3:
                score += 10

            elif below_high_pct < -2:
                score -= 12
                warnings.append(
                    "Breakout may already be extended."
                )

        trigger = None

        if high_20 > 0:
            trigger = round(
                high_20 * 1.001,
                4,
            )

        elif price > 0:
            trigger = round(
                price * 1.002,
                4,
            )

        invalidation = None

        if (
            price > 0
            and atr > 0
        ):
            invalidation = round(
                price
                - atr * 1.5,
                4,
            )

        return StrategyCandidate(
            strategy=(
                StrategyName.MOMENTUM_BREAKOUT
            ),
            score=max(
                0.0,
                min(100.0, score),
            ),
            trigger_price=trigger,
            invalidation_price=invalidation,
            reasons=reasons,
            warnings=warnings,
        )

    # ========================================================
    # VWAP RECLAIM
    # ========================================================

    def _vwap_reclaim(
        self,
        f: Any,
    ) -> StrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(f)
        )

        price = self._num(
            self._get(
                f,
                "price",
            )
        )

        vwap = self._num(
            self._get(
                f,
                "vwap",
            )
        )

        vwap_distance = self._num(
            self._get(
                f,
                "distance_from_vwap_pct",
            )
        )

        above_vwap = bool(
            self._get(
                f,
                "above_vwap",
                False,
            )
        )

        ema_9 = self._num(
            self._get(
                f,
                "ema_9",
            )
        )

        ema_20 = self._num(
            self._get(
                f,
                "ema_20",
            )
        )

        momentum = self._num(
            self._get(
                f,
                "momentum_score",
            )
        )

        atr = self._num(
            self._get(
                f,
                "atr",
            )
        )

        if (
            above_vwap
            and 0 <= vwap_distance <= 1.5
        ):
            score += 25
            reasons.append(
                "Fresh VWAP reclaim."
            )

        elif (
            above_vwap
            and vwap_distance <= 3
        ):
            score += 15

        else:
            score -= 15
            warnings.append(
                "No clean VWAP reclaim."
            )

        if (
            ema_9 > 0
            and ema_20 > 0
            and ema_9 > ema_20
        ):
            score += 15
            reasons.append(
                "EMA trend supports continuation."
            )

        if 55 <= momentum <= 85:
            score += 12

        elif momentum > 85:
            score += 6

        trigger = None

        if price > 0:
            trigger = round(
                price * 1.001,
                4,
            )

        invalidation = None

        if vwap > 0:
            invalidation = round(
                vwap * 0.995,
                4,
            )

        elif (
            price > 0
            and atr > 0
        ):
            invalidation = round(
                price
                - atr * 1.3,
                4,
            )

        return StrategyCandidate(
            strategy=(
                StrategyName.VWAP_RECLAIM
            ),
            score=max(
                0.0,
                min(100.0, score),
            ),
            trigger_price=trigger,
            invalidation_price=invalidation,
            reasons=reasons,
            warnings=warnings,
        )

    # ========================================================
    # PULLBACK CONTINUATION
    # ========================================================

    def _pullback_continuation(
        self,
        f: Any,
    ) -> StrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(f)
        )

        price = self._num(
            self._get(
                f,
                "price",
            )
        )

        high_20 = self._num(
            self._get(
                f,
                "high_20",
            )
        )

        ema_9 = self._num(
            self._get(
                f,
                "ema_9",
            )
        )

        ema_20 = self._num(
            self._get(
                f,
                "ema_20",
            )
        )

        momentum = self._num(
            self._get(
                f,
                "momentum_score",
            )
        )

        above_vwap = bool(
            self._get(
                f,
                "above_vwap",
                False,
            )
        )

        atr = self._num(
            self._get(
                f,
                "atr",
            )
        )

        if above_vwap:
            score += 15

        else:
            score -= 20
            warnings.append(
                "Pullback lost VWAP."
            )

        if (
            ema_9 > 0
            and ema_20 > 0
            and ema_9 > ema_20
        ):
            score += 18
            reasons.append(
                "Trend remains constructive."
            )

        if (
            price > 0
            and ema_9 > 0
        ):
            ema9_distance = abs(
                (
                    price - ema_9
                )
                / price
                * 100
            )

            if ema9_distance <= 1.5:
                score += 15
                reasons.append(
                    "Price is near EMA9 support."
                )

            elif ema9_distance <= 3:
                score += 8

        if (
            price > 0
            and high_20 > 0
        ):
            pullback_pct = (
                (high_20 - price)
                / high_20
                * 100
            )

            if 1 <= pullback_pct <= 5:
                score += 15
                reasons.append(
                    "Healthy pullback from recent high."
                )

            elif pullback_pct > 8:
                score -= 12

        if 55 <= momentum <= 80:
            score += 10

        trigger = None

        if (
            ema_9 > 0
            and price > 0
        ):
            trigger = round(
                max(
                    price,
                    ema_9,
                )
                * 1.002,
                4,
            )

        invalidation = None

        if (
            ema_20 > 0
            and atr > 0
        ):
            invalidation = round(
                min(
                    ema_20,
                    price - atr * 1.2,
                ),
                4,
            )

        return StrategyCandidate(
            strategy=(
                StrategyName.PULLBACK_CONTINUATION
            ),
            score=max(
                0.0,
                min(100.0, score),
            ),
            trigger_price=trigger,
            invalidation_price=invalidation,
            reasons=reasons,
            warnings=warnings,
        )

    # ========================================================
    # CATALYST BREAKOUT
    # ========================================================

    def _catalyst_breakout(
        self,
        f: Any,
    ) -> StrategyCandidate:

        score, reasons, warnings = (
            self._common_quality(f)
        )

        price = self._num(
            self._get(
                f,
                "price",
            )
        )

        high_20 = self._num(
            self._get(
                f,
                "high_20",
            )
        )

        news_score_raw = self._get(
            f,
            "news_score",
            None,
        )

        options_score_raw = self._get(
            f,
            "options_flow_score",
            None,
        )

        above_vwap = bool(
            self._get(
                f,
                "above_vwap",
                False,
            )
        )

        momentum = self._num(
            self._get(
                f,
                "momentum_score",
            )
        )

        atr = self._num(
            self._get(
                f,
                "atr",
            )
        )

        if news_score_raw is None:
            score -= 25
            warnings.append(
                "No confirmed catalyst score."
            )

        else:
            news_score = self._num(
                news_score_raw
            )

            if news_score >= 80:
                score += 25
                reasons.append(
                    "Strong positive catalyst."
                )

            elif news_score >= 65:
                score += 18

            elif news_score >= 55:
                score += 10

            else:
                score -= 15
                warnings.append(
                    "Catalyst quality is weak."
                )

        if above_vwap:
            score += 12

        else:
            score -= 15

        if momentum >= 65:
            score += 12

        # Options are confirmation only.
        if options_score_raw is not None:

            options_score = self._num(
                options_score_raw
            )

            if options_score >= 80:
                score += 6
                reasons.append(
                    "Options bias confirms setup."
                )

            elif options_score <= 30:
                score -= 5

        trigger = None

        if high_20 > 0:
            trigger = round(
                high_20 * 1.001,
                4,
            )

        elif price > 0:
            trigger = round(
                price * 1.002,
                4,
            )

        invalidation = None

        if (
            price > 0
            and atr > 0
        ):
            invalidation = round(
                price
                - atr * 1.5,
                4,
            )

        return StrategyCandidate(
            strategy=(
                StrategyName.CATALYST_BREAKOUT
            ),
            score=max(
                0.0,
                min(100.0, score),
            ),
            trigger_price=trigger,
            invalidation_price=invalidation,
            reasons=reasons,
            warnings=warnings,
        )

    # ========================================================
    # MAIN ROUTER
    # ========================================================

    def evaluate(
        self,
        features: Any,
    ) -> OpportunityAnalysis:

        symbol = str(
            self._get(
                features,
                "symbol",
                "",
            )
        ).upper().strip()

        quality_ok = bool(
            self._get(
                features,
                "data_quality_ok",
                False,
            )
        )

        stale = bool(
            self._get(
                features,
                "data_is_stale",
                False,
            )
        )

        price = self._num(
            self._get(
                features,
                "price",
            )
        )

        # Hard data safety gate.
        if (
            not symbol
            or not quality_ok
            or stale
            or price <= 0
        ):

            return OpportunityAnalysis(
                symbol=symbol,
                approved=False,
                strategy=None,
                score=0.0,
                grade=(
                    OpportunityGrade.REJECT
                ),
                recommended_risk_pct=0.0,
                trigger_price=None,
                invalidation_price=None,
                warnings=[
                    "Invalid, stale, or incomplete "
                    "market data."
                ],
            )

        candidates = [
            self._momentum_breakout(
                features
            ),
            self._vwap_reclaim(
                features
            ),
            self._pullback_continuation(
                features
            ),
            self._catalyst_breakout(
                features
            ),
        ]

        candidates.sort(
            key=lambda x: x.score,
            reverse=True,
        )

        best = candidates[0]

        grade = self._grade(
            best.score
        )

        approved = (
            best.score
            >= self.minimum_score
            and grade
            != OpportunityGrade.REJECT
        )

        return OpportunityAnalysis(
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

            grade=grade,

            recommended_risk_pct=(
                self._risk_for_grade(
                    grade
                )
                if approved
                else 0.0
            ),

            trigger_price=(
                best.trigger_price
            ),

            invalidation_price=(
                best.invalidation_price
            ),

            reasons=best.reasons,

            warnings=best.warnings,

            candidates=candidates,
        )


_opportunity_engine: Optional[
    OpportunityEngine
] = None


def get_opportunity_engine(
) -> OpportunityEngine:

    global _opportunity_engine

    if _opportunity_engine is None:
        _opportunity_engine = (
            OpportunityEngine()
        )

    return _opportunity_engine