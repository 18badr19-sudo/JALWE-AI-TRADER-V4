import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from broker.alpaca_client import AlpacaClient
from broker.stop_pricing import normalize_stop_price, observed_stop_price
from core.models import BrokerOrder, TradeSide, OrderStatus
from trading.execution_engine import ExecutionEngine
from trading.paper_trade_orchestrator import PaperTradeOrchestrator
import trading.paper_trade_orchestrator as orchestrator
import jalwe_research_watcher as watcher
import test_partial_stop_rejection as fixtures


class StopPriceContractTests(unittest.TestCase):
    setUp = fixtures.PartialStopRejectionTests.setUp

    def protect(self, price=9.):
        self.trade.metadata.update(protective_stop_order_id='original',
            protective_stop_quantity=3, protective_stop_price=price,
            protective_stop_active=True)
        self.db.save_managed_trade('t', self.trade)

    def raw(self, price='9', status='new'):
        return NS(id='original', symbol='TEST', side='sell', qty='3',
            status=status, filled_qty='0', filled_avg_price=None, stop_price=price)

    def test_invalid_replacement_cannot_cancel_or_mutate_existing_protection(self):
        self.protect(); before=self.db.load_managed_trade('t')
        for bad in (float('nan'),float('inf'),-float('inf'),0.,-1.,.00001,True,None,'bad'):
            with self.subTest(price=bad):
                self.trade.current_stop=bad;execution=Mock()
                with patch.object(watcher,'database',self.db),self.assertRaises(ValueError):
                    watcher._sync_protective_stop('t',self.trade,execution)
                execution.broker.get_order.assert_not_called()
                execution.cancel_protective_stop.assert_not_called()
                execution.submit_protective_stop.assert_not_called()
                self.assertEqual(self.db.load_managed_trade('t'),before)

    def test_weaker_actual_broker_stop_is_replaced_despite_matching_local_metadata(self):
        self.protect();execution=Mock()
        execution.broker.get_order.side_effect=[self.raw('8'),self.raw('8'),self.raw('8','canceled')]
        execution.submit_protective_stop.return_value=BrokerOrder(symbol='TEST',side=TradeSide.SELL,
            quantity=3,order_id='new',status=OrderStatus.ACCEPTED,requested_price=9.)
        with patch.object(watcher,'database',self.db),patch.object(watcher.time,'sleep'):
            result=watcher._sync_protective_stop('t',self.trade,execution)
        execution.cancel_protective_stop.assert_called_once_with('original')
        self.assertEqual(execution.submit_protective_stop.call_args.kwargs['stop_price'],9.)
        self.assertTrue(result['changed']);self.assertEqual(result['stop_price'],9.)

    def test_rounding_matches_actual_broker_price_without_cancel_churn(self):
        for desired,actual in ((2.7661,'2.77'),(.38555,'0.3856'),(.99999,'1.0')):
            with self.subTest(price=desired):
                self.trade.current_stop=desired;self.protect(desired);execution=Mock()
                execution.broker.get_order.return_value=self.raw(actual)
                with patch.object(watcher,'database',self.db):
                    result=watcher._sync_protective_stop('t',self.trade,execution)
                self.assertFalse(result['changed']);self.assertEqual(result['stop_price'],float(actual))
                execution.cancel_protective_stop.assert_not_called()
                execution.submit_protective_stop.assert_not_called()

    def test_stronger_broker_stop_is_preserved_and_actual_price_reported(self):
        self.protect();execution=Mock();execution.broker.get_order.return_value=self.raw('9.2')
        with patch.object(watcher,'database',self.db):
            result=watcher._sync_protective_stop('t',self.trade,execution)
        self.assertEqual(result['stop_price'],9.2);self.assertEqual(result['requested_stop_price'],9.)
        self.assertEqual(self.trade.current_stop,9.)
        execution.cancel_protective_stop.assert_not_called()
        execution.submit_protective_stop.assert_not_called()

    def test_quantity_replacement_retains_stronger_observed_stop(self):
        self.protect();self.trade.metadata['protective_stop_quantity']=5
        execution=Mock()
        live=self.raw('9.2');live.qty='5'
        canceled=self.raw('9.2','canceled');canceled.qty='5'
        execution.broker.get_order.side_effect=[live,live,canceled]
        execution.submit_protective_stop.return_value=BrokerOrder(symbol='TEST',side=TradeSide.SELL,
            quantity=3,order_id='new',status=OrderStatus.ACCEPTED,requested_price=9.2)
        with patch.object(watcher,'database',self.db),patch.object(watcher.time,'sleep'):
            watcher._sync_protective_stop('t',self.trade,execution)
        request=execution.submit_protective_stop.call_args.kwargs
        self.assertEqual(request['quantity'],3);self.assertEqual(request['stop_price'],9.2)

    def test_missing_or_invalid_active_broker_price_cannot_be_assumed_or_cancelled(self):
        self.protect()
        for price in (None,'NaN','Infinity','0','-1','0.00001','9.001',True):
            with self.subTest(price=price):
                before=copy.deepcopy(self.trade);execution=Mock()
                execution.broker.get_order.return_value=self.raw(price)
                with patch.object(watcher,'database',self.db),self.assertRaises(ValueError):
                    watcher._sync_protective_stop('t',self.trade,execution)
                self.assertEqual(self.trade,before)
                execution.cancel_protective_stop.assert_not_called()
                execution.submit_protective_stop.assert_not_called()

    def test_subdollar_one_tick_weaker_stop_is_not_treated_as_matching(self):
        self.trade.current_stop=.3856;self.protect(.3856);execution=Mock()
        execution.broker.get_order.side_effect=[self.raw('.3855'),self.raw('.3855'),self.raw('.3855','canceled')]
        execution.submit_protective_stop.return_value=BrokerOrder(symbol='TEST',side=TradeSide.SELL,
            quantity=3,order_id='new',status=OrderStatus.ACCEPTED,requested_price=.3856)
        with patch.object(watcher,'database',self.db),patch.object(watcher.time,'sleep'):
            result=watcher._sync_protective_stop('t',self.trade,execution)
        self.assertTrue(result['changed']);execution.cancel_protective_stop.assert_called_once()

    def test_all_submission_boundaries_reject_invalid_price_before_callbacks_or_broker(self):
        for bad in (float('nan'),float('inf'),0.,-1.,.00001,True,None):
            with self.subTest(price=bad):
                client=AlpacaClient.__new__(AlpacaClient);client.client=Mock();callback=Mock()
                with self.assertRaises(ValueError):
                    client.submit_stop_order('TEST',3,bad,before_submit=callback)
                client.client.submit_order.assert_not_called();callback.assert_not_called()
                engine=ExecutionEngine.__new__(ExecutionEngine);engine.broker=Mock()
                with self.assertRaises(ValueError):
                    engine.submit_protective_stop(symbol='TEST',quantity=3,stop_price=bad,before_submit=callback)
                engine.broker.get_position.assert_not_called();engine.broker.submit_stop_order.assert_not_called()
                obj=PaperTradeOrchestrator.__new__(PaperTradeOrchestrator);obj.execution_engine=Mock()
                trade=copy.deepcopy(self.trade);trade.current_stop=bad;db=Mock();db.load_managed_trade.return_value=trade
                with patch.object(orchestrator,'database',db),self.assertRaises(ValueError):
                    obj._arm_managed_trade_stop('t')
                db.save_managed_trade.assert_not_called();obj.execution_engine.submit_protective_stop.assert_not_called()

    def test_rounding_contract_preserves_existing_valid_submission_prices(self):
        for value in (2.7661,9.876,.38555,.99999,1.,2.675):
            expected=round(value,2 if value>=1 else 4)
            self.assertEqual(normalize_stop_price(value),expected)
            self.assertEqual(observed_stop_price(str(expected)),expected)
