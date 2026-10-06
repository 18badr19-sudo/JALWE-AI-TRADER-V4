from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockTradesRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from core.config import settings
from market.bar_quality import INTRADAY_MINUTES, completed_intraday_bars


logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    """
    Raised when reliable market data cannot be obtained.
    """


class MarketData:
    """
    Central market-data service for JALWE AI TRADER V4.

    Rules:
    - Real Alpaca data only.
    - IEX or SIP according to configuration.
    - Latest bars requested first.
    - Returned DataFrame sorted oldest -> newest.
    - No synthetic/random fallback.
    """

    def __init__(self) -> None:
        self._validate_configuration()

        self.client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )

        self.feed = self._resolve_feed(
            settings.ALPACA_DATA_FEED
        )

        logger.info(
            "MarketData initialized | feed=%s",
            self.get_feed_name(),
        )

    # ========================================================
    # CONFIGURATION
    # ========================================================

    def _validate_configuration(self) -> None:

        if not settings.ALPACA_API_KEY:
            raise RuntimeError(
                "ALPACA_API_KEY is missing."
            )

        if not settings.ALPACA_SECRET_KEY:
            raise RuntimeError(
                "ALPACA_SECRET_KEY is missing."
            )

    @staticmethod
    def _resolve_feed(
        feed_name: str,
    ) -> DataFeed:

        normalized = str(
            feed_name or "iex"
        ).strip().lower()

        if normalized == "iex":
            return DataFeed.IEX

        if normalized == "sip":
            return DataFeed.SIP

        raise ValueError(
            f"Unsupported ALPACA_DATA_FEED: {feed_name}"
        )

    def get_feed_name(self) -> str:

        value = getattr(
            self.feed,
            "value",
            self.feed,
        )

        return str(value).lower()

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
    def _timeframe(
        timeframe: str,
    ) -> TimeFrame:

        value = str(
            timeframe
        ).strip().lower()

        mapping = {
            "1m": TimeFrame.Minute,

            "5m": TimeFrame(
                5,
                TimeFrameUnit.Minute,
            ),

            "15m": TimeFrame(
                15,
                TimeFrameUnit.Minute,
            ),

            "30m": TimeFrame(
                30,
                TimeFrameUnit.Minute,
            ),

            "1h": TimeFrame.Hour,

            "1d": TimeFrame.Day,
        }

        if value not in mapping:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}"
            )

        return mapping[value]

    @staticmethod
    def _lookback_days(
        timeframe: str,
        limit: int,
    ) -> int:
        """
        Request enough calendar history while keeping
        the request reasonably small.
        """

        value = timeframe.lower()

        if value == "1d":
            return max(
                limit * 2,
                30,
            )

        if value == "1h":
            return max(
                30,
                int(limit / 5) + 10,
            )

        if value == "30m":
            return max(
                15,
                int(limit / 10) + 10,
            )

        if value == "15m":
            return max(
                10,
                int(limit / 20) + 7,
            )

        if value == "5m":
            return max(
                7,
                int(limit / 50) + 5,
            )

        if value == "1m":
            return max(
                5,
                int(limit / 200) + 3,
            )

        return 10

    # ========================================================
    # HISTORICAL BARS
    # ========================================================

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 300,
    ) -> pd.DataFrame:

        symbol = self._normalize_symbol(
            symbol
        )

        if limit <= 0:
            raise ValueError(
                "limit must be greater than zero."
            )

        if limit > 10000:
            raise ValueError(
                "limit cannot exceed 10000."
            )

        end = datetime.now(
            timezone.utc
        )

        start = end - timedelta(
            days=self._lookback_days(
                timeframe,
                limit,
            )
        )

        request = StockBarsRequest(
            symbol_or_symbols=symbol,

            timeframe=self._timeframe(
                timeframe
            ),

            start=start,
            end=end,

            # IMPORTANT:
            # Request the newest bars first.
            # The API may include the current unfinished interval.
            limit=min(limit + 1, 10000) if timeframe.lower() in INTRADAY_MINUTES else limit,
            sort=Sort.DESC,

            feed=self.feed,
        )

        try:
            response = (
                self.client
                .get_stock_bars(
                    request
                )
            )

            df = response.df

        except Exception as exc:

            logger.exception(
                "Failed to fetch bars | "
                "symbol=%s timeframe=%s "
                "limit=%s feed=%s",
                symbol,
                timeframe,
                limit,
                self.get_feed_name(),
            )

            raise MarketDataError(
                f"Market data unavailable for {symbol}"
            ) from exc

        if df is None or df.empty:
            raise MarketDataError(
                f"No market bars returned for {symbol}"
            )

        # Alpaca normally returns:
        # MultiIndex(symbol, timestamp)
        if isinstance(
            df.index,
            pd.MultiIndex,
        ):
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
                        f"Unable to normalize bars "
                        f"for {symbol}"
                    ) from exc

        # API returned newest -> oldest because Sort.DESC.
        # JALWE indicators require chronological order.
        df = df.sort_index().copy()

        required_columns = {
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        missing_columns = (
            required_columns
            .difference(df.columns)
        )

        if missing_columns:
            raise MarketDataError(
                f"Missing columns for {symbol}: "
                f"{sorted(missing_columns)}"
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

        # Defensive final limit.
        df = completed_intraday_bars(df, timeframe, end)
        if df.empty:
            raise MarketDataError(f"No completed market bars returned for {symbol}")
        df.attrs["data_feed"] = self.get_feed_name()
        return df.tail(limit)

    def get_observation_bars(
        self, symbol: str, start: datetime, end: datetime,
        *, feed: Optional[str] = None,
    ) -> pd.DataFrame:
        """Read a fixed research window, including after restart. No latest-bar fallback."""
        symbol = self._normalize_symbol(symbol)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Observation timestamps must include a timezone.")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if not timedelta(0) < end - start <= timedelta(minutes=60):
            raise ValueError("Observation window must be at most 60 minutes.")
        observation_feed = self.feed if feed is None else self._resolve_feed(feed)
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=self._timeframe("1m"),
            start=start, end=end, sort=Sort.ASC, limit=1000, feed=observation_feed,
        )
        try:
            frame = self.client.get_stock_bars(request).df
        except Exception as exc:
            raise MarketDataError(f"Observation data unavailable for {symbol}") from exc
        if frame is None or frame.empty:
            frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol, level=0)
        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
        frame = frame.loc[(frame.index >= start) & (frame.index < end)]
        frame = completed_intraday_bars(frame.sort_index(), "1m", end)
        frame.attrs["data_feed"] = str(getattr(observation_feed, "value", observation_feed)).lower()
        return frame

    def get_research_observation_bars(self, symbol, start, end, *, as_of=None):
        """Historical SIP only after the free-access delay; execution feed is untouched."""
        from market.research_diagnostics import SIP_RESEARCH_DELAY_MINUTES
        now = as_of or datetime.now(timezone.utc)
        if end.tzinfo is None or now.tzinfo is None:
            raise ValueError("Research timestamps must include a timezone")
        if now - end < timedelta(minutes=SIP_RESEARCH_DELAY_MINUTES):
            raise ValueError("Historical SIP research window is not old enough")
        return self.get_observation_bars(symbol, start, end, feed="sip")

    # ========================================================
    # LATEST QUOTE
    # ========================================================

    def get_execution_quote(self, symbol):
        # Extended-session execution requires consolidated live quotes. IEX-only
        # or delayed data cannot establish an executable after-hours bid.
        request = StockLatestQuoteRequest(symbol_or_symbols=self._normalize_symbol(symbol), feed=DataFeed.SIP)
        quote = self.client.get_stock_latest_quote(request)[self._normalize_symbol(symbol)]
        return {'bid': quote.bid_price, 'ask': quote.ask_price, 'timestamp': quote.timestamp}

    def get_latest_quote(
        self,
        symbol: str,
    ) -> dict[str, Optional[float]]:

        symbol = self._normalize_symbol(
            symbol
        )

        request = StockLatestQuoteRequest(
            symbol_or_symbols=symbol,
            feed=self.feed,
        )

        try:
            quotes = (
                self.client
                .get_stock_latest_quote(
                    request
                )
            )

            quote = quotes[symbol]

        except Exception as exc:

            logger.exception(
                "Failed to fetch latest quote | "
                "symbol=%s feed=%s",
                symbol,
                self.get_feed_name(),
            )

            raise MarketDataError(
                f"Latest quote unavailable "
                f"for {symbol}"
            ) from exc

        bid = None
        ask = None

        if getattr(
            quote,
            "bid_price",
            None,
        ) is not None:

            bid = float(
                quote.bid_price
            )

        if getattr(
            quote,
            "ask_price",
            None,
        ) is not None:

            ask = float(
                quote.ask_price
            )

        mid = None
        spread = None
        spread_pct = None

        if (
            bid is not None
            and ask is not None
            and bid > 0
            and ask > 0
            and ask >= bid
        ):
            mid = (
                bid + ask
            ) / 2.0

            spread = ask - bid

            if mid > 0:
                spread_pct = (
                    spread / mid
                ) * 100.0

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
        }

    # ========================================================
    # LAST PRICE
    # ========================================================

    def get_last_price(
        self,
        symbol: str,
    ) -> float:

        symbol = self._normalize_symbol(
            symbol
        )

        # Trigger/chase logic must use a real executed trade price.
        # A bid/ask midpoint can jump when the spread widens on a
        # thin stock and can create false trigger crosses or false
        # "too extended" rejections.
        request = StockLatestTradeRequest(
            symbol_or_symbols=symbol,
            feed=self.feed,
        )

        try:
            trades = (
                self.client
                .get_stock_latest_trade(
                    request
                )
            )

            trade = trades[symbol]

            price = float(
                trade.price
            )

            if price > 0:
                return price

        except Exception as exc:

            logger.warning(
                "Latest trade unavailable for %s "
                "on feed=%s; using latest real bar: %s",
                symbol,
                self.get_feed_name(),
                exc,
            )

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

    # ========================================================
    # TRADE RANGE BETWEEN MANAGEMENT POLLS
    # ========================================================

    def get_trade_range(
        self,
        symbol: str,
        start: datetime,
        end: Optional[datetime] = None,
        limit: int = 10000,
    ) -> dict[str, object]:
        """
        Return the real executed-trade price range observed on the
        configured Alpaca feed between two timestamps.

        This is used by PAPER position management so a brief target
        touch is not lost just because the next management poll sees
        a lower last trade.
        """

        symbol = self._normalize_symbol(
            symbol
        )

        if not isinstance(start, datetime):
            raise TypeError(
                "start must be a datetime."
            )

        if start.tzinfo is None:
            start_utc = start.replace(
                tzinfo=timezone.utc
            )
        else:
            start_utc = start.astimezone(
                timezone.utc
            )

        final_end = (
            end
            if end is not None
            else datetime.now(
                timezone.utc
            )
        )

        if not isinstance(final_end, datetime):
            raise TypeError(
                "end must be a datetime."
            )

        if final_end.tzinfo is None:
            end_utc = final_end.replace(
                tzinfo=timezone.utc
            )
        else:
            end_utc = final_end.astimezone(
                timezone.utc
            )

        if limit <= 0 or limit > 10000:
            raise ValueError(
                "limit must be between 1 and 10000."
            )

        if end_utc <= start_utc:
            return {
                "high": None,
                "low": None,
                "last": None,
                "count": 0,
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
                "feed": self.get_feed_name(),
            }

        request = StockTradesRequest(
            symbol_or_symbols=symbol,
            start=start_utc,
            end=end_utc,
            limit=limit,
            sort=Sort.ASC,
            feed=self.feed,
        )

        try:
            response = (
                self.client
                .get_stock_trades(
                    request
                )
            )

            try:
                trades = response[
                    symbol
                ]
            except (
                KeyError,
                TypeError,
            ):
                trades = []

        except Exception as exc:
            logger.exception(
                "Failed to fetch trade range | "
                "symbol=%s start=%s end=%s "
                "feed=%s",
                symbol,
                start_utc.isoformat(),
                end_utc.isoformat(),
                self.get_feed_name(),
            )

            raise MarketDataError(
                f"Trade range unavailable for {symbol}"
            ) from exc

        prices: list[float] = []

        for trade in trades or []:
            try:
                price = float(
                    getattr(
                        trade,
                        "price",
                        0.0,
                    )
                    or 0.0
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            if price > 0:
                prices.append(
                    price
                )

        if not prices:
            return {
                "high": None,
                "low": None,
                "last": None,
                "count": 0,
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
                "feed": self.get_feed_name(),
            }

        return {
            "high": max(prices),
            "low": min(prices),
            "last": prices[-1],
            "count": len(prices),
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "feed": self.get_feed_name(),
        }

    # ========================================================
    # DATA QUALITY
    # ========================================================

    def data_is_fresh(
        self,
        dataframe: pd.DataFrame,
        max_age_minutes: int = 15,
    ) -> bool:

        if (
            dataframe is None
            or dataframe.empty
        ):
            return False

        if max_age_minutes <= 0:
            return False

        try:
            last_timestamp = pd.Timestamp(
                dataframe.index[-1]
            )

            if last_timestamp.tzinfo is None:

                last_timestamp = (
                    last_timestamp
                    .tz_localize("UTC")
                )

            else:

                last_timestamp = (
                    last_timestamp
                    .tz_convert("UTC")
                )

            now = pd.Timestamp.now(
                tz="UTC"
            )

            age_minutes = (
                now - last_timestamp
            ).total_seconds() / 60.0

            return (
                0
                <= age_minutes
                <= max_age_minutes
            )

        except Exception:

            logger.exception(
                "Unable to validate "
                "market-data freshness."
            )

            return False


# ============================================================
# LAZY SINGLETON
# ============================================================

market_data: Optional[MarketData] = None


def get_market_data() -> MarketData:

    global market_data

    if market_data is None:
        market_data = MarketData()

    return market_data

