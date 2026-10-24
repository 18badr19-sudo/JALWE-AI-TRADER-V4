from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from numbers import Real
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
            self._available(minimum_watch_score)
            and 0
            <= minimum_watch_score
            <= 100
        ):
            raise ValueError(
                "minimum_watch_score must be between 0 and 100."
            )

        if not (
            self._available(minimum_buy_score)
            and 0
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

        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)

    # ========================================================
    # EVALUATION
    # ========================================================

    def evaluate(
        self,
        features: FeatureSnapshot,
    ) -> AIAnalysis:

        symbol = features.symbol.upper()

        # Validate our own contract even when the upstream quality flag is true.
        # Never change the caller's snapshot or let bool/NaN count as evidence.
        invalid_inputs = []
        sanitized = {}
        domains = {
            "price": (0, None, True), "vwap": (0, None, True),
            "ema_9": (0, None, True), "ema_20": (0, None, True),
            "rvol": (0, None, False), "range_compression": (0, None, False),
            "distance_from_vwap_pct": (None, None, False),
            "distance_to_high_20_pct": (None, None, False),
            **{name: (0, 100, False) for name in (
                "liquidity_score", "momentum_score", "rsi_14",
                "news_score", "options_flow_score")},
        }
        for name, (low, high, positive) in domains.items():
            value = getattr(features, name)
            valid = self._available(value)
            if valid:
                valid = ((low is None or value >= low)
                         and (not positive or value > 0)
                         and (high is None or value <= high))
            if value is not None and not valid:
                invalid_inputs.append(name)
            sanitized[name] = float(value) if valid else None
        if type(features.above_vwap) is not bool:
            sanitized["above_vwap"] = None
            if features.above_vwap is not None:
                invalid_inputs.append("above_vwap")
        features = replace(features, **sanitized)
        if features.price is None:
            return AIAnalysis(
                symbol=symbol, action=SignalAction.REJECT, score=0.0,
                probability=0.0, confidence=0.0, bullish_score=0.0, bearish_score=0.0,
                reasons=["A finite positive price is required."],
                warnings=["Invalid or missing mandatory price."],
                metadata={"mode": "DETERMINISTIC", "input_validation": "REJECTED",
                          "invalid_inputs": sorted(set(invalid_inputs + ["price"])),
                          "probability_kind": "SCORE_PROXY_NOT_CALIBRATED",
                          "confidence_kind": "FEATURE_COVERAGE",
                          "evidence_count": 0, "possible_evidence": 10,
                          "factor_breakdown": []})

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
                confidence=0.0,
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
                confidence=0.0,
                bullish_score=0.0,
                bearish_score=100.0,
                reasons=[
                    "Market data is stale."
                ],
                warnings=[
                    "Fresh market data is required."
                ],
            )

        if invalid_inputs:
            warnings.append("Invalid optional AI inputs excluded: " + ", ".join(sorted(invalid_inputs)))
        factor_breakdown = []

        def points(factor, base_points, side):
            weighted = self.learning.apply(factor, base_points)
            if not self._available(weighted) or weighted < 0:
                raise ValueError("Invalid learned factor weight: " + factor)
            factor_breakdown.append({"factor": factor, "side": side,
                                     "base_points": base_points, "weighted_points": weighted})
            return weighted

        bullish = 0.0
        bearish = 0.0

        feature_metadata = getattr(
            features,
            "metadata",
            {},
        )
        if not isinstance(feature_metadata, dict):
            feature_metadata = {}

        override = feature_metadata.get("scoring_rvol")
        if override is not None and (not self._available(override) or override < 0):
            invalid_inputs.append("metadata.scoring_rvol")
            warnings.append("Invalid metadata.scoring_rvol excluded.")
            override = None
        scoring_rvol = max(features.rvol or 0.0, override or 0.0)

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
                bullish += points("liquidity", 18, "bullish")
                reasons.append(
                    "Strong liquidity conditions."
                )

            elif liquidity >= 60:
                bullish += points("liquidity", 12, "bullish")
                reasons.append(
                    "Acceptable liquidity."
                )

            elif liquidity < 30:
                bearish += points("liquidity", 15, "bearish")
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

            if scoring_rvol >= 3.0:
                bullish += points("rvol", 18, "bullish")
                reasons.append(
                    "Very strong recent relative volume."
                )

            elif scoring_rvol >= 2.0:
                bullish += points("rvol", 14, "bullish")
                reasons.append(
                    "Strong recent relative volume."
                )

            elif scoring_rvol >= 1.5:
                bullish += points("rvol", 9, "bullish")
                reasons.append(
                    "Elevated recent relative volume."
                )

            elif scoring_rvol < 0.8:
                bearish += points("rvol", 8, "bearish")
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
            and type(features.above_vwap) is bool
        ):
            evidence_count += 1

            if features.above_vwap is True:

                bullish += points("vwap", 14, "bullish")

                reasons.append(
                    "Price is above session VWAP."
                )

            else:

                bearish += points("vwap", 10, "bearish")

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
                    bullish += points("vwap", 4, "bullish")

                    reasons.append(
                        "Price is holding close above VWAP."
                    )

                elif distance < -2.0:
                    bearish += points("vwap", 5, "bearish")

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
                bullish += points("trend", 14, "bullish")

                reasons.append(
                    "Short-term trend structure is bullish."
                )

            elif (
                features.price
                < features.ema_9
                < features.ema_20
            ):
                bearish += points("trend", 12, "bearish")

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
                bullish += points("momentum", 14, "bullish")

                reasons.append(
                    "Momentum score is very strong."
                )

            elif momentum >= 60:
                bullish += points("momentum", 10, "bullish")

                reasons.append(
                    "Momentum is supportive."
                )

            elif momentum < 30:
                bearish += points("momentum", 8, "bearish")

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

                bullish += points("rsi", 8, "bullish")

                reasons.append(
                    "RSI supports bullish momentum."
                )

            elif 40 <= rsi < 50:

                bullish += points("rsi", 3, "bullish")

            elif rsi > 80:

                bearish += points("rsi", 5, "bearish")

                warnings.append(
                    "RSI is extremely extended."
                )

            elif rsi < 30:

                bearish += points("rsi", 4, "bearish")

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
                bullish += points("breakout", 8, "bullish")

                reasons.append(
                    "Price is near the 20-bar breakout level."
                )

            elif distance > 5.0:

                bearish += points("breakout", 3, "bearish")

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

                bullish += points("compression", 6, "bullish")

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
                bullish += points("news", 8, "bullish")

                reasons.append(
                    "News catalyst is supportive."
                )

            elif news_score <= 30:
                bearish += points("news", 8, "bearish")

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

                bullish += points("options_flow", 10, "bullish")

                reasons.append(
                    "Options flow is strongly supportive."
                )

            elif options_score <= 25:

                bearish += points("options_flow", 8, "bearish")

                warnings.append(
                    "Options flow is bearish."
                )

        # ====================================================
        # MARKET REGIME ADJUSTMENT
        # ====================================================

        regime = features.market_regime

        if regime == MarketRegime.BULL_TREND:

            bullish += points("market_regime", 5, "bullish")

            reasons.append(
                "Broad market regime is bullish."
            )

        elif regime == MarketRegime.BEAR_TREND:

            bearish += points("market_regime", 5, "bearish")

            warnings.append(
                "Broad market regime is bearish."
            )

        elif regime == MarketRegime.HIGH_VOLATILITY:

            bearish += points("market_regime", 7, "bearish")

            warnings.append(
                "Market volatility is elevated."
            )

        elif regime in {
            MarketRegime.PANIC,
            MarketRegime.RISK_OFF,
        }:

            bearish += points("market_regime", 20, "bearish")

            warnings.append(
                "Market regime is risk-off."
            )

        # ====================================================
        # FINAL SCORE
        # ====================================================

        raw_bullish, raw_bearish = bullish, bearish

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
                "input_validation": "VALIDATED",
                "invalid_inputs": sorted(invalid_inputs),
                "factor_breakdown": factor_breakdown,
                "score_formula": "clamp(clamp(bullish,0,100)-0.70*clamp(bearish,0,100),0,100)",
                "raw_bullish_score": raw_bullish,
                "raw_bearish_score": raw_bearish,
                "minimum_buy_score": self.minimum_buy_score,
                "minimum_watch_score": self.minimum_watch_score,
                "engine": "JALWE_V4_AI",
                "mode": "DETERMINISTIC",
                "probability_kind": "SCORE_PROXY_NOT_CALIBRATED",
                "confidence_kind": "FEATURE_COVERAGE",
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
