from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from market.market_data import get_market_data
from market.market_regime import (
    MarketRegime,
    MarketRegimeResult,
)


@dataclass
class MarketContext:
    spy_price: float

    spy_change_pct: float

    sma20: float
    sma50: float

    spy_above_sma20: bool
    spy_above_sma50: bool

    realized_volatility_pct: float

    trend_state: str

    regime: MarketRegime
    confidence: float

    reason: str


class MarketContextEngine:
    """
    JALWE V4 - Automatic SPY Market Context.

    Uses REAL SPY price data.

    It does not fabricate VIX.

    Later a true volatility-index provider can be
    connected independently.
    """

    def __init__(self) -> None:
        self.market_data = get_market_data()

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _realized_volatility(
        closes: pd.Series,
        lookback: int = 20,
    ) -> float:

        returns = (
            closes
            .pct_change()
            .dropna()
            .tail(lookback)
        )

        if len(returns) < 5:
            return 0.0

        volatility = (
            returns.std()
            * (252 ** 0.5)
            * 100.0
        )

        return float(volatility)

    # ========================================================
    # LOAD MARKET CONTEXT
    # ========================================================

    def analyze(
        self,
    ) -> MarketContext:

        df = self.market_data.get_bars(
            "SPY",
            "1d",
            70,
        )

        if df is None or len(df) < 52:
            raise RuntimeError(
                "Not enough SPY daily data "
                "to calculate market regime."
            )

        df = df.copy()

        closes = (
            pd.to_numeric(
                df["close"],
                errors="coerce",
            )
            .dropna()
        )

        if len(closes) < 52:
            raise RuntimeError(
                "SPY close history is incomplete."
            )

        current = float(
            closes.iloc[-1]
        )

        previous = float(
            closes.iloc[-2]
        )

        if previous <= 0:
            raise RuntimeError(
                "Invalid SPY previous close."
            )

        change_pct = (
            (current - previous)
            / previous
            * 100.0
        )

        sma20 = float(
            closes
            .tail(20)
            .mean()
        )

        sma50 = float(
            closes
            .tail(50)
            .mean()
        )

        above20 = (
            current > sma20
        )

        above50 = (
            current > sma50
        )

        realized_vol = (
            self._realized_volatility(
                closes,
                20,
            )
        )

        # ====================================================
        # PANIC / EXTREME VOLATILITY
        # ====================================================

        if (
            realized_vol >= 45
            and change_pct <= -1.5
        ):

            return MarketContext(
                spy_price=current,

                spy_change_pct=change_pct,

                sma20=sma20,
                sma50=sma50,

                spy_above_sma20=above20,
                spy_above_sma50=above50,

                realized_volatility_pct=(
                    realized_vol
                ),

                trend_state="BEARISH",

                regime=MarketRegime.PANIC,

                confidence=0.90,

                reason=(
                    "Extreme SPY realized volatility "
                    "with strong downside move."
                ),
            )

        # ====================================================
        # HIGH VOLATILITY
        # ====================================================

        if realized_vol >= 30:

            if (
                above20
                and above50
            ):
                trend = "BULLISH"

            elif (
                not above20
                and not above50
            ):
                trend = "BEARISH"

            else:
                trend = "MIXED"

            return MarketContext(
                spy_price=current,

                spy_change_pct=change_pct,

                sma20=sma20,
                sma50=sma50,

                spy_above_sma20=above20,
                spy_above_sma50=above50,

                realized_volatility_pct=(
                    realized_vol
                ),

                trend_state=trend,

                regime=(
                    MarketRegime.HIGH_VOLATILITY
                ),

                confidence=0.80,

                reason=(
                    "SPY realized volatility "
                    "is elevated."
                ),
            )

        # ====================================================
        # BULL TREND
        # ====================================================

        if (
            above20
            and above50
            and sma20 > sma50
        ):

            confidence = 0.75

            if change_pct >= 0.75:
                confidence += 0.10

            return MarketContext(
                spy_price=current,

                spy_change_pct=change_pct,

                sma20=sma20,
                sma50=sma50,

                spy_above_sma20=True,
                spy_above_sma50=True,

                realized_volatility_pct=(
                    realized_vol
                ),

                trend_state="BULLISH",

                regime=(
                    MarketRegime.BULL_TREND
                ),

                confidence=min(
                    confidence,
                    1.0,
                ),

                reason=(
                    "SPY is above SMA20 and SMA50 "
                    "with bullish trend structure."
                ),
            )

        # ====================================================
        # BEAR TREND
        # ====================================================

        if (
            not above20
            and not above50
            and sma20 < sma50
        ):

            confidence = 0.75

            if change_pct <= -0.75:
                confidence += 0.10

            return MarketContext(
                spy_price=current,

                spy_change_pct=change_pct,

                sma20=sma20,
                sma50=sma50,

                spy_above_sma20=False,
                spy_above_sma50=False,

                realized_volatility_pct=(
                    realized_vol
                ),

                trend_state="BEARISH",

                regime=(
                    MarketRegime.BEAR_TREND
                ),

                confidence=min(
                    confidence,
                    1.0,
                ),

                reason=(
                    "SPY is below SMA20 and SMA50 "
                    "with bearish trend structure."
                ),
            )

        # ====================================================
        # SIDEWAYS / MIXED
        # ====================================================

        return MarketContext(
            spy_price=current,

            spy_change_pct=change_pct,

            sma20=sma20,
            sma50=sma50,

            spy_above_sma20=above20,
            spy_above_sma50=above50,

            realized_volatility_pct=(
                realized_vol
            ),

            trend_state="SIDEWAYS",

            regime=MarketRegime.SIDEWAYS,

            confidence=0.65,

            reason=(
                "SPY trend structure is mixed "
                "or non-directional."
            ),
        )

    # ========================================================
    # ROUTER FORMAT
    # ========================================================

    def get_regime_result(
        self,
    ) -> MarketRegimeResult:

        context = self.analyze()

        return MarketRegimeResult(
            regime=context.regime,

            confidence=context.confidence,

            spy_change_pct=(
                context.spy_change_pct
            ),

            spy_above_sma20=(
                context.spy_above_sma20
            ),

            spy_above_sma50=(
                context.spy_above_sma50
            ),

            # We deliberately leave VIX unavailable
            # rather than inventing a VIX value.
            vix_level=None,

            volatility_state=(
                "HIGH"
                if context.regime
                == MarketRegime.HIGH_VOLATILITY
                else (
                    "PANIC"
                    if context.regime
                    == MarketRegime.PANIC
                    else "NORMAL"
                )
            ),

            trend_state=(
                context.trend_state
            ),

            reason=context.reason,
        )


_market_context_engine: Optional[
    MarketContextEngine
] = None


def get_market_context_engine(
) -> MarketContextEngine:

    global _market_context_engine

    if _market_context_engine is None:
        _market_context_engine = (
            MarketContextEngine()
        )

    return _market_context_engine