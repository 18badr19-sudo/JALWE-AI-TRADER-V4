"""Research-only regression: follow native T1 policy without claiming broker fills."""
import json
import unittest
import pandas as pd
from research_variants import runner_stops,RUNNER_STOP_PROTOCOL
from strategy_experiments import simulate
import test_strategy_experiments as fixtures


def bars(rows):
    return pd.DataFrame(rows,columns=['open','high','low','close','volume'],
        index=pd.date_range('2026-10-21T14:00Z',periods=len(rows),freq='min'))


class RunnerPolicyTests(unittest.TestCase):
    def test_t1_touch_with_lows_above_entry_remains_measurable(self):
        frame=bars([[11.8,12.2,11.6,12.1,100],[12.1,12.3,11.7,12,100]])
        r=runner_stops(frame,10.,9.,entry_fill=10.)
        self.assertEqual(r['protocol'],RUNNER_STOP_PROTOCOL)
        for rule in r['rules'].values():
            self.assertEqual(rule['outcome'],'TIME_EXIT')
            self.assertFalse(rule['reached_t2'])
            self.assertAlmostEqual(rule['net_r'],12*.999-10)

    def test_t1_then_gap_below_entry_uses_gap_execution_and_cost(self):
        frame=bars([[11.8,12.2,11.6,12.1,100],[9.8,10.3,9.5,10.1,100]])
        for rule in runner_stops(frame,10.,9.,entry_fill=10.1)['rules'].values():
            self.assertEqual(rule['outcome'],'STOP')
            self.assertAlmostEqual(rule['net_r'],9.8*.999-10.1)

    def test_raised_t2_floor_never_falls_back_to_entry_on_a_later_bar(self):
        frame=bars([[12.2,12.6,12.1,12.4,100],[13.2,13.4,13.1,13.3,100],
            [12.5,12.7,12.4,12.6,100],[12.2,12.3,11.8,12,100]])
        r=runner_stops(frame,10.,9.,entry_fill=10.2)
        self.assertAlmostEqual(r['rules']['T1']['net_r'],12*.999-10.2)
        self.assertEqual(r['rules']['T1']['outcome'],'STOP')
        self.assertAlmostEqual(r['rules']['T2']['net_r'],12.5*.999-10.2)
        self.assertFalse(r['auto_apply'])

    def test_same_bar_entry_floor_touch_stays_ambiguous(self):
        frame=bars([[11.,12.2,9.9,12.,100]])
        for rule in runner_stops(frame,10.,9.)['rules'].values():
            self.assertEqual(rule['outcome'],'AMBIGUOUS_RAISED_STOP')
            self.assertIsNone(rule['net_r'])

    def test_initial_stop_and_target_same_bar_remains_unknown(self):
        frame=bars([[10.,13.2,8.9,12.,100]])
        for rule in runner_stops(frame,10.,9.)['rules'].values():
            self.assertEqual(rule['outcome'],'AMBIGUOUS_STOP_TARGET')
            self.assertIsNone(rule['net_r'])


class RunnerProtocolTests(unittest.TestCase):
    setUp=fixtures.StrategyExperimentTests.setUp
    payload=fixtures.StrategyExperimentTests.payload
    analysis=fixtures.StrategyExperimentTests.analysis
    bars=fixtures.StrategyExperimentTests.bars

    def test_new_protocol_is_frozen_and_baseline_result_is_unchanged(self):
        self.memory.record(self.payload(),'new-runner-policy')
        with self.db.connection() as c:
            row=c.execute("SELECT * FROM strategy_trials WHERE strategy='OPENING_RANGE_BREAKOUT'").fetchone()
            self.assertEqual(json.loads(row['candidate_json'])['runner_stop_protocol'],RUNNER_STOP_PROTOCOL)
            self.engine.observe(c,row['decision_key'],self.bars(),'COMPLETE')
            result=json.loads(c.execute('SELECT result_json FROM strategy_trials WHERE trial_key=?',(row['trial_key'],)).fetchone()[0])
            self.assertEqual(result['runner_stop_comparison']['protocol'],RUNNER_STOP_PROTOCOL)
            self.assertEqual(result['net_r'],simulate(self.bars(),10.,9.)['net_r'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM strategy_preferences').fetchone()[0],0)

    def test_legacy_unfrozen_pending_candidate_does_not_get_a_new_protocol(self):
        self.memory.record(self.payload(),'legacy-runner-policy')
        with self.db.connection() as c:
            rows=c.execute('SELECT * FROM strategy_trials').fetchall()
            for row in rows:
                candidate=json.loads(row['candidate_json']);candidate.pop('runner_stop_protocol')
                c.execute('UPDATE strategy_trials SET candidate_json=? WHERE trial_key=?',(json.dumps(candidate),row['trial_key']))
            self.engine.observe(c,rows[0]['decision_key'],self.bars(),'COMPLETE')
            for row in c.execute('SELECT result_json FROM strategy_trials'):
                self.assertNotIn('runner_stop_comparison',json.loads(row[0]))

    def test_legacy_saved_runner_result_is_excluded_from_new_summary(self):
        self.memory.record(self.payload(),'legacy-saved-runner')
        with self.db.connection() as c:
            row=c.execute('SELECT * FROM strategy_trials LIMIT 1').fetchone()
            result={'runner_stop_comparison':{'protocol':'old','rules':{
                'T1':{'reached_t2':True,'net_r':-100.},'T2':{'reached_t2':True,'net_r':100.}}}}
            c.execute("UPDATE strategy_trials SET result_json=?,status='COMPLETE' WHERE trial_key=?",(json.dumps(result),row['trial_key']))
        text=self.engine.status_text()
        self.assertIn('مقارنات وقف قديمة مستبعدة: 1',text)
        self.assertIn('لا توجد مقارنة مكتملة بعد',text)
        self.assertNotIn('+200.000R',text)
