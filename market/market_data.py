from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from core.config import settings

logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    """Raised when reliable market data cannot be obtained."""


class MarketData:
    """
    Central market-data service for JALWE AI TRADER V4.

    Rules:
    - Real Alpaca market data only.
    - No random/synthetic fallback data.
    - Missing or invalid data raises MarketDataError.
    """

    def __init__(self) -> None:
        self._validate_configuration()

        self.client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )

    def _validate_configuration(self) -> None:
        if not settings.ALPACA_API_KEY:
            raise RuntimeError("ALPACA_API_KEY is missing.")

        if not settings.ALPACA_SECRET_KEY:
            raise RuntimeError("ALPACA_SECRET_KEY is missing.")

    @staticmethod
    def _timeframe(timeframe: str) -> TimeFrame:
        value = timeframe.strip().lower()

        mapping = {
            "1m": TimeFrame.Minute,
            "5m": TimeFrame(5, TimeFrameUnit.Minute),
            "15m": TimeFrame(15, TimeFrameUnit.Minute),
            "30m": TimeFrame(30, TimeFrameUnit.Minute),
            "1h": TimeFrame.Hour,
            "1d": TimeFrame.Day,
        }

        if value not in mapping:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}"
            )

        return mapping[value]

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 300,
    ) -> pd.DataFrame:
        """
        Fetch historical stock bars.

        Returns:
            DataFrame containing:
            open, high, low, close, volume, trade_count, vwap
        """

        symbol = symbol.upper().strip()

        if not symbol:
            raise ValueError("Symbol cannot be empty.")

        if limit <= 0:
            raise ValueError("Limit must be greater than zero.")

        end = datetime.now(timezone.utc)

        # Request enough calendar history to cover
        # weekends and non-trading hours.
        if timeframe.lower() == "1d":
            start = end - timedelta(days=max(limit * 2, 30))
        else:
            start = end - timedelta(days=10)

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=self._timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
        )

        try:
            bars = self.client.get_stock_bars(request)

            df = bars.df

        except Exception as exc:
            logger.exception(
                "Failed to fetch market bars for %s",
                symbol,
            )
            raise MarketDataError(
                f"Market data unavailable for {symbol}"
            ) from exc

        if df is None or df.empty:
            raise MarketDataError(
                f"No market bars returned for {symbol}"
            )

        # Alpaca normally returns a MultiIndex:
        # symbol + timestamp.
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(
                    symbol,
                    level="symbol",
                )
            except Exception:
                try:
                    df = df.xs(
                        symbol,
                        level=0,
                    )
                except Exception as exc:
                    raise MarketDataError(
                        f"Unable to normalize bars for {symbol}"
                    ) from exc

        df = df.sort_index().copy()

        required_columns = {
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        missing = required_columns.difference(df.columns)

        if missing:
            raise MarketDataError(
                f"Missing columns for {symbol}: "
                f"{sorted(missing)}"
            )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
        ]

        for column in numeric_columns:
            if column in df.columns:
                df[column] = pd.to_numeric(
                    df[column],
                    errors="coerce",
                )

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        if df.empty:
            raise MarketDataError(
                f"Invalid market bars for {symbol}"
            )

        return df.tail(limit)

    def get_latest_quote(
        self,
        symbol: str,
    ) -> dict[str, Optional[float]]:
        """
        Return the latest bid/ask quote.
        """

        symbol = symbol.upper().strip()

        request = StockLatestQuoteRequest(
            symbol_or_symbols=symbol
        )

        try:
            quotes = self.client.get_stock_latest_quote(
                request
            )

            quote = quotes[symbol]

        except Exception as exc:
            logger.exception(
                "Failed to fetch latest quote for %s",
                symbol,
            )
            raise MarketDataError(
                f"Latest quote unavailable for {symbol}"
            ) from exc

        bid = (
            float(quote.bid_price)
            if quote.bid_price is not None
            else None
        )

        ask = (
            float(quote.ask_price)
            if quote.ask_price is not None
            else None
        )

        spread = None
        spread_pct = None
        mid = None

        if (
            bid is not None
            and ask is not None
            and bid > 0
            and ask >= bid
        ):
            spread = ask - bid
            mid = (ask + bid) / 2

            if mid > 0:
                spread_pct = (spread / mid) * 100

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
        }

    def get_last_price(
        self,
        symbol: str,
    ) -> float:
        """
        Return a reliable recent market price.

        Uses quote midpoint when possible.
        Falls back to the latest real 1-minute bar,
        never to fabricated data.
        """

        try:
            quote = self.get_latest_quote(symbol)

            mid = quote.get("mid")

            if mid is not None and mid > 0:
                return float(mid)

        except MarketDataError:
            pass

        bars = self.get_bars(
            symbol=symbol,
            timeframe="1m",
            limit=1,
        )

        price = float(
            bars["close"].iloc[-1]
        )

        if price <= 0:
            raise MarketDataError(
                f"Invalid latest price for {symbol}"
            )

        return price

    def data_is_fresh(
        self,
        dataframe: pd.DataFrame,
        max_age_minutes: int = 15,
    ) -> bool:
        """
        Check whether the latest bar is recent enough.
        """

        if dataframe is None or dataframe.empty:
            return False

        try:
            last_timestamp = pd.Timestamp(
                dataframe.index[-1]
            )

            if last_timestamp.tzinfo is None:
                last_timestamp = last_timestamp.tz_localize(
                    "UTC"
                )
            else:
                last_timestamp = last_timestamp.tz_convert(
                    "UTC"
                )

            now = pd.Timestamp.now(tz="UTC")

            age_minutes = (
                now - last_timestamp
            ).total_seconds() / 60

            return age_minutes <= max_age_minutes

        except Exception:
            return False


market_data: Optional[MarketData] = None


def get_market_data() -> MarketData:
    """
    Lazy singleton for the central market-data service.
    """

    global market_data

    if market_data is None:
        market_data = MarketData()

    return market_data
