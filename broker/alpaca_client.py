from __future__ import annotations

import logging
from typing import Any, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    OrderSide,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
    MarketOrderRequest,
)

from core.config import settings


logger = logging.getLogger(__name__)


class AlpacaClient:
    """
    Central Alpaca broker client for JALWE AI TRADER V4.

    PAPER TRADING ONLY.

    جميع عمليات الوسيط تمر من خلال هذا الملف.
    لا توجد مفاتيح API مكتوبة داخل الكود.
    """

    def __init__(self) -> None:
        self._validate_configuration()

        self.client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=True,
        )

        logger.info(
            "JALWE V4 Alpaca PAPER client initialized."
        )

    # ========================================================
    # SAFETY
    # ========================================================

    def _validate_configuration(self) -> None:
        """
        Prevent accidental live trading and verify credentials exist.
        """

        if not settings.ALPACA_API_KEY:
            raise RuntimeError(
                "ALPACA_API_KEY is missing."
            )

        if not settings.ALPACA_SECRET_KEY:
            raise RuntimeError(
                "ALPACA_SECRET_KEY is missing."
            )

        if not settings.PAPER_TRADING:
            raise RuntimeError(
                "JALWE V4 safety lock: "
                "PAPER_TRADING must remain enabled."
            )

        if settings.ALLOW_LIVE_TRADING:
            raise RuntimeError(
                "JALWE V4 safety lock: "
                "live trading is disabled."
            )

        if (
            "paper-api.alpaca.markets"
            not in settings.ALPACA_BASE_URL.lower()
        ):
            raise RuntimeError(
                "ALPACA_BASE_URL must point to "
                "the Alpaca Paper Trading API."
            )

    # ========================================================
    # ACCOUNT
    # ========================================================

    def get_account(self) -> Any:
        """
        Return the current Alpaca paper account.
        """

        return self.client.get_account()

    def get_account_snapshot(self) -> dict[str, float]:
        """
        Return important account values.

        No fake balance fallback is allowed.
        """

        account = self.get_account()

        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(
                account.buying_power
            ),
            "portfolio_value": float(
                account.portfolio_value
            ),
        }

    def get_clock(self) -> Any:
        """
        Return Alpaca market clock.
        """

        return self.client.get_clock()

    def market_is_open(self) -> bool:
        """
        Return True only when Alpaca says
        the market is currently open.
        """

        clock = self.get_clock()

        return bool(clock.is_open)

    # ========================================================
    # ASSETS / MARKET UNIVERSE
    # ========================================================

    def list_assets(self) -> list[Any]:
        """
        Return the US equity universe available through Alpaca.
        """

        request = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY
        )

        assets = self.client.get_all_assets(
            request
        )

        return list(assets)

    def get_asset(
        self,
        symbol: str,
    ) -> Any:
        """
        Return broker information about one symbol.
        """

        symbol = self._normalize_symbol(symbol)

        return self.client.get_asset(symbol)

    # ========================================================
    # POSITIONS
    # ========================================================

    def get_all_positions(self) -> list[Any]:
        """
        Return all currently open positions.
        """

        return list(
            self.client.get_all_positions()
        )

    def get_position(
        self,
        symbol: str,
    ) -> Optional[Any]:
        """
        Find a position without hiding broker connection errors.

        Returns None only when the symbol is not present
        in the broker's returned positions.
        """

        symbol = self._normalize_symbol(symbol)

        positions = self.get_all_positions()

        for position in positions:
            position_symbol = str(
                getattr(
                    position,
                    "symbol",
                    "",
                )
            ).upper()

            if position_symbol == symbol:
                return position

        return None

    # ========================================================
    # ORDERS
    # ========================================================

    def submit_market_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        client_order_id: Optional[str] = None,
    ) -> Any:
        """
        Submit a market order to Alpaca PAPER.

        Accepted/submitted does NOT mean filled.
        Fill confirmation will be handled later
        by the reconciliation engine.
        """

        symbol = self._normalize_symbol(symbol)

        if quantity <= 0:
            raise ValueError(
                "Order quantity must be greater than zero."
            )

        normalized_side = side.strip().upper()

        if normalized_side == "BUY":
            order_side = OrderSide.BUY

        elif normalized_side == "SELL":
            order_side = OrderSide.SELL

        else:
            raise ValueError(
                f"Unsupported order side: {side}"
            )

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )

        order = self.client.submit_order(
            order_data=order_request
        )

        logger.info(
            "PAPER order submitted | "
            "symbol=%s side=%s qty=%s order_id=%s",
            symbol,
            normalized_side,
            quantity,
            getattr(order, "id", None),
        )

        return order

    def get_order(
        self,
        order_id: str,
    ) -> Any:
        """
        Retrieve one broker order by Alpaca order ID.
        """

        if not order_id:
            raise ValueError(
                "order_id cannot be empty."
            )

        return self.client.get_order_by_id(
            order_id
        )

    def get_orders(self) -> list[Any]:
        """
        Retrieve broker orders.
        """

        request = GetOrdersRequest()

        orders = self.client.get_orders(
            filter=request
        )

        return list(orders)

    def cancel_order(
        self,
        order_id: str,
    ) -> None:
        """
        Cancel an existing broker order.
        """

        if not order_id:
            raise ValueError(
                "order_id cannot be empty."
            )

        self.client.cancel_order_by_id(
            order_id
        )

    # ========================================================
    # POSITION EXIT
    # ========================================================

    def close_position(
        self,
        symbol: str,
    ) -> Any:
        """
        Close an entire PAPER position.

        TradeManager / RiskEngine will decide
        when this method is allowed to run.
        """

        symbol = self._normalize_symbol(symbol)

        return self.client.close_position(
            symbol
        )

    # ========================================================
    # CONNECTION / HEALTH
    # ========================================================

    def verify_connection(
        self,
    ) -> dict[str, Any]:
        """
        Verify the Alpaca PAPER connection.

        Does not expose API credentials.
        """

        account = self.get_account()
        clock = self.get_clock()

        return {
            "connected": True,
            "paper": True,
            "account_status": str(
                account.status
            ),
            "equity": float(
                account.equity
            ),
            "cash": float(
                account.cash
            ),
            "buying_power": float(
                account.buying_power
            ),
            "market_open": bool(
                clock.is_open
            ),
        }

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:
        """
        Normalize and validate a stock ticker.
        """

        normalized = str(
            symbol or ""
        ).strip().upper()

        if not normalized:
            raise ValueError(
                "Symbol cannot be empty."
            )

        return normalized


# ============================================================
# LAZY SINGLETON
# ============================================================

alpaca_client: Optional[AlpacaClient] = None


def get_alpaca_client() -> AlpacaClient:
    """
    Create the Alpaca connection only when needed.
    """

    global alpaca_client

    if alpaca_client is None:
        alpaca_client = AlpacaClient()

    return alpaca_client
