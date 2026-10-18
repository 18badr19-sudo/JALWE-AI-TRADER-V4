from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from intelligence.decision_engine import DecisionState, FinalTradeDecision
from core.models import FeatureSnapshot, SignalAction
from intelligence.ai_engine import AIAnalysis
from trading.risk_engine import get_risk_engine
from market_move_trial import snapshot_match
from research_lab.native import HistoricalData, NativeDecision, UnsupportedBoundary
from research_lab.simulator import Bundle, Wallet, compare, run_arm, utc
from lab_fixture import fixture, ready_fixture


def ready(symbol='TEST',quantity=8):
    return FinalTradeDecision(symbol,DecisionState.READY_FOR_PAPER_EXECUTION,True,
        quantity=quantity,entry_price=10.,stop_price=9.5,target_1=11.,target_2=11.5,target_3=12.)


class HistoricalLabTests(unittest.TestCase):
    def setUp(self):
        self.raw=fixture(); self.bundle=Bundle(self.raw); self.now=self.bundle.start

    def observation(self,seconds,price,*,bid=None,ask=None,size=100):
        return dict(symbol='TEST',at=self.now+timedelta(seconds=seconds),price=price,
            bid=price if bid is None else bid,ask=price if ask is None else ask,
            bid_size=size,ask_size=size)

    def wallet_with_entry(self,quantity=8):
        wallet=Wallet(self.bundle)
        wallet.submit_entry(ready(quantity=quantity),self.now)
        wallet.observe(self.observation(1,10.))
        self.assertEqual(len(wallet.trades),1)
        return wallet

    def test_native_gates_are_recomputed_without_broker_provider_or_database_reads(self):
        with patch('trading.risk_engine.get_alpaca_client',side_effect=AssertionError('broker forbidden')), \
             patch('intelligence.decision_engine.database.save_feature_snapshot',side_effect=AssertionError('write forbidden')), \
             patch('trading.risk_engine.database.get_active_managed_trade_rows',side_effect=AssertionError('db forbidden')), \
             patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            result=compare(self.raw)
        self.assertFalse(result['execution_authority'])
        self.assertEqual(result['state'],'SYNTHETIC_ENGINEERING_RUN')
        decision=result['baseline']['decisions'][0]
        self.assertNotEqual(decision['state'],'DATA_UNAVAILABLE',decision)
        self.assertIn('feature_engine.build',decision['calls'])
        self.assertIn('ai_engine.evaluate',decision['calls'])
        self.assertEqual(result['baseline']['decisions'],result['researcher']['decisions'])

    def test_full_native_latch_entry_risk_partials_and_exit_without_stubbed_gate_answers(self):
        with patch('trading.risk_engine.get_alpaca_client',side_effect=AssertionError('no SDK')):
            result=compare(ready_fixture())
        arm=result['baseline']
        self.assertEqual([d['state'] for d in arm['decisions']],['WATCHING','READY_FOR_PAPER_EXECUTION'])
        self.assertTrue(all(arm['decisions'][-1]['gates'].values()))
        self.assertIn('risk_engine.evaluate',arm['decisions'][-1]['calls'])
        self.assertIn('assess_entry',arm['decisions'][-1]['calls'])
        self.assertEqual(arm['closed_trades'],1)
        self.assertEqual(arm['open_trades'],0)
        self.assertAlmostEqual(arm['cash'],arm['starting_capital']+arm['net_realized'])
        self.assertEqual(arm['fills'],result['researcher']['fills'])

    def test_slippage_is_adverse_both_sides_and_fees_reduce_results(self):
        raw=ready_fixture(); zero=compare(raw)['baseline']
        raw['costs']=dict(slippage_bps=2.,fee_bps=10.,per_share_fee=.001)
        costly=compare(raw)['baseline']
        self.assertTrue(costly['fills'])
        self.assertGreater(costly['fills'][0]['price'],zero['fills'][0]['price'])
        self.assertGreater(costly['fees'],0.)
        self.assertLess(costly['cash'],zero['cash'])

    def test_unknown_quote_order_cannot_be_invented_from_a_daily_high(self):
        raw=ready_fixture(); raw['observations']=[]
        result=compare(raw)
        self.assertEqual(result['baseline']['fills'],[])
        self.assertFalse(result['evidence_complete'])

    def test_partial_target_is_canceled_before_gap_stop_and_quantity_never_oversells(self):
        wallet=self.wallet_with_entry(8)
        wallet.observe(self.observation(2,11.))
        wallet.observe(self.observation(3,11.,size=1))
        wallet.observe(self.observation(4,9.))
        wallet.observe(self.observation(5,8.9,size=100))
        sold=sum(fill['quantity'] for fill in wallet.fills if fill['side']=='SELL')
        self.assertEqual(sold,8)
        self.assertEqual(wallet.trades[0]['trade'].remaining_quantity,0)
        self.assertTrue(any(a['action']=='VIRTUAL_CANCEL_TARGET_FOR_STOP' for a in wallet.actions))

    def test_entry_expires_and_does_not_consume_next_session_quote(self):
        wallet=Wallet(self.bundle); wallet.submit_entry(ready(),self.now)
        wallet.observe(self.observation(31,10.))
        self.assertFalse(wallet.trades)
        self.assertEqual(wallet.actions[-1]['action'],'ENTRY_EXPIRED')

    def test_predeclared_holdout_and_future_or_overlapping_windows(self):
        raw=ready_fixture()
        window=dict(start=raw['start'],end=raw['end'],training_end='2026-10-15T22:00:00Z',
                    declared_at='2026-10-15T22:00:00Z')
        raw['holdout_windows']=[window]
        result=compare(raw)
        self.assertEqual(len(result['holdout_windows']),1)
        self.assertEqual(result['holdout_windows'][0]['baseline_closed'],1)
        raw['holdout_windows'].append(deepcopy(window))
        with self.assertRaises(ValueError): compare(raw)
        raw['holdout_windows']=[dict(window,declared_at=raw['end'])]
        with self.assertRaises(ValueError): compare(raw)

    def test_explicit_ny_calendar_supports_dst_and_early_closes(self):
        raw=deepcopy(self.raw)
        raw['sessions']=[dict(date='2026-11-27',open='2026-11-27T14:30:00Z',close='2026-11-27T18:00:00Z')]
        raw.update(start='2026-11-27T14:30:00Z',end='2026-11-27T18:00:00Z',observations=[],batches=[])
        bundle=Bundle(raw)
        self.assertIsNotNone(bundle.session(utc('2026-11-27T17:59:59Z')))
        self.assertIsNone(bundle.session(utc('2026-11-27T18:00:00Z')))

    def test_future_and_unfinished_bars_never_reach_native_features(self):
        raw=deepcopy(self.raw); future=deepcopy(raw['bars'][69])
        future.update(at=(self.now+timedelta(minutes=5)).isoformat(),
            end=(self.now+timedelta(minutes=10)).isoformat(),available_at=(self.now+timedelta(minutes=10)).isoformat(),
            open=999,high=1001,low=998,close=1000)
        raw['bars'].append(future)
        a,b=compare(self.raw),compare(raw)
        self.assertEqual(a['baseline']['decisions'],b['baseline']['decisions'])
        delayed=deepcopy(self.raw)
        delayed['bars'][69]['available_at']=(self.now+timedelta(minutes=2)).isoformat()
        data=HistoricalData(Bundle(delayed)); data.now=self.now
        self.assertEqual(len(data.get_bars('TEST','5m',500)),69)

    def test_missing_enrichment_and_spy_are_incomplete_not_strategy_losses(self):
        for key in ('enrichments','spy'):
            raw=deepcopy(self.raw)
            if key=='spy': raw['bars']=[r for r in raw['bars'] if r['symbol']!='SPY']
            else: raw['enrichments']=[]
            result=compare(raw)
            self.assertFalse(result['evidence_complete'])
            self.assertIsNone(result['paired_marked_equity_difference'])
            self.assertEqual(result['baseline']['decisions'][0]['state'],'DATA_UNAVAILABLE')

    def test_bad_geometry_duplicates_nan_bool_and_timezone_are_rejected(self):
        variants=[]
        raw=deepcopy(self.raw); raw['bars'].append(deepcopy(raw['bars'][0])); variants.append(raw)
        raw=deepcopy(self.raw); raw['bars'][0]['low']=100; variants.append(raw)
        raw=deepcopy(self.raw); raw['observations'][0]['bid']=float('nan'); variants.append(raw)
        raw=deepcopy(self.raw); raw['observations'][0]['ask_size']=True; variants.append(raw)
        raw=deepcopy(self.raw); raw['start']='2026-10-16T15:00:00'; variants.append(raw)
        for raw in variants:
            with self.subTest(),self.assertRaises(ValueError): Bundle(raw)

    def test_learning_and_config_after_start_are_rejected(self):
        for key in ('learning','configuration_frozen_at'):
            raw=deepcopy(self.raw)
            if key=='learning': raw[key]['frozen_at']='2026-10-17T00:00:00Z'
            else: raw[key]='2026-10-17T00:00:00Z'
            with self.assertRaises(ValueError): Bundle(raw)

    def test_same_observation_cannot_fill_entry_and_limit_gap_does_not_fill(self):
        wallet=Wallet(self.bundle); wallet.submit_entry(ready(),self.now)
        wallet.observe(self.observation(0,10.))
        self.assertEqual(wallet.trades,[])
        wallet.observe(self.observation(1,10.5))
        self.assertEqual(wallet.trades,[])
        self.assertEqual(wallet.cash,100.)
        self.assertEqual(wallet.entries,{})

    def test_entry_partial_fill_cancels_remainder_and_rebases_targets_on_fill(self):
        wallet=Wallet(self.bundle); wallet.submit_entry(ready(),self.now)
        wallet.observe(self.observation(1,10.02,size=3))
        trade=wallet.trades[0]['trade']
        self.assertEqual(trade.initial_quantity,3)
        self.assertAlmostEqual(trade.target_1,11.06)
        self.assertEqual(wallet.entries,{})
        wallet.observe(self.observation(2,10.02,size=100))
        self.assertEqual(trade.initial_quantity,3)

    def test_t1_to_entry_t2_to_t1_and_t3_to_t2_use_native_manager(self):
        wallet=self.wallet_with_entry()
        trade=wallet.trades[0]['trade']
        for seconds,price in [(2,11.),(3,11.)]: wallet.observe(self.observation(seconds,price))
        self.assertTrue(trade.t1_completed); self.assertAlmostEqual(trade.current_stop,10.)
        for seconds,price in [(4,11.5),(5,11.5)]: wallet.observe(self.observation(seconds,price))
        self.assertTrue(trade.t2_completed); self.assertAlmostEqual(trade.current_stop,11.)
        for seconds,price in [(6,12.),(7,12.)]: wallet.observe(self.observation(seconds,price))
        # Native runner may ratchet above the T2 floor on the next poll.
        self.assertTrue(trade.t3_completed); self.assertGreaterEqual(trade.current_stop,11.5)

    def test_stop_gap_executes_at_later_bid_not_trigger_price(self):
        wallet=self.wallet_with_entry(2)
        wallet.observe(self.observation(2,9.))
        self.assertEqual(wallet.trades[0]['trade'].remaining_quantity,2)
        wallet.observe(self.observation(3,8.9,bid=8.88))
        self.assertAlmostEqual(wallet.cash,97.76)
        self.assertAlmostEqual(wallet.gross,-2.24)
        self.assertEqual(wallet.result(self.now+timedelta(seconds=3))['closed_trades'],1)

    def test_partial_exit_cumulative_average_and_fees_conserve_cash(self):
        self.bundle.fee_bps=.001; self.bundle.per_share_fee=.01
        wallet=self.wallet_with_entry(3)
        wallet.observe(self.observation(2,9.))
        wallet.observe(self.observation(3,9.,size=1))
        wallet.observe(self.observation(4,8.8,size=1))
        wallet.observe(self.observation(5,8.7,size=1))
        self.assertEqual(wallet.trades[0]['trade'].remaining_quantity,0)
        self.assertAlmostEqual(wallet.gross,9+8.8+8.7-30)
        self.assertAlmostEqual(wallet.cash,100+wallet.gross-wallet.fees)
        self.assertEqual(sum(r['quantity'] for r in wallet.fills if r['side']=='SELL'),3)

    def test_zero_size_and_missing_next_quote_cannot_create_fills(self):
        wallet=self.wallet_with_entry(1)
        wallet.observe(self.observation(2,9.))
        wallet.observe(self.observation(3,9.,size=0))
        report=wallet.result(self.now+timedelta(seconds=3))
        self.assertEqual(report['closed_trades'],0)
        self.assertEqual(report['pending_exits'],1)
        self.assertEqual(len(report['fills']),1)

    def test_end_position_is_marked_not_force_closed_and_stale_mark_is_unknown(self):
        wallet=self.wallet_with_entry(1)
        report=wallet.result(self.now+timedelta(seconds=1))
        self.assertEqual(report['open_trades'],1); self.assertEqual(report['net_realized'],0.)
        self.assertEqual(report['marked_equity'],100.)
        self.assertIsNone(wallet.result(self.now+timedelta(minutes=2))['marked_equity'])

    def test_pending_entries_reserve_cash_and_count_as_positions(self):
        wallet=Wallet(self.bundle); wallet.submit_entry(ready(quantity=8),self.now)
        wallet.submit_entry(ready('OTHER',quantity=8),self.now)
        self.assertEqual(list(wallet.entries),['TEST'])
        self.assertEqual(len(wallet.get_all_positions()),1)
        self.assertLess(wallet.get_account_snapshot()['cash'],20.)

    def test_native_risk_enforces_daily_loss_with_virtual_account(self):
        wallet=Wallet(self.bundle); wallet.gross=-5; wallet.cash=95
        f=FeatureSnapshot('TEST',price=10,data_quality_ok=True,data_is_stale=False,atr=.05)
        ai=AIAnalysis('TEST',SignalAction.BUY,95.,.9,.9,95.,0.)
        with patch('trading.risk_engine.get_alpaca_client',side_effect=AssertionError('no real broker')):
            risk=get_risk_engine().evaluate(f,ai,100,strategy_equity=95,recommended_risk_pct=1.,
                setup_grade='A',confirmed_entry_price=10,confirmed_stop_price=9.8,confirmation_approved=True,
                account_provider=lambda:wallet,position_reader=wallet.position_rows)
        self.assertFalse(risk.approved)
        self.assertIn('loss',risk.reason.lower())

    def test_researcher_only_reorders_same_batch_and_never_creates_candidates(self):
        raw=deepcopy(self.raw)
        raw['batches'][0]['reports'].insert(0,dict(symbol='OTHER',published_at=self.now.isoformat()))
        prior=deepcopy(raw['batches'][0]['snapshots']['TEST']); prior['features']['symbol']='OTHER'
        prior['features']['range_pct']=2
        raw['batches'][0]['snapshots']['OTHER']=prior
        b=Bundle(raw)
        class Reject:
            def __init__(self,*args): self.calls=[]
            def analyze(self,sym): return FinalTradeDecision(sym,DecisionState.REJECTED,False)
        a=run_arm(b,researcher=False,decision_factory=Reject)
        c=run_arm(b,researcher=True,decision_factory=Reject)
        self.assertEqual([r['symbol'] for r in a['schedule']],['OTHER','TEST'])
        self.assertEqual([r['symbol'] for r in c['schedule']],['TEST','OTHER'])
        self.assertEqual(a['fills'],[]); self.assertEqual(c['fills'],[])

    def test_priority_snapshots_cannot_look_ahead(self):
        prior=self.raw['batches'][0]['snapshots']['TEST']
        self.assertTrue(snapshot_match(prior,'TEST',self.now))
        prior['prepared_at']=self.now.isoformat()
        self.assertIsNone(snapshot_match(prior,'TEST',self.now))

    def test_boundary_rejects_unknown_native_io_instead_of_optional_fallback(self):
        native=NativeDecision(HistoricalData(self.bundle),Wallet(self.bundle),self.bundle.weights)
        with self.assertRaises(UnsupportedBoundary):
            native.observed('new_unknown_provider',lambda:1,(),{})

    def test_worker_ignores_parent_credentials_and_uses_temporary_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'must-not-be-opened.db'; db.write_bytes(b'UNCHANGED')
            env=dict(os.environ,JALWE_DATABASE_PATH=str(db),ALPACA_API_KEY='not-for-the-worker',
                     ALLOW_LIVE_TRADING='true')
            result=subprocess.run([sys.executable,'-m','research_lab.worker'],input=json.dumps(ready_fixture()),
                text=True,capture_output=True,env=env,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(db.read_bytes(),b'UNCHANGED')
            report=json.loads(result.stdout)
            self.assertFalse(report['execution_authority'])
            self.assertNotIn('not-for-the-worker',result.stdout+result.stderr)

    def test_selected_winners_and_missing_coverage_cannot_claim_comparison(self):
        for field,value in [('universe','SELECTED_WINNERS'),('coverage','UNKNOWN')]:
            raw=deepcopy(self.raw); raw['provenance'][field]=value
            result=compare(raw)
            self.assertFalse(result['evidence_complete'])
            self.assertIsNone(result['paired_marked_equity_difference'])


if __name__=='__main__': unittest.main()
