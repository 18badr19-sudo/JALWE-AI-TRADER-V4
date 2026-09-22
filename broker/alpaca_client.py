from __future__ import annotations

import logging
from typing import Any, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
)

from core.config import settings

logger = logging.getLogger(__name__)


class AlpacaClient:
    """
    Central Alpaca broker client for JALWE AI TRADER V4.

    PAPER TRADING ONLY.
    All broker operations should pass through this class.
    """

    def __init__(self) -> None:
        self._validate_configuration()

        self.client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=True,
        )

        logger.info("Alpaca PAPER trading client initialized.")

    def _validate_configuration(self) -> None:
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
                "live trading is disabled."
            )

    def get_account(self) -> Any:
        """
        Return the Alpaca paper account.
        """
        return self.client.get_account()

    def get_account_snapshot(self) -> dict[str, float]:
        """
        Return important account values.
        Never substitutes fake balances when Alpaca is unavailable.
        """
        account = self.get_account()

        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
        }

    def get_all_positions(self) -> list[Any]:
        """
        Return all currently open broker positions.
        """
        return list(
            self.client.get_all_positions()
        )

    def get_position(
        self,
        symbol: str,
    ) -> Optional[Any]:
        """
        Return one position or None if it does not exist.
        """
        try:
            return self.client.get_open_position(
                symbol.upper()
            )
        except Exception:
            return None

    def submit_market_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
    ) -> Any:
        """
        Submit a market order to the PAPER account.
        """

        if quantity <= 0:
            raise ValueError(
                "Order quantity must be greater than zero."
            )

        normalized_side = side.upper()

        if normalized_side == "BUY":
            order_side = OrderSide.BUY

        elif normalized_side == "SELL":
            order_side = OrderSide.SELL

        else:
            raise ValueError(
                f"Unsupported order side: {side}"
            )

        order_request = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )

        order = self.client.submit_order(
            order_data=order_request
        )

        logger.info(
            "Submitted PAPER order: %s %s x%s | order_id=%s",
            normalized_side,
            symbol.upper(),
            quantity,
            getattr(order, "id", None),
        )

        return order

    def get_order(
        self,
        order_id: str,
    ) -> Any:
        """
        Retrieve an order directly from Alpaca.
        """
        return self.client.get_order_by_id(
            order_id
        )

    def get_orders(self) -> list[Any]:
        """
        Retrieve broker orders.
        """
        request = GetOrdersRequest()

        return list(
            self.client.get_orders(
                filter=request
            )
        )

    def cancel_order(
        self,
        order_id: str,
    ) -> None:
        """
        Cancel an existing broker order.
        """
        self.client.cancel_order_by_id(
            order_id
        )

    def close_position(
        self,
        symbol: str,
    ) -> Any:
        """
        Close an entire position through Alpaca PAPER.
        """
        return self.client.close_position(
            symbol.upper()
        )

    def verify_connection(self) -> dict[str, Any]:
        """
        Test the PAPER connection and return safe account information.
        """
        account = self.get_account()

        return {
            "connected": True,
            "paper": True,
            "account_status": str(account.status),
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
        }


alpaca_client: Optional[AlpacaClient] = None


def get_alpaca_client() -> AlpacaClient:
    """
    Lazy singleton.

    The connection is created only when another module
    actually requests broker access.
    """
    global alpaca_client

    if alpaca_client is None:
        alpaca_client = AlpacaClient()

    return alpaca_client
