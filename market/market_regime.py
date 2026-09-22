from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional


logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    PANIC = "PANIC"
    UNKNOWN = "UNKNOWN"


@dataclass
class MarketRegimeResult:
    regime: MarketRegime
    confidence: float

    spy_change_pct: Optional[float] = None
    spy_above_sma20: Optional[bool] = None
    spy_above_sma50: Optional[bool] = None

    vix_level: Optional[float] = None

    volatility_state: str = "UNKNOWN"
    trend_state: str = "UNKNOWN"

    reason: str = ""


class MarketRegimeEngine:
    """
    JALWE V4 - Market Regime Engine

    يحدد حالة السوق العامة قبل السماح لباقي النظام
    بتقييم فرص التداول.

    هذا المحرك لا ينفذ صفقات ولا يحدد حجم الصفقة.
    القرار النهائي للمخاطرة يبقى داخل Risk Engine.
    """

    def __init__(
        self,
        high_vix_threshold: float = 25.0,
        panic_vix_threshold: float = 35.0,
        strong_move_threshold: float = 0.75,
    ):
        self.high_vix_threshold = high_vix_threshold
        self.panic_vix_threshold = panic_vix_threshold
        self.strong_move_threshold = strong_move_threshold

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        return max(0.0, min(float(value), 1.0))

    def detect(
        self,
        spy_change_pct: Optional[float],
        vix_level: Optional[float],
        spy_above_sma20: Optional[bool] = None,
        spy_above_sma50: Optional[bool] = None,
    ) -> MarketRegimeResult:

        if spy_change_pct is None or vix_level is None:
            return MarketRegimeResult(
                regime=MarketRegime.UNKNOWN,
                confidence=0.0,
                spy_change_pct=spy_change_pct,
                vix_level=vix_level,
                spy_above_sma20=spy_above_sma20,
                spy_above_sma50=spy_above_sma50,
                reason="Required market regime data is unavailable.",
            )

        try:
            spy_change_pct = float(spy_change_pct)
            vix_level = float(vix_level)
        except (TypeError, ValueError):
            logger.warning("Invalid market regime input data.")

            return MarketRegimeResult(
                regime=MarketRegime.UNKNOWN,
                confidence=0.0,
                reason="Invalid market regime input data.",
            )

        volatility_state = "NORMAL"

        if vix_level >= self.panic_vix_threshold:
            volatility_state = "PANIC"

        elif vix_level >= self.high_vix_threshold:
            volatility_state = "HIGH"

        trend_state = "SIDEWAYS"

        if spy_above_sma20 is True and spy_above_sma50 is True:
            trend_state = "BULLISH"

        elif spy_above_sma20 is False and spy_above_sma50 is False:
            trend_state = "BEARISH"

        elif spy_change_pct >= self.strong_move_threshold:
            trend_state = "BULLISH"

        elif spy_change_pct <= -self.strong_move_threshold:
            trend_state = "BEARISH"

        # -------------------------
        # PANIC
        # -------------------------

        if volatility_state == "PANIC":
            return MarketRegimeResult(
                regime=MarketRegime.PANIC,
                confidence=0.95,
                spy_change_pct=spy_change_pct,
                spy_above_sma20=spy_above_sma20,
                spy_above_sma50=spy_above_sma50,
                vix_level=vix_level,
                volatility_state=volatility_state,
                trend_state=trend_state,
                reason="VIX reached panic threshold.",
            )

        # -------------------------
        # HIGH VOLATILITY
        # -------------------------

        if volatility_state == "HIGH":
            confidence = 0.80

            if abs(spy_change_pct) >= self.strong_move_threshold:
                confidence += 0.05

            return MarketRegimeResult(
                regime=MarketRegime.HIGH_VOLATILITY,
                confidence=self._clamp_confidence(confidence),
                spy_change_pct=spy_change_pct,
                spy_above_sma20=spy_above_sma20,
                spy_above_sma50=spy_above_sma50,
                vix_level=vix_level,
                volatility_state=volatility_state,
                trend_state=trend_state,
                reason="VIX indicates elevated market volatility.",
            )

        # -------------------------
        # BULL TREND
        # -------------------------

        if trend_state == "BULLISH":
            confidence = 0.70

            if spy_change_pct >= self.strong_move_threshold:
                confidence += 0.10

            if spy_above_sma20 is True:
                confidence += 0.05

            if spy_above_sma50 is True:
                confidence += 0.05

            return MarketRegimeResult(
                regime=MarketRegime.BULL_TREND,
                confidence=self._clamp_confidence(confidence),
                spy_change_pct=spy_change_pct,
                spy_above_sma20=spy_above_sma20,
                spy_above_sma50=spy_above_sma50,
                vix_level=vix_level,
                volatility_state=volatility_state,
                trend_state=trend_state,
                reason="SPY trend structure is bullish.",
            )

        # -------------------------
        # BEAR TREND
        # -------------------------

        if trend_state == "BEARISH":
            confidence = 0.70

            if spy_change_pct <= -self.strong_move_threshold:
                confidence += 0.10

            if spy_above_sma20 is False:
                confidence += 0.05

            if spy_above_sma50 is False:
                confidence += 0.05

            return MarketRegimeResult(
                regime=MarketRegime.BEAR_TREND,
                confidence=self._clamp_confidence(confidence),
                spy_change_pct=spy_change_pct,
                spy_above_sma20=spy_above_sma20,
                spy_above_sma50=spy_above_sma50,
                vix_level=vix_level,
                volatility_state=volatility_state,
                trend_state=trend_state,
                reason="SPY trend structure is bearish.",
            )

        # -------------------------
        # SIDEWAYS
        # -------------------------

        return MarketRegimeResult(
            regime=MarketRegime.SIDEWAYS,
            confidence=0.65,
            spy_change_pct=spy_change_pct,
            spy_above_sma20=spy_above_sma20,
            spy_above_sma50=spy_above_sma50,
            vix_level=vix_level,
            volatility_state=volatility_state,
            trend_state=trend_state,
            reason="No strong directional market regime detected.",
        )
