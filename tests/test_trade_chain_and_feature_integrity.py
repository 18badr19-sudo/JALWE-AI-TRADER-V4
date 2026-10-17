import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from core.database import Database
from intelligence.feature_engine import FeatureEngine
from feature_integrity import audit_frame,status_text as indicator_status
import trade_chain_audit as audit


def bars(n=300):
    close=10+np.sin(np.arange(n)/7)*.2+np.arange(n)*.001
    return pd.DataFrame(dict(open=close-.01,high=close+.05,low=close-.06,close=close,
        volume=100+np.arange(n)%21),index=pd.date_range('2026-10-15T13:30Z',periods=n,freq='5min'))


class FeatureIntegrityTests(unittest.TestCase):
    def test_native_diagnostics_and_build_reject_malformed_bars(self):
        f=bars(); engine=FeatureEngine()
        bad=[pd.concat([f,f.iloc[-1:]]),f.assign(high=1),f.assign(low=20),
            f.assign(volume=np.inf),f.assign(volume=-1),f.assign(close=0),f.assign(open=np.nan)]
        invalid=f.copy(); invalid.index=list(f.index[:-1])+['not-a-time'];bad.append(invalid)
        duplicated_columns=pd.concat([f,f[['close']]],axis=1);bad.append(duplicated_columns)
        for sample in bad:
            self.assertTrue(engine.input_issues(sample))
            self.assertFalse(engine.build('TEST',sample).data_quality_ok)
            if not sample.columns.has_duplicates:
                self.assertFalse(engine.diagnose_input(sample)['data_quality_ok'])
        self.assertTrue(engine.build('TEST',f).data_quality_ok)
        zero=f.copy();zero.iloc[5,4]=0
        self.assertEqual(engine.input_issues(zero),[])

    def test_native_series_are_causal_and_input_is_not_mutated(self):
        f=bars(); before=f.copy(deep=True)
        r=audit_frame(f)
        self.assertEqual(r['state'],'OBSERVED_CHECKS_PASSED');self.assertEqual(r['failures'],[])
        self.assertIn('rsi_14',r['checked_columns']);self.assertIn('vwap',r['checked_columns'])
        self.assertEqual(r['checked_cutoffs'],3);self.assertFalse(r['execution_authority'])
        pd.testing.assert_frame_equal(f,before)
        self.assertTrue(any(v['absolute']>1e-10 for v in r['warmup'][0]['differences'].values()))
        self.assertIn('SENSITIVITY_ONLY',r['warmup_interpretation'])

    def test_auditor_detects_real_future_dependence_in_injected_bad_calculation(self):
        class Bad(FeatureEngine):
            def calculate_indicators(self,f):
                out=super().calculate_indicators(f)
                out['bad_global_mean']=f.close.mean()
                out['bad_next_close']=f.close.shift(-1)
                return out
        r=audit_frame(bars(),Bad())
        self.assertEqual(r['state'],'CAUSALITY_DIFFERENCE')
        self.assertEqual({f['column'] for f in r['failures']},{'bad_global_mean','bad_next_close'})

    def test_bad_or_short_input_cannot_claim_success(self):
        self.assertEqual(audit_frame(bars(51))['state'],'INSUFFICIENT_OR_OVERSIZED_INPUT')
        self.assertEqual(audit_frame(bars().assign(volume=np.inf))['state'],'INVALID_INPUT')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'absent.db';self.assertIn('ينتظر',indicator_status(p));self.assertFalse(p.exists())


class TradeChainTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'native.db';self.db=Database(str(self.path))
        self.trade=dict(trade_id='managed',symbol='TEST',entry_order_id='BUY1',entry_fill_price=10.,
            entry_price=10.,initial_quantity=10,remaining_quantity=0,initial_stop=9.,current_stop=10.,
            target_1=11.,target_2=12.,target_3=13.,stage='CLOSED',t1_completed=1,t2_completed=0,
            metadata_json='{}',created_at='2026-10-16T14:00:00Z',closed_at='2026-10-16T15:00:00Z')
        self.entry=dict(order_id='BUY1',symbol='TEST',side='BUY',quantity=10,filled_quantity=10,
            status='FILLED',filled_price=10.,submitted_at='2026-10-16T14:00:01Z',filled_at='2026-10-16T14:00:03Z')
        self.exit=dict(self.entry,order_id='SELL1',side='SELL',filled_price=9.8)
        self.ledger=[dict(event_key='SELL1:4',trade_id='managed',order_id='SELL1',symbol='TEST',
            action='TAKE_PROFIT_1',quantity=4,fill_price=11.,entry_price=10.,realized_pnl=4.,event_time='2026-10-16T14:01Z'),
            dict(event_key='SELL1:10',trade_id='managed',order_id='SELL1',symbol='TEST',action='PROTECTIVE_STOP',quantity=6,
            fill_price=9.,entry_price=10.,realized_pnl=-6.,event_time='2026-10-16T15:00Z')]
        self.decision=dict(timestamp='2026-10-16T14:00:04Z',symbol='TEST',jalwe=dict(metadata={'entry_quality':{'checked_at':'2026-10-16T14:00:00Z'}},state='READY_FOR_PAPER_EXECUTION',entry_price=9.99,reason='recorded entry'),
            execution=dict(managed_trade_id='managed',broker_order_id='BUY1',broker_order_submitted=True))
        self.seed()

    def insert(self,conn,table,row):
        conn.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))

    def seed(self):
        with self.db.connection() as conn:
            self.insert(conn,'managed_trades',self.trade)
            self.insert(conn,'broker_orders',self.entry);self.insert(conn,'broker_orders',self.exit)
            for row in self.ledger:self.insert(conn,'strategy_pnl_ledger',row)
            for p in [self.decision,dict(symbol='TEST',jalwe=dict(state='REJECTED',reason='later unrelated rejection'),execution={})]:
                self.insert(conn,'system_events',dict(event_type='APEX_RESEARCH_DECISION',severity='INFO',message='saved',metadata_json=json.dumps(p)))

    def test_exact_entry_identity_not_latest_symbol_and_partial_fill_pnl(self):
        before=self.path.read_bytes();r=audit.audit(self.path,'TEST');t=r['trades'][0]
        self.assertEqual(t['state'],'SAVED_EVIDENCE_CONSISTENT');self.assertTrue(t['ledger_complete'])
        self.assertEqual(t['realized_pnl_before_broker_fees'],-2.)
        self.assertEqual(t['ledger_exit_quantity'],10);self.assertEqual(t['exit_events'],2)
        self.assertEqual(t['decision_reason'],'recorded entry')
        self.assertAlmostEqual(t['fill_vs_decision_reference_bps'],(10-9.99)/9.99*10000)
        self.assertFalse(t['reference_deviation_is_measured_execution_cost'])
        self.assertEqual(t['timing'],{'entry_quote_check_to_submission_seconds':1.,'submission_to_fill_seconds':2.})
        self.assertEqual(t['initial_risk_dollars'],10.)
        self.assertEqual(before,self.path.read_bytes());self.assertFalse(r['execution_authority'])

    def test_mismatch_checks_do_not_guess_a_market_loss_cause(self):
        with self.db.connection() as conn:
            conn.execute("UPDATE strategy_pnl_ledger SET realized_pnl=-4 WHERE event_key='SELL1:10'")
            conn.execute("UPDATE broker_orders SET filled_quantity=9,filled_price=9.7 WHERE order_id='SELL1'")
        r=audit.audit(self.path);f=r['trades'][0]['findings']
        self.assertIn('EXIT_PNL_ARITHMETIC_DIFF',f);self.assertIn('EXIT_CUMULATIVE_QUANTITY_DIFF',f)
        self.assertIn('EXIT_AVERAGE_PRICE_SNAPSHOT_DIFF',f)
        self.assertIn('UNPROVEN',r['loss_cause'])

    def test_missing_snapshots_and_entry_reason_are_unknown(self):
        with self.db.connection() as conn:
            conn.execute('DELETE FROM system_events');conn.execute('DELETE FROM broker_orders')
        t=audit.audit(self.path)['trades'][0]
        self.assertEqual(t['state'],'INCOMPLETE_EVIDENCE')
        self.assertIn('EXACT_ENTRY_DECISION_LINK_MISSING',t['unknown'])
        self.assertIsNone(t['decision_reason']);self.assertEqual(t['realized_pnl_before_broker_fees'],-2.)

    def test_open_stop_floor_and_protection_are_saved_state_findings_only(self):
        t=copy.deepcopy(self.trade);t.update(remaining_quantity=5,stage='T2_HIT',t2_completed=1,current_stop=10.5)
        t['metadata_json']=json.dumps(dict(protective_stop_active=True,protective_stop_quantity=4,protective_stop_price=10.))
        result=audit.inspect_trade(t,self.entry,[],{},self.decision)
        self.assertIn('CURRENT_STOP_BELOW_RECORDED_COMPLETED_STAGE_FLOOR',result['findings'])
        self.assertIn('PROTECTION_SNAPSHOT_DIFF',result['findings'])
        self.assertIn('SAVED_SNAPSHOTS',result['scope'])

    def test_partial_evidence_and_wrong_identity_never_claim_a_reconciled_trade(self):
        with self.db.connection() as conn:
            conn.execute("DELETE FROM strategy_pnl_ledger WHERE event_key='SELL1:4'")
            conn.execute("UPDATE broker_orders SET side='SELL' WHERE order_id='BUY1'")
        t=audit.audit(self.path)['trades'][0]
        self.assertIn('LEDGER_POSITION_QUANTITY_DIFF',t['findings']);self.assertFalse(t['ledger_complete'])
        self.assertIn('ENTRY_ORDER_IDENTITY_MISMATCH',t['findings'])

    def test_text_prefilter_never_trusts_unrelated_or_malformed_json(self):
        with self.db.connection() as conn:
            self.insert(conn,'system_events',dict(event_type='APEX_RESEARCH_DECISION',severity='INFO',message='bad',metadata_json='{"managed": invalid'))
            p=copy.deepcopy(self.decision);p['execution']['broker_order_id']='OTHER'
            p['jalwe']['reason']='wrong entry order'
            self.insert(conn,'system_events',dict(event_type='APEX_RESEARCH_DECISION',severity='INFO',message='wrong',metadata_json=json.dumps(p,separators=(',',':'))))
        result=audit.audit(self.path)
        self.assertEqual(result['entry_link_read_state'],'COMPLETE_SEARCH')
        self.assertEqual(result['trades'][0]['decision_reason'],'recorded entry')

    def test_entry_link_budget_never_erases_independent_ledger_evidence(self):
        with self.db.connection() as conn:
            for i in range(500):
                self.insert(conn,'system_events',dict(event_type='APEX_RESEARCH_DECISION',severity='INFO',message='unrelated',metadata_json=json.dumps({'symbol':'OTHER','n':i})))
        with patch.object(audit.time,'monotonic',side_effect=[0.]+[20.]*10000):
            result=audit.audit(self.path)
        self.assertEqual(result['state'],'AUDITED')
        self.assertEqual(result['entry_link_read_state'],'READ_BUDGET_EXCEEDED')
        self.assertEqual(result['trades'][0]['realized_pnl_before_broker_fees'],-2.)
        self.assertTrue(result['trades'][0]['ledger_complete'])
        self.assertIn('EXACT_ENTRY_DECISION_LINK_MISSING',result['trades'][0]['unknown'])

    def test_symbol_injection_missing_database_and_commands_are_read_only(self):
        self.assertEqual(audit.audit(self.path,"X' OR 1=1")['state'],'INVALID_SYMBOL')
        absent=self.path.parent/'absent.db'
        self.assertEqual(audit.audit(absent)['state'],'DATABASE_UNAVAILABLE');self.assertFalse(absent.exists())
        import railway_controller as controller
        with patch.object(audit,'status_text',return_value='evidence') as show:
            self.assertEqual(controller.handle('/trade_audit TEST'),'evidence');show.assert_called_once_with('TEST')
        with patch('feature_integrity.status_text',return_value='indicators'):
            self.assertEqual(controller.handle('/indicator_audit'),'indicators')
        text=audit.status_text('TEST',self.path)
        self.assertIn('$-2.00',text);self.assertIn('الرسوم غير متاحة',text)


if __name__=='__main__':unittest.main()
