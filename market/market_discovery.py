from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from broker.alpaca_client import AlpacaClient


logger = logging.getLogger(__name__)


@dataclass
class MarketCandidate:
    symbol: str
    exchange: str = ""
    tradable: bool = True
    shortable: bool = False
    fractionable: bool = False


class MarketDiscovery:
    """
    JALWE V4 - Market Discovery Engine

    مسؤول عن بناء قائمة الأسهم التي يسمح للنظام بتحليلها.

    هذه الطبقة لا تعطي إشارة شراء أو بيع.
    وظيفتها فقط اكتشاف الأسهم الصالحة للانتقال
    إلى مراحل التحليل التالية.
    """

    ALLOWED_EXCHANGES = {
        "NASDAQ",
        "NYSE",
        "AMEX",
        "ARCA",
        "BATS",
    }

    def __init__(
        self,
        broker: Optional[AlpacaClient] = None,
        max_symbols: Optional[int] = None,
    ):
        self.broker = broker or AlpacaClient()
        self.max_symbols = max_symbols

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        return str(symbol or "").strip().upper()

    @staticmethod
    def _is_common_stock_symbol(symbol: str) -> bool:
        """
        فلتر أولي بسيط لاستبعاد الرموز غير المناسبة.

        لا نعتمد عليه وحده لتحديد نوع الأصل.
        """
        if not symbol:
            return False

        if len(symbol) > 10:
            return False

        blocked_chars = {"/", "^", "="}

        if any(char in symbol for char in blocked_chars):
            return False

        return True

    def _asset_to_candidate(self, asset) -> Optional[MarketCandidate]:
        symbol = self._clean_symbol(getattr(asset, "symbol", ""))

        if not self._is_common_stock_symbol(symbol):
            return None

        status = str(getattr(asset, "status", "")).lower()

        if status and "active" not in status:
            return None

        tradable = bool(getattr(asset, "tradable", False))

        if not tradable:
            return None

        exchange = str(getattr(asset, "exchange", "")).upper()

        if exchange and exchange not in self.ALLOWED_EXCHANGES:
            return None

        return MarketCandidate(
            symbol=symbol,
            exchange=exchange,
            tradable=tradable,
            shortable=bool(getattr(asset, "shortable", False)),
            fractionable=bool(getattr(asset, "fractionable", False)),
        )

    def discover_candidates(self) -> List[MarketCandidate]:
        """
        يجلب الأصول من الوسيط ثم ينظفها ويعيد قائمة المرشحين.

        مهم:
        لا توجد قائمة أسهم ثابتة كخطة بديلة.
        إذا فشل مصدر البيانات نعيد قائمة فارغة حتى لا يعمل
        النظام على بيانات غير مؤكدة.
        """
        try:
            assets = self.broker.list_assets()
        except Exception:
            logger.exception("Market discovery failed while loading assets.")
            return []

        candidates: List[MarketCandidate] = []

        for asset in assets:
            candidate = self._asset_to_candidate(asset)

            if candidate is None:
                continue

            candidates.append(candidate)

        candidates.sort(key=lambda item: item.symbol)

        if self.max_symbols is not None:
            candidates = candidates[: self.max_symbols]

        logger.info(
            "Market discovery completed: %s tradable symbols found.",
            len(candidates),
        )

        return candidates

    def discover_symbols(self) -> List[str]:
        return [
            candidate.symbol
            for candidate in self.discover_candidates()
        ]
