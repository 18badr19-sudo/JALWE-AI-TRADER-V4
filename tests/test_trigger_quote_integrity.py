import unittest
from types import SimpleNamespace
import pandas as pd
from intelligence.trigger_engine import TriggerEngine, TriggerState


class TriggerQuoteIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.engine = TriggerEngine()
        self.setup = SimpleNamespace(symbol='TEST', approved=True,
                                     trigger_price=10.0, invalidation_price=9.0)
        self.bars = pd.DataFrame({'close': [9.9, 10.01]})

    def test_invalid_explicit_quote_cannot_fall_back_to_candle(self):
        for price in (float('nan'), float('inf'), -float('inf'), 'bad', True, False):
            with self.subTest(price=price):
                result = self.engine.evaluate(self.setup, current_price=price, bars=self.bars)
                self.assertEqual(result.state, TriggerState.REJECTED)
                self.assertFalse(result.approved_for_entry)

    def test_absent_quote_preserves_candle_fallback(self):
        result = self.engine.evaluate(self.setup, bars=self.bars)
        self.assertTrue(result.approved_for_entry)

    def test_fresh_numeric_quote_still_confirms(self):
        for price in (10.01, '10.01'):
            with self.subTest(price=price):
                self.assertTrue(self.engine.evaluate(self.setup, price, self.bars).approved_for_entry)

    def test_boolean_trigger_and_stop_are_not_prices(self):
        for field in ('trigger_price', 'invalidation_price'):
            with self.subTest(field=field):
                setup = SimpleNamespace(symbol='TEST', approved=True,
                                        trigger_price=1.0, invalidation_price=0.9)
                setattr(setup, field, True)
                self.assertEqual(self.engine.evaluate(setup, 1.01).state, TriggerState.REJECTED)

    def test_boolean_latest_candle_is_rejected(self):
        setup = SimpleNamespace(symbol='TEST', approved=True,
                                trigger_price=1.0, invalidation_price=0.9)
        result = self.engine.evaluate(setup, bars=pd.DataFrame({'close': [0.99, True]}))
        self.assertEqual(result.state, TriggerState.REJECTED)

    def test_chase_limit_is_unchanged(self):
        self.assertEqual(self.engine.evaluate(self.setup, 10.2, self.bars).state, TriggerState.TOO_LATE)

if __name__ == '__main__':
    unittest.main()
