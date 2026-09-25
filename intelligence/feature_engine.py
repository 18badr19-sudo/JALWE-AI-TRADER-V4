from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.models import FeatureSnapshot


logger = logging.getLogger(__name__)


NEW_YORK_TZ = ZoneInfo("America/New_York")


class FeatureEngine:
    """
    JALWE V4 - Central Feature Engine.

    Converts raw OHLCV market data into the canonical
    FeatureSnapshot defined in core.models.

    Features include:
    - Returns
    - RVOL
    - Volume acceleration
    - Session VWAP
    - ATR
    - RSI
    - EMA / SMA
    - Market structure
    - Momentum score
    - Liquidity score

    This engine:
    - does not place trades
    - does not make the final BUY/SELL decision
    - does not fabricate missing market data
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
    ) -> None:

        if rvol_window <= 1:
            raise ValueError(
                "rvol_window must be greater than 1."
            )

        if atr_period <= 1:
            raise ValueError(
                "atr_period must be greater than 1."
            )

        if rsi_period <= 1:
            raise ValueError(
                "rsi_period must be greater than 1."
            )

        self.rvol_window = rvol_window
        self.atr_period = atr_period
        self.rsi_period = rsi_period

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:

        try:
            converted = float(value)

            if not np.isfinite(converted):
                return None

            return converted

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _timestamp_from_index(
        value: Any,
    ) -> datetime:

        timestamp = pd.Timestamp(value)

        if timestamp.tzinfo is None:

            timestamp = timestamp.tz_localize(
                "UTC"
            )

        else:

            timestamp = timestamp.tz_convert(
                "UTC"
            )

        return timestamp.to_pydatetime()

    @staticmethod
    def _ensure_utc_index(
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Ensure DataFrame uses a timezone-aware UTC DatetimeIndex.
        """

        normalized = df.copy()

        index = pd.to_datetime(
            normalized.index,
            utc=True,
            errors="coerce",
        )

        valid_mask = ~index.isna()

        normalized = normalized.loc[
            valid_mask
        ].copy()

        normalized.index = index[
            valid_mask
        ]

        return normalized.sort_index()

    # ========================================================
    # RSI
    # ========================================================

    @staticmethod
    def _calculate_rsi(
        close: pd.Series,
        period: int = 14,
    ) -> pd.Series:

        delta = close.diff()

        gain = delta.clip(
            lower=0.0
        )

        loss = -delta.clip(
            upper=0.0
        )

        average_gain = gain.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        average_loss = loss.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        relative_strength = (
            average_gain
            / average_loss.replace(
                0,
                np.nan,
            )
        )

        rsi = (
            100
            - (
                100
                / (
                    1
                    + relative_strength
                )
            )
        )

        return rsi.clip(
            0,
            100,
        )

    # ========================================================
    # ATR
    # ========================================================

    @staticmethod
    def _calculate_atr(
        df: pd.DataFrame,
        period: int = 14,
    ) -> pd.Series:

        previous_close = (
            df["close"]
            .shift(1)
        )

        true_range = pd.concat(
            [
                (
                    df["high"]
                    - df["low"]
                ),

                (
                    df["high"]
                    - previous_close
                ).abs(),

                (
                    df["low"]
                    - previous_close
                ).abs(),
            ],
            axis=1,
        ).max(
            axis=1
        )

        return true_range.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

    # ========================================================
    # SESSION VWAP
    # ========================================================

    @staticmethod
    def _calculate_session_vwap(
        df: pd.DataFrame,
    ) -> pd.Series:
        """
        Calculate VWAP independently for each US market date.

        Important:
        The reset is based on America/New_York calendar date,
        not UTC date.

        Example:
        Yesterday's volume does not affect today's VWAP.
        """

        if df.empty:
            return pd.Series(
                index=df.index,
                dtype=float,
            )

        typical_price = (
            df["high"]
            + df["low"]
            + df["close"]
        ) / 3.0

        volume = (
            df["volume"]
            .astype(float)
        )

        new_york_index = (
            df.index
            .tz_convert(
                NEW_YORK_TZ
            )
        )

        session_date = pd.Series(
            new_york_index.date,
            index=df.index,
        )

        price_volume = (
            typical_price
            * volume
        )

        cumulative_price_volume = (
            price_volume
            .groupby(
                session_date
            )
            .cumsum()
        )

        cumulative_volume = (
            volume
            .groupby(
                session_date
            )
            .cumsum()
        )

        return (
            cumulative_price_volume
            / cumulative_volume.replace(
                0,
                np.nan,
            )
        )

    # ========================================================
    # VALIDATION
    # ========================================================

    def _minimum_rows(
        self,
    ) -> int:

        return max(
            self.rvol_window + 1,
            self.atr_period + 1,
            self.rsi_period + 1,
            51,
        )

    def _validate_dataframe(
        self,
        df: pd.DataFrame,
    ) -> bool:

        if (
            df is None
            or df.empty
        ):
            return False

        if not self.REQUIRED_COLUMNS.issubset(
            df.columns
        ):
            return False

        if len(df) < self._minimum_rows():
            return False

        return True

    def _quality_metadata(
        self,
        df: Optional[pd.DataFrame],
        *,
        stage: str,
    ) -> dict[str, Any]:
        required_rows = self._minimum_rows()

        if df is None:
            return {
                "validation_stage": stage,
                "bars_received": 0,
                "required_rows": required_rows,
                "missing_columns": sorted(
                    self.REQUIRED_COLUMNS
                ),
                "last_bar_timestamp": None,
                "last_bar_age_minutes": None,
            }

        bars_received = len(df)

        columns = set(
            getattr(
                df,
                "columns",
                [],
            )
        )

        missing_columns = sorted(
            self.REQUIRED_COLUMNS
            - columns
        )

        last_bar_timestamp = None
        last_bar_age_minutes = None

        if bars_received > 0:
            try:
                latest = pd.Timestamp(
                    df.index[-1]
                )

                if latest.tzinfo is None:
                    latest = latest.tz_localize(
                        "UTC"
                    )
                else:
                    latest = latest.tz_convert(
                        "UTC"
                    )

                now = pd.Timestamp.now(
                    tz="UTC"
                )

                age = (
                    now
                    - latest
                ).total_seconds() / 60.0

                last_bar_timestamp = (
                    latest.isoformat()
                )

                last_bar_age_minutes = round(
                    max(
                        float(age),
                        0.0,
                    ),
                    2,
                )

            except Exception:
                pass

        return {
            "validation_stage": stage,
            "bars_received": int(
                bars_received
            ),
            "required_rows": int(
                required_rows
            ),
            "missing_columns": (
                missing_columns
            ),
            "last_bar_timestamp": (
                last_bar_timestamp
            ),
            "last_bar_age_minutes": (
                last_bar_age_minutes
            ),
        }

    # ========================================================
    # BUILD FEATURES
    # ========================================================

    def build(
        self,
        symbol: str,
        bars: pd.DataFrame,
    ) -> FeatureSnapshot:

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not symbol:
            raise ValueError(
                "Symbol cannot be empty."
            )

        if not self._validate_dataframe(
            bars
        ):
            quality = self._quality_metadata(
                bars,
                stage="RAW_INPUT",
            )

            logger.warning(
                "Insufficient market data "
                "for feature generation | "
                "symbol=%s bars=%s required=%s "
                "missing=%s last_age_min=%s",
                symbol,
                quality.get("bars_received"),
                quality.get("required_rows"),
                quality.get("missing_columns"),
                quality.get(
                    "last_bar_age_minutes"
                ),
            )

            return FeatureSnapshot(
                symbol=symbol,
                data_quality_ok=False,
                metadata={
                    "feature_engine": "JALWE_V4",
                    "quality_failure": quality,
                },
            )

        df = self._ensure_utc_index(
            bars
        )

        # ----------------------------------------------------
        # NUMERIC NORMALIZATION
        # ----------------------------------------------------

        for column in self.REQUIRED_COLUMNS:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        df = df.dropna(
            subset=list(
                self.REQUIRED_COLUMNS
            )
        )

        if not self._validate_dataframe(
            df
        ):
            quality = self._quality_metadata(
                df,
                stage="NORMALIZED_OHLCV",
            )

            return FeatureSnapshot(
                symbol=symbol,
                data_quality_ok=False,
                metadata={
                    "feature_engine": "JALWE_V4",
                    "quality_failure": quality,
                },
            )

        close = df["close"]
        volume = df["volume"]

        # ----------------------------------------------------
        # MOVING AVERAGES
        # ----------------------------------------------------

        df["ema_9"] = close.ewm(
            span=9,
            adjust=False,
        ).mean()

        df["ema_20"] = close.ewm(
            span=20,
            adjust=False,
        ).mean()

        df["sma_50"] = close.rolling(
            window=50
        ).mean()

        # ----------------------------------------------------
        # RSI / ATR / SESSION VWAP
        # ----------------------------------------------------

        df["rsi_14"] = self._calculate_rsi(
            close,
            self.rsi_period,
        )

        df["atr"] = self._calculate_atr(
            df,
            self.atr_period,
        )

        df["vwap"] = (
            self._calculate_session_vwap(
                df
            )
        )

        # ----------------------------------------------------
        # VOLUME / RVOL
        # ----------------------------------------------------

        df["avg_volume"] = (
            volume
            .shift(1)
            .rolling(
                self.rvol_window
            )
            .mean()
        )

        df["rvol"] = (
            volume
            / df["avg_volume"].replace(
                0,
                np.nan,
            )
        )

        df["volume_acceleration"] = (
            volume
            / volume.shift(1).replace(
                0,
                np.nan,
            )
        )

        # ----------------------------------------------------
        # RETURNS
        # ----------------------------------------------------

        df["return_1"] = (
            close.pct_change()
            * 100.0
        )

        df["return_5"] = (
            close.pct_change(5)
            * 100.0
        )

        # ----------------------------------------------------
        # STRUCTURE
        # ----------------------------------------------------

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
            / longer_range.replace(
                0,
                np.nan,
            )
        )

        # ----------------------------------------------------
        # LATEST BAR
        # ----------------------------------------------------

        latest = df.iloc[-1]

        timestamp = self._timestamp_from_index(
            df.index[-1]
        )

        price = self._safe_float(
            latest["close"]
        )

        latest_volume = self._safe_float(
            latest["volume"]
        )

        average_volume = self._safe_float(
            latest["avg_volume"]
        )

        rvol = self._safe_float(
            latest["rvol"]
        )

        volume_acceleration = self._safe_float(
            latest[
                "volume_acceleration"
            ]
        )

        vwap = self._safe_float(
            latest["vwap"]
        )

        atr = self._safe_float(
            latest["atr"]
        )

        ema_9 = self._safe_float(
            latest["ema_9"]
        )

        ema_20 = self._safe_float(
            latest["ema_20"]
        )

        sma_50 = self._safe_float(
            latest["sma_50"]
        )

        rsi = self._safe_float(
            latest["rsi_14"]
        )

        high_20 = self._safe_float(
            latest["high_20"]
        )

        low_20 = self._safe_float(
            latest["low_20"]
        )

        # ----------------------------------------------------
        # DERIVED VALUES
        # ----------------------------------------------------

        dollar_volume = None

        if (
            price is not None
            and latest_volume is not None
        ):
            dollar_volume = (
                price
                * latest_volume
            )

        distance_from_vwap_pct = None

        if (
            price is not None
            and vwap is not None
            and vwap > 0
        ):
            distance_from_vwap_pct = (
                (
                    price
                    - vwap
                )
                / vwap
            ) * 100.0

        above_vwap = None

        if (
            price is not None
            and vwap is not None
        ):
            above_vwap = (
                price > vwap
            )

        atr_pct = None

        if (
            price is not None
            and atr is not None
            and price > 0
        ):
            atr_pct = (
                atr
                / price
            ) * 100.0

        distance_to_high_20_pct = None

        if (
            price is not None
            and high_20 is not None
            and high_20 > 0
        ):
            distance_to_high_20_pct = (
                (
                    high_20
                    - price
                )
                / high_20
            ) * 100.0

        # ----------------------------------------------------
        # MOMENTUM SCORE
        # ----------------------------------------------------

        momentum_score = 0.0

        if (
            price is not None
            and ema_9 is not None
            and price > ema_9
        ):
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
                max(
                    rvol - 1.0,
                    0.0,
                ) * 15.0,
                20.0,
            )

        if (
            volume_acceleration is not None
            and volume_acceleration > 1.0
        ):
            momentum_score += min(
                (
                    volume_acceleration
                    - 1.0
                ) * 10.0,
                10.0,
            )

        momentum_score = min(
            max(
                momentum_score,
                0.0,
            ),
            100.0,
        )

        # ----------------------------------------------------
        # LIQUIDITY SCORE
        # ----------------------------------------------------

        liquidity_score = 0.0

        if dollar_volume is not None:

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
            max(
                liquidity_score,
                0.0,
            ),
            100.0,
        )

        # ----------------------------------------------------
        # FINAL SNAPSHOT
        # ----------------------------------------------------

        return FeatureSnapshot(
            symbol=symbol,
            timestamp=timestamp,

            price=price,

            return_1=self._safe_float(
                latest["return_1"]
            ),

            return_5=self._safe_float(
                latest["return_5"]
            ),

            volume=latest_volume,
            avg_volume=average_volume,

            rvol=rvol,

            dollar_volume=dollar_volume,

            volume_acceleration=(
                volume_acceleration
            ),

            liquidity_score=(
                liquidity_score
            ),

            vwap=vwap,

            distance_from_vwap_pct=(
                distance_from_vwap_pct
            ),

            above_vwap=above_vwap,

            atr=atr,
            atr_pct=atr_pct,

            ema_9=ema_9,
            ema_20=ema_20,
            sma_50=sma_50,

            rsi_14=rsi,

            momentum_score=(
                momentum_score
            ),

            high_20=high_20,
            low_20=low_20,

            distance_to_high_20_pct=(
                distance_to_high_20_pct
            ),

            range_compression=(
                self._safe_float(
                    latest[
                        "range_compression"
                    ]
                )
            ),

            data_quality_ok=True,

            metadata={
                "feature_engine": "JALWE_V4",
                "bars_used": len(df),
                "required_rows": (
                    self._minimum_rows()
                ),
                "vwap_mode": "NY_SESSION",
                "quality": (
                    self._quality_metadata(
                        df,
                        stage="FINAL",
                    )
                ),
            },
        )