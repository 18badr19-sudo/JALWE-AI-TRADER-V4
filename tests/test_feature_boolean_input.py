import unittest
import numpy as np
import pandas as pd
from intelligence.feature_engine import FeatureEngine


class FeatureBooleanInputTests(unittest.TestCase):
    def bars(self):
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0},
            index=pd.date_range("2026-11-12T14:30Z", periods=60, freq="5min"),
        )

    def test_boolean_cells_are_not_numeric_market_evidence(self):
        engine = FeatureEngine()
        for column in sorted(engine.REQUIRED_COLUMNS):
            for value in (True, False, np.bool_(True), np.bool_(False)):
                with self.subTest(column=column, value=repr(value)):
                    bars = self.bars()
                    bars[column] = bars[column].astype(object)
                    bars.loc[bars.index[-1], column] = value
                    self.assertIn("BOOLEAN_OHLCV", engine.input_issues(bars))
                    self.assertFalse(engine.build("TEST", bars).data_quality_ok)
                    diagnostic = engine.diagnose_input(bars)
                    self.assertFalse(diagnostic["data_quality_ok"])
                    self.assertIn("BOOLEAN_OHLCV", diagnostic["input_issues"])

    def test_boolean_dtype_and_nullable_boolean_columns_are_rejected(self):
        engine = FeatureEngine()
        for dtype in ("bool", "boolean"):
            with self.subTest(dtype=dtype):
                bars = self.bars()
                bars["volume"] = pd.Series(True, index=bars.index, dtype=dtype)
                self.assertIn("BOOLEAN_OHLCV", engine.input_issues(bars))
                self.assertFalse(engine.build("TEST", bars).data_quality_ok)

    def test_real_numeric_values_and_strings_remain_valid(self):
        engine = FeatureEngine()
        for kind in ("float", "int", "str"):
            with self.subTest(kind=kind):
                bars = self.bars().astype(kind)
                self.assertEqual(engine.input_issues(bars), [])
                self.assertTrue(engine.build("TEST", bars).data_quality_ok)
        bars = self.bars()
        bars.loc[bars.index[-1], "volume"] = 0
        self.assertEqual(engine.input_issues(bars), [])


if __name__ == "__main__":
    unittest.main()
