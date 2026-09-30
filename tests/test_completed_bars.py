import ast
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pandas as pd
from intelligence.feature_engine import FeatureEngine
from market.bar_quality import completed_intraday_bars, INTRADAY_MINUTES


class CompletedBarTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, 14, 12, tzinfo=timezone.utc)

    def bars(self):
        index = pd.date_range(end="2026-09-30T14:10:00Z", periods=60, freq="5min")
        frame = pd.DataFrame({"open": 2., "high": 2.02, "low": 1.98,
                              "close": 2., "volume": 100.}, index=index)
        frame.iloc[-1, frame.columns.get_loc("volume")] = 1.
        return frame

    def test_actual_features_do_not_compare_partial_volume_to_complete_bars(self):
        bars = self.bars()
        old = FeatureEngine().build("TEST", bars)
        filtered = completed_intraday_bars(bars, "5m", self.now)
        corrected = FeatureEngine().build("TEST", filtered)
        self.assertAlmostEqual(old.rvol, .01)
        self.assertAlmostEqual(old.volume_acceleration, .01)
        self.assertAlmostEqual(corrected.rvol, 1.)
        self.assertAlmostEqual(corrected.volume_acceleration, 1.)
        self.assertEqual(corrected.dollar_volume, 200.)
        self.assertEqual(corrected.timestamp, datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc))

    def test_close_boundary_and_timezones(self):
        bars = self.bars()
        bars.index = bars.index.tz_convert("America/New_York")
        self.assertEqual(len(completed_intraday_bars(bars, "5m", self.now)), 59)
        self.assertEqual(len(completed_intraday_bars(bars, "5m", self.now.replace(minute=15))), 60)
        for timeframe, minutes in INTRADAY_MINUTES.items():
            with self.subTest(timeframe=timeframe):
                frame = bars.iloc[-1:].copy()
                frame.index = pd.DatetimeIndex([self.now - timedelta(minutes=minutes)])
                self.assertEqual(len(completed_intraday_bars(frame, timeframe, self.now)), 1)
                self.assertEqual(len(completed_intraday_bars(frame, timeframe, self.now - timedelta(microseconds=1))), 0)

    def test_future_and_invalid_are_removed_without_filling_gaps(self):
        bars = self.bars().iloc[-3:].copy()
        bars.index = ["2026-09-30T14:00:00Z", "bad", "2026-09-30T14:15:00Z"]
        result = completed_intraday_bars(bars, "5m", self.now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.attrs["completed_bar_diagnostics"]["excluded_rows"], 2)
        self.assertIs(completed_intraday_bars(bars, "1d", self.now), bars)

    def test_production_get_bars_filters_api_result_and_requests_spare_row(self):
        # Execute the actual production method with a mocked HTTP client.
        source = Path(__file__).resolve().parents[1] / "market/market_data.py"
        cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "MarketData")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "get_bars")
        now = self.now
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        request = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        ns = {"pd": pd, "datetime": Clock, "timezone": timezone, "timedelta": timedelta,
              "StockBarsRequest": request, "Sort": SimpleNamespace(DESC="desc"),
              "MarketDataError": RuntimeError, "logger": logging.getLogger(__name__),
              "INTRADAY_MINUTES": INTRADAY_MINUTES,
              "completed_intraday_bars": completed_intraday_bars}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), ns)
        client = Mock()
        client.get_stock_bars.return_value = SimpleNamespace(df=self.bars())
        fake = SimpleNamespace(client=client, feed="iex", _normalize_symbol=lambda s: s,
                               _lookback_days=lambda t, n: 5, _timeframe=lambda t: t,
                               get_feed_name=lambda: "iex")
        result = ns["get_bars"](fake, "TEST", "5m", 51)
        self.assertEqual(request.call_args.kwargs["limit"], 52)
        self.assertEqual(len(result), 51)
        self.assertEqual(result.index[-1], pd.Timestamp("2026-09-30T14:05:00Z"))
        self.assertTrue(result.attrs["completed_bar_diagnostics"]["completed_intraday_only"])
        self.assertEqual(result.attrs["data_feed"], "iex")
        client.get_stock_bars.return_value = SimpleNamespace(df=self.bars().iloc[-1:])
        with self.assertRaisesRegex(RuntimeError, "No completed"):
            ns["get_bars"](fake, "TEST", "5m", 51)


if __name__ == "__main__":
    unittest.main()
