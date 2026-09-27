from __future__ import annotations

import logging
import os

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv


# ============================================================
# ENV / PATH
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# LIQUIDITY RESULT
# ============================================================

@dataclass
class LiquidityResult:

    symbol: str

    status: str = "UNKNOWN"

    liquidity_score: float = 0.0
    confidence: float = 0.0

    liquidity_bias: str = "NEUTRAL"

    buy_pressure: float = 0.0
    sell_pressure: float = 0.0

    money_flow: float = 0.0

    volume_acceleration: float = 0.0

    price_volume_confirmation: bool = False

    absorption: bool = False
    distribution: bool = False

    bullish_absorption_score: float = 0.0
    distribution_score: float = 0.0

    price_change_pct: float = 0.0

    current_price: Optional[float] = None

    current_volume: Optional[float] = None

    average_volume: Optional[float] = None

    relative_volume: Optional[float] = None

    recent_dollar_volume: Optional[float] = None

    bars_1m: int = 0
    bars_5m: int = 0
    bars_15m: int = 0

    reasons: list[str] = field(
        default_factory=list
    )

    risk_flags: list[str] = field(
        default_factory=list
    )

    diagnostics: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None


# ============================================================
# LIQUIDITY ENGINE V2
# ============================================================

class LiquidityEngine:
    """
    APEX LIQUIDITY ENGINE V2

    RESEARCH ONLY.

    Pipeline:

        Scanner
            ↓
        PreBreakout
            ↓
        News
            ↓
        Liquidity Engine
            ↓
        AI
            ↓
        JALWE

    IMPORTANT:

    BUY_PRESSURE / SELL_PRESSURE / MONEY_FLOW here are
    OHLCV-derived research estimates.

    They are NOT proof of institutional orders and are NOT
    true exchange-level order-flow or Level-2 tape data.

    This engine NEVER:
        - buys
        - sells
        - submits orders
        - manages positions
    """

    # ========================================================
    # TIMEFRAMES
    # ========================================================

    TIMEFRAME_CONFIG = {

        "1M": {
            "timeframe": "1Min",
            "lookback_days": 3,
            "keep_bars": 180,
            "min_bars": 25,
        },

        "5M": {
            "timeframe": "5Min",
            "lookback_days": 10,
            "keep_bars": 120,
            "min_bars": 25,
        },

        "15M": {
            "timeframe": "15Min",
            "lookback_days": 20,
            "keep_bars": 100,
            "min_bars": 20,
        },
    }

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
        alpaca_api=None,
    ) -> None:

        self.api_key = (

            os.getenv(
                "APCA_API_KEY_ID"
            )

            or

            os.getenv(
                "ALPACA_API_KEY"
            )

            or

            ""
        ).strip()

        self.api_secret = (

            os.getenv(
                "APCA_API_SECRET_KEY"
            )

            or

            os.getenv(
                "ALPACA_SECRET_KEY"
            )

            or

            ""
        ).strip()

        self.base_url = (

            os.getenv(
                "APCA_API_BASE_URL"
            )

            or

            os.getenv(
                "ALPACA_BASE_URL"
            )

            or

            "https://paper-api.alpaca.markets"

        ).strip()

        self.alpaca = alpaca_api

        if self.alpaca is None:

            self._initialize_alpaca()


    # ========================================================
    # INITIALIZE ALPACA
    # ========================================================

    def _initialize_alpaca(
        self,
    ) -> None:

        if (
            not self.api_key
            or
            not self.api_secret
        ):

            logger.warning(
                "LiquidityEngine credentials missing."
            )

            return

        if (
            "paper-api.alpaca.markets"
            not in
            self.base_url.lower()
        ):

            logger.error(
                "LiquidityEngine blocked: "
                "Alpaca URL is not PAPER."
            )

            return

        try:

            self.alpaca = tradeapi.REST(

                self.api_key,

                self.api_secret,

                self.base_url,

                api_version="v2",
            )

        except Exception as exc:

            logger.exception(
                "LiquidityEngine Alpaca init failed: %s",
                exc,
            )

            self.alpaca = None


    # ========================================================
    # SYMBOL
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        value = str(
            symbol
            or ""
        ).strip().upper()

        if not value:

            raise ValueError(
                "Symbol cannot be empty."
            )

        return value


    # ========================================================
    # NORMALIZE DATA
    # ========================================================

    def _normalize_dataframe(
        self,
        dataframe: Any,
        symbol: str,
    ) -> pd.DataFrame:

        if dataframe is None:

            return pd.DataFrame()

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

            except Exception:

                return pd.DataFrame()

        dataframe = dataframe.copy()

        if dataframe.empty:

            return dataframe

        # ----------------------------------------------------
        # Alpaca MultiIndex support
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

                    for actual_value in raw_values:

                        if (
                            str(actual_value)
                            .strip()
                            .upper()
                            ==
                            symbol
                        ):

                            dataframe = (
                                dataframe.xs(
                                    actual_value,
                                    level=level,
                                )
                            )

                            extracted = True
                            break

                    if extracted:
                        break

                except Exception:
                    continue

            if not extracted:

                try:
                    dataframe = (
                        dataframe.reset_index()
                    )
                except Exception:
                    pass

        dataframe.columns = [

            str(column)
            .strip()
            .lower()

            for column
            in dataframe.columns
        ]

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

        missing = (

            self.REQUIRED_COLUMNS

            -

            set(
                dataframe.columns
            )
        )

        if missing:

            return pd.DataFrame()

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

        try:

            dataframe = (
                dataframe.sort_index()
            )

        except Exception:
            pass

        return dataframe


    # ========================================================
    # FETCH BARS
    # ========================================================

    def _fetch_bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        lookback_days: int,
        keep_bars: int,
    ) -> pd.DataFrame:

        if self.alpaca is None:

            return pd.DataFrame()

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

        try:

            try:

                bars = (
                    self.alpaca.get_bars(

                        symbol,

                        timeframe,

                        start=start,

                        end=end,

                        limit=1000,

                        feed="iex",

                        adjustment="raw",
                    )
                )

            except TypeError:

                bars = (
                    self.alpaca.get_bars(

                        symbol,

                        timeframe,

                        start=start,

                        end=end,

                        limit=1000,
                    )
                )

        except Exception as exc:

            logger.warning(
                "Liquidity bars failed %s %s: %s",
                symbol,
                timeframe,
                exc,
            )

            return pd.DataFrame()

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

        if (
            not dataframe.empty
            and
            len(dataframe) > keep_bars
        ):

            dataframe = dataframe.tail(
                keep_bars
            )

        return dataframe


    # ========================================================
    # CLOSE LOCATION VALUE
    # ========================================================

    @staticmethod
    def _close_location_value(
        dataframe: pd.DataFrame,
    ) -> pd.Series:

        spread = (

            dataframe["high"]

            -

            dataframe["low"]
        )

        spread = (
            spread.replace(
                0,
                np.nan,
            )
        )

        clv = (

            (
                (
                    dataframe["close"]
                    -
                    dataframe["low"]
                )

                -

                (
                    dataframe["high"]
                    -
                    dataframe["close"]
                )
            )

            /

            spread
        )

        return (
            clv
            .replace(
                [np.inf, -np.inf],
                np.nan,
            )
            .fillna(0.0)
            .clip(
                -1.0,
                1.0,
            )
        )


    # ========================================================
    # ANALYZE ONE TIMEFRAME
    # ========================================================

    def _analyze_bars(
        self,
        dataframe: pd.DataFrame,
    ) -> dict[str, Any]:

        if (
            dataframe is None
            or
            dataframe.empty
            or
            len(dataframe) < 10
        ):

            return {
                "available": False,
            }

        working = dataframe.copy()

        close = working[
            "close"
        ]

        volume = working[
            "volume"
        ]

        open_price = working[
            "open"
        ]

        high = working[
            "high"
        ]

        low = working[
            "low"
        ]

        bars = len(
            working
        )

        # ----------------------------------------------------
        # CLV SIGNED FLOW PROXY
        # ----------------------------------------------------

        clv = (
            self._close_location_value(
                working
            )
        )

        signed_flow = (

            clv

            *

            volume
        )

        lookback = min(
            20,
            bars,
        )

        recent_flow = (
            signed_flow.tail(
                lookback
            )
        )

        recent_volume = (
            volume.tail(
                lookback
            )
        )

        total_volume = float(
            recent_volume.sum()
        )

        positive_flow = float(

            recent_flow[
                recent_flow > 0
            ].sum()
        )

        negative_flow = abs(
            float(

                recent_flow[
                    recent_flow < 0
                ].sum()
            )
        )

        total_directional_flow = (

            positive_flow

            +

            negative_flow
        )

        if total_directional_flow > 0:

            buy_pressure = (

                positive_flow

                /

                total_directional_flow

                *

                100.0
            )

            sell_pressure = (

                negative_flow

                /

                total_directional_flow

                *

                100.0
            )

        else:

            buy_pressure = 50.0
            sell_pressure = 50.0

        if total_volume > 0:

            money_flow = (

                float(
                    recent_flow.sum()
                )

                /

                total_volume

                *

                100.0
            )

        else:

            money_flow = 0.0

        # ----------------------------------------------------
        # RVOL
        # ----------------------------------------------------

        previous = (

            volume.iloc[
                -21:-1
            ]

            if bars >= 21

            else

            volume.iloc[:-1]
        )

        average_volume = float(
            previous.mean()
        ) if len(previous) else 0.0

        current_volume = float(
            volume.iloc[-1]
        )

        relative_volume = (

            current_volume
            /
            average_volume

            if average_volume > 0

            else 0.0
        )

        # ----------------------------------------------------
        # VOLUME ACCELERATION
        # ----------------------------------------------------

        recent3 = float(
            volume
            .tail(3)
            .mean()
        )

        baseline_slice = (

            volume.iloc[
                -23:-3
            ]

            if bars >= 23

            else

            volume.iloc[:-3]
        )

        baseline_volume = float(
            baseline_slice.mean()
        ) if len(baseline_slice) else 0.0

        volume_acceleration = (

            recent3
            /
            baseline_volume

            if baseline_volume > 0

            else 0.0
        )

        # ----------------------------------------------------
        # PRICE CHANGE
        # ----------------------------------------------------

        price_reference_index = min(
            6,
            bars,
        )

        reference_price = float(
            close.iloc[
                -price_reference_index
            ]
        )

        current_price = float(
            close.iloc[-1]
        )

        price_change_pct = (

            (
                current_price
                /
                reference_price
            )

            -

            1.0

        ) * 100.0 if reference_price > 0 else 0.0

        # ----------------------------------------------------
        # PRICE / FLOW CONFIRMATION
        # ----------------------------------------------------

        price_volume_confirmation = bool(

            (
                price_change_pct > 0

                and

                money_flow > 5
            )

            or

            (
                price_change_pct < 0

                and

                money_flow < -5
            )
        )

        # ----------------------------------------------------
        # ABSORPTION / DISTRIBUTION
        # ----------------------------------------------------

        latest_range = float(
            high.iloc[-1]
            -
            low.iloc[-1]
        )

        latest_body = abs(
            float(
                close.iloc[-1]
                -
                open_price.iloc[-1]
            )
        )

        body_ratio = (

            latest_body
            /
            latest_range

            if latest_range > 0

            else 0.0
        )

        latest_clv = float(
            clv.iloc[-1]
        )

        high_volume_bar = bool(
            relative_volume >= 1.5
        )

        absorption = bool(

            high_volume_bar

            and

            body_ratio <= 0.40

            and

            latest_clv >= 0.35
        )

        distribution = bool(

            high_volume_bar

            and

            (
                latest_clv <= -0.35

                or

                (
                    price_change_pct < 0

                    and

                    money_flow < -10
                )
            )
        )

        bullish_absorption_score = 0.0

        if absorption:

            bullish_absorption_score = min(
                100.0,

                40.0

                +

                max(
                    0.0,
                    latest_clv
                )
                *
                30.0

                +

                min(
                    relative_volume,
                    4.0,
                )
                *
                7.5,
            )

        distribution_score = 0.0

        if distribution:

            distribution_score = min(
                100.0,

                40.0

                +

                abs(
                    min(
                        latest_clv,
                        0.0,
                    )
                )
                *
                30.0

                +

                min(
                    relative_volume,
                    4.0,
                )
                *
                7.5,
            )

        # ----------------------------------------------------
        # RECENT DOLLAR VOLUME
        # ----------------------------------------------------

        recent_dollar_volume = float(

            (
                close.tail(20)

                *

                volume.tail(20)
            )
            .sum()
        )

        return {

            "available":
                True,

            "bars":
                bars,

            "current_price":
                current_price,

            "current_volume":
                current_volume,

            "average_volume":
                average_volume,

            "relative_volume":
                float(
                    relative_volume
                ),

            "buy_pressure":
                float(
                    buy_pressure
                ),

            "sell_pressure":
                float(
                    sell_pressure
                ),

            "money_flow":
                float(
                    money_flow
                ),

            "volume_acceleration":
                float(
                    volume_acceleration
                ),

            "price_change_pct":
                float(
                    price_change_pct
                ),

            "price_volume_confirmation":
                price_volume_confirmation,

            "absorption":
                absorption,

            "distribution":
                distribution,

            "bullish_absorption_score":
                float(
                    bullish_absorption_score
                ),

            "distribution_score":
                float(
                    distribution_score
                ),

            "recent_dollar_volume":
                recent_dollar_volume,
        }


    # ========================================================
    # SCORE
    # ========================================================

    @staticmethod
    def _build_score(
        metrics_1m: dict[str, Any],
        metrics_5m: dict[str, Any],
        metrics_15m: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 50.0

        reasons: list[str] = []

        risk_flags: list[str] = []

        # ----------------------------------------------------
        # 5M is primary liquidity timeframe.
        # ----------------------------------------------------

        primary = (

            metrics_5m

            if metrics_5m.get(
                "available"
            )

            else

            metrics_15m
        )

        if not primary.get(
            "available"
        ):

            return (
                0.0,
                reasons,
                [
                    "LIQUIDITY_DATA_UNAVAILABLE"
                ],
            )

        buy_pressure = float(
            primary.get(
                "buy_pressure",
                50.0,
            )
        )

        sell_pressure = float(
            primary.get(
                "sell_pressure",
                50.0,
            )
        )

        money_flow = float(
            primary.get(
                "money_flow",
                0.0,
            )
        )

        acceleration = float(
            primary.get(
                "volume_acceleration",
                0.0,
            )
        )

        # ----------------------------------------------------
        # BUY / SELL PRESSURE
        # ----------------------------------------------------

        if buy_pressure >= 70:

            score += 18

            reasons.append(
                "STRONG_BUY_PRESSURE"
            )

        elif buy_pressure >= 60:

            score += 10

            reasons.append(
                "BUY_PRESSURE_DOMINANT"
            )

        elif sell_pressure >= 70:

            score -= 20

            risk_flags.append(
                "STRONG_SELL_PRESSURE"
            )

        elif sell_pressure >= 60:

            score -= 10

            risk_flags.append(
                "SELL_PRESSURE_DOMINANT"
            )

        # ----------------------------------------------------
        # MONEY FLOW
        # ----------------------------------------------------

        if money_flow >= 30:

            score += 15

            reasons.append(
                "STRONG_POSITIVE_MONEY_FLOW"
            )

        elif money_flow >= 15:

            score += 9

            reasons.append(
                "POSITIVE_MONEY_FLOW"
            )

        elif money_flow <= -30:

            score -= 18

            risk_flags.append(
                "STRONG_NEGATIVE_MONEY_FLOW"
            )

        elif money_flow <= -15:

            score -= 9

            risk_flags.append(
                "NEGATIVE_MONEY_FLOW"
            )

        # ----------------------------------------------------
        # VOLUME ACCELERATION
        # ----------------------------------------------------

        if acceleration >= 2.0:

            score += 12

            reasons.append(
                "VOLUME_ACCELERATION_STRONG"
            )

        elif acceleration >= 1.4:

            score += 7

            reasons.append(
                "VOLUME_ACCELERATION"
            )

        elif acceleration < 0.60:

            score -= 5

            risk_flags.append(
                "VOLUME_DECELERATION"
            )

        # ----------------------------------------------------
        # PRICE + FLOW CONFIRMATION
        # ----------------------------------------------------

        if primary.get(
            "price_volume_confirmation"
        ):

            if (
                primary.get(
                    "price_change_pct",
                    0.0,
                )
                >
                0
            ):

                score += 8

                reasons.append(
                    "PRICE_FLOW_CONFIRMATION"
                )

            else:

                score -= 5

                risk_flags.append(
                    "BEARISH_PRICE_FLOW_CONFIRMATION"
                )

        # ----------------------------------------------------
        # ABSORPTION
        # ----------------------------------------------------

        if primary.get(
            "absorption"
        ):

            score += 8

            reasons.append(
                "BULLISH_ABSORPTION_PROXY"
            )

        # ----------------------------------------------------
        # DISTRIBUTION
        # ----------------------------------------------------

        if primary.get(
            "distribution"
        ):

            score -= 15

            risk_flags.append(
                "DISTRIBUTION_PROXY_DETECTED"
            )

        # ----------------------------------------------------
        # CROSS-TIMEFRAME CONFIRMATION
        # ----------------------------------------------------

        positive_frames = 0
        negative_frames = 0

        for metrics in (
            metrics_1m,
            metrics_5m,
            metrics_15m,
        ):

            if not metrics.get(
                "available"
            ):

                continue

            flow = float(
                metrics.get(
                    "money_flow",
                    0.0,
                )
            )

            if flow >= 10:

                positive_frames += 1

            elif flow <= -10:

                negative_frames += 1

        if positive_frames >= 2:

            score += 8

            reasons.append(
                "MULTI_TIMEFRAME_LIQUIDITY_ALIGNMENT"
            )

        if negative_frames >= 2:

            score -= 10

            risk_flags.append(
                "MULTI_TIMEFRAME_SELLING_PRESSURE"
            )

        if (
            positive_frames > 0
            and
            negative_frames > 0
        ):

            risk_flags.append(
                "LIQUIDITY_TIMEFRAME_CONFLICT"
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

            risk_flags,
        )


    # ========================================================
    # CONFIDENCE
    # ========================================================

    @staticmethod
    def _calculate_confidence(
        *,
        bars_1m: int,
        bars_5m: int,
        bars_15m: int,
    ) -> float:

        confidence = 0.0

        # 5M is most important
        if bars_5m >= 25:

            confidence += 0.50

        elif bars_5m >= 15:

            confidence += 0.25

        # 15M context
        if bars_15m >= 20:

            confidence += 0.30

        elif bars_15m >= 10:

            confidence += 0.15

        # 1M confirmation
        if bars_1m >= 25:

            confidence += 0.20

        elif bars_1m >= 10:

            confidence += 0.10

        return round(
            min(
                confidence,
                1.0,
            ),
            3,
        )


    # ========================================================
    # BIAS
    # ========================================================

    @staticmethod
    def _bias(
        score: float,
        buy_pressure: float,
        sell_pressure: float,
    ) -> str:

        if (
            score >= 68
            and
            buy_pressure
            >
            sell_pressure
        ):

            return "BULLISH"

        if (
            score <= 35
            and
            sell_pressure
            >
            buy_pressure
        ):

            return "BEARISH"

        return "NEUTRAL"


    # ========================================================
    # ANALYZE SYMBOL
    # ========================================================

    def analyze_symbol(
        self,
        symbol: str,
    ) -> LiquidityResult:

        try:

            symbol = (
                self._normalize_symbol(
                    symbol
                )
            )

        except Exception as exc:

            return LiquidityResult(

                symbol="",

                status="ERROR",

                error=str(
                    exc
                ),
            )

        if self.alpaca is None:

            return LiquidityResult(

                symbol=symbol,

                status="API_UNAVAILABLE",

                risk_flags=[
                    "ALPACA_UNAVAILABLE"
                ],

                error=(
                    "Alpaca client unavailable."
                ),
            )

        # ----------------------------------------------------
        # FETCH
        # ----------------------------------------------------

        frames: dict[
            str,
            pd.DataFrame,
        ] = {}

        diagnostics: list[str] = []

        for (
            name,
            config,
        ) in (
            self.TIMEFRAME_CONFIG.items()
        ):

            dataframe = (
                self._fetch_bars(

                    symbol,

                    timeframe=(
                        config[
                            "timeframe"
                        ]
                    ),

                    lookback_days=(
                        config[
                            "lookback_days"
                        ]
                    ),

                    keep_bars=(
                        config[
                            "keep_bars"
                        ]
                    ),
                )
            )

            frames[
                name
            ] = dataframe

            diagnostics.append(

                f"{name}:BARS="
                f"{len(dataframe)}"
            )

        # ----------------------------------------------------
        # METRICS
        # ----------------------------------------------------

        metrics_1m = (
            self._analyze_bars(
                frames["1M"]
            )
        )

        metrics_5m = (
            self._analyze_bars(
                frames["5M"]
            )
        )

        metrics_15m = (
            self._analyze_bars(
                frames["15M"]
            )
        )

        bars_1m = len(
            frames["1M"]
        )

        bars_5m = len(
            frames["5M"]
        )

        bars_15m = len(
            frames["15M"]
        )

        # ----------------------------------------------------
        # DATA CHECK
        # ----------------------------------------------------

        if (
            not metrics_5m.get(
                "available"
            )

            and

            not metrics_15m.get(
                "available"
            )
        ):

            return LiquidityResult(

                symbol=symbol,

                status="INSUFFICIENT_DATA",

                bars_1m=bars_1m,

                bars_5m=bars_5m,

                bars_15m=bars_15m,

                confidence=(
                    self._calculate_confidence(

                        bars_1m=bars_1m,

                        bars_5m=bars_5m,

                        bars_15m=bars_15m,
                    )
                ),

                risk_flags=[
                    "LIQUIDITY_DATA_UNAVAILABLE"
                ],

                diagnostics=diagnostics,

                error=(
                    "Not enough 5M/15M data "
                    "for reliable liquidity analysis."
                ),
            )

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        (
            liquidity_score,
            reasons,
            risk_flags,
        ) = (
            self._build_score(

                metrics_1m,

                metrics_5m,

                metrics_15m,
            )
        )

        # ----------------------------------------------------
        # PRIMARY FRAME
        # ----------------------------------------------------

        primary = (

            metrics_5m

            if metrics_5m.get(
                "available"
            )

            else

            metrics_15m
        )

        confidence = (
            self._calculate_confidence(

                bars_1m=bars_1m,

                bars_5m=bars_5m,

                bars_15m=bars_15m,
            )
        )

        if confidence < 0.50:

            risk_flags.append(
                "LOW_LIQUIDITY_DATA_CONFIDENCE"
            )

        # ----------------------------------------------------
        # DOLLAR VOLUME RISK
        # ----------------------------------------------------

        dollar_volume = (
            primary.get(
                "recent_dollar_volume"
            )
        )

        if (
            dollar_volume is not None

            and

            dollar_volume < 250_000
        ):

            risk_flags.append(
                "LOW_RECENT_DOLLAR_VOLUME"
            )

        # ----------------------------------------------------
        # EXTREME VOLUME WITHOUT PRICE CONFIRMATION
        # ----------------------------------------------------

        if (
            primary.get(
                "volume_acceleration",
                0.0,
            )
            >= 2.5

            and

            not primary.get(
                "price_volume_confirmation"
            )
        ):

            risk_flags.append(
                "EXTREME_VOLUME_WITHOUT_PRICE_CONFIRMATION"
            )

        buy_pressure = float(
            primary.get(
                "buy_pressure",
                50.0,
            )
        )

        sell_pressure = float(
            primary.get(
                "sell_pressure",
                50.0,
            )
        )

        liquidity_bias = (
            self._bias(

                liquidity_score,

                buy_pressure,

                sell_pressure,
            )
        )

        return LiquidityResult(

            symbol=symbol,

            status="SUCCESS",

            liquidity_score=(
                liquidity_score
            ),

            confidence=(
                confidence
            ),

            liquidity_bias=(
                liquidity_bias
            ),

            buy_pressure=round(
                buy_pressure,
                2,
            ),

            sell_pressure=round(
                sell_pressure,
                2,
            ),

            money_flow=round(
                float(
                    primary.get(
                        "money_flow",
                        0.0,
                    )
                ),
                2,
            ),

            volume_acceleration=round(
                float(
                    primary.get(
                        "volume_acceleration",
                        0.0,
                    )
                ),
                2,
            ),

            price_volume_confirmation=bool(
                primary.get(
                    "price_volume_confirmation",
                    False,
                )
            ),

            absorption=bool(
                primary.get(
                    "absorption",
                    False,
                )
            ),

            distribution=bool(
                primary.get(
                    "distribution",
                    False,
                )
            ),

            bullish_absorption_score=round(
                float(
                    primary.get(
                        "bullish_absorption_score",
                        0.0,
                    )
                ),
                2,
            ),

            distribution_score=round(
                float(
                    primary.get(
                        "distribution_score",
                        0.0,
                    )
                ),
                2,
            ),

            price_change_pct=round(
                float(
                    primary.get(
                        "price_change_pct",
                        0.0,
                    )
                ),
                3,
            ),

            current_price=(
                primary.get(
                    "current_price"
                )
            ),

            current_volume=(
                primary.get(
                    "current_volume"
                )
            ),

            average_volume=(
                primary.get(
                    "average_volume"
                )
            ),

            relative_volume=round(
                float(
                    primary.get(
                        "relative_volume",
                        0.0,
                    )
                ),
                3,
            ),

            recent_dollar_volume=(
                dollar_volume
            ),

            bars_1m=(
                bars_1m
            ),

            bars_5m=(
                bars_5m
            ),

            bars_15m=(
                bars_15m
            ),

            reasons=(
                reasons
            ),

            risk_flags=list(
                dict.fromkeys(
                    risk_flags
                )
            ),

            diagnostics=(
                diagnostics
            ),

            error=None,
        )


    # ========================================================
    # CANDIDATE SYMBOL
    # ========================================================

    @staticmethod
    def _candidate_symbol(
        candidate: Any,
    ) -> Optional[str]:

        if candidate is None:

            return None

        if isinstance(
            candidate,
            str,
        ):

            value = (
                candidate
            )

        elif isinstance(
            candidate,
            dict,
        ):

            value = (
                candidate.get(
                    "symbol"
                )
            )

        else:

            value = (
                getattr(
                    candidate,
                    "symbol",
                    None,
                )
            )

        symbol = str(
            value
            or ""
        ).strip().upper()

        return (
            symbol
            if symbol
            else None
        )


    # ========================================================
    # ANALYZE MULTIPLE CANDIDATES
    # ========================================================

    def analyze_candidates(
        self,
        candidates: Iterable[Any],
        *,
        top_n: int = 5,
    ) -> list[
        LiquidityResult
    ]:

        symbols: list[str] = []

        seen: set[str] = set()

        for candidate in (
            candidates
            or []
        ):

            symbol = (
                self._candidate_symbol(
                    candidate
                )
            )

            if not symbol:

                continue

            if symbol in seen:

                continue

            seen.add(
                symbol
            )

            symbols.append(
                symbol
            )

            if len(symbols) >= max(
                1,
                int(
                    top_n
                ),
            ):

                break

        results: list[
            LiquidityResult
        ] = []

        for symbol in symbols:

            result = (
                self.analyze_symbol(
                    symbol
                )
            )

            results.append(
                result
            )

        results.sort(

            key=lambda item: (

                item.liquidity_score,

                item.confidence,

            ),

            reverse=True,
        )

        return results


    # ========================================================
    # HEALTH CHECK
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        return {

            "version":
                "2.0",

            "env_exists":
                ENV_FILE.exists(),

            "credentials_present":
                bool(
                    self.api_key
                    and
                    self.api_secret
                ),

            "alpaca_available":
                self.alpaca
                is not None,

            "paper_url":
                (
                    "paper-api.alpaca.markets"
                    in
                    self.base_url.lower()
                ),

            "timeframes": [
                "1M",
                "5M",
                "15M",
            ],

            "method":
                "OHLCV_FLOW_PROXY",

            "research_only":
                True,

            "order_execution_enabled":
                False,
        }


# ============================================================
# LEGACY HELPER
# ============================================================

def analyze_liquidity(
    symbol: str,
) -> LiquidityResult:

    engine = (
        LiquidityEngine()
    )

    return (
        engine.analyze_symbol(
            symbol
        )
    )


# ============================================================
# STANDALONE
# ============================================================

if __name__ == "__main__":

    engine = (
        LiquidityEngine()
    )

    print(
        engine.health_check()
    )
