"""Risk gate evidence must be finite before sizing or daily-loss approval."""
import math
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from core.models import SignalAction
from trading.risk_engine import RiskEngine
import trading.risk_engine as module


class RiskInputIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine()
        self.features = NS(symbol='TEST', data_quality_ok=True,
                           data_is_stale=False, price=10., atr=.2)
        self.ai = NS(action=SignalAction.BUY, score=90.)
        self.broker = Mock()
        self.broker.get_account_snapshot.return_value = {'equity': 100000., 'cash': 100000.}
        self.broker.get_all_positions.return_value = []

    def evaluate(self, **kwargs):
        return self.engine.evaluate(self.features, self.ai,
            **dict({'daily_start_equity': 100., 'strategy_equity': 100.,
                    'recommended_risk_pct': 1., 'account_provider': lambda: self.broker,
                    'position_reader': lambda: []}, **kwargs))

    def test_invalid_daily_baseline_cannot_disable_loss_breaker(self):
        for value in (float('nan'), float('inf'), float('-inf'), True, 'nan', 'bad'):
            with self.subTest(value=value):
                self.assertFalse(self.evaluate(daily_start_equity=value).approved)

    def test_invalid_ai_score_cannot_pass_threshold(self):
        for value in (float('nan'), float('inf'), float('-inf'), True, None, 'bad', 101.):
            with self.subTest(value=value):
                self.ai.score = value
                self.assertFalse(self.evaluate().approved)

    def test_invalid_account_balances_fail_closed(self):
        for field in ('equity', 'cash'):
            for value in (float('nan'), float('inf'), float('-inf'), True, None, 'bad'):
                with self.subTest(field=field, value=value):
                    self.broker.get_account_snapshot.return_value = {'equity': 100000., 'cash': 100000., field: value}
                    self.assertFalse(self.evaluate().approved)
        for account in ({}, {'cash': 100000.}, None):
            self.broker.get_account_snapshot.return_value = account
            self.assertFalse(self.evaluate().approved)

    def test_invalid_entry_risk_stop_and_wallet_reject_without_exception(self):
        for field in ('strategy_equity','recommended_risk_pct','confirmed_entry_price','confirmed_stop_price'):
            for value in (float('nan'),float('inf'),float('-inf'),True,'bad'):
                with self.subTest(field=field,value=value):
                    self.assertFalse(self.evaluate(**{field:value}).approved)

    def test_invalid_atr_is_not_clamped_into_valid_stop(self):
        for value in (float('nan'),float('inf'),float('-inf'),True,'bad',None):
            with self.subTest(value=value):
                self.features.atr=value
                self.assertFalse(self.evaluate().approved)

    def test_nonfinite_configuration_never_becomes_a_full_allocation(self):
        for field in ('strategy_starting_capital','minimum_price','maximum_price',
                      'atr_stop_multiplier','maximum_stop_pct','minimum_stop_pct',
                      'allocation_b_pct','allocation_a_pct','allocation_a_plus_pct'):
            for value in (float('nan'),float('inf'),float('-inf')):
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    RiskEngine(**{field:value})

    def test_valid_numeric_strings_preserve_normal_decision(self):
        baseline=self.evaluate()
        self.assertTrue(baseline.approved)
        self.broker.get_account_snapshot.return_value={'equity':'100000','cash':'100000'}
        other=self.evaluate(daily_start_equity='100',strategy_equity='100',recommended_risk_pct='1',confirmed_entry_price='10',confirmed_stop_price='9.7')
        self.assertTrue(other.approved)
        self.assertEqual((other.quantity,other.stop_price),(baseline.quantity,baseline.stop_price))
        self.assertEqual(other.metadata['strategy_equity'],100.)
        self.assertEqual(other.metadata['broker_equity'],100000.)

    def test_daily_loss_boundary_and_atr_cap_keep_existing_policy(self):
        self.assertFalse(self.evaluate(strategy_equity=97.).approved)
        self.assertTrue(self.evaluate(strategy_equity=97.01).approved)
        self.features.atr=100.
        self.assertEqual(self.evaluate().stop_price,9.5)

    def test_sizing_respects_independent_decimal_budget(self):
        with patch.object(module,'settings',NS(MAX_ENTRY_SLIPPAGE_PCT=.5)):
            for entry in (0.51, 1.01, 2.80, 10.00, 33.33):
                for cash in (1.,10.,50.,100.):
                    for risk_pct in (.5,1.,1.5):
                        stop=round(entry*.98,4)
                        qty,risk,value=self.engine._calculate_quantity(entry,stop,100.,cash,risk_pct,75.)
                        cap=(Decimal(str(entry))*Decimal('1.005')).quantize(Decimal('.01') if entry*1.005>=1 else Decimal('.0001'),rounding='ROUND_FLOOR')
                        per_share=cap-Decimal(str(stop))
                        expected=max(0,min(int(Decimal(str(risk_pct))//per_share),int(Decimal(75)//cap),int(Decimal(str(cash))//cap)))
                        self.assertEqual(qty,expected)
                        self.assertLessEqual(risk,risk_pct+1e-10)
                        self.assertLessEqual(value,min(75.,cash)+1e-10)

class FinalEntryIntegrityTests(unittest.TestCase):
    def test_fractional_boolean_and_nonfinite_quantity_never_reach_broker(self):
        from test_paper_safety import ready_decision
        from trading.execution_engine import ExecutionEngine
        for value in (1.9, True, float('inf'), float('nan'), '1.5'):
            with self.subTest(value=value):
                engine=ExecutionEngine.__new__(ExecutionEngine)
                engine.broker=Mock()
                decision=ready_decision(); decision.quantity=value
                with self.assertRaises(RuntimeError): engine.submit_final_decision(decision)
                engine.broker.market_is_open.assert_not_called()
                engine.broker.submit_protected_entry.assert_not_called()

    def test_nonfinite_target_or_boolean_level_never_reach_broker(self):
        from test_paper_safety import ready_decision
        from trading.execution_engine import ExecutionEngine
        for field,value in [('target_3',float('inf')),('stop_price',True),('risk_pct',True)]:
            with self.subTest(field=field):
                engine=ExecutionEngine.__new__(ExecutionEngine); engine.broker=Mock()
                decision=ready_decision(); setattr(decision,field,value)
                with self.assertRaises(RuntimeError): engine.submit_final_decision(decision)
                engine.broker.market_is_open.assert_not_called()
                engine.broker.submit_protected_entry.assert_not_called()


if __name__=='__main__': unittest.main()
