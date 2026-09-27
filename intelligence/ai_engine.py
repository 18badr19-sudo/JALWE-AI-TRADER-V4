from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from core.models import (
    FeatureSnapshot,
    MarketRegime,
    SignalAction,
)

from intelligence.learning_engine import (
    get_learning_engine,
)


@dataclass
class AIAnalysis:
    symbol: str

    action: SignalAction

    score: float
    probability: float
    confidence: float

    bullish_score: float
    bearish_score: float

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


class AIEngine:
    """
    JALWE V4 - Central Intelligence Engine.

    المرحلة الحالية:
    Deterministic institutional scoring engine.

    لا يستخدم:
    - random numbers
    - بيانات وهمية
    - قرار تداول مباشر

    لاحقًا سيتم دمج النموذج المتعلم معه
    بعد وجود بيانات تدريب موثوقة.
    """

    def __init__(
        self,
        minimum_watch_score: float = 60.0,
        minimum_buy_score: float = 82.0,
    ) -> None:

        if not (
            0
            <= minimum_watch_score
            <= 100
        ):
            raise ValueError(
                "minimum_watch_score must be between 0 and 100."
            )

        if not (
            0
            <= minimum_buy_score
            <= 100
        ):
            raise ValueError(
                "minimum_buy_score must be between 0 and 100."
            )

        if (
            minimum_buy_score
            < minimum_watch_score
        ):
            raise ValueError(
                "minimum_buy_score cannot be below minimum_watch_score."
            )

        self.minimum_watch_score = (
            minimum_watch_score
        )

        self.minimum_buy_score = (
            minimum_buy_score
        )

        self.learning = (
            get_learning_engine()
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _clamp(
        value: float,
        minimum: float = 0.0,
        maximum: float = 100.0,
    ) -> float:

        return max(
            minimum,
            min(
                float(value),
                maximum,
            ),
        )

    @staticmethod
    def _available(
        value: Optional[float],
    ) -> bool:

        return value is not None

    # ========================================================
    # EVALUATION
    # ========================================================

    def evaluate(
        self,
        features: FeatureSnapshot,
    ) -> AIAnalysis:

        symbol = features.symbol.upper()

        reasons: list[str] = []
        warnings: list[str] = []

        # ----------------------------------------------------
        # DATA QUALITY
        # ----------------------------------------------------

        if not features.data_quality_ok:

            return AIAnalysis(
                symbol=symbol,
                action=SignalAction.REJECT,
                score=0.0,
                probability=0.0,
                confidence=1.0,
                bullish_score=0.0,
                bearish_score=100.0,
                reasons=[
                    "Market data quality check failed."
                ],
                warnings=[
                    "Opportunity rejected because features are unreliable."
                ],
            )

        if features.data_is_stale:

            return AIAnalysis(
                symbol=symbol,
                action=SignalAction.REJECT,
                score=0.0,
                probability=0.0,
                confidence=1.0,
                bullish_score=0.0,
                bearish_score=100.0,
                reasons=[
                    "Market data is stale."
                ],
                warnings=[
                    "Fresh market data is required."
                ],
            )

        bullish = 0.0
        bearish = 0.0

        evidence_count = 0
        possible_evidence = 0

        # ====================================================
        # 1. LIQUIDITY
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.liquidity_score
        ):
            evidence_count += 1

            liquidity = (
                features.liquidity_score
            )

            if liquidity >= 80:
                bullish += self.learning.apply("liquidity", 18)
                reasons.append(
                    "Strong liquidity conditions."
                )

            elif liquidity >= 60:
                bullish += self.learning.apply("liquidity", 12)
                reasons.append(
                    "Acceptable liquidity."
                )

            elif liquidity < 30:
                bearish += self.learning.apply("liquidity", 15)
                warnings.append(
                    "Weak liquidity."
                )

        # ====================================================
        # 2. RVOL
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.rvol
        ):
            evidence_count += 1

            if features.rvol >= 3.0:
                bullish += self.learning.apply("rvol", 18)
                reasons.append(
                    "Very strong relative volume."
                )

            elif features.rvol >= 2.0:
                bullish += self.learning.apply("rvol", 14)
                reasons.append(
                    "Strong relative volume."
                )

            elif features.rvol >= 1.5:
                bullish += self.learning.apply("rvol", 9)
                reasons.append(
                    "Elevated relative volume."
                )

            elif features.rvol < 0.8:
                bearish += self.learning.apply("rvol", 8)
                warnings.append(
                    "Relative volume is weak."
                )

        # ====================================================
        # 3. VWAP
        # ====================================================

        possible_evidence += 1

        if (
            features.price is not None
            and features.vwap is not None
        ):
            evidence_count += 1

            if features.above_vwap is True:

                bullish += self.learning.apply("vwap", 14)

                reasons.append(
                    "Price is above session VWAP."
                )

            else:

                bearish += self.learning.apply("vwap", 10)

                warnings.append(
                    "Price is below session VWAP."
                )

            distance = (
                features.distance_from_vwap_pct
            )

            if distance is not None:

                if (
                    0
                    <= distance
                    <= 1.0
                ):
                    bullish += self.learning.apply("vwap", 4)

                    reasons.append(
                        "Price is holding close above VWAP."
                    )

                elif distance < -2.0:
                    bearish += self.learning.apply("vwap", 5)

        # ====================================================
        # 4. TREND STRUCTURE
        # ====================================================

        possible_evidence += 1

        if (
            features.price is not None
            and features.ema_9 is not None
            and features.ema_20 is not None
        ):
            evidence_count += 1

            if (
                features.price
                > features.ema_9
                > features.ema_20
            ):
                bullish += self.learning.apply("trend", 14)

                reasons.append(
                    "Short-term trend structure is bullish."
                )

            elif (
                features.price
                < features.ema_9
                < features.ema_20
            ):
                bearish += self.learning.apply("trend", 12)

                warnings.append(
                    "Short-term trend structure is bearish."
                )

        # ====================================================
        # 5. MOMENTUM SCORE
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.momentum_score
        ):
            evidence_count += 1

            momentum = (
                features.momentum_score
            )

            if momentum >= 80:
                bullish += self.learning.apply("momentum", 14)

                reasons.append(
                    "Momentum score is very strong."
                )

            elif momentum >= 60:
                bullish += self.learning.apply("momentum", 10)

                reasons.append(
                    "Momentum is supportive."
                )

            elif momentum < 30:
                bearish += self.learning.apply("momentum", 8)

                warnings.append(
                    "Momentum is weak."
                )

        # ====================================================
        # 6. RSI
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.rsi_14
        ):
            evidence_count += 1

            rsi = features.rsi_14

            if 50 <= rsi <= 70:

                bullish += self.learning.apply("rsi", 8)

                reasons.append(
                    "RSI supports bullish momentum."
                )

            elif 40 <= rsi < 50:

                bullish += self.learning.apply("rsi", 3)

            elif rsi > 80:

                bearish += self.learning.apply("rsi", 5)

                warnings.append(
                    "RSI is extremely extended."
                )

            elif rsi < 30:

                bearish += self.learning.apply("rsi", 4)

                warnings.append(
                    "RSI shows strong downside pressure."
                )

        # ====================================================
        # 7. BREAKOUT PROXIMITY
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.distance_to_high_20_pct
        ):
            evidence_count += 1

            distance = (
                features.distance_to_high_20_pct
            )

            if (
                -0.5
                <= distance
                <= 1.0
            ):
                bullish += self.learning.apply("breakout", 8)

                reasons.append(
                    "Price is near the 20-bar breakout level."
                )

            elif distance > 5.0:

                bearish += self.learning.apply("breakout", 3)

        # ====================================================
        # 8. COMPRESSION
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.range_compression
        ):
            evidence_count += 1

            compression = (
                features.range_compression
            )

            if compression <= 0.35:

                bullish += self.learning.apply("compression", 6)

                reasons.append(
                    "Price range is compressed before potential expansion."
                )

        # ====================================================
        # 9. NEWS
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.news_score
        ):
            evidence_count += 1

            news_score = (
                features.news_score
            )

            if news_score >= 70:
                bullish += self.learning.apply("news", 8)

                reasons.append(
                    "News catalyst is supportive."
                )

            elif news_score <= 30:
                bearish += self.learning.apply("news", 8)

                warnings.append(
                    "News catalyst is negative."
                )

        # ====================================================
        # 10. OPTIONS FLOW
        # ====================================================

        possible_evidence += 1

        if self._available(
            features.options_flow_score
        ):
            evidence_count += 1

            options_score = (
                features.options_flow_score
            )

            if options_score >= 75:

                bullish += self.learning.apply("options_flow", 10)

                reasons.append(
                    "Options flow is strongly supportive."
                )

            elif options_score <= 25:

                bearish += self.learning.apply("options_flow", 8)

                warnings.append(
                    "Options flow is bearish."
                )

        # ====================================================
        # MARKET REGIME ADJUSTMENT
        # ====================================================

        regime = features.market_regime

        if regime == MarketRegime.BULL_TREND:

            bullish += self.learning.apply("market_regime", 5)

            reasons.append(
                "Broad market regime is bullish."
            )

        elif regime == MarketRegime.BEAR_TREND:

            bearish += self.learning.apply("market_regime", 5)

            warnings.append(
                "Broad market regime is bearish."
            )

        elif regime == MarketRegime.HIGH_VOLATILITY:

            bearish += self.learning.apply("market_regime", 7)

            warnings.append(
                "Market volatility is elevated."
            )

        elif regime in {
            MarketRegime.PANIC,
            MarketRegime.RISK_OFF,
        }:

            bearish += self.learning.apply("market_regime", 20)

            warnings.append(
                "Market regime is risk-off."
            )

        # ====================================================
        # FINAL SCORE
        # ====================================================

        bullish = self._clamp(
            bullish
        )

        bearish = self._clamp(
            bearish
        )

        raw_score = (
            bullish
            - (
                bearish * 0.70
            )
        )

        score = self._clamp(
            raw_score
        )

        probability = (
            score / 100.0
        )

        confidence = 0.0

        if possible_evidence > 0:

            confidence = (
                evidence_count
                / possible_evidence
            )

        confidence = max(
            0.0,
            min(
                confidence,
                1.0,
            ),
        )

        # ====================================================
        # ACTION
        # ====================================================

        if (
            score
            >= self.minimum_buy_score
        ):
            action = SignalAction.BUY

        elif (
            score
            >= self.minimum_watch_score
        ):
            action = SignalAction.WATCH

        else:
            action = SignalAction.REJECT

        return AIAnalysis(
            symbol=symbol,

            action=action,

            score=round(
                score,
                2,
            ),

            probability=round(
                probability,
                4,
            ),

            confidence=round(
                confidence,
                4,
            ),

            bullish_score=round(
                bullish,
                2,
            ),

            bearish_score=round(
                bearish,
                2,
            ),

            reasons=reasons,
            warnings=warnings,

            metadata={
                "engine": "JALWE_V4_AI",
                "mode": "DETERMINISTIC",
                "evidence_count": (
                    evidence_count
                ),
                "possible_evidence": (
                    possible_evidence
                ),
                "learning_enabled": (
                    self.learning.enabled
                ),
                "learning_profile": (
                    self.learning.profile()
                ),
            },
        )