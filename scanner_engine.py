from __future__ import annotations

import logging
import math
import os

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from alpaca_trade_api.rest import REST
from dotenv import load_dotenv

from finvizfinance.screener.custom import Custom
from finvizfinance.screener.overview import Overview


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# LOAD .ENV
# ============================================================

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

ENV_FILE = (
    BASE_DIR
    / ".env"
)

load_dotenv(
    ENV_FILE
)


# ============================================================
# RANKED CANDIDATE
# ============================================================

@dataclass
class RankedCandidate:

    symbol: str

    rank_score: float = 0.0

    relative_volume: Optional[float] = None

    volume: Optional[float] = None

    average_volume: Optional[float] = None

    price: Optional[float] = None

    change_pct: Optional[float] = None

    float_shares: Optional[float] = None

    dollar_volume: Optional[float] = None

    tradable: bool = False

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    raw: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# RADAR RESULT
# ============================================================

@dataclass
class RadarResult:

    symbols: list[str] = field(
        default_factory=list
    )

    ranked_candidates: list[
        RankedCandidate
    ] = field(
        default_factory=list
    )

    status: str = "UNKNOWN"

    source: str = "FINVIZ"

    scanned_count: int = 0

    tradable_count: int = 0

    returned_count: int = 0

    filters: dict[str, str] = field(
        default_factory=dict
    )

    warnings: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None


# ============================================================
# SCANNER ENGINE
# ============================================================

