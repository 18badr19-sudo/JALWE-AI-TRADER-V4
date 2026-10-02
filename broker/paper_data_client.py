"""PAPER-only Alpaca data adapter.

Replaces alpaca-trade-api (the legacy REST SDK) so scanner and
liquidity use the same alpaca-py client as execution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd

from alpaca.data.enums import DataFeed
from alpaca.common.enums import Sort
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest


_TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "30Min": TimeFrame(30, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


class PaperBarsClient:
    """Drop-in for the two get_bars() call shapes used by LiquidityEngine."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._data_client = StockHistoricalDataClient(api_key, api_secret)

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 1000,
        feed: str = "iex",
        adjustment: str = "raw",
    ) -> Any:
        mapped = _TIMEFRAMES.get(str(timeframe))
        if mapped is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=mapped,
            start=_parse_ts(start),
            end=_parse_ts(end),
            limit=int(limit),
            sort=Sort.DESC,
            feed=DataFeed.IEX if str(feed).lower() == "iex" else DataFeed.SIP,
            adjustment=adjustment or "raw",
        )
        bars = self._data_client.get_stock_bars(request)
        frame = bars.df if hasattr(bars, "df") else pd.DataFrame()
        return _BarsResult(frame.sort_index())


class PaperAssetClient(PaperBarsClient):
    """Drop-in for ScannerEngine.list_assets(status=..., asset_class=...)."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        super().__init__(api_key, api_secret)
        self._client = TradingClient(api_key, api_secret, paper=True)

    def list_assets(
        self,
        status: str = "active",
        asset_class: str = "us_equity",
    ) -> list[Any]:
        status_enum = (
            AssetStatus.ACTIVE
            if str(status).lower() == "active"
            else AssetStatus.INACTIVE
        )
        class_enum = (
            AssetClass.US_EQUITY
            if str(asset_class).lower() in {"us_equity", "us-equity"}
            else AssetClass.US_EQUITY
        )
        return list(
            self._client.get_all_assets(
                GetAssetsRequest(
                    status=status_enum,
                    asset_class=class_enum,
                )
            )
            or []
        )


class _BarsResult:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)
