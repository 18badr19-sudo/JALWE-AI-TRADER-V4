import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

import decision_explanations as explanations
import sma_crossover_study as study
from market_move_research import MoveStore


def history():
    prices=[100.]*50+[101.+i*2 for i in range(30)]+[160.-i*4 for i in range(30)]
    days=[(datetime(2026,1,1)+timedelta(days=i)).date().isoformat() for i in range(len(prices))]
    sessions=[dict(date=d) for d in days]
    rows=[dict(t=d+'T05:00:00Z',o=p+.2,h=p+1,l=p-1,c=p,v=10000) for d,p in zip(days,prices)]
    return rows,sessions


class SMAStudyTests(unittest.TestCase):
    def test_cross_is_closed_and_entry_is_next_open_costs_and_matched_benchmark(self):
        rows,sessions=history(); r=study.simulate(rows,sessions)
        self.assertEqual(r['state'],'SIMULATED'); self.assertFalse(r['execution_authority'])
        low,high=r['scenarios']['10'],r['scenarios']['25']
        self.assertEqual(low['entries'][0]['signal_day'],sessions[50]['date'])
        self.assertEqual(low['entries'][0]['entry_day'],sessions[51]['date'])
        self.assertEqual(low['entries'][0]['price'],rows[51]['o'])
        self.assertEqual(low['closed_trades'],1)
        self.assertLess(high['marked_equity'],low['marked_equity'])
        self.assertAlmostEqual(low['buy_hold_marked_return_pct'],100/(rows[51]['o']*1.001)*rows[-1]['c']-100)
        changed=copy.deepcopy(rows)
        for b in changed[70:]: b.update(o=300,h=301,l=299,c=300)
        other=study.simulate(changed,sessions)
        self.assertEqual(other['scenarios']['10']['entries'][0],low['entries'][0])

    def test_missing_duplicate_invalid_and_short_history_never_gain(self):
        rows,sessions=history()
        for data,state in [(rows[:-1],'INCOMPLETE_DAILY_DATA'),(rows+rows[:1],'INVALID_DAILY_DATA')]:
            r=study.simulate(data,sessions); self.assertEqual(r['state'],state); self.assertNotIn('scenarios',r)
        for value in [float('nan'),-1,True]:
            bad=copy.deepcopy(rows); bad[0]['c']=value
            self.assertEqual(study.simulate(bad,sessions)['state'],'INVALID_DAILY_DATA')
        self.assertEqual(study.simulate(rows[:30],sessions[:30])['state'],'INSUFFICIENT_HISTORY')

    def test_open_position_is_marked_not_closed_profit(self):
        rows,sessions=history(); r=study.simulate(rows[:75],sessions[:75])
        s=r['scenarios']['10']; self.assertTrue(s['open_position'])
        self.assertEqual(s['closed_trades'],0); self.assertEqual(s['closed_trade_pnl'],0)
        self.assertGreater(s['marked_equity'],100)

    def test_refresh_is_paced_read_only_and_status_uses_same_saved_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'move.db'; store=MoveStore(path); provider=Mock()
            rows,sessions=history()
            provider.get.return_value=[dict(date=s['date'],close='16:00') for s in sessions]
            provider.bars.return_value={'SPY':rows}
            now=datetime(2026,10,17,tzinfo=timezone.utc)
            r=study.refresh(store,provider,now)
            self.assertEqual(r['state'],'SIMULATED')
            self.assertEqual(study.refresh(store,provider,now),r)
            provider.get.assert_called_once(); provider.bars.assert_called_once()
            self.assertEqual(provider.bars.call_args.args[0],['SPY'])
            before=path.read_bytes(); self.assertIn('SIMULATED',study.status_text(path)); self.assertEqual(before,path.read_bytes())
            self.assertIn('سهم واحد',study.status_text(path))

    def test_provider_failures_are_unknown_and_back_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'move.db'); provider=Mock(); provider.get.side_effect=RuntimeError('secret')
            now=datetime(2026,10,17,tzinfo=timezone.utc)
            r=study.refresh(store,provider,now); self.assertEqual(r['state'],'DATA_UNAVAILABLE')
            self.assertNotIn('secret',json.dumps(r)); study.refresh(store,provider,now+timedelta(minutes=10))
            provider.get.assert_called_once()


