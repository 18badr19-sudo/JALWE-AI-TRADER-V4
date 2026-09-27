from __future__ import annotations

import logging
import os

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

from sklearn.ensemble import RandomForestClassifier


logger = logging.getLogger(__name__)


# ============================================================
# DATA ERROR
# ============================================================

class PreBreakoutDataError(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        status: str = "DATA_ERROR",
    ) -> None:

        super().__init__(message)
        self.status = status


# ============================================================
# TIMEFRAME RESULT
# ============================================================

@dataclass
class TimeframeAnalysis:

    name: str
    alpaca_timeframe: str

    available: bool = False
    bars_used: int = 0

    score: float = 0.0

    current_price: Optional[float] = None

    rvol: Optional[float] = None
    volume_speed_ratio: Optional[float] = None

    resistance: Optional[float] = None
    distance_to_resistance_pct: Optional[float] = None

    compression: bool = False
    compression_ratio: Optional[float] = None

    vwap: Optional[float] = None
    above_vwap: Optional[bool] = None
    vwap_reclaimed: Optional[bool] = None

    ma10: Optional[float] = None
    ma20: Optional[float] = None

    trend: str = "UNKNOWN"

    momentum_3_pct: Optional[float] = None
    momentum_10_pct: Optional[float] = None

    upper_wick_ratio: Optional[float] = None

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None


# ============================================================
# FINAL RESULT
# ============================================================

@dataclass
class PreBreakoutResult:

    symbol: str

    score: float = 0.0
    multi_timeframe_score: float = 0.0
    data_confidence: float = 0.0

    score_5m: Optional[float] = None
    score_30m: Optional[float] = None
    score_1h: Optional[float] = None
    score_1d: Optional[float] = None

    status: str = "WATCH"
    ready: bool = False

    model_source: str = "MULTI_TIMEFRAME_RULES"
    ml_probability: Optional[float] = None

    radar_rank_score: Optional[float] = None
    radar_relative_volume: Optional[float] = None
    radar_change_pct: Optional[float] = None

    current_price: Optional[float] = None

    timeframe_5m: Optional[TimeframeAnalysis] = None
    timeframe_30m: Optional[TimeframeAnalysis] = None
    timeframe_1h: Optional[TimeframeAnalysis] = None
    timeframe_1d: Optional[TimeframeAnalysis] = None

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None


# ============================================================
# PRE BREAKOUT ENGINE V2.3.1
# ============================================================

