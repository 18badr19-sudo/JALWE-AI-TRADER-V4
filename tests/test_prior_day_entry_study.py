import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch

import prior_day_entry_study as study
from market_move_research import MoveStore,utc
from market_jump_requests import schedule_studies
from test_prior_day_bus import packet,SESSION
import test_market_move_research as fixtures


class PriorDayEntryTests(unittest.TestCase):
    def data(self):
        data=fixtures.frame(SESSION)
        data.loc[:,:]=[10.,10.01,9.99,10.,100.]
        data.iloc[:15]=[10.,10.05,9.95,10.,100.]
        data.iloc[15]=[10.01,10.11,10.01,10.10,1000.]
        data.iloc[16]=[10.11,10.13,10.,10.11,500.]
        data.iloc[17]=[10.11,10.5,10.11,10.4,100.]
        return data

    def test_closed_signal_next_open_and_cost_stress_not_actual_profit(self):
        data=self.data(); r=study.simulate(data,SESSION)
        self.assertEqual(r['state'],'SIMULATED_EXIT'); self.assertEqual(r['outcome'],'TARGET')
        self.assertEqual(r['signal_at'],data.index[15].isoformat())
        self.assertEqual(r['entry_at'],data.index[16].isoformat())
        self.assertEqual(r['signal_confirmed_at'],r['entry_at'])
        self.assertEqual(r['stop'],9.95)
        self.assertLess(r['scenarios']['25']['net_r'],r['scenarios']['10']['net_r'])
        self.assertLess(r['scenarios']['10']['net_r'],2.)
        self.assertFalse(r['execution_authority']); self.assertIn('NOT_MEASURED',r['cost_model'])
        json.dumps(r,allow_nan=False)

    def test_missing_duplicate_and_bad_bars_never_get_modelled_profit(self):
        data=self.data()
        import pandas as pd
        samples=[data.drop(data.index[-1]),pd.concat([data,data.iloc[:1]])]
        bad=data.copy(); bad.iloc[5,4]=float('nan'); samples.append(bad)
        for sample in samples:
            r=study.simulate(sample,SESSION)
            self.assertEqual(r['state'],'UNKNOWN_DATA'); self.assertEqual(r['scenarios'],{})

    def test_stop_target_same_minute_is_unknown_and_gap_uses_worse_open(self):
        data=self.data(); data.iloc[17]=[10.11,10.5,9.8,10.4,100.]
        r=study.simulate(data,SESSION)
        self.assertEqual(r['state'],'AMBIGUOUS_STOP_TARGET'); self.assertEqual(r['scenarios'],{})
        data.iloc[17]=[9.8,9.9,9.7,9.8,100.]
        r=study.simulate(data,SESSION)
        self.assertEqual(r['outcome'],'STOP_GAP'); self.assertEqual(r['modelled_exit'],9.8)
        self.assertLess(r['scenarios']['10']['net_r'],-1.)

    def test_chase_or_stop_gap_cannot_fake_a_fill(self):
        data=self.data(); data.iloc[16]=[11.,11.1,10.8,11.,100.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'NO_FILL_CHASE_LIMIT')
        data.iloc[16]=[9.8,10.,9.7,9.9,100.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'NO_FILL_STOP_GAP')
        data.iloc[16]=[10.,10.1,9.99,10.,100.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'NO_FILL_TRIGGER_LOST')

    def test_target_at_open_precedes_a_later_same_bar_stop(self):
        data=self.data(); data.iloc[17]=[10.6,10.7,9.8,10.,100.]
        result=study.simulate(data,SESSION)
        self.assertEqual(result['outcome'],'TARGET_AT_OPEN')
        self.assertEqual(result['modelled_exit'],result['target'])

    def test_stop_invalidation_no_signal_and_last_minute_signal(self):
        data=self.data(); data.iloc[15:]=[10.,10.01,9.99,10.,100.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'NO_ENTRY')
        data.iloc[-1]=[10.01,10.11,10.01,10.10,1000.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'NO_NEXT_BAR')
        data.iloc[15]=[10.,10.01,9.9,10.,100.]
        self.assertEqual(study.simulate(data,SESSION)['state'],'INVALIDATED_BEFORE_ENTRY')

    def test_future_prices_cannot_change_signal_or_opening_range(self):
        data=self.data(); first=study.simulate(data,SESSION)
        changed=data.copy(); changed.iloc[18:]=[90.,91.,89.,90.,100.]
        second=study.simulate(changed,SESSION)
        self.assertEqual(first,second)

    def test_queue_is_preopen_immutable_and_original_cohorts_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MoveStore(Path(directory)/'move.db'); p=packet(); signal=p['signals'][0]
            f=dict(signal['features'],symbol=signal['symbol'])
            store.save_pattern_features([f],SESSION,utc(signal['prepared_at']))
            with patch.object(study,'publish',return_value={'state':'PUBLISHED'}):
                result=study.prepare(store,utc('2026-10-17T05:00Z'),paper=True)
                self.assertEqual(result['queued'],1)
                self.assertEqual(study.prepare(store,utc('2026-10-17T06:00Z'),paper=True)['queued'],0)
            rows=store.episodes(); self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['role'],'ENTRY_STUDY'); self.assertFalse(rows[0]['prospective'])
            self.assertIsNone(store.state('frozen:'+SESSION['date']))
            with patch.object(study,'publish') as publish:
                study.prepare(store,utc(SESSION['open']),paper=True)
                publish.assert_not_called()

    def test_entry_batch_crossing_open_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MoveStore(Path(directory)/'move.db'); signal=packet()['signals'][0]
            store.save_pattern_features([dict(signal['features'],symbol=signal['symbol'])],SESSION,utc(signal['prepared_at']))
            op=utc(SESSION['open']); from datetime import timedelta
            clock=Mock(side_effect=[op-timedelta(seconds=2),op-timedelta(seconds=1),op])
            with patch.object(study,'publish') as publish:
                study.prepare(store,utc('2026-10-17T05:00Z'),paper=True,clock=clock)
                publish.assert_not_called()
            self.assertEqual(store.episodes(),[])

    def test_scheduler_reserves_four_without_duplicates_or_extra_budget(self):
        def row(i,role,prospective=False):
            return dict(episode_key=str(i),symbol='TEST'+str(i),role=role,prospective=prospective,session_day='2026-10-19',
                attempts=0,status='PENDING',features={},frozen_at='2026-10-17T05:00Z')
        rows=[row(i,'ACTIVITY',True) for i in range(16)]
        rows += [row(i,'ENTRY_STUDY') for i in range(16,26)]
        rows += [row(i,'JUMP_CASE') for i in range(26,126)]
        selected=schedule_studies(rows,set(),limit=48)
        self.assertEqual(len(selected),48); self.assertEqual(len({r['episode_key'] for r in selected}),48)
        self.assertEqual(sum(r['role']=='ENTRY_STUDY' for r in selected),4)
        self.assertTrue(all(r['prospective'] for r in selected[:16]))

    def test_native_reject_and_submitted_order_are_distinct_from_simulation(self):
        p=packet(); proof=dict(protocol=p['protocol'],signal=p['signals'][0],session=SESSION,scan_id='scan')
        research=NS(symbol='MATCH',metadata={'prior_day_signal':proof},created_at='report')
        payload=dict(symbol='MATCH',timestamp='decision1',jalwe={'state':'REJECTED','reason':'QUALITY'},execution={})
        with patch.object(study,'events'):
            rows=study.trace_decision(research,payload)
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['state'],'REJECTED')
            payload['execution']={'broker_order_submitted':True,'broker_order_id':'order','managed_trade_id':'trade','orchestrator_state':'PENDING'}
            rows=study.trace_decision(research,payload)
            self.assertEqual(rows[-1]['trace_key'],'order')
            self.assertEqual(rows[-1]['reason'],'PAPER_SUBMISSION_NOT_FILL_CONFIRMATION')

    def test_native_ledger_only_maps_confirmed_related_trade_and_never_writes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE strategy_pnl_ledger(event_key TEXT,trade_id TEXT,order_id TEXT,symbol TEXT,quantity INTEGER,fill_price REAL,realized_pnl REAL,event_time TEXT)')
                conn.executemany('INSERT INTO strategy_pnl_ledger VALUES (?,?,?,?,?,?,?,?)',[
                    ('e1','native','exit','MATCH',1,11.,1.,'2026-10-19T15:00:00Z'),
                    ('e2','unrelated','other','OTHER',1,15.,100.,'2026-10-19T15:00:00Z')])
            before=path.read_bytes()
            rows=study.native_ledger_rows([dict(managed_trade_id='native',signal_id='fixed')]*2,path)
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['realized_pnl_before_broker_fees'],1.)
            self.assertEqual(rows[0]['reason'],'NATIVE_BROKER_RECONCILED_LEDGER')
            self.assertEqual(path.read_bytes(),before)

    def test_partial_results_cannot_be_counted_as_simulated_exits(self):
        result=study.simulate(self.data(),SESSION)
        row=dict(role='ENTRY_STUDY',status='PARTIAL_DATA',features={'entry_protocol':study.PROTOCOL},result={'entry_study':result})
        summary=study.summary([row])
        self.assertEqual(summary['states'],{'UNKNOWN_DATA':1})
        self.assertEqual(summary['costs']['10']['simulated_exits'],0)