class ExplanationTests(unittest.TestCase):
    def test_saved_evidence_not_invented_causes_and_fills_are_correlated_by_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'native.db'
            p=dict(symbol='SNAP',jalwe=dict(state='READY_FOR_PAPER_EXECUTION',reason='all recorded gates passed'),
                execution=dict(broker_order_submitted=True,managed_trade_id='own'))
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE system_events(id INTEGER PRIMARY KEY,event_type TEXT,metadata_json TEXT,created_at TEXT)')
                conn.execute('INSERT INTO system_events VALUES(1,?,?,?)',('APEX_RESEARCH_DECISION',json.dumps(p),'2026-10-12T14:00:00Z'))
                conn.execute('CREATE TABLE strategy_pnl_ledger(order_id TEXT,trade_id TEXT,symbol TEXT,action TEXT,quantity INTEGER,fill_price REAL,realized_pnl REAL,event_time TEXT)')
                conn.executemany('INSERT INTO strategy_pnl_ledger VALUES(?,?,?,?,?,?,?,?)',[
                    ('order-1','own','SNAP','PROTECTIVE_STOP',1,6.4,.2,'2026-10-12T14:01Z'),('order-2','other','SNAP','RECOVERED_EXIT',1,7,100,'2026-10-12T14:02Z')])
            before=path.read_bytes(); text=explanations.explain('SNAP',path)
            self.assertIn('$+0.20',text); self.assertNotIn('100.00',text)
            self.assertIn('السبب المسجل',text); self.assertIn('لا يثبت سبب الخسارة',text)
            self.assertEqual(before,path.read_bytes())
            self.assertIn('لا يوجد قرار',explanations.explain('DRH',path))
            self.assertIn('/explain SNAP',explanations.explain("SNAP' OR 1=1",path))

    def test_controller_routes_read_commands_without_execution(self):
        import railway_controller as controller
        with patch.object(explanations,'explain',return_value='saved reason') as reader:
            self.assertEqual(controller.handle('/explain SNAP'),'saved reason')
            reader.assert_called_once_with('SNAP')
        with patch.object(study,'status_text',return_value='study result'):
            self.assertEqual(controller.handle('/sma_study'),'study result')

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'missing.db'
            self.assertIn('لا توجد',explanations.explain('SNAP',path)); self.assertFalse(path.exists())



class AIScoreExplanationTests(unittest.TestCase):
    def test_saved_factor_evidence_is_shown_without_probability_or_old_score_reconstruction(self):
        from decision_explanations import ai_score_details
        self.assertIn('غير محفوظ', '\n'.join(ai_score_details({})))
        decision={'metadata':{'ai_score_evidence':{
            'minimum_buy_score':74, 'factor_breakdown':[
                {'factor':'liquidity','side':'bullish','weighted_points':12},
                {'factor':'vwap','side':'bullish','weighted_points':14},
                {'factor':'vwap','side':'bullish','weighted_points':4},
                {'factor':'news','side':'bearish','weighted_points':8},
                {'factor':'fake_order','side':'bullish','weighted_points':1000},
                {'factor':'momentum','side':'bullish','weighted_points':float('nan')}],
            'invalid_inputs':['rsi_14']}}}
        text='\n'.join(ai_score_details(decision))
        self.assertIn('السيولة 12.00',text);self.assertIn('VWAP 18.00',text)
        self.assertIn('درجة الأخبار 8.00',text);self.assertIn('74.00',text)
        self.assertIn('rsi_14',text);self.assertNotIn('fake_order',text)
        self.assertNotIn('nan',text);self.assertIn('ليست احتمال ربح',text)


if __name__=='__main__': unittest.main()
