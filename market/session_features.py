from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, Optional

import pandas as pd


@dataclass
class SessionFeatures:
    symbol: str

    session_date: str

    current_price: float

    previous_close: Optional[float]

    gap_pct: Optional[float]

    premarket_high: Optional[float]
    premarket_low: Optional[float]
    premarket_volume: int

    regular_open: Optional[float]

    opening_range_high: Optional[float]
    opening_range_low: Optional[float]
    opening_range_ready: bool

    above_premarket_high: Optional[bool]
    above_opening_range_high: Optional[bool]

    distance_to_premarket_high_pct: Optional[float]
    distance_to_opening_range_high_pct: Optional[float]

    regular_volume: int

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


class SessionFeatureEngine:
    """
    JALWE V4 - Session Feature Engine

    Creates real intraday session features used by:

    - Premarket High Break
    - Opening Range Breakout
    - Gap & Go
    - First Pullback
    - Round 2 / Round 3 re-entry

    Assumes the DataFrame index contains timestamps.
    UTC data is converted to New York market time.

    No synthetic values are created.
    """

    MARKET_TZ = "America/New_York"

    PREMARKET_START = time(4, 0)
    REGULAR_OPEN = time(9, 30)
    OPENING_RANGE_END = time(9, 45)
    REGULAR_CLOSE = time(16, 0)

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _distance_pct(
        price: float,
        level: Optional[float],
    ) -> Optional[float]:

        if (
            level is None
            or level <= 0
            or price <= 0
        ):
            return None

        return (
            (price - level)
            / level
            * 100.0
        )

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:

        try:
            if value is None:
                return None

            value = float(value)

            if pd.isna(value):
                return None

            return value

        except (
            TypeError,
            ValueError,
        ):
            return None

    # ========================================================
    # PREPARE DATA
    # ========================================================

    def _prepare(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:

        if df is None or df.empty:
            raise ValueError(
                "Session data is empty."
            )

        required = {
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        missing = (
            required
            - set(df.columns)
        )

        if missing:
            raise ValueError(
                "Missing session columns: "
                + ", ".join(
                    sorted(missing)
                )
            )

        data = df.copy()

        index = pd.to_datetime(
            data.index,
            utc=True,
            errors="coerce",
        )

        valid = ~index.isna()

        data = data.loc[valid].copy()
        index = index[valid]

        if data.empty:
            raise ValueError(
                "No valid timestamps available."
            )

        data.index = (
            index.tz_convert(
                self.MARKET_TZ
            )
        )

        for column in (
            "open",
            "high",
            "low",
            "close",
            "volume",
        ):

            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
            ]
        )

        if data.empty:
            raise ValueError(
                "No valid OHLC session data."
            )

        data = data.sort_index()

        data["_date"] = (
            data.index.date
        )

        data["_time"] = (
            data.index.time
        )

        return data

    # ========================================================
    # BUILD
    # ========================================================

    def build(
        self,
        symbol: str,
        df: pd.DataFrame,
    ) -> SessionFeatures:

        symbol = str(
            symbol
        ).strip().upper()

        if not symbol:
            raise ValueError(
                "Symbol cannot be empty."
            )

        data = self._prepare(
            df
        )

        latest_timestamp = (
            data.index[-1]
        )

        session_date = (
            latest_timestamp.date()
        )

        today = data[
            data["_date"]
            == session_date
        ].copy()

        if today.empty:
            raise RuntimeError(
                "Unable to locate latest session."
            )

        current_price = float(
            today["close"].iloc[-1]
        )

        # ====================================================
        # PREMARKET
        # ====================================================

        premarket = today[
            (
                today["_time"]
                >= self.PREMARKET_START
            )
            &
            (
                today["_time"]
                < self.REGULAR_OPEN
            )
        ]

        premarket_high: Optional[float] = None
        premarket_low: Optional[float] = None
        premarket_volume = 0

        if not premarket.empty:

            premarket_high = float(
                premarket["high"].max()
            )

            premarket_low = float(
                premarket["low"].min()
            )

            premarket_volume = int(
                premarket["volume"]
                .fillna(0)
                .sum()
            )

        # ====================================================
        # REGULAR SESSION
        # ====================================================

        regular = today[
            (
                today["_time"]
                >= self.REGULAR_OPEN
            )
            &
            (
                today["_time"]
                < self.REGULAR_CLOSE
            )
        ]

        regular_open: Optional[float] = None
        regular_volume = 0

        if not regular.empty:

            regular_open = float(
                regular["open"].iloc[0]
            )

            regular_volume = int(
                regular["volume"]
                .fillna(0)
                .sum()
            )

        # ====================================================
        # PREVIOUS REGULAR CLOSE
        # ====================================================

        previous_close: Optional[float] = None

        previous_dates = sorted(
            date_value
            for date_value in data["_date"].unique()
            if date_value < session_date
        )

        if previous_dates:

            previous_date = (
                previous_dates[-1]
            )

            previous_day = data[
                data["_date"]
                == previous_date
            ]

            previous_regular = (
                previous_day[
                    (
                        previous_day["_time"]
                        >= self.REGULAR_OPEN
                    )
                    &
                    (
                        previous_day["_time"]
                        < self.REGULAR_CLOSE
                    )
                ]
            )

            if not previous_regular.empty:

                previous_close = float(
                    previous_regular[
                        "close"
                    ].iloc[-1]
                )

            elif not previous_day.empty:

                # Historical provider may not contain
                # complete regular-session segmentation.
                previous_close = float(
                    previous_day[
                        "close"
                    ].iloc[-1]
                )

        # ====================================================
        # GAP
        # ====================================================

        gap_pct: Optional[float] = None

        if (
            regular_open is not None
            and previous_close is not None
            and previous_close > 0
        ):

            gap_pct = (
                (
                    regular_open
                    - previous_close
                )
                / previous_close
                * 100.0
            )

        # ====================================================
        # OPENING RANGE - FIRST 15 MINUTES
        # ====================================================

        opening_range = today[
            (
                today["_time"]
                >= self.REGULAR_OPEN
            )
            &
            (
                today["_time"]
                < self.OPENING_RANGE_END
            )
        ]

        opening_range_high: Optional[
            float
        ] = None

        opening_range_low: Optional[
            float
        ] = None

        if not opening_range.empty:

            opening_range_high = float(
                opening_range[
                    "high"
                ].max()
            )

            opening_range_low = float(
                opening_range[
                    "low"
                ].min()
            )

        opening_range_ready = bool(
            latest_timestamp.time()
            >= self.OPENING_RANGE_END
            and opening_range_high
            is not None
            and opening_range_low
            is not None
        )

        # ====================================================
        # POSITION RELATIVE TO LEVELS
        # ====================================================

        above_premarket_high = None

        if premarket_high is not None:

            above_premarket_high = (
                current_price
                > premarket_high
            )

        above_opening_range_high = None

        if opening_range_high is not None:

            above_opening_range_high = (
                current_price
                > opening_range_high
            )

        distance_to_premarket_high = (
            self._distance_pct(
                current_price,
                premarket_high,
            )
        )

        distance_to_or_high = (
            self._distance_pct(
                current_price,
                opening_range_high,
            )
        )

        # ====================================================
        # RESULT
        # ====================================================

        return SessionFeatures(
            symbol=symbol,

            session_date=(
                session_date.isoformat()
            ),

            current_price=(
                current_price
            ),

            previous_close=(
                previous_close
            ),

            gap_pct=(
                gap_pct
            ),

            premarket_high=(
                premarket_high
            ),

            premarket_low=(
                premarket_low
            ),

            premarket_volume=(
                premarket_volume
            ),

            regular_open=(
                regular_open
            ),

            opening_range_high=(
                opening_range_high
            ),

            opening_range_low=(
                opening_range_low
            ),

            opening_range_ready=(
                opening_range_ready
            ),

            above_premarket_high=(
                above_premarket_high
            ),

            above_opening_range_high=(
                above_opening_range_high
            ),

            distance_to_premarket_high_pct=(
                distance_to_premarket_high
            ),

            distance_to_opening_range_high_pct=(
                distance_to_or_high
            ),

            regular_volume=(
                regular_volume
            ),

            metadata={
                "market_timezone": (
                    self.MARKET_TZ
                ),

                "opening_range_minutes": 15,

                "latest_timestamp": (
                    latest_timestamp.isoformat()
                ),
            },
        )


_session_feature_engine: Optional[
    SessionFeatureEngine
] = None


def get_session_feature_engine(
) -> SessionFeatureEngine:

    global _session_feature_engine

    if _session_feature_engine is None:

        _session_feature_engine = (
            SessionFeatureEngine()
        )

    return _session_feature_engine