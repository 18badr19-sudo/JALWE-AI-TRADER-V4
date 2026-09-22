from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from broker.alpaca_client import (
    AlpacaClient,
    get_alpaca_client,
)


logger = logging.getLogger(__name__)


class MarketDiscoveryError(RuntimeError):
    """
    Raised when JALWE cannot safely build
    the tradable US equity universe.
    """


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    exchange: str

    tradable: bool = True
    shortable: bool = False
    fractionable: bool = False
    marginable: bool = False
    easy_to_borrow: bool = False


class MarketDiscovery:
    """
    JALWE AI TRADER V4
    Market Discovery Engine.

    مسؤول عن اكتشاف الأسهم الأمريكية
    القابلة للتداول من Alpaca.

    هذه الطبقة:
    - لا تحلل السهم.
    - لا تعطي BUY / SELL.
    - لا تنفذ صفقات.
    - لا تستخدم قائمة ثابتة كبديل.
    """

    ALLOWED_EXCHANGES = {
        "NASDAQ",
        "NYSE",
        "AMEX",
        "ARCA",
        "BATS",
    }

    BLOCKED_SYMBOL_CHARACTERS = {
        "/",
        "^",
        "=",
        " ",
    }

    def __init__(
        self,
        broker: Optional[AlpacaClient] = None,
        max_symbols: Optional[int] = None,
    ) -> None:

        self.broker = broker or get_alpaca_client()

        if (
            max_symbols is not None
            and max_symbols <= 0
        ):
            raise ValueError(
                "max_symbols must be greater than zero."
            )

        self.max_symbols = max_symbols

    # ========================================================
    # NORMALIZATION
    # ========================================================

    @staticmethod
    def _enum_value(value) -> str:
        """
        Safely normalize Alpaca Enum/string values.

        Examples:
        AssetStatus.ACTIVE -> ACTIVE
        AssetExchange.NASDAQ -> NASDAQ
        """

        if value is None:
            return ""

        raw_value = getattr(
            value,
            "value",
            value,
        )

        return str(
            raw_value
        ).strip().upper()

    @staticmethod
    def _clean_symbol(
        symbol: str,
    ) -> str:

        return str(
            symbol or ""
        ).strip().upper()

    def _symbol_is_valid(
        self,
        symbol: str,
    ) -> bool:
        """
        Basic symbol sanity validation.

        This is not used to decide trade quality.
        """

        if not symbol:
            return False

        if len(symbol) > 12:
            return False

        if any(
            character in symbol
            for character
            in self.BLOCKED_SYMBOL_CHARACTERS
        ):
            return False

        return True

    # ========================================================
    # ASSET FILTERING
    # ========================================================

    def _asset_to_candidate(
        self,
        asset,
    ) -> Optional[MarketCandidate]:

        symbol = self._clean_symbol(
            getattr(
                asset,
                "symbol",
                "",
            )
        )

        if not self._symbol_is_valid(symbol):
            return None

        # Exact status comparison.
        # This prevents INACTIVE from accidentally
        # matching ACTIVE.
        status = self._enum_value(
            getattr(
                asset,
                "status",
                None,
            )
        )

        if status != "ACTIVE":
            return None

        tradable = bool(
            getattr(
                asset,
                "tradable",
                False,
            )
        )

        if not tradable:
            return None

        exchange = self._enum_value(
            getattr(
                asset,
                "exchange",
                None,
            )
        )

        if exchange not in self.ALLOWED_EXCHANGES:
            return None

        return MarketCandidate(
            symbol=symbol,
            exchange=exchange,
            tradable=True,

            shortable=bool(
                getattr(
                    asset,
                    "shortable",
                    False,
                )
            ),

            fractionable=bool(
                getattr(
                    asset,
                    "fractionable",
                    False,
                )
            ),

            marginable=bool(
                getattr(
                    asset,
                    "marginable",
                    False,
                )
            ),

            easy_to_borrow=bool(
                getattr(
                    asset,
                    "easy_to_borrow",
                    False,
                )
            ),
        )

    # ========================================================
    # DISCOVERY
    # ========================================================

    def discover_candidates(
        self,
    ) -> list[MarketCandidate]:
        """
        Build the clean tradable stock universe.

        Important:
        If Alpaca fails, JALWE raises an error instead
        of using a fake or hardcoded fallback universe.
        """

        try:
            assets = self.broker.list_assets()

        except Exception as exc:
            logger.exception(
                "Unable to load market universe from Alpaca."
            )

            raise MarketDiscoveryError(
                "Market discovery unavailable."
            ) from exc

        # Dictionary prevents duplicate symbols.
        candidates_by_symbol: dict[
            str,
            MarketCandidate,
        ] = {}

        for asset in assets:

            candidate = self._asset_to_candidate(
                asset
            )

            if candidate is None:
                continue

            candidates_by_symbol[
                candidate.symbol
            ] = candidate

        candidates = sorted(
            candidates_by_symbol.values(),
            key=lambda item: item.symbol,
        )

        if self.max_symbols is not None:
            candidates = candidates[
                : self.max_symbols
            ]

        logger.info(
            "Market discovery completed | "
            "tradable_symbols=%s",
            len(candidates),
        )

        return candidates

    def discover_symbols(
        self,
    ) -> list[str]:
        """
        Return only ticker symbols.
        """

        return [
            candidate.symbol
            for candidate
            in self.discover_candidates()
        ]
