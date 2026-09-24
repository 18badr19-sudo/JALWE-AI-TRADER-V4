from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from alpaca.data.enums import OptionsFeed
from alpaca.data.historical.option import (
    OptionHistoricalDataClient,
)
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
)

from core.config import settings
from core.models import FeatureSnapshot
from market.market_data import get_market_data


logger = logging.getLogger(__name__)

NEW_YORK_TZ = ZoneInfo("America/New_York")


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class OptionContractSnapshot:
    symbol: str

    option_type: str

    expiration_date: date
    strike_price: float
    dte: int

    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None

    spread: Optional[float] = None
    spread_pct: Optional[float] = None

    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    implied_volatility: Optional[float] = None

    open_interest: int = 0

    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    latest_trade_price: Optional[float] = None
    latest_trade_size: Optional[float] = None

    distance_from_stock_pct: Optional[float] = None

    quality_score: float = 0.0
    directional_weight: float = 0.0


@dataclass
class OptionsFlowAnalysis:
    symbol: str

    available: bool

    score: Optional[float]

    bias: str

    underlying_price: Optional[float]

    contract_count: int
    usable_contract_count: int

    call_count: int
    put_count: int

    call_open_interest: int
    put_open_interest: int

    put_call_oi_ratio: Optional[float]

    call_strength: float
    put_strength: float

    top_calls: list[
        OptionContractSnapshot
    ] = field(
        default_factory=list
    )

    top_puts: list[
        OptionContractSnapshot
    ] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


class OptionsFlowError(RuntimeError):
    pass


# ============================================================
# OPTIONS ENGINE
# ============================================================