class ScannerEngine:
    """
    APEX SCANNER ENGINE V3
    Ranked Small-Cap Pre-Breakout Radar

    PURPOSE:

        Finviz
            ↓
        Small-cap filter
            ↓
        Relative Volume / Volume / Float / Change
            ↓
        Alpaca tradability verification
            ↓
        Ranked candidates
            ↓
        PreBreakoutEngine
            ↓
        News / AI
            ↓
        JALWE

    RESEARCH ONLY.

    This engine cannot:
        - buy
        - sell
        - submit orders
        - manage positions
    """

    # ========================================================
    # FINVIZ FILTERS
    # ========================================================

    DEFAULT_FILTERS = {
        "Price": "Under $15",
        "Float": "Under 20M",
        "Relative Volume": "Over 1.5",
        "Industry": "Stocks only (ex-Funds)",
    }

    # ========================================================
    # CUSTOM FINVIZ COLUMNS
    #
    # 1  = Ticker
    # 25 = Shares Float
    # 63 = Average Volume
    # 64 = Relative Volume
    # 65 = Price
    # 66 = Change
    # 67 = Volume
    # ========================================================

    CUSTOM_COLUMNS = [
        1,
        25,
        63,
        64,
        65,
        66,
        67,
    ]

    # ========================================================
    # INIT
    # ========================================================

    def __init__(
        self,
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

        self.alpaca: Optional[
            REST
        ] = None

        self._initialize_alpaca()

    # ========================================================
    # CREDENTIALS
    # ========================================================

    def credentials_ready(
        self,
    ) -> bool:

        return bool(
            self.api_key
            and
            self.api_secret
        )

    # ========================================================
    # INITIALIZE ALPACA
    # ========================================================

    def _initialize_alpaca(
        self,
    ) -> None:

        if not self.credentials_ready():

            logger.warning(
                "ScannerEngine Alpaca "
                "credentials are missing."
            )

            return

        if (
            "paper-api.alpaca.markets"
            not in
            self.base_url.lower()
        ):

            logger.error(
                "ScannerEngine blocked: "
                "Alpaca URL is not PAPER."
            )

            return

        try:

            self.alpaca = REST(
                self.api_key,
                self.api_secret,
                self.base_url,
                api_version="v2",
            )

        except Exception as exc:

            logger.exception(
                "ScannerEngine Alpaca "
                "initialization failed: %s",
                exc,
            )

            self.alpaca = None

    # ========================================================
    # SYMBOL
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        value: Any,
    ) -> Optional[str]:

        symbol = str(
            value
            or ""
        ).strip().upper()

        if not symbol:
            return None

        if len(symbol) > 15:
            return None

        blocked = {
            "/",
            "^",
            " ",
        }

        if any(
            character in symbol
            for character in blocked
        ):

            return None

        return symbol

    # ========================================================
    # NUMERIC CONVERSION
    # ========================================================

    @staticmethod
    def _number(
        value: Any,
    ) -> Optional[float]:

        if value is None:
            return None

        try:

            if pd.isna(
                value
            ):
                return None

        except Exception:
            pass

        if isinstance(
            value,
            (
                int,
                float,
            ),
        ):

            number = float(
                value
            )

            if math.isfinite(
                number
            ):
                return number

            return None

        text = str(
            value
        ).strip().upper()

        if not text:
            return None

        text = (
            text
            .replace(
                ",",
                "",
            )
            .replace(
                "$",
                "",
            )
        )

        is_percent = (
            text.endswith(
                "%"
            )
        )

        if is_percent:

            text = text[:-1]

        multiplier = 1.0

        if text.endswith(
            "K"
        ):

            multiplier = 1_000.0

            text = text[:-1]

        elif text.endswith(
            "M"
        ):

            multiplier = 1_000_000.0

            text = text[:-1]

        elif text.endswith(
            "B"
        ):

            multiplier = 1_000_000_000.0

            text = text[:-1]

        elif text.endswith(
            "T"
        ):

            multiplier = (
                1_000_000_000_000.0
            )

            text = text[:-1]

        try:

            number = float(
                text
            ) * multiplier

        except (
            TypeError,
            ValueError,
        ):

            return None

        if not math.isfinite(
            number
        ):

            return None

        return number

    # ========================================================
    # COLUMN LOOKUP
    # ========================================================

    @staticmethod
    def _find_column(
        dataframe: pd.DataFrame,
        aliases: list[str],
    ) -> Optional[str]:

        normalized = {
            str(column)
            .strip()
            .lower():
            column
            for column
            in dataframe.columns
        }

        for alias in aliases:

            key = (
                alias
                .strip()
                .lower()
            )

            if key in normalized:

                return normalized[
                    key
                ]

        return None

    # ========================================================
    # GET FINVIZ DATA
    # ========================================================

    def _run_custom_screen(
        self,
        filters: dict[str, str],
    ) -> tuple[
        pd.DataFrame,
        list[str],
        str,
    ]:

        warnings: list[str] = []

        try:

            screener = (
                Custom()
            )

            screener.set_filter(
                filters_dict=filters
            )

            dataframe = (
                screener.screener_view(
                    columns=(
                        self.CUSTOM_COLUMNS
                    ),
                    verbose=0,
                )
            )

            if (
                dataframe is not None
                and
                not dataframe.empty
            ):

                return (
                    dataframe,
                    warnings,
                    "FINVIZ_CUSTOM",
                )

        except Exception as exc:

            warnings.append(
                "FINVIZ_CUSTOM_FAILED: "
                + str(
                    exc
                )
            )

            logger.warning(
                "Finviz Custom screener "
                "failed: %s",
                exc,
            )

        # ----------------------------------------------------
        # Secondary compatibility fallback.
        # Still real Finviz data.
        # ----------------------------------------------------

        try:

            overview = (
                Overview()
            )

            overview.set_filter(
                filters_dict=filters
            )

            dataframe = (
                overview.screener_view(
                    verbose=0
                )
            )

            if (
                dataframe is None
                or
                dataframe.empty
            ):

                return (
                    pd.DataFrame(),
                    warnings,
                    "FINVIZ",
                )

            warnings.append(
                "CUSTOM_COLUMNS_UNAVAILABLE_"
                "USING_OVERVIEW"
            )

            return (
                dataframe,
                warnings,
                "FINVIZ_OVERVIEW",
            )

        except Exception as exc:

            raise RuntimeError(
                "Finviz scan failed: "
                + str(
                    exc
                )
            ) from exc

    # ========================================================
    # ALPACA TRADABLE SYMBOLS
    # ========================================================

    def _get_tradable_symbols(
        self,
    ) -> set[str]:

        if self.alpaca is None:

            return set()

        try:

            assets = (
                self.alpaca
                .list_assets(
                    status="active",
                    asset_class="us_equity",
                )
            )

        except Exception as exc:

            logger.exception(
                "Could not fetch Alpaca "
                "asset universe: %s",
                exc,
            )

            return set()

        symbols: set[str] = set()

        for asset in assets:

            if not bool(
                getattr(
                    asset,
                    "tradable",
                    False,
                )
            ):

                continue

            symbol = (
                self._normalize_symbol(
                    getattr(
                        asset,
                        "symbol",
                        "",
                    )
                )
            )

            if symbol:

                symbols.add(
                    symbol
                )

        return symbols

    # ========================================================
    # SCORE CANDIDATE
    # ========================================================

    @staticmethod
    def _rank_candidate(
        *,
        relative_volume: Optional[float],
        volume: Optional[float],
        average_volume: Optional[float],
        price: Optional[float],
        change_pct: Optional[float],
        float_shares: Optional[float],
    ) -> tuple[
        float,
        list[str],
        list[str],
    ]:

        score = 0.0

        reasons: list[str] = []

        warnings: list[str] = []

        # ====================================================
        # RVOL - 35 POINTS
        # ====================================================

        if relative_volume is not None:

            if relative_volume >= 5.0:

                score += 35.0

                reasons.append(
                    "Extreme relative volume"
                )

            elif relative_volume >= 3.0:

                score += 30.0

                reasons.append(
                    "Very strong relative volume"
                )

            elif relative_volume >= 2.0:

                score += 24.0

                reasons.append(
                    "Strong relative volume"
                )

            elif relative_volume >= 1.5:

                score += 17.0

                reasons.append(
                    "Elevated relative volume"
                )

        else:

            warnings.append(
                "Relative volume unavailable"
            )

        # ====================================================
        # CURRENT VOLUME - 20 POINTS
        # ====================================================

        if volume is not None:

            if volume >= 10_000_000:

                score += 20.0

                reasons.append(
                    "Exceptional trading volume"
                )

            elif volume >= 5_000_000:

                score += 17.0

                reasons.append(
                    "Very high trading volume"
                )

            elif volume >= 2_000_000:

                score += 14.0

                reasons.append(
                    "Strong trading volume"
                )

            elif volume >= 1_000_000:

                score += 10.0

            elif volume >= 500_000:

                score += 6.0

            else:

                warnings.append(
                    "Low absolute volume"
                )

        # ====================================================
        # FLOAT - 15 POINTS
        # ====================================================

        if float_shares is not None:

            if float_shares <= 3_000_000:

                score += 15.0

                reasons.append(
                    "Very low float"
                )

            elif float_shares <= 5_000_000:

                score += 13.0

                reasons.append(
                    "Low float"
                )

            elif float_shares <= 10_000_000:

                score += 10.0

            elif float_shares <= 20_000_000:

                score += 6.0

        else:

            warnings.append(
                "Float unavailable"
            )

        # ====================================================
        # PRICE QUALITY - 10 POINTS
        # ====================================================

        if price is not None:

            if (
                1.0
                <= price
                <= 15.0
            ):

                score += 10.0

            elif (
                0.50
                <= price
                < 1.0
            ):

                score += 5.0

                warnings.append(
                    "Sub-dollar stock"
                )

            elif price < 0.50:

                score += 1.0

                warnings.append(
                    "Very low price stock"
                )

        # ====================================================
        # MOMENTUM / CHANGE - 15 POINTS
        # ====================================================

        if change_pct is not None:

            if (
                5.0
                <= change_pct
                <= 20.0
            ):

                score += 15.0

                reasons.append(
                    "Strong positive momentum"
                )

            elif (
                2.0
                <= change_pct
                < 5.0
            ):

                score += 11.0

                reasons.append(
                    "Positive momentum"
                )

            elif (
                0.0
                <= change_pct
                < 2.0
            ):

                score += 6.0

            elif (
                20.0
                <
                change_pct
                <= 40.0
            ):

                score += 10.0

                warnings.append(
                    "Already extended"
                )

            elif change_pct > 40.0:

                score += 4.0

                warnings.append(
                    "Highly extended move"
                )

            elif change_pct < -5.0:

                warnings.append(
                    "Negative momentum"
                )

        # ====================================================
        # VOLUME VS AVG BONUS - 5 POINTS
        # ====================================================

        if (
            volume is not None
            and
            average_volume is not None
            and
            average_volume > 0
        ):

            ratio = (
                volume
                /
                average_volume
            )

            if ratio >= 2.0:

                score += 5.0

                reasons.append(
                    "Volume exceeds average"
                )

            elif ratio >= 1.5:

                score += 3.0

        return (
            round(
                min(
                    score,
                    100.0,
                ),
                2,
            ),
            reasons,
            warnings,
        )

    # ========================================================
    # BUILD CANDIDATES
    # ========================================================

    def _build_ranked_candidates(
        self,
        dataframe: pd.DataFrame,
        tradable_symbols: set[str],
    ) -> list[RankedCandidate]:

        ticker_column = (
            self._find_column(
                dataframe,
                [
                    "Ticker",
                    "Symbol",
                ],
            )
        )

        if ticker_column is None:

            raise RuntimeError(
                "Finviz response does not "
                "contain ticker column."
            )

        float_column = (
            self._find_column(
                dataframe,
                [
                    "Shares Float",
                    "Float",
                ],
            )
        )

        avg_volume_column = (
            self._find_column(
                dataframe,
                [
                    "Average Volume",
                    "Avg Volume",
                ],
            )
        )

        relative_volume_column = (
            self._find_column(
                dataframe,
                [
                    "Relative Volume",
                    "Rel Volume",
                ],
            )
        )

        price_column = (
            self._find_column(
                dataframe,
                [
                    "Price",
                ],
            )
        )

        change_column = (
            self._find_column(
                dataframe,
                [
                    "Change",
                    "Change %",
                ],
            )
        )

        volume_column = (
            self._find_column(
                dataframe,
                [
                    "Volume",
                ],
            )
        )

        output: list[
            RankedCandidate
        ] = []

        seen: set[str] = set()

        for _, row in (
            dataframe.iterrows()
        ):

            symbol = (
                self._normalize_symbol(
                    row.get(
                        ticker_column
                    )
                )
            )

            if (
                not symbol
                or
                symbol in seen
            ):

                continue

            seen.add(
                symbol
            )

            # -----------------------------------------------
            # Must exist as tradable Alpaca asset.
            # -----------------------------------------------

            tradable = (
                symbol
                in tradable_symbols
            )

            if not tradable:

                continue

            relative_volume = (
                self._number(
                    row.get(
                        relative_volume_column
                    )
                )
                if relative_volume_column
                else None
            )

            volume = (
                self._number(
                    row.get(
                        volume_column
                    )
                )
                if volume_column
                else None
            )

            average_volume = (
                self._number(
                    row.get(
                        avg_volume_column
                    )
                )
                if avg_volume_column
                else None
            )

            price = (
                self._number(
                    row.get(
                        price_column
                    )
                )
                if price_column
                else None
            )

            change_pct = (
                self._number(
                    row.get(
                        change_column
                    )
                )
                if change_column
                else None
            )

            # ------------------------------------------------
            # Some Finviz versions already convert
            # percentages to decimal form.
            # ------------------------------------------------

            if (
                change_pct is not None
                and
                abs(
                    change_pct
                )
                <= 1.0
            ):

                change_pct = (
                    change_pct
                    *
                    100.0
                )

            float_shares = (
                self._number(
                    row.get(
                        float_column
                    )
                )
                if float_column
                else None
            )

            dollar_volume = None

            if (
                price is not None
                and
                volume is not None
            ):

                dollar_volume = (
                    price
                    *
                    volume
                )

            (
                rank_score,
                reasons,
                warnings,
            ) = (
                self._rank_candidate(
                    relative_volume=(
                        relative_volume
                    ),
                    volume=volume,
                    average_volume=(
                        average_volume
                    ),
                    price=price,
                    change_pct=(
                        change_pct
                    ),
                    float_shares=(
                        float_shares
                    ),
                )
            )

            output.append(
                RankedCandidate(
                    symbol=symbol,

                    rank_score=(
                        rank_score
                    ),

                    relative_volume=(
                        relative_volume
                    ),

                    volume=volume,

                    average_volume=(
                        average_volume
                    ),

                    price=price,

                    change_pct=(
                        change_pct
                    ),

                    float_shares=(
                        float_shares
                    ),

                    dollar_volume=(
                        dollar_volume
                    ),

                    tradable=True,

                    reasons=reasons,

                    warnings=warnings,

                    raw={
                        str(key): value
                        for key, value
                        in row.to_dict().items()
                    },
                )
            )

        # ====================================================
        # SORT
        # ====================================================

        output.sort(
            key=lambda item: (
                item.rank_score,

                item.relative_volume
                or 0.0,

                item.dollar_volume
                or 0.0,

                item.volume
                or 0.0,
            ),
            reverse=True,
        )

        return output

    # ========================================================
    # RUN RADAR
    # ========================================================

    def run_radar(
        self,
        *,
        filters: Optional[
            dict[str, str]
        ] = None,
        top_n: int = 40,
    ) -> RadarResult:

        active_filters = dict(
            filters
            or
            self.DEFAULT_FILTERS
        )

        # ----------------------------------------------------
        # Alpaca must be available because we want
        # tradability verified.
        # ----------------------------------------------------

        if self.alpaca is None:

            return RadarResult(
                status=(
                    "VALIDATION_UNAVAILABLE"
                ),

                source="NONE",

                filters=active_filters,

                warnings=[
                    "ALPACA_UNAVAILABLE"
                ],

                error=(
                    "Alpaca validation "
                    "is unavailable."
                ),
            )

        # ----------------------------------------------------
        # FINVIZ
        # ----------------------------------------------------

        try:

            (
                dataframe,
                warnings,
                source,
            ) = (
                self._run_custom_screen(
                    active_filters
                )
            )

        except Exception as exc:

            return RadarResult(
                status="ERROR",

                source="FINVIZ",

                filters=active_filters,

                warnings=[
                    "FINVIZ_SCAN_FAILED"
                ],

                error=str(
                    exc
                ),
            )

        if dataframe.empty:

            return RadarResult(
                status="NO_MATCHES",

                source=source,

                filters=active_filters,

                warnings=warnings,
            )

        scanned_count = len(
            dataframe
        )

        # ----------------------------------------------------
        # ALPACA UNIVERSE
        # ----------------------------------------------------

        tradable_symbols = (
            self._get_tradable_symbols()
        )

        if not tradable_symbols:

            return RadarResult(
                status=(
                    "VALIDATION_UNAVAILABLE"
                ),

                source=source,

                scanned_count=(
                    scanned_count
                ),

                filters=active_filters,

                warnings=(
                    warnings
                    +
                    [
                        "ALPACA_ASSET_UNIVERSE_EMPTY"
                    ]
                ),

                error=(
                    "Could not obtain tradable "
                    "Alpaca asset universe."
                ),
            )

        # ----------------------------------------------------
        # BUILD RANKING
        # ----------------------------------------------------

        try:

            ranked = (
                self._build_ranked_candidates(
                    dataframe,
                    tradable_symbols,
                )
            )

        except Exception as exc:

            return RadarResult(
                status="ERROR",

                source=source,

                scanned_count=(
                    scanned_count
                ),

                filters=active_filters,

                warnings=warnings,

                error=str(
                    exc
                ),
            )

        tradable_count = len(
            ranked
        )

        if not ranked:

            return RadarResult(
                status=(
                    "NO_TRADABLE_TARGETS"
                ),

                source=source,

                scanned_count=(
                    scanned_count
                ),

                tradable_count=0,

                filters=active_filters,

                warnings=warnings,
            )

        top_n = max(
            1,
            int(
                top_n
            ),
        )

        selected = (
            ranked[
                :top_n
            ]
        )

        symbols = [
            candidate.symbol
            for candidate
            in selected
        ]

        logger.info(
            "Scanner V3: %s Finviz -> "
            "%s Alpaca tradable -> "
            "%s ranked candidates.",
            scanned_count,
            tradable_count,
            len(
                selected
            ),
        )

        return RadarResult(
            symbols=symbols,

            ranked_candidates=(
                selected
            ),

            status="SUCCESS",

            source=(
                source
                +
                "+ALPACA"
            ),

            scanned_count=(
                scanned_count
            ),

            tradable_count=(
                tradable_count
            ),

            returned_count=(
                len(
                    selected
                )
            ),

            filters=(
                active_filters
            ),

            warnings=warnings,

            error=None,
        )

    # ========================================================
    # HEALTH
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        return {
            "env_file_exists": (
                ENV_FILE.exists()
            ),

            "credentials_present": (
                self.credentials_ready()
            ),

            "alpaca_initialized": (
                self.alpaca
                is not None
            ),

            "paper_url": (
                "paper-api.alpaca.markets"
                in
                self.base_url.lower()
            ),

            "strategy_profile": (
                "RANKED_SMALL_CAP_PRE_BREAKOUT"
            ),

            "ranking_fields": [
                "relative_volume",
                "volume",
                "average_volume",
                "float",
                "price",
                "change_pct",
                "dollar_volume",
            ],

            "order_execution_enabled": (
                False
            ),
        }


