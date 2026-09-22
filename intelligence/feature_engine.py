from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


@dataclass
class FeatureSnapshot:
    """
    مجموعة الخصائص الموحدة التي ستصل إلى AI Engine.
    """

    symbol: str

    price: Optional[float] = None
    return_1: Optional[float] = None
    return_5: Optional[float] = None

    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    rvol: Optional[float] = None
    dollar_volume: Optional[float] = None
    volume_acceleration: Optional[float] = None

    vwap: Optional[float] = None
    distance_from_vwap_pct: Optional[float] = None
    above_vwap: Optional[bool] = None

    atr: Optional[float] = None
    atr_pct: Optional[float] = None

    ema_9: Optional[float] = None
    ema_20: Optional[float] = None
    sma_50: Optional[float] = None

    rsi_14: Optional[float] = None

    high_20: Optional[float] = None
    low_20: Optional[float] = None
    distance_to_high_20_pct: Optional[float] = None

    range_compression: Optional[float] = None

    momentum_score: Optional[float] = None
    liquidity_score: Optional[float] = None

    data_quality_ok: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FeatureEngine:
    """
    JALWE V4 - Feature Engine

    يحول بيانات OHLCV الخام إلى خصائص موحدة تستخدمها:
    - Opportunity Engine
    - AI Engine
    - Risk Engine
    - Learning Engine

    لا ينفذ صفقات ولا يصدر أمر شراء أو بيع.
    """

    REQUIRED_COLUMNS = {
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    def __init__(
        self,
        rvol_window: int = 20,
        atr_period: int = 14,
        rsi_period: int = 14,
    ):
        self.rvol_window = rvol_window
        self.atr_period = atr_period
        self.rsi_period = rsi_period

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            value = float(value)

            if not np.isfinite(value):
                return None

            return value

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _calculate_rsi(
        close: pd.Series,
        period: int = 14,
    ) -> pd.Series:

        delta = close.diff()

        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)

        avg_gain = gain.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        avg_loss = loss.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)

        rsi = 100 - (100 / (1 + rs))

        return rsi.clip(0, 100)

    @staticmethod
    def _calculate_atr(
        df: pd.DataFrame,
        period: int = 14,
    ) -> pd.Series:

        previous_close = df["close"].shift(1)

        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - previous_close).abs(),
                (df["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        return true_range.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

    @staticmethod
    def _calculate_vwap(df: pd.DataFrame) -> pd.Series:
        typical_price = (
            df["high"]
            + df["low"]
            + df["close"]
        ) / 3.0

        cumulative_volume = df["volume"].cumsum()

        cumulative_price_volume = (
            typical_price * df["volume"]
        ).cumsum()

        return (
            cumulative_price_volume
            / cumulative_volume.replace(0, np.nan)
        )

    def _validate_dataframe(
        self,
        df: pd.DataFrame,
    ) -> bool:

        if df is None or df.empty:
            return False

        if not self.REQUIRED_COLUMNS.issubset(df.columns):
            return False

        minimum_rows = max(
            self.rvol_window + 1,
            self.atr_period + 1,
            self.rsi_period + 1,
            51,
        )

        if len(df) < minimum_rows:
            return False

        return True

    def build(
        self,
        symbol: str,
        bars: pd.DataFrame,
    ) -> FeatureSnapshot:

        symbol = str(symbol).strip().upper()

        if not self._validate_dataframe(bars):
            logger.warning(
                "Insufficient or invalid market data for %s.",
                symbol,
            )

            return FeatureSnapshot(
                symbol=symbol,
                data_quality_ok=False,
            )

        df = bars.copy()

        for column in self.REQUIRED_COLUMNS:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        df = df.dropna(
            subset=list(self.REQUIRED_COLUMNS)
        )

        if not self._validate_dataframe(df):
            return FeatureSnapshot(
                symbol=symbol,
                data_quality_ok=False,
            )

        close = df["close"]
        volume = df["volume"]

        df["ema_9"] = close.ewm(
            span=9,
            adjust=False,
        ).mean()

        df["ema_20"] = close.ewm(
            span=20,
            adjust=False,
        ).mean()

        df["sma_50"] = close.rolling(
            50
        ).mean()

        df["rsi_14"] = self._calculate_rsi(
            close,
            self.rsi_period,
        )

        df["atr"] = self._calculate_atr(
            df,
            self.atr_period,
        )

        df["vwap"] = self._calculate_vwap(df)

        df["avg_volume"] = (
            volume.shift(1)
            .rolling(self.rvol_window)
            .mean()
        )

        df["rvol"] = (
            volume
            / df["avg_volume"].replace(0, np.nan)
        )

        df["high_20"] = (
            df["high"]
            .shift(1)
            .rolling(20)
            .max()
        )

        df["low_20"] = (
            df["low"]
            .shift(1)
            .rolling(20)
            .min()
        )

        df["return_1"] = close.pct_change() * 100.0

        df["return_5"] = (
            close.pct_change(5) * 100.0
        )

        previous_volume = volume.shift(1)

        df["volume_acceleration"] = (
            volume
            / previous_volume.replace(0, np.nan)
        )

        recent_range = (
            df["high"]
            .rolling(5)
            .max()
            - df["low"]
            .rolling(5)
            .min()
        )

        longer_range = (
            df["high"]
            .rolling(20)
            .max()
            - df["low"]
            .rolling(20)
            .min()
        )

        df["range_compression"] = (
            recent_range
            / longer_range.replace(0, np.nan)
        )

        latest = df.iloc[-1]

        price = self._safe_float(latest["close"])
        vwap = self._safe_float(latest["vwap"])
        atr = self._safe_float(latest["atr"])
        high_20 = self._safe_float(latest["high_20"])

        distance_from_vwap_pct = None
        atr_pct = None
        distance_to_high_20_pct = None

        if price and vwap:
            distance_from_vwap_pct = (
                (price - vwap) / vwap
            ) * 100.0

        if price and atr:
            atr_pct = (
                atr / price
            ) * 100.0

        if price and high_20:
            distance_to_high_20_pct = (
                (high_20 - price) / high_20
            ) * 100.0

        rvol = self._safe_float(latest["rvol"])

        volume_acceleration = self._safe_float(
            latest["volume_acceleration"]
        )

        momentum_score = 0.0

        if price is not None:
            ema_9 = self._safe_float(latest["ema_9"])
            ema_20 = self._safe_float(latest["ema_20"])
            sma_50 = self._safe_float(latest["sma_50"])

            if ema_9 is not None and price > ema_9:
                momentum_score += 25.0

            if (
                ema_9 is not None
                and ema_20 is not None
                and ema_9 > ema_20
            ):
                momentum_score += 25.0

            if (
                ema_20 is not None
                and sma_50 is not None
                and ema_20 > sma_50
            ):
                momentum_score += 20.0

        if rvol is not None:
            momentum_score += min(
                max(rvol - 1.0, 0.0) * 15.0,
                20.0,
            )

        if (
            volume_acceleration is not None
            and volume_acceleration > 1.0
        ):
            momentum_score += min(
                (volume_acceleration - 1.0) * 10.0,
                10.0,
            )

        momentum_score = min(
            momentum_score,
            100.0,
        )

        liquidity_score = 0.0

        dollar_volume = None

        latest_volume = self._safe_float(
            latest["volume"]
        )

        if price is not None and latest_volume is not None:
            dollar_volume = price * latest_volume

            if dollar_volume >= 10_000_000:
                liquidity_score += 50.0

            elif dollar_volume >= 5_000_000:
                liquidity_score += 35.0

            elif dollar_volume >= 1_000_000:
                liquidity_score += 20.0

        if rvol is not None:
            if rvol >= 3.0:
                liquidity_score += 30.0

            elif rvol >= 2.0:
                liquidity_score += 25.0

            elif rvol >= 1.5:
                liquidity_score += 15.0

        if volume_acceleration is not None:
            if volume_acceleration >= 2.0:
                liquidity_score += 20.0

            elif volume_acceleration >= 1.25:
                liquidity_score += 10.0

        liquidity_score = min(
            liquidity_score,
            100.0,
        )

        return FeatureSnapshot(
            symbol=symbol,
            price=price,

            return_1=self._safe_float(
                latest["return_1"]
            ),

            return_5=self._safe_float(
                latest["return_5"]
            ),

            volume=latest_volume,

            avg_volume=self._safe_float(
                latest["avg_volume"]
            ),

            rvol=rvol,
            dollar_volume=dollar_volume,

            volume_acceleration=volume_acceleration,

            vwap=vwap,

            distance_from_vwap_pct=(
                distance_from_vwap_pct
            ),

            above_vwap=(
                price > vwap
                if price is not None
                and vwap is not None
                else None
            ),

            atr=atr,
            atr_pct=atr_pct,

            ema_9=self._safe_float(
                latest["ema_9"]
            ),

            ema_20=self._safe_float(
                latest["ema_20"]
            ),

            sma_50=self._safe_float(
                latest["sma_50"]
            ),

            rsi_14=self._safe_float(
                latest["rsi_14"]
            ),

            high_20=high_20,

            low_20=self._safe_float(
                latest["low_20"]
            ),

            distance_to_high_20_pct=(
                distance_to_high_20_pct
            ),

            range_compression=self._safe_float(
                latest["range_compression"]
            ),

            momentum_score=momentum_score,
            liquidity_score=liquidity_score,

            data_quality_ok=True,
        )