class OptionsFlowEngine:
    """
    JALWE AI TRADER V4 - Options Intelligence Engine.

    PHASE 1:
        OPTION CHAIN / OPEN INTEREST BIAS

    This version analyzes:

    - Calls vs puts
    - Open interest
    - Greeks
    - Delta
    - Implied volatility
    - Bid/ask spread
    - Strike proximity
    - DTE
    - Quote liquidity

    IMPORTANT:

    This is NOT yet a sweep/block detector.

    A chain snapshot alone cannot prove that
    institutional sweeps occurred.

    Real unusual-options-flow detection will be
    added later from option trades / streaming data.
    """

    OCC_PATTERN = re.compile(
        r"^([A-Z0-9]{1,6})"
        r"(\d{6})"
        r"([CP])"
        r"(\d{8})$"
    )

    def __init__(
        self,
        min_dte: int = 7,
        max_dte: int = 35,
        strike_range_pct: float = 7.5,
        max_spread_pct: float = 30.0,
    ) -> None:

        if min_dte < 0:
            raise ValueError(
                "min_dte cannot be negative."
            )

        if max_dte <= min_dte:
            raise ValueError(
                "max_dte must be greater than min_dte."
            )

        if strike_range_pct <= 0:
            raise ValueError(
                "strike_range_pct must be positive."
            )

        self.min_dte = min_dte
        self.max_dte = max_dte

        self.strike_range_pct = (
            strike_range_pct
        )

        self.max_spread_pct = (
            max_spread_pct
        )

        if not settings.ALPACA_API_KEY:
            raise RuntimeError(
                "ALPACA_API_KEY is missing."
            )

        if not settings.ALPACA_SECRET_KEY:
            raise RuntimeError(
                "ALPACA_SECRET_KEY is missing."
            )

        self.data_client = (
            OptionHistoricalDataClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
            )
        )

        # Paper connection only.
        self.trading_client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=True,
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        normalized = str(
            symbol or ""
        ).strip().upper()

        if not normalized:
            raise ValueError(
                "Symbol cannot be empty."
            )

        return normalized

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:

        try:
            number = float(value)

            if not math.isfinite(
                number
            ):
                return None

            return number

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int:

        try:
            number = int(
                float(value)
            )

            return max(
                number,
                0,
            )

        except (
            TypeError,
            ValueError,
        ):
            return 0

    @staticmethod
    def _enum_value(
        value: Any,
    ) -> str:

        raw = getattr(
            value,
            "value",
            value,
        )

        return str(
            raw or ""
        ).lower()

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

    # ========================================================
    # OCC SYMBOL FALLBACK
    # ========================================================

    def _parse_occ_symbol(
        self,
        option_symbol: str,
    ) -> Optional[
        tuple[str, date, float]
    ]:

        match = self.OCC_PATTERN.match(
            option_symbol
        )

        if not match:
            return None

        _, expiration_raw, cp, strike_raw = (
            match.groups()
        )

        expiration = datetime.strptime(
            expiration_raw,
            "%y%m%d",
        ).date()

        strike = (
            int(strike_raw)
            / 1000.0
        )

        option_type = (
            "CALL"
            if cp == "C"
            else "PUT"
        )

        return (
            option_type,
            expiration,
            strike,
        )

    # ========================================================
    # CONTRACT METADATA
    # ========================================================

    def _get_contract_metadata(
        self,
        symbol: str,
        stock_price: float,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:

        lower_strike = (
            stock_price
            * (
                1.0
                - self.strike_range_pct
                / 100.0
            )
        )

        upper_strike = (
            stock_price
            * (
                1.0
                + self.strike_range_pct
                / 100.0
            )
        )

        request = GetOptionContractsRequest(
            underlying_symbols=[
                symbol
            ],

            expiration_date_gte=(
                start_date
            ),

            expiration_date_lte=(
                end_date
            ),

            strike_price_gte=str(
                round(
                    lower_strike,
                    2,
                )
            ),

            strike_price_lte=str(
                round(
                    upper_strike,
                    2,
                )
            ),

            limit=10000,
        )

        try:
            response = (
                self.trading_client
                .get_option_contracts(
                    request
                )
            )

        except Exception:

            logger.exception(
                "Unable to retrieve option "
                "contract metadata | symbol=%s",
                symbol,
            )

            return {}

        contracts = getattr(
            response,
            "option_contracts",
            [],
        ) or []

        return {
            str(
                getattr(
                    contract,
                    "symbol",
                    "",
                )
            ): contract
            for contract in contracts
            if getattr(
                contract,
                "symbol",
                None,
            )
        }

    # ========================================================
    # CONTRACT QUALITY
    # ========================================================

    def _calculate_quality_score(
        self,
        *,
        spread_pct: Optional[float],
        delta: Optional[float],
        distance_pct: float,
        open_interest: int,
    ) -> float:

        # ----------------------------------------------------
        # BID / ASK QUALITY
        # ----------------------------------------------------

        spread_component = 0.0

        if (
            spread_pct is not None
            and spread_pct >= 0
        ):
            spread_component = max(
                0.0,
                1.0
                - (
                    spread_pct
                    / self.max_spread_pct
                ),
            )

        # ----------------------------------------------------
        # DELTA QUALITY
        # Prefer approximately 0.30 - 0.70 delta.
        # ----------------------------------------------------

        delta_component = 0.0

        if delta is not None:

            absolute_delta = abs(
                delta
            )

            delta_component = max(
                0.0,
                1.0
                - (
                    abs(
                        absolute_delta
                        - 0.50
                    )
                    / 0.50
                ),
            )

        # ----------------------------------------------------
        # STRIKE PROXIMITY
        # ----------------------------------------------------

        distance_component = max(
            0.0,
            1.0
            - (
                distance_pct
                / self.strike_range_pct
            ),
        )

        # ----------------------------------------------------
        # OPEN INTEREST
        # logarithmic normalization
        # ----------------------------------------------------

        oi_component = min(
            math.log10(
                open_interest + 1
            )
            / 4.0,
            1.0,
        )

        quality = (
            spread_component * 35.0
            + delta_component * 25.0
            + distance_component * 20.0
            + oi_component * 20.0
        )

        return round(
            self._clamp(
                quality
            ),
            2,
        )

    # ========================================================
    # BUILD CONTRACT
    # ========================================================

    def _build_contract_snapshot(
        self,
        option_symbol: str,
        snapshot: Any,
        metadata: Optional[Any],
        underlying_price: float,
        today: date,
    ) -> Optional[
        OptionContractSnapshot
    ]:

        option_type = None
        expiration = None
        strike = None
        open_interest = 0

        # ----------------------------------------------------
        # PRIMARY METADATA
        # ----------------------------------------------------

        if metadata is not None:

            option_type_raw = (
                self._enum_value(
                    getattr(
                        metadata,
                        "type",
                        None,
                    )
                )
            )

            if option_type_raw == "call":
                option_type = "CALL"

            elif option_type_raw == "put":
                option_type = "PUT"

            expiration = getattr(
                metadata,
                "expiration_date",
                None,
            )

            strike = self._safe_float(
                getattr(
                    metadata,
                    "strike_price",
                    None,
                )
            )

            open_interest = (
                self._safe_int(
                    getattr(
                        metadata,
                        "open_interest",
                        None,
                    )
                )
            )

        # ----------------------------------------------------
        # OCC SYMBOL FALLBACK
        # ----------------------------------------------------

        if (
            option_type is None
            or expiration is None
            or strike is None
        ):
            parsed = (
                self._parse_occ_symbol(
                    option_symbol
                )
            )

            if parsed is None:
                return None

            (
                parsed_type,
                parsed_expiration,
                parsed_strike,
            ) = parsed

            option_type = (
                option_type
                or parsed_type
            )

            expiration = (
                expiration
                or parsed_expiration
            )

            strike = (
                strike
                if strike is not None
                else parsed_strike
            )

        if isinstance(
            expiration,
            datetime,
        ):
            expiration = (
                expiration.date()
            )

        if not isinstance(
            expiration,
            date,
        ):
            return None

        dte = (
            expiration
            - today
        ).days

        if (
            dte < self.min_dte
            or dte > self.max_dte
        ):
            return None

        # ----------------------------------------------------
        # QUOTE
        # ----------------------------------------------------

        quote = getattr(
            snapshot,
            "latest_quote",
            None,
        )

        bid = self._safe_float(
            getattr(
                quote,
                "bid_price",
                None,
            )
        )

        ask = self._safe_float(
            getattr(
                quote,
                "ask_price",
                None,
            )
        )

        bid_size = self._safe_float(
            getattr(
                quote,
                "bid_size",
                None,
            )
        )

        ask_size = self._safe_float(
            getattr(
                quote,
                "ask_size",
                None,
            )
        )

        mid = None
        spread = None
        spread_pct = None

        if (
            bid is not None
            and ask is not None
            and bid >= 0
            and ask > 0
            and ask >= bid
        ):
            mid = (
                bid + ask
            ) / 2.0

            spread = (
                ask - bid
            )

            if mid > 0:

                spread_pct = (
                    spread
                    / mid
                ) * 100.0

        # ----------------------------------------------------
        # TRADE
        # ----------------------------------------------------

        latest_trade = getattr(
            snapshot,
            "latest_trade",
            None,
        )

        latest_trade_price = (
            self._safe_float(
                getattr(
                    latest_trade,
                    "price",
                    None,
                )
            )
        )

        latest_trade_size = (
            self._safe_float(
                getattr(
                    latest_trade,
                    "size",
                    None,
                )
            )
        )

        # ----------------------------------------------------
        # GREEKS
        # ----------------------------------------------------

        greeks = getattr(
            snapshot,
            "greeks",
            None,
        )

        delta = self._safe_float(
            getattr(
                greeks,
                "delta",
                None,
            )
        )

        gamma = self._safe_float(
            getattr(
                greeks,
                "gamma",
                None,
            )
        )

        theta = self._safe_float(
            getattr(
                greeks,
                "theta",
                None,
            )
        )

        vega = self._safe_float(
            getattr(
                greeks,
                "vega",
                None,
            )
        )

        iv = self._safe_float(
            getattr(
                snapshot,
                "implied_volatility",
                None,
            )
        )

        # ----------------------------------------------------
        # STRIKE DISTANCE
        # ----------------------------------------------------

        distance_pct = (
            abs(
                strike
                - underlying_price
            )
            / underlying_price
            * 100.0
        )

        quality_score = (
            self._calculate_quality_score(
                spread_pct=spread_pct,
                delta=delta,
                distance_pct=distance_pct,
                open_interest=open_interest,
            )
        )

        # ----------------------------------------------------
        # DIRECTIONAL WEIGHT
        #
        # This is positioning / chain bias,
        # NOT confirmed order flow.
        # ----------------------------------------------------

        absolute_delta = (
            abs(delta)
            if delta is not None
            else 0.50
        )

        oi_weight = max(
            open_interest,
            1,
        )

        directional_weight = (
            oi_weight
            * max(
                absolute_delta,
                0.10,
            )
            * (
                quality_score
                / 100.0
            )
        )

        return OptionContractSnapshot(
            symbol=option_symbol,

            option_type=option_type,

            expiration_date=expiration,

            strike_price=float(
                strike
            ),

            dte=dte,

            bid=bid,
            ask=ask,
            mid=mid,

            spread=spread,
            spread_pct=spread_pct,

            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,

            implied_volatility=iv,

            open_interest=open_interest,

            bid_size=bid_size,
            ask_size=ask_size,

            latest_trade_price=(
                latest_trade_price
            ),

            latest_trade_size=(
                latest_trade_size
            ),

            distance_from_stock_pct=(
                round(
                    distance_pct,
                    4,
                )
            ),

            quality_score=(
                quality_score
            ),

            directional_weight=round(
                directional_weight,
                4,
            ),
        )

    # ========================================================
    # MAIN ANALYSIS
    # ========================================================

    def analyze(
        self,
        symbol: str,
    ) -> OptionsFlowAnalysis:

        symbol = self._normalize_symbol(
            symbol
        )

        try:
            underlying_price = (
                get_market_data()
                .get_last_price(
                    symbol
                )
            )

        except Exception as exc:

            return OptionsFlowAnalysis(
                symbol=symbol,
                available=False,
                score=None,
                bias="UNAVAILABLE",
                underlying_price=None,
                contract_count=0,
                usable_contract_count=0,
                call_count=0,
                put_count=0,
                call_open_interest=0,
                put_open_interest=0,
                put_call_oi_ratio=None,
                call_strength=0.0,
                put_strength=0.0,
                warnings=[
                    f"Underlying price unavailable: {exc}"
                ],
            )

        today = datetime.now(
            NEW_YORK_TZ
        ).date()

        start_date = (
            today
            + timedelta(
                days=self.min_dte
            )
        )

        end_date = (
            today
            + timedelta(
                days=self.max_dte
            )
        )

        lower_strike = (
            underlying_price
            * (
                1.0
                - self.strike_range_pct
                / 100.0
            )
        )

        upper_strike = (
            underlying_price
            * (
                1.0
                + self.strike_range_pct
                / 100.0
            )
        )

        chain_request = OptionChainRequest(
            underlying_symbol=symbol,

            feed=(
                OptionsFeed.INDICATIVE
            ),

            strike_price_gte=(
                lower_strike
            ),

            strike_price_lte=(
                upper_strike
            ),

            expiration_date_gte=(
                start_date
            ),

            expiration_date_lte=(
                end_date
            ),
        )

        try:
            chain = (
                self.data_client
                .get_option_chain(
                    chain_request
                )
            )

        except Exception as exc:

            logger.exception(
                "Option chain request failed | symbol=%s",
                symbol,
            )

            return OptionsFlowAnalysis(
                symbol=symbol,
                available=False,
                score=None,
                bias="UNAVAILABLE",
                underlying_price=(
                    underlying_price
                ),
                contract_count=0,
                usable_contract_count=0,
                call_count=0,
                put_count=0,
                call_open_interest=0,
                put_open_interest=0,
                put_call_oi_ratio=None,
                call_strength=0.0,
                put_strength=0.0,
                warnings=[
                    f"Option chain unavailable: {exc}"
                ],
            )

        contract_count = len(
            chain
        )

        metadata_map = (
            self._get_contract_metadata(
                symbol=symbol,
                stock_price=(
                    underlying_price
                ),
                start_date=start_date,
                end_date=end_date,
            )
        )

        contracts: list[
            OptionContractSnapshot
        ] = []

        for (
            option_symbol,
            snapshot,
        ) in chain.items():

            contract = (
                self._build_contract_snapshot(
                    option_symbol=(
                        option_symbol
                    ),
                    snapshot=snapshot,
                    metadata=(
                        metadata_map.get(
                            option_symbol
                        )
                    ),
                    underlying_price=(
                        underlying_price
                    ),
                    today=today,
                )
            )

            if contract is None:
                continue

            # Ignore extremely poor / invalid quotes.
            if (
                contract.spread_pct
                is not None
                and contract.spread_pct
                > self.max_spread_pct
            ):
                continue

            contracts.append(
                contract
            )

        calls = [
            contract
            for contract in contracts
            if contract.option_type
            == "CALL"
        ]

        puts = [
            contract
            for contract in contracts
            if contract.option_type
            == "PUT"
        ]

        call_open_interest = sum(
            contract.open_interest
            for contract in calls
        )

        put_open_interest = sum(
            contract.open_interest
            for contract in puts
        )

        put_call_oi_ratio = None

        if call_open_interest > 0:

            put_call_oi_ratio = (
                put_open_interest
                / call_open_interest
            )

        call_strength = sum(
            contract.directional_weight
            for contract in calls
        )

        put_strength = sum(
            contract.directional_weight
            for contract in puts
        )

        total_strength = (
            call_strength
            + put_strength
        )

        score = None
        bias = "NEUTRAL"

        if total_strength > 0:

            directional_balance = (
                call_strength
                - put_strength
            ) / total_strength

            score = (
                50.0
                + directional_balance
                * 50.0
            )

            score = round(
                self._clamp(
                    score
                ),
                2,
            )

            if score >= 60:
                bias = "BULLISH"

            elif score <= 40:
                bias = "BEARISH"

            else:
                bias = "NEUTRAL"

        top_calls = sorted(
            calls,
            key=lambda item: (
                item.directional_weight,
                item.quality_score,
            ),
            reverse=True,
        )[:5]

        top_puts = sorted(
            puts,
            key=lambda item: (
                item.directional_weight,
                item.quality_score,
            ),
            reverse=True,
        )[:5]

        warnings: list[str] = []

        if not contracts:
            warnings.append(
                "No usable option contracts found."
            )

        return OptionsFlowAnalysis(
            symbol=symbol,

            available=True,

            score=score,

            bias=bias,

            underlying_price=round(
                underlying_price,
                4,
            ),

            contract_count=(
                contract_count
            ),

            usable_contract_count=len(
                contracts
            ),

            call_count=len(
                calls
            ),

            put_count=len(
                puts
            ),

            call_open_interest=(
                call_open_interest
            ),

            put_open_interest=(
                put_open_interest
            ),

            put_call_oi_ratio=(
                round(
                    put_call_oi_ratio,
                    4,
                )
                if put_call_oi_ratio
                is not None
                else None
            ),

            call_strength=round(
                call_strength,
                2,
            ),

            put_strength=round(
                put_strength,
                2,
            ),

            top_calls=top_calls,

            top_puts=top_puts,

            warnings=warnings,

            metadata={
                "engine": (
                    "JALWE_V4_OPTIONS"
                ),

                "analysis_mode": (
                    "CHAIN_BIAS_OI"
                ),

                "feed": (
                    "INDICATIVE"
                ),

                "min_dte": (
                    self.min_dte
                ),

                "max_dte": (
                    self.max_dte
                ),

                "strike_range_pct": (
                    self.strike_range_pct
                ),

                "institutional_sweeps_confirmed": (
                    False
                ),
            },
        )

    # ========================================================
    # FEATURE INTEGRATION
    # ========================================================

    def apply_to_features(
        self,
        features: FeatureSnapshot,
    ) -> OptionsFlowAnalysis:

        analysis = self.analyze(
            features.symbol
        )

        features.options_flow_score = (
            analysis.score
        )

        features.metadata[
            "options_available"
        ] = analysis.available

        features.metadata[
            "options_bias"
        ] = analysis.bias

        features.metadata[
            "options_contract_count"
        ] = analysis.contract_count

        features.metadata[
            "options_usable_contracts"
        ] = analysis.usable_contract_count

        features.metadata[
            "put_call_oi_ratio"
        ] = analysis.put_call_oi_ratio

        features.metadata[
            "options_analysis_mode"
        ] = "CHAIN_BIAS_OI"

        return analysis


# ============================================================
# LAZY SINGLETON
# ============================================================

options_flow_engine: Optional[
    OptionsFlowEngine
] = None


def get_options_flow_engine(
) -> OptionsFlowEngine:

    global options_flow_engine

    if options_flow_engine is None:

        options_flow_engine = (
            OptionsFlowEngine()
        )

    return options_flow_engine