# ============================================================
# OLD COMPATIBILITY FUNCTION
# ============================================================

def run_jalwe_radar(
) -> list[str]:

    try:

        engine = (
            ScannerEngine()
        )

        result = (
            engine.run_radar(
                top_n=40
            )
        )

        return list(
            result.symbols
        )

    except Exception as exc:

        logger.exception(
            "run_jalwe_radar failed: %s",
            exc,
        )

        return []


# ============================================================
# INDEPENDENT TEST
# ============================================================

if __name__ == "__main__":

    engine = (
        ScannerEngine()
    )

    result = (
        engine.run_radar(
            top_n=40
        )
    )

    print(
        "========================================"
    )

    print(
        "APEX RANKED RADAR V3"
    )

    print(
        "========================================"
    )

    print(
        "STATUS:",
        result.status,
    )

    print(
        "SOURCE:",
        result.source,
    )

    print(
        "FINVIZ:",
        result.scanned_count,
    )

    print(
        "TRADABLE:",
        result.tradable_count,
    )

    print(
        "RETURNED:",
        result.returned_count,
    )

    print(
        "WARNINGS:",
        result.warnings,
    )

    print(
        "ERROR:",
        result.error,
    )

    print()

    for (
        index,
        candidate,
    ) in enumerate(
        result.ranked_candidates[
            :20
        ],
        start=1,
    ):

        print(
            index,
            candidate.symbol,
            "| SCORE:",
            candidate.rank_score,
            "| RVOL:",
            candidate.relative_volume,
            "| VOL:",
            candidate.volume,
            "| FLOAT:",
            candidate.float_shares,
            "| PRICE:",
            candidate.price,
            "| CHANGE:",
            candidate.change_pct,
        )