class PreBreakoutEngine:
    """
    APEX PRE-BREAKOUT ENGINE V2.3.1

    RESEARCH ONLY.

    MULTI TIMEFRAME:

        5M  = 40%
        30M = 25%
        1H  = 20%
        1D  = 15%

    5M:
        - Timing
        - VWAP
        - Volume acceleration
        - Resistance proximity
        - Candle quality

    30M:
        - Compression
        - Structure
        - Resistance
        - Momentum

    1H:
        - Trend
        - MA structure
        - Momentum
        - Larger resistance

    1D:
        - Major trend
        - Daily resistance
        - Daily momentum
        - Extension risk

    IMPORTANT:
        This engine NEVER buys or sells.
        It only performs research for JALWE.
    """

    # ========================================================
    # TIMEFRAME WEIGHTS
    # ========================================================

    TIMEFRAME_WEIGHTS = {

        "5M": 0.40,

        "30M": 0.25,

        "1H": 0.20,

        "1D": 0.15,
    }

    # ========================================================
    # TIMEFRAME CONFIG
    #
    # lookback_days fixes the previous problem where
    # Alpaca only returned:
    #
    # 30M = 16 bars
    # 1H  = 8 bars
    # 1D  = 1 bar
    # ========================================================

    TIMEFRAME_CONFIG = {

        "5M": {
            "alpaca": "5Min",
            "limit": 100,
            "min_bars": 25,
            "lookback_days": 10,
        },

        "30M": {
            "alpaca": "30Min",
            "limit": 90,
            "min_bars": 20,
            "lookback_days": 30,
        },

        "1H": {
            "alpaca": "1Hour",
            "limit": 90,
            "min_bars": 20,
            "lookback_days": 60,
        },

        "1D": {
            "alpaca": "1Day",
            "limit": 90,
            "min_bars": 30,
            "lookback_days": 180,
        },
    }

    DEFAULT_MIN_TRAINING_SAMPLES = 40

    FEATURE_NAMES = [

        "rvol",

        "compression",

        "vwap_reclaimed",

        "distance_to_resistance",

        "volume_speed_high",
    ]

    REQUIRED_COLUMNS = {

        "open",

        "high",

        "low",

        "close",

        "volume",
    }

    # ========================================================
    # INIT
    # ========================================================

    def __init__(
        self,
        alpaca_api,
        learning_engine=None,
        *,
        min_training_samples: Optional[int] = None,
    ) -> None:

        self.alpaca = alpaca_api

        self.learning_engine = (
            learning_engine
        )

        if min_training_samples is None:

            try:

                min_training_samples = int(
                    os.getenv(
                        "PREBREAKOUT_MIN_TRAINING_SAMPLES",
                        str(
                            self.DEFAULT_MIN_TRAINING_SAMPLES
                        ),
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                min_training_samples = (
                    self.DEFAULT_MIN_TRAINING_SAMPLES
                )

        self.min_training_samples = max(
            10,
            int(
                min_training_samples
            ),
        )

        self.ai_model = (
            RandomForestClassifier(

                n_estimators=150,

                max_depth=6,

                min_samples_leaf=2,

                random_state=42,

                class_weight="balanced",
            )
        )

        self._is_trained = False

        self._training_samples = 0

        self._initial_train()

    # ========================================================
    # SYMBOL
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        symbol = str(
            symbol
            or ""
        ).strip().upper()

        if not symbol:

            raise ValueError(
                "Symbol cannot be empty."
            )

        return symbol

    # ========================================================
    # REAL ML TRAINING DATA
    # ========================================================

    def _fetch_training_data(
        self,
    ) -> tuple[list, list]:

        if self.learning_engine is None:

            return [], []

        method = getattr(
            self.learning_engine,
            "fetch_training_data",
            None,
        )

        if not callable(method):

            return [], []

        try:

            result = method()

        except Exception as exc:

            logger.warning(
                "PreBreakout training data error: %s",
                exc,
            )

            return [], []

        if (
            not isinstance(
                result,
                tuple,
            )
            or
            len(result) != 2
        ):

            return [], []

        X_data, y_data = result

        return (

            list(
                X_data
                or []
            ),

            list(
                y_data
                or []
            ),
        )

    # ========================================================
    # PREPARE TRAINING
    # ========================================================

    def _prepare_training_data(
        self,
        X_data: list,
        y_data: list,
    ) -> tuple[
        Optional[np.ndarray],
        Optional[np.ndarray],
    ]:

        if len(
            X_data
        ) != len(
            y_data
        ):

            return None, None

        if (
            len(X_data)
            <
            self.min_training_samples
        ):

            return None, None

        try:

            X = np.asarray(
                X_data,
                dtype=float,
            )

            y = np.asarray(
                y_data,
                dtype=int,
            )

        except Exception:

            return None, None

        if (
            X.ndim != 2
            or
            X.shape[1]
            != len(
                self.FEATURE_NAMES
            )
        ):

            return None, None

        if len(
            np.unique(y)
        ) < 2:

            return None, None

        if not np.isfinite(
            X
        ).all():

            return None, None

        return X, y

    # ========================================================
    # INITIAL TRAIN
    # ========================================================

    def _initial_train(
        self,
    ) -> None:

        X_data, y_data = (
            self._fetch_training_data()
        )

        self._training_samples = len(
            X_data
        )

        X, y = (
            self._prepare_training_data(
                X_data,
                y_data,
            )
        )

        if (
            X is None
            or
            y is None
        ):

            self._is_trained = False

            return

        self.ai_model.fit(
            X,
            y,
        )

        self._is_trained = True

        self._training_samples = len(
            X
        )

    # ========================================================
    # UPDATE REAL ML
    # ========================================================

    def update_model_with_real_data(
        self,
    ) -> bool:

        X_data, y_data = (
            self._fetch_training_data()
        )

        self._training_samples = len(
            X_data
        )

        X, y = (
            self._prepare_training_data(
                X_data,
                y_data,
            )
        )

        if (
            X is None
            or
            y is None
        ):

            return False

        self.ai_model.fit(
            X,
            y,
        )

        self._is_trained = True

        self._training_samples = len(
            X
        )

        return True

    # ========================================================
    # NORMALIZE MARKET DATA
    # ========================================================

    def _normalize_dataframe(
        self,
        dataframe: Any,
        symbol: str,
    ) -> pd.DataFrame:

        if dataframe is None:

            raise PreBreakoutDataError(
                "Dataframe is None.",
                status="EMPTY_DATA",
            )

        if not isinstance(
            dataframe,
            pd.DataFrame,
        ):

            try:

                dataframe = (
                    pd.DataFrame(
                        dataframe
                    )
                )

            except Exception as exc:

                raise PreBreakoutDataError(
                    "Invalid dataframe.",
                    status="INVALID_DATA",
                ) from exc

        dataframe = dataframe.copy()

        if dataframe.empty:

            raise PreBreakoutDataError(
                "Dataframe is empty.",
                status="EMPTY_DATA",
            )

        # ----------------------------------------------------
        # ALPACA CAN RETURN MULTIINDEX
        # ----------------------------------------------------

        if isinstance(
            dataframe.index,
            pd.MultiIndex,
        ):

            extracted = False

            for level in range(
                dataframe.index.nlevels
            ):

                try:

                    raw_values = (
                        dataframe.index
                        .get_level_values(
                            level
                        )
                    )

                    values = [
                        str(value)
                        .strip()
                        .upper()
                        for value
                        in raw_values
                    ]

                    if symbol in set(
                        values
                    ):

                        actual_value = None

                        for original in raw_values:

                            if (
                                str(original)
                                .strip()
                                .upper()
                                ==
                                symbol
                            ):

                                actual_value = (
                                    original
                                )

                                break

                        if actual_value is not None:

                            dataframe = (
                                dataframe.xs(
                                    actual_value,
                                    level=level,
                                )
                            )

                            extracted = True

                            break

                except Exception:

                    continue

            if not extracted:

                dataframe = (
                    dataframe
                    .reset_index()
                )

        # ----------------------------------------------------
        # LOWERCASE COLUMNS
        # ----------------------------------------------------

        dataframe.columns = [

            str(column)
            .strip()
            .lower()

            for column
            in dataframe.columns
        ]

        # ----------------------------------------------------
        # FILTER SYMBOL COLUMN IF PRESENT
        # ----------------------------------------------------

        if "symbol" in dataframe.columns:

            dataframe["symbol"] = (

                dataframe["symbol"]
                .astype(str)
                .str.strip()
                .str.upper()
            )

            dataframe = dataframe[
                dataframe["symbol"]
                ==
                symbol
            ]

        # ----------------------------------------------------
        # REQUIRED COLUMNS
        # ----------------------------------------------------

        missing = (

            self.REQUIRED_COLUMNS
            -
            set(
                dataframe.columns
            )
        )

        if missing:

            raise PreBreakoutDataError(

                "Missing columns: "
                +
                ", ".join(
                    sorted(
                        missing
                    )
                ),

                status="INVALID_COLUMNS",
            )

        # ----------------------------------------------------
        # NUMERIC CONVERSION
        # ----------------------------------------------------

        for column in (
            self.REQUIRED_COLUMNS
        ):

            dataframe[column] = (
                pd.to_numeric(

                    dataframe[column],

                    errors="coerce",
                )
            )

        dataframe = dataframe.dropna(

            subset=[
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        if dataframe.empty:

            raise PreBreakoutDataError(
                "No valid OHLCV rows.",
                status="EMPTY_DATA",
            )

        # ----------------------------------------------------
        # ENSURE CHRONOLOGICAL ORDER
        # ----------------------------------------------------

        try:

            dataframe = (
                dataframe.sort_index()
            )

        except Exception:

            pass

        return dataframe

    # ========================================================
    # FETCH HISTORICAL BARS
    # ========================================================

    def _fetch_bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int,
        lookback_days: int,
    ) -> pd.DataFrame:

        if self.alpaca is None:

            raise PreBreakoutDataError(
                "Alpaca unavailable.",
                status="API_UNAVAILABLE",
            )

        # ----------------------------------------------------
        # CALCULATE HISTORICAL WINDOW
        # ----------------------------------------------------

        end_dt = datetime.now(
            timezone.utc
        )

        start_dt = (

            end_dt
            -
            timedelta(
                days=int(
                    lookback_days
                )
            )
        )

        start = (
            start_dt
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )

        end = (
            end_dt
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )

        # ----------------------------------------------------
        # REQUEST ENOUGH DATA THEN KEEP LATEST BARS LOCALLY
        #
        # Using 1000 prevents us from accidentally getting
        # only a tiny number of historical bars.
        # ----------------------------------------------------

        request_limit = 1000

        try:

            try:

                bars = (
                    self.alpaca
                    .get_bars(

                        symbol,

                        timeframe,

                        start=start,

                        end=end,

                        limit=request_limit,

                        feed="iex",

                        adjustment="raw",
                    )
                )

            except TypeError:

                bars = (
                    self.alpaca
                    .get_bars(

                        symbol,

                        timeframe,

                        start=start,

                        end=end,

                        limit=request_limit,
                    )
                )

        except Exception as exc:

            raise PreBreakoutDataError(
                str(exc),
                status="API_ERROR",
            ) from exc

        dataframe = getattr(
            bars,
            "df",
            bars,
        )

        dataframe = (
            self._normalize_dataframe(
                dataframe,
                symbol,
            )
        )

        # ----------------------------------------------------
        # ONLY USE THE MOST RECENT REQUESTED NUMBER OF BARS
        # ----------------------------------------------------

        if len(
            dataframe
        ) > int(
            limit
        ):

            dataframe = dataframe.tail(
                int(
                    limit
                )
            )

        return dataframe

    # ========================================================
    # SESSION VWAP
    # ========================================================

    @staticmethod
    def _session_vwap(
        dataframe: pd.DataFrame,
    ) -> pd.Series:

        working = dataframe

        if isinstance(
            dataframe.index,
            pd.DatetimeIndex,
        ):

            try:

                last_date = (
                    dataframe.index[-1]
                    .date()
                )

                session = dataframe[
                    dataframe.index.date
                    ==
                    last_date
                ]

                if len(
                    session
                ) >= 2:

                    working = session

            except Exception:

                working = dataframe

        typical = (

            working["high"]
            +
            working["low"]
            +
            working["close"]

        ) / 3.0

        cumulative_volume = (

            working["volume"]
            .cumsum()
            .replace(
                0,
                np.nan,
            )
        )

        vwap = (

            (
                typical
                *
                working["volume"]
            )
            .cumsum()

            /

            cumulative_volume
        )

        return vwap

    # ========================================================
    # COMMON METRICS
    # ========================================================

    def _calculate_metrics(
        self,
        dataframe: pd.DataFrame,
        *,
        intraday: bool,
    ) -> dict[str, Any]:

        close = dataframe[
            "close"
        ]

        high = dataframe[
            "high"
        ]

        low = dataframe[
            "low"
        ]

        volume = dataframe[
            "volume"
        ]

        bars = len(
            dataframe
        )

        current_price = float(
            close.iloc[-1]
        )

        # ----------------------------------------------------
        # BAR RELATIVE VOLUME
        # ----------------------------------------------------

        lookback = min(
            20,
            bars - 1,
        )

        previous_volume = (

            volume.iloc[
                -(lookback + 1):-1
            ]
        )

        avg_volume = float(
            previous_volume.mean()
        )

        current_volume = float(
            volume.iloc[-1]
        )

        rvol = (

            current_volume
            /
            avg_volume

            if avg_volume > 0

            else 0.0
        )

        # ----------------------------------------------------
        # VOLUME ACCELERATION
        # ----------------------------------------------------

        if bars >= 6:

            previous_5_volume = (
                volume.iloc[
                    -6:-1
                ]
            )

        else:

            previous_5_volume = (
                previous_volume
            )

        speed_base = float(
            previous_5_volume.mean()
        )

        volume_speed_ratio = (

            current_volume
            /
            speed_base

            if speed_base > 0

            else 0.0
        )

        # ----------------------------------------------------
        # RESISTANCE
        # ----------------------------------------------------

        previous_highs = (

            high.iloc[
                -(lookback + 1):-1
            ]
        )

        resistance = float(
            previous_highs.max()
        )

        distance_to_resistance = (

            (
                resistance
                -
                current_price
            )

            /
            resistance

            if resistance > 0

            else 1.0
        )

        # ----------------------------------------------------
        # COMPRESSION
        # ----------------------------------------------------

        candle_range = (

            high
            -
            low
        )

        recent_range = float(

            candle_range
            .iloc[-5:]
            .mean()
        )

        if bars >= 25:

            historical_series = (
                candle_range
                .iloc[-25:-5]
            )

        else:

            historical_series = (
                candle_range
                .iloc[:-5]
            )

        historical_range = float(
            historical_series.mean()
        )

        if (
            historical_range > 0
            and
            np.isfinite(
                historical_range
            )
        ):

            compression_ratio = (

                recent_range
                /
                historical_range
            )

        else:

            compression_ratio = 999.0

        compression = bool(
            compression_ratio
            <=
            0.75
        )

        # ----------------------------------------------------
        # MA 10 / MA 20
        # ----------------------------------------------------

        ma10 = float(

            close
            .rolling(10)
            .mean()
            .iloc[-1]
        )

        ma20 = float(

            close
            .rolling(20)
            .mean()
            .iloc[-1]
        )

        if (
            current_price > ma10
            and
            ma10 > ma20
        ):

            trend = "BULLISH"

        elif (
            current_price < ma10
            and
            ma10 < ma20
        ):

            trend = "BEARISH"

        else:

            trend = "MIXED"

        # ----------------------------------------------------
        # MOMENTUM 3 BARS
        # ----------------------------------------------------

        momentum_3 = 0.0

        if (
            bars >= 4
            and
            float(
                close.iloc[-4]
            ) > 0
        ):

            momentum_3 = (

                (
                    current_price
                    /
                    float(
                        close.iloc[-4]
                    )
                )
                -
                1.0

            ) * 100.0

        # ----------------------------------------------------
        # MOMENTUM 10 BARS
        # ----------------------------------------------------

        momentum_10 = 0.0

        if (
            bars >= 11
            and
            float(
                close.iloc[-11]
            ) > 0
        ):

            momentum_10 = (

                (
                    current_price
                    /
                    float(
                        close.iloc[-11]
                    )
                )
                -
                1.0

            ) * 100.0

        # ----------------------------------------------------
        # UPPER WICK
        # ----------------------------------------------------

        latest_open = float(
            dataframe[
                "open"
            ].iloc[-1]
        )

        latest_high = float(
            high.iloc[-1]
        )

        latest_low = float(
            low.iloc[-1]
        )

        latest_close = float(
            close.iloc[-1]
        )

        candle_total = (

            latest_high
            -
            latest_low
        )

        upper_wick = (

            latest_high
            -
            max(
                latest_open,
                latest_close,
            )
        )

        upper_wick_ratio = (

            upper_wick
            /
            candle_total

            if candle_total > 0

            else 0.0
        )

        # ----------------------------------------------------
        # VWAP
        # ----------------------------------------------------

        vwap = None

        above_vwap = None

        vwap_reclaimed = None

        if intraday:

            try:

                vwap_series = (
                    self._session_vwap(
                        dataframe
                    )
                )

                vwap = float(
                    vwap_series.iloc[-1]
                )

                above_vwap = bool(
                    current_price
                    >
                    vwap
                )

                if len(
                    vwap_series
                ) >= 2:

                    previous_vwap = float(
                        vwap_series.iloc[-2]
                    )

                    previous_close = float(
                        close.iloc[-2]
                    )

                    vwap_reclaimed = bool(

                        previous_close
                        <=
                        previous_vwap

                        and

                        current_price
                        >
                        vwap
                    )

                else:

                    vwap_reclaimed = False

            except Exception:

                vwap = None

                above_vwap = None

                vwap_reclaimed = None

        return {

            "current_price": (
                current_price
            ),

            "rvol": float(
                rvol
            ),

            "volume_speed_ratio": float(
                volume_speed_ratio
            ),

            "resistance": (
                resistance
            ),

            "distance_to_resistance": float(
                distance_to_resistance
            ),

            "distance_to_resistance_pct": float(
                distance_to_resistance
                *
                100.0
            ),

            "compression": (
                compression
            ),

            "compression_ratio": float(
                compression_ratio
            ),

            "ma10": (
                ma10
            ),

            "ma20": (
                ma20
            ),

            "trend": (
                trend
            ),

            "momentum_3_pct": float(
                momentum_3
            ),

            "momentum_10_pct": float(
                momentum_10
            ),

            "upper_wick_ratio": float(
                upper_wick_ratio
            ),

            "vwap": (
                vwap
            ),

            "above_vwap": (
                above_vwap
            ),

            "vwap_reclaimed": (
                vwap_reclaimed
            ),
        }

    # ========================================================
    # 5 MINUTE SCORE
    # ========================================================

    @staticmethod
    def _score_5m(
        metrics: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        # ----------------------------------------------------
        # RVOL
        # ----------------------------------------------------

        rvol = metrics[
            "rvol"
        ]

        if rvol >= 3.0:

            score += 25

            reasons.append(
                "5M exceptional volume"
            )

        elif rvol >= 2.0:

            score += 21

            reasons.append(
                "5M strong volume"
            )

        elif rvol >= 1.5:

            score += 16

        elif rvol >= 1.2:

            score += 8

        else:

            warnings.append(
                "5M volume weak"
            )

        # ----------------------------------------------------
        # VOLUME SPEED
        # ----------------------------------------------------

        speed = metrics[
            "volume_speed_ratio"
        ]

        if speed >= 1.5:

            score += 15

            reasons.append(
                "5M volume acceleration"
            )

        elif speed >= 1.1:

            score += 8

        # ----------------------------------------------------
        # VWAP
        # ----------------------------------------------------

        if metrics.get(
            "vwap_reclaimed"
        ):

            score += 15

            reasons.append(
                "5M VWAP reclaimed"
            )

        elif metrics.get(
            "above_vwap"
        ):

            score += 10

            reasons.append(
                "5M above VWAP"
            )

        else:

            warnings.append(
                "5M below VWAP"
            )

        # ----------------------------------------------------
        # RESISTANCE
        # ----------------------------------------------------

        distance = metrics[
            "distance_to_resistance"
        ]

        if (
            0
            <= distance
            <= 0.01
        ):

            score += 25

            reasons.append(
                "5M within 1% resistance"
            )

        elif (
            0.01
            <
            distance
            <= 0.02
        ):

            score += 20

        elif (
            0.02
            <
            distance
            <= 0.03
        ):

            score += 12

        elif distance < 0:

            score += 10

            reasons.append(
                "5M testing breakout"
            )

        # ----------------------------------------------------
        # COMPRESSION
        # ----------------------------------------------------

        if metrics[
            "compression"
        ]:

            score += 15

            reasons.append(
                "5M compression"
            )

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        momentum = metrics[
            "momentum_3_pct"
        ]

        if (
            0.2
            <= momentum
            <= 4.0
        ):

            score += 10

            reasons.append(
                "5M positive momentum"
            )

        # ----------------------------------------------------
        # UPPER WICK RISK
        # ----------------------------------------------------

        if (
            metrics[
                "upper_wick_ratio"
            ]
            >
            0.45
        ):

            score -= 10

            warnings.append(
                "5M large upper wick"
            )

        return (

            round(
                max(
                    0.0,
                    min(
                        score,
                        100.0,
                    ),
                ),
                2,
            ),

            reasons,

            warnings,
        )

    # ========================================================
    # 30 MINUTE SCORE
    # ========================================================

    @staticmethod
    def _score_30m(
        metrics: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        # ----------------------------------------------------
        # TREND
        # ----------------------------------------------------

        if (
            metrics[
                "trend"
            ]
            ==
            "BULLISH"
        ):

            score += 20

            reasons.append(
                "30M bullish structure"
            )

        elif (
            metrics[
                "trend"
            ]
            ==
            "BEARISH"
        ):

            warnings.append(
                "30M bearish structure"
            )

        # ----------------------------------------------------
        # COMPRESSION
        # ----------------------------------------------------

        if metrics[
            "compression"
        ]:

            score += 25

            reasons.append(
                "30M price compression"
            )

        # ----------------------------------------------------
        # RVOL
        # ----------------------------------------------------

        rvol = metrics[
            "rvol"
        ]

        if rvol >= 2.0:

            score += 20

        elif rvol >= 1.5:

            score += 15

        elif rvol >= 1.2:

            score += 8

        # ----------------------------------------------------
        # RESISTANCE
        # ----------------------------------------------------

        distance = metrics[
            "distance_to_resistance"
        ]

        if (
            0
            <= distance
            <= 0.015
        ):

            score += 25

            reasons.append(
                "30M near resistance"
            )

        elif (
            0.015
            <
            distance
            <= 0.03
        ):

            score += 15

        elif distance < 0:

            score += 10

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        momentum = metrics[
            "momentum_3_pct"
        ]

        if (
            0
            <
            momentum
            <= 8
        ):

            score += 10

        # ----------------------------------------------------
        # VWAP
        # ----------------------------------------------------

        if metrics.get(
            "above_vwap"
        ):

            score += 5

        return (

            round(
                max(
                    0.0,
                    min(
                        score,
                        100.0,
                    ),
                ),
                2,
            ),

            reasons,

            warnings,
        )

    # ========================================================
    # 1 HOUR SCORE
    # ========================================================

    @staticmethod
    def _score_1h(
        metrics: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        # ----------------------------------------------------
        # TREND
        # ----------------------------------------------------

        if (
            metrics[
                "trend"
            ]
            ==
            "BULLISH"
        ):

            score += 35

            reasons.append(
                "1H bullish trend"
            )

        elif (
            metrics[
                "trend"
            ]
            ==
            "MIXED"
        ):

            score += 15

        else:

            warnings.append(
                "1H bearish trend"
            )

        # ----------------------------------------------------
        # PRICE ABOVE MA20
        # ----------------------------------------------------

        if (
            metrics[
                "current_price"
            ]
            >
            metrics[
                "ma20"
            ]
        ):

            score += 15

        # ----------------------------------------------------
        # RVOL
        # ----------------------------------------------------

        rvol = metrics[
            "rvol"
        ]

        if rvol >= 2.0:

            score += 15

        elif rvol >= 1.3:

            score += 10

        # ----------------------------------------------------
        # RESISTANCE
        # ----------------------------------------------------

        distance = metrics[
            "distance_to_resistance"
        ]

        if (
            0
            <= distance
            <= 0.02
        ):

            score += 20

            reasons.append(
                "1H near resistance"
            )

        elif (
            0.02
            <
            distance
            <= 0.04
        ):

            score += 10

        elif distance < 0:

            score += 8

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        momentum = metrics[
            "momentum_10_pct"
        ]

        if (
            0
            <
            momentum
            <= 15
        ):

            score += 15

        elif momentum > 15:

            score += 5

            warnings.append(
                "1H move extended"
            )

        return (

            round(
                max(
                    0.0,
                    min(
                        score,
                        100.0,
                    ),
                ),
                2,
            ),

            reasons,

            warnings,
        )

    # ========================================================
    # DAILY SCORE
    # ========================================================

    @staticmethod
    def _score_1d(
        metrics: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        current = metrics[
            "current_price"
        ]

        ma20 = metrics[
            "ma20"
        ]

        # ----------------------------------------------------
        # DAILY TREND
        # ----------------------------------------------------

        if (
            metrics[
                "trend"
            ]
            ==
            "BULLISH"
        ):

            score += 35

            reasons.append(
                "Daily bullish trend"
            )

        elif (
            metrics[
                "trend"
            ]
            ==
            "MIXED"
        ):

            score += 15

        else:

            warnings.append(
                "DAILY_TREND_CONFLICT"
            )

        # ----------------------------------------------------
        # ABOVE DAILY MA20
        # ----------------------------------------------------

        if current > ma20:

            score += 15

        # ----------------------------------------------------
        # DAILY RVOL
        # ----------------------------------------------------

        rvol = metrics[
            "rvol"
        ]

        if rvol >= 2.0:

            score += 10

        elif rvol >= 1.3:

            score += 6

        # ----------------------------------------------------
        # DAILY RESISTANCE
        # ----------------------------------------------------

        distance = metrics[
            "distance_to_resistance"
        ]

        if (
            0
            <= distance
            <= 0.03
        ):

            score += 20

            reasons.append(
                "Near major daily resistance"
            )

        elif (
            0.03
            <
            distance
            <= 0.07
        ):

            score += 10

        elif distance < 0:

            score += 8

        # ----------------------------------------------------
        # DAILY MOMENTUM
        # ----------------------------------------------------

        momentum = metrics[
            "momentum_10_pct"
        ]

        if (
            0
            <
            momentum
            <= 20
        ):

            score += 15

        elif (
            20
            <
            momentum
            <= 40
        ):

            score += 7

            warnings.append(
                "Daily move extended"
            )

        elif momentum > 40:

            score -= 10

            warnings.append(
                "EXTENDED_DAILY_MOVE"
            )

        # ----------------------------------------------------
        # DISTANCE ABOVE DAILY MA20
        # ----------------------------------------------------

        if ma20 > 0:

            extension_from_ma20 = (

                (
                    current
                    /
                    ma20
                )
                -
                1.0

            ) * 100.0

            if (
                extension_from_ma20
                >
                25.0
            ):

                score -= 15

                warnings.append(
                    "Far above daily MA20"
                )

        return (

            round(
                max(
                    0.0,
                    min(
                        score,
                        100.0,
                    ),
                ),
                2,
            ),

            reasons,

            warnings,
        )

    # ========================================================
    # ANALYZE TIMEFRAME
    # ========================================================

    def _analyze_timeframe(
        self,
        symbol: str,
        name: str,
    ) -> TimeframeAnalysis:

        config = (
            self.TIMEFRAME_CONFIG[
                name
            ]
        )

        result = TimeframeAnalysis(

            name=name,

            alpaca_timeframe=(
                config[
                    "alpaca"
                ]
            ),
        )

        try:

            dataframe = (
                self._fetch_bars(

                    symbol,

                    timeframe=(
                        config[
                            "alpaca"
                        ]
                    ),

                    limit=(
                        config[
                            "limit"
                        ]
                    ),

                    lookback_days=(
                        config[
                            "lookback_days"
                        ]
                    ),
                )
            )

        except PreBreakoutDataError as exc:

            result.error = str(
                exc
            )

            return result

        # ----------------------------------------------------
        # IMPORTANT:
        # Record actual bars BEFORE checking minimum.
        # ----------------------------------------------------

        result.bars_used = len(
            dataframe
        )

        if (
            result.bars_used
            <
            config[
                "min_bars"
            ]
        ):

            result.error = (

                f"Insufficient bars: "
                f"{result.bars_used}/"
                f"{config['min_bars']}"
            )

            return result

        # ----------------------------------------------------
        # DAILY DOES NOT USE INTRADAY VWAP
        # ----------------------------------------------------

        intraday = (
            name
            !=
            "1D"
        )

        try:

            metrics = (
                self._calculate_metrics(

                    dataframe,

                    intraday=intraday,
                )
            )

        except Exception as exc:

            result.error = (
                "Metric calculation failed: "
                +
                str(
                    exc
                )
            )

            return result

        # ----------------------------------------------------
        # TIMEFRAME SCORE
        # ----------------------------------------------------

        if name == "5M":

            (
                score,
                reasons,
                warnings,
            ) = (
                self._score_5m(
                    metrics
                )
            )

        elif name == "30M":

            (
                score,
                reasons,
                warnings,
            ) = (
                self._score_30m(
                    metrics
                )
            )

        elif name == "1H":

            (
                score,
                reasons,
                warnings,
            ) = (
                self._score_1h(
                    metrics
                )
            )

        else:

            (
                score,
                reasons,
                warnings,
            ) = (
                self._score_1d(
                    metrics
                )
            )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        result.available = True

        result.score = (
            score
        )

        result.current_price = (
            metrics[
                "current_price"
            ]
        )

        result.rvol = (
            metrics[
                "rvol"
            ]
        )

        result.volume_speed_ratio = (
            metrics[
                "volume_speed_ratio"
            ]
        )

        result.resistance = (
            metrics[
                "resistance"
            ]
        )

        result.distance_to_resistance_pct = (
            metrics[
                "distance_to_resistance_pct"
            ]
        )

        result.compression = (
            metrics[
                "compression"
            ]
        )

        result.compression_ratio = (
            metrics[
                "compression_ratio"
            ]
        )

        result.vwap = (
            metrics[
                "vwap"
            ]
        )

        result.above_vwap = (
            metrics[
                "above_vwap"
            ]
        )

        result.vwap_reclaimed = (
            metrics[
                "vwap_reclaimed"
            ]
        )

        result.ma10 = (
            metrics[
                "ma10"
            ]
        )

        result.ma20 = (
            metrics[
                "ma20"
            ]
        )

        result.trend = (
            metrics[
                "trend"
            ]
        )

        result.momentum_3_pct = (
            metrics[
                "momentum_3_pct"
            ]
        )

        result.momentum_10_pct = (
            metrics[
                "momentum_10_pct"
            ]
        )

        result.upper_wick_ratio = (
            metrics[
                "upper_wick_ratio"
            ]
        )

        result.reasons = (
            reasons
        )

        result.warnings = (
            warnings
        )

        result.error = None

        return result

    # ========================================================
    # OPTIONAL REAL ML
    # ========================================================

    def _ml_probability(
        self,
        timeframe_5m: TimeframeAnalysis,
    ) -> Optional[float]:

        if not self._is_trained:

            return None

        if not timeframe_5m.available:

            return None

        try:

            distance = 1.0

            if (
                timeframe_5m.resistance
                and
                timeframe_5m.current_price
            ):

                distance = (

                    (
                        timeframe_5m.resistance
                        -
                        timeframe_5m.current_price
                    )

                    /
                    timeframe_5m.resistance
                )

            features = np.asarray(

                [[

                    float(
                        timeframe_5m.rvol
                        or
                        0.0
                    ),

                    (
                        1.0
                        if
                        timeframe_5m.compression
                        else
                        0.0
                    ),

                    (
                        1.0
                        if
                        timeframe_5m.vwap_reclaimed
                        else
                        0.0
                    ),

                    float(
                        distance
                    ),

                    (
                        1.0
                        if
                        (
                            timeframe_5m
                            .volume_speed_ratio
                            or
                            0.0
                        )
                        >=
                        1.5
                        else
                        0.0
                    ),
                ]],

                dtype=float,
            )

            probabilities = (
                self.ai_model
                .predict_proba(
                    features
                )[0]
            )

            classes = list(
                self.ai_model.classes_
            )

            if 1 not in classes:

                return None

            probability = float(
                probabilities[
                    classes.index(
                        1
                    )
                ]
            )

            return probability

        except Exception:

            return None

    # ========================================================
    # RADAR CONTEXT
    # ========================================================

    @staticmethod
    def _extract_radar_context(
        radar_context: Any,
    ) -> tuple[
        Optional[float],
        Optional[float],
        Optional[float],
    ]:

        if radar_context is None:

            return (
                None,
                None,
                None,
            )

        if isinstance(
            radar_context,
            dict,
        ):

            return (

                radar_context.get(
                    "rank_score"
                ),

                radar_context.get(
                    "relative_volume"
                ),

                radar_context.get(
                    "change_pct"
                ),
            )

        return (

            getattr(
                radar_context,
                "rank_score",
                None,
            ),

            getattr(
                radar_context,
                "relative_volume",
                None,
            ),

            getattr(
                radar_context,
                "change_pct",
                None,
            ),
        )

    # ========================================================
    # ANALYZE SYMBOL
    # ========================================================

    def analyze_symbol(
        self,
        symbol: str,
        radar_context: Any = None,
    ) -> PreBreakoutResult:

        try:

            symbol = (
                self._normalize_symbol(
                    symbol
                )
            )

        except Exception as exc:

            return PreBreakoutResult(

                symbol="",

                status="ERROR",

                error=str(
                    exc
                ),
            )

        # ----------------------------------------------------
        # ANALYZE ALL 4 TIMEFRAMES
        # ----------------------------------------------------

        tf5 = (
            self._analyze_timeframe(
                symbol,
                "5M",
            )
        )

        tf30 = (
            self._analyze_timeframe(
                symbol,
                "30M",
            )
        )

        tf1h = (
            self._analyze_timeframe(
                symbol,
                "1H",
            )
        )

        tf1d = (
            self._analyze_timeframe(
                symbol,
                "1D",
            )
        )

        timeframes = {

            "5M": tf5,

            "30M": tf30,

            "1H": tf1h,

            "1D": tf1d,
        }

        # ----------------------------------------------------
        # WEIGHTED SCORE
        # ----------------------------------------------------

        weighted_score = 0.0

        confidence = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        for (
            name,
            timeframe,
        ) in timeframes.items():

            weight = (
                self.TIMEFRAME_WEIGHTS[
                    name
                ]
            )

            if timeframe.available:

                weighted_score += (

                    timeframe.score
                    *
                    weight
                )

                confidence += (
                    weight
                )

                reasons.extend(

                    [
                        f"{name}: {reason}"

                        for reason
                        in timeframe.reasons
                    ]
                )

                warnings.extend(

                    [
                        f"{name}: {warning}"

                        for warning
                        in timeframe.warnings
                    ]
                )

            else:

                warnings.append(

                    f"{name}: DATA_UNAVAILABLE"

                    +
                    (
                        f" ({timeframe.error})"

                        if timeframe.error

                        else ""
                    )
                )

        multi_score = round(
            weighted_score,
            2,
        )

        data_confidence = round(
            confidence
            *
            100.0,
            2,
        )

        # ----------------------------------------------------
        # OPTIONAL ML BLEND
        # ----------------------------------------------------

        ml_probability = (
            self._ml_probability(
                tf5
            )
        )

        model_source = (
            "MULTI_TIMEFRAME_RULES"
        )

        final_score = (
            multi_score
        )

        if ml_probability is not None:

            ml_score = (
                ml_probability
                *
                100.0
            )

            final_score = (

                multi_score
                *
                0.80

                +

                ml_score
                *
                0.20
            )

            model_source = (
                "MULTI_TIMEFRAME_RULES_REAL_ML_BLEND"
            )

        final_score = round(

            max(
                0.0,
                min(
                    final_score,
                    100.0,
                ),
            ),

            2,
        )

        # ----------------------------------------------------
        # RADAR CONTEXT
        # ----------------------------------------------------

        (
            radar_score,
            radar_rvol,
            radar_change,
        ) = (
            self._extract_radar_context(
                radar_context
            )
        )

        # ----------------------------------------------------
        # EXTENDED MOVE SAFETY
        # ----------------------------------------------------

        if (
            radar_change is not None
            and
            radar_change > 40
        ):

            warnings.append(
                "RADAR: EXTENDED_MOVE"
            )

        if (
            radar_change is not None
            and
            radar_change > 100
        ):

            warnings.append(
                "RADAR: EXTREME_EXTENSION"
            )

        # ----------------------------------------------------
        # FINAL RESEARCH STATUS
        # ----------------------------------------------------

        status = "WATCH"

        ready = False

        if (
            final_score >= 78
            and
            data_confidence >= 75
        ):

            status = (
                "READY_FOR_JALWE_RESEARCH"
            )

            ready = True

        elif (
            final_score >= 68
            and
            data_confidence >= 65
        ):

            status = (
                "CONFIRMED"
            )

            ready = True

        elif (
            final_score >= 55
        ):

            status = (
                "SETUP"
            )

        # ----------------------------------------------------
        # CURRENT PRICE
        # ----------------------------------------------------

        current_price = None

        for frame in (

            tf5,

            tf30,

            tf1h,

            tf1d,
        ):

            if (
                frame.available
                and
                frame.current_price
                is not None
            ):

                current_price = (
                    frame.current_price
                )

                break

        # ----------------------------------------------------
        # LOW CONFIDENCE WARNING
        # ----------------------------------------------------

        if data_confidence < 50:

            warnings.append(
                "LOW_DATA_CONFIDENCE"
            )

        return PreBreakoutResult(

            symbol=(
                symbol
            ),

            score=(
                final_score
            ),

            multi_timeframe_score=(
                multi_score
            ),

            data_confidence=(
                data_confidence
            ),

            score_5m=(

                tf5.score

                if tf5.available

                else None
            ),

            score_30m=(

                tf30.score

                if tf30.available

                else None
            ),

            score_1h=(

                tf1h.score

                if tf1h.available

                else None
            ),

            score_1d=(

                tf1d.score

                if tf1d.available

                else None
            ),

            status=(
                status
            ),

            ready=(
                ready
            ),

            model_source=(
                model_source
            ),

            ml_probability=(

                round(
                    ml_probability,
                    4,
                )

                if ml_probability is not None

                else None
            ),

            radar_rank_score=(
                radar_score
            ),

            radar_relative_volume=(
                radar_rvol
            ),

            radar_change_pct=(
                radar_change
            ),

            current_price=(
                current_price
            ),

            timeframe_5m=(
                tf5
            ),

            timeframe_30m=(
                tf30
            ),

            timeframe_1h=(
                tf1h
            ),

            timeframe_1d=(
                tf1d
            ),

            reasons=(
                reasons
            ),

            warnings=(
                warnings
            ),

            error=None,
        )

    # ========================================================
    # ANALYZE MULTIPLE SYMBOLS
    # ========================================================

    def analyze_symbols(
        self,
        symbols: list[str],
        *,
        radar_candidates: Optional[
            list[Any]
        ] = None,
        minimum_score: float = 55.0,
        max_results: int = 20,
    ) -> list[PreBreakoutResult]:

        radar_map: dict[
            str,
            Any,
        ] = {}

        # ----------------------------------------------------
        # MAP RADAR CANDIDATES
        # ----------------------------------------------------

        for candidate in (
            radar_candidates
            or []
        ):

            if isinstance(
                candidate,
                dict,
            ):

                candidate_symbol = (
                    candidate.get(
                        "symbol",
                        ""
                    )
                )

            else:

                candidate_symbol = (
                    getattr(
                        candidate,
                        "symbol",
                        "",
                    )
                )

            symbol = str(
                candidate_symbol
                or ""
            ).strip().upper()

            if symbol:

                radar_map[
                    symbol
                ] = candidate

        # ----------------------------------------------------
        # ANALYZE
        # ----------------------------------------------------

        results: list[
            PreBreakoutResult
        ] = []

        seen: set[
            str
        ] = set()

        for raw_symbol in (
            symbols
            or []
        ):

            symbol = str(
                raw_symbol
                or ""
            ).strip().upper()

            if not symbol:

                continue

            if symbol in seen:

                continue

            seen.add(
                symbol
            )

            result = (
                self.analyze_symbol(

                    symbol,

                    radar_context=(
                        radar_map.get(
                            symbol
                        )
                    ),
                )
            )

            if result.error:

                continue

            if (
                result.score
                <
                minimum_score
            ):

                continue

            results.append(
                result
            )

        # ----------------------------------------------------
        # SORT BEST FIRST
        # ----------------------------------------------------

        results.sort(

            key=lambda item: (

                item.score,

                item.data_confidence,

                item.radar_rank_score
                or
                0.0,
            ),

            reverse=True,
        )

        return results[
            :max(
                1,
                int(
                    max_results
                ),
            )
        ]

    # ========================================================
    # HEALTH CHECK
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        return {

            "version": (
                "2.3.1"
            ),

            "alpaca_available": (
                self.alpaca
                is not None
            ),

            "ml_trained": (
                self._is_trained
            ),

            "training_samples": (
                self._training_samples
            ),

            "minimum_training_samples": (
                self.min_training_samples
            ),

            "timeframes": [

                "5M",

                "30M",

                "1H",

                "1D",
            ],

            "weights": dict(
                self.TIMEFRAME_WEIGHTS
            ),

            "lookback_days": {

                name: config[
                    "lookback_days"
                ]

                for (
                    name,
                    config,
                )
                in
                self.TIMEFRAME_CONFIG.items()
            },

            "role": (
                "MULTI_TIMEFRAME_PRE_BREAKOUT_RESEARCH_ONLY"
            ),

            "order_execution_enabled": (
                False
            ),
        }


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":

    print(
        "PreBreakout Engine V2.3.1"
    )

    print(
        "5M + 30M + 1H + 1D"
    )

    print(
        "Research only - no order execution."
    )
