from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

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
    StopOrderRequest,
)

from core.config import settings


logger = logging.getLogger(__name__)


class AlpacaClient:
    """
    Central Alpaca broker client for JALWE AI TRADER V4.

    PAPER TRADING ONLY.

    جميع عمليات الوسيط تمر من خلال هذا الملف.

    Responsibilities:
    - Account information
    - Market clock
    - Assets
    - Positions
    - PAPER order submission
    - Order lookup by broker order ID
    - Order lookup by client_order_id
    - Order cancellation
    - Position closing

    Important:
    - No API keys are hardcoded here.
    - Accepted/submitted does NOT mean filled.
    - ReconciliationEngine remains responsible
      for confirming broker execution state.
    """

    def __init__(
        self,
    ) -> None:

        self._validate_configuration()

        self.client = TradingClient(
            api_key=(
                settings.ALPACA_API_KEY
            ),
            secret_key=(
                settings.ALPACA_SECRET_KEY
            ),
            paper=True,
        )

        logger.info(
            "JALWE V4 Alpaca PAPER "
            "client initialized."
        )

    # ========================================================
    # SAFETY
    # ========================================================

    def _validate_configuration(
        self,
    ) -> None:
        """
        Prevent accidental live trading
        and verify credentials exist.
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

        base_url = str(
            settings.ALPACA_BASE_URL
            or ""
        ).lower()

        if (
            "paper-api.alpaca.markets"
            not in base_url
        ):

            raise RuntimeError(
                "ALPACA_BASE_URL must point "
                "to the Alpaca Paper Trading API."
            )

    # ========================================================
    # ACCOUNT
    # ========================================================

    def get_account(
        self,
    ) -> Any:
        """
        Return the current Alpaca paper account.
        """

        return self.client.get_account()

    def get_account_snapshot(
        self,
    ) -> dict[str, float]:
        """
        Return important account values.

        No fake balance fallback is allowed.
        """

        account = (
            self.get_account()
        )

        last_equity_raw = getattr(
            account,
            "last_equity",
            None,
        )

        return {

            "equity": float(
                account.equity
            ),

            "last_equity": (
                float(last_equity_raw)
                if last_equity_raw is not None
                else float(account.equity)
            ),

            "cash": float(
                account.cash
            ),

            "buying_power": float(
                account.buying_power
            ),

            "portfolio_value": float(
                account.portfolio_value
            ),
        }

    # ========================================================
    # CASH TRANSFER ACTIVITIES
    # ========================================================

    def get_cash_transfer_activities(
        self,
        *,
        after: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Read Alpaca PAPER cash deposits/withdrawals.

        CSD = cash deposit / inbound cash.
        CSW = cash withdrawal / outbound cash.

        This is read-only and is used only so the risk engine
        can distinguish capital flows from trading PnL.
        """

        page_size = max(
            1,
            min(
                int(limit),
                100,
            ),
        )

        params = {
            "activity_types": "CSD,CSW",
            "direction": "asc",
            "page_size": page_size,
        }

        if after:
            params["after"] = str(after)

        if until:
            params["until"] = str(until)

        query = urllib.parse.urlencode(
            params
        )

        url = (
            str(
                settings.ALPACA_BASE_URL
            ).rstrip("/")
            + "/v2/account/activities?"
            + query
        )

        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": (
                    settings.ALPACA_API_KEY
                ),
                "APCA-API-SECRET-KEY": (
                    settings.ALPACA_SECRET_KEY
                ),
                "Accept": "application/json",
            },
            method="GET",
        )

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:
            payload = json.loads(
                response.read().decode(
                    "utf-8",
                    errors="replace",
                )
            )

        if not isinstance(
            payload,
            list,
        ):
            raise RuntimeError(
                "Unexpected Alpaca activity response."
            )

        result: list[dict[str, Any]] = []

        for item in payload:
            if not isinstance(
                item,
                dict,
            ):
                continue

            activity_type = str(
                item.get(
                    "activity_type",
                    "",
                )
                or ""
            ).upper()

            if activity_type not in {
                "CSD",
                "CSW",
            }:
                continue

            result.append(
                item
            )

        return result

    # ========================================================
    # MARKET CLOCK
    # ========================================================

    def get_clock(
        self,
    ) -> Any:
        """
        Return Alpaca market clock.
        """

        return self.client.get_clock()

    def market_is_open(
        self,
    ) -> bool:
        """
        Return True only when Alpaca says
        the market is currently open.
        """

        clock = (
            self.get_clock()
        )

        return bool(
            clock.is_open
        )

    # ========================================================
    # ASSETS / MARKET UNIVERSE
    # ========================================================

    def list_assets(
        self,
    ) -> list[Any]:
        """
        Return the US equity universe
        available through Alpaca.
        """

        request = (
            GetAssetsRequest(
                asset_class=(
                    AssetClass.US_EQUITY
                )
            )
        )

        assets = (
            self.client
            .get_all_assets(
                request
            )
        )

        return list(
            assets
        )

    def get_asset(
        self,
        symbol: str,
    ) -> Any:
        """
        Return broker information
        about one symbol.
        """

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        return (
            self.client
            .get_asset(
                symbol
            )
        )

    # ========================================================
    # POSITIONS
    # ========================================================

    def get_all_positions(
        self,
    ) -> list[Any]:
        """
        Return all currently open positions.
        """

        return list(
            self.client
            .get_all_positions()
        )

    def get_position(
        self,
        symbol: str,
    ) -> Optional[Any]:
        """
        Find a position without hiding
        broker connection errors.

        Returns None only when the symbol
        is not present in the broker's
        returned positions.
        """

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        positions = (
            self.get_all_positions()
        )

        for position in positions:

            position_symbol = str(

                getattr(
                    position,
                    "symbol",
                    "",
                )

                or ""

            ).strip().upper()

            if (
                position_symbol
                == symbol
            ):

                return position

        return None

    # ========================================================
    # SUBMIT MARKET ORDER
    # ========================================================

    def submit_market_order(
        self,

        symbol: str,

        quantity: int,

        side: str,

        client_order_id: Optional[
            str
        ] = None,

    ) -> Any:
        """
        Submit a market order to Alpaca PAPER.

        Accepted/submitted does NOT mean filled.

        Fill confirmation is handled later
        by ReconciliationEngine.

        client_order_id is important for
        crash/restart recovery.
        """

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        quantity = int(
            quantity
        )

        if quantity <= 0:

            raise ValueError(
                "Order quantity must be "
                "greater than zero."
            )

        normalized_side = str(
            side or ""
        ).strip().upper()

        if normalized_side == "BUY":

            order_side = (
                OrderSide.BUY
            )

        elif normalized_side == "SELL":

            order_side = (
                OrderSide.SELL
            )

        else:

            raise ValueError(
                f"Unsupported order side: {side}"
            )

        normalized_client_order_id = None

        if client_order_id is not None:

            normalized_client_order_id = (
                self._normalize_client_order_id(
                    client_order_id
                )
            )

        order_request = (
            MarketOrderRequest(

                symbol=symbol,

                qty=quantity,

                side=order_side,

                time_in_force=(
                    TimeInForce.DAY
                ),

                client_order_id=(
                    normalized_client_order_id
                ),
            )
        )

        order = (
            self.client
            .submit_order(
                order_data=(
                    order_request
                )
            )
        )

        logger.info(

            "PAPER order submitted | "
            "symbol=%s side=%s qty=%s "
            "order_id=%s client_order_id=%s",

            symbol,

            normalized_side,

            quantity,

            getattr(
                order,
                "id",
                None,
            ),

            getattr(
                order,
                "client_order_id",
                normalized_client_order_id,
            ),
        )

        return order

    # ========================================================
    # BROKER-NATIVE PROTECTIVE STOP
    # ========================================================

    def submit_stop_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        client_order_id: Optional[str] = None,
    ) -> Any:
        """
        Submit a PAPER GTC SELL stop order.

        This is a broker-side safety layer for an already-filled
        long position. It is not an entry signal and cannot BUY.
        """

        symbol = self._normalize_symbol(
            symbol
        )

        quantity = int(
            quantity
        )

        stop_price = float(
            stop_price
        )

        if quantity <= 0:
            raise ValueError(
                "Stop quantity must be positive."
            )

        if stop_price <= 0:
            raise ValueError(
                "Stop price must be positive."
            )

        normalized_client_order_id = None

        if client_order_id is not None:
            normalized_client_order_id = (
                self._normalize_client_order_id(
                    client_order_id
                )
            )

        order_request = StopOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price,
            client_order_id=(
                normalized_client_order_id
            ),
        )

        order = self.client.submit_order(
            order_data=order_request
        )

        logger.info(
            "PAPER protective stop submitted | "
            "symbol=%s qty=%s stop=%s order_id=%s "
            "client_order_id=%s",
            symbol,
            quantity,
            stop_price,
            getattr(
                order,
                "id",
                None,
            ),
            getattr(
                order,
                "client_order_id",
                normalized_client_order_id,
            ),
        )

        return order

    # ========================================================
    # GET ORDER BY ALPACA ORDER ID
    # ========================================================

    def get_order(
        self,
        order_id: str,
    ) -> Any:
        """
        Retrieve one broker order
        by Alpaca broker order ID.

        Broker/API errors are intentionally
        allowed to propagate so RecoveryEngine
        can distinguish a failed lookup from
        an actual order state.
        """

        order_id = str(
            order_id or ""
        ).strip()

        if not order_id:

            raise ValueError(
                "order_id cannot be empty."
            )

        return (
            self.client
            .get_order_by_id(
                order_id
            )
        )

    # ========================================================
    # GET ORDER BY CLIENT ORDER ID
    # ========================================================

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> Any:
        """
        Retrieve one broker order using
        JALWE's client_order_id.

        This is critical for Pending Entry
        crash recovery.

        Example failure scenario:

            1. JALWE saves EntryIntent.
            2. JALWE submits BUY to Alpaca.
            3. Alpaca accepts the order.
            4. Computer loses power BEFORE
               broker_order_id is saved.
            5. JALWE restarts.
            6. RecoveryEngine still has the
               persisted client_order_id.
            7. This method locates the order
               at Alpaca without submitting
               another BUY.

        Broker/API errors are NOT converted
        to None here.

        RecoveryEngine decides how to handle
        not-found/network/error conditions.
        """

        client_order_id = (
            self._normalize_client_order_id(
                client_order_id
            )
        )

        order = (
            self.client
            .get_order_by_client_id(
                client_order_id
            )
        )

        logger.info(

            "Broker order retrieved by "
            "client_order_id | "
            "client_order_id=%s "
            "order_id=%s status=%s",

            client_order_id,

            getattr(
                order,
                "id",
                None,
            ),

            getattr(
                order,
                "status",
                None,
            ),
        )

        return order

    # ========================================================
    # GET ORDERS
    # ========================================================

    def get_orders(
        self,
    ) -> list[Any]:
        """
        Retrieve broker orders.

        Used by startup reconciliation to
        inspect unresolved/open broker orders.
        """

        request = (
            GetOrdersRequest()
        )

        orders = (
            self.client
            .get_orders(
                filter=request
            )
        )

        return list(
            orders
        )

    # ========================================================
    # CANCEL ORDER
    # ========================================================

    def cancel_order(
        self,
        order_id: str,
    ) -> None:
        """
        Cancel an existing broker order.
        """

        order_id = str(
            order_id or ""
        ).strip()

        if not order_id:

            raise ValueError(
                "order_id cannot be empty."
            )

        self.client.cancel_order_by_id(
            order_id
        )

        logger.info(
            "PAPER order cancellation "
            "requested | order_id=%s",
            order_id,
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

        TradeManager / RiskEngine decides
        when this method is allowed to run.
        """

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        return (
            self.client
            .close_position(
                symbol
            )
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

        account = (
            self.get_account()
        )

        clock = (
            self.get_clock()
        )

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
    # SYMBOL NORMALIZATION
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

    # ========================================================
    # CLIENT ORDER ID NORMALIZATION
    # ========================================================

    @staticmethod
    def _normalize_client_order_id(
        client_order_id: str,
    ) -> str:
        """
        Normalize and validate a client order ID.

        We preserve case because the value is
        an identifier, not a ticker.
        """

        normalized = str(
            client_order_id or ""
        ).strip()

        if not normalized:

            raise ValueError(
                "client_order_id "
                "cannot be empty."
            )

        return normalized


# ============================================================
# LAZY SINGLETON
# ============================================================

alpaca_client: Optional[
    AlpacaClient
] = None


def get_alpaca_client(
) -> AlpacaClient:
    """
    Create the Alpaca PAPER connection
    only when needed.
    """

    global alpaca_client

    if alpaca_client is None:

        alpaca_client = (
            AlpacaClient()
        )

    return alpaca_client