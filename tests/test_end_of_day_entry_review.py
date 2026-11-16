import copy
import unittest
from datetime import timedelta
import pandas as pd
from market_move_research import utc
import end_of_day_entry_review as review

SESSION = dict(date='2026-11-13', open='2026-11-13T14:30:00+00:00',
               close='2026-11-13T15:00:00+00:00')


class EndOfDayReviewTests(unittest.TestCase):
    def frame(self):
        index = pd.date_range(SESSION['open'], periods=30, freq='min')
        data = pd.DataFrame([[10.,10.01,9.99,10.,100.]]*30,
            index=index, columns=['open','high','low','close','volume'])
        data.iloc[:15] = [10.,10.05,9.95,10.,100.]
        data.iloc[15] = [10.01,10.11,10.01,10.10,1000.]
        data.iloc[16] = [10.11,10.13,10.,10.11,500.]
        data.iloc[17] = [10.11,10.5,10.11,10.4,100.]
        return data

    def evaluate(self, data=None):
        return review.evaluate(self.frame() if data is None else data, SESSION,
                               utc(SESSION['close'])+timedelta(minutes=16))

    def row(self, role='JUMP_CASE', symbol='ABC'):
        return dict(symbol=symbol,session_day=SESSION['date'],session=SESSION,
            role=role,prospective=True,frozen_at='2026-11-13T14:00:00+00:00',
            status='COMPLETE',result=dict(end_of_day_entry_review=self.evaluate()))

    def test_close_plus_delay_and_early_close(self):
        r=review.evaluate(self.frame(),SESSION,utc(SESSION['close'])+timedelta(minutes=15))
        self.assertEqual(r['state'],'WAITING_SESSION_DATA')
        self.assertEqual(self.evaluate()['state'],'SIMULATED_EXIT')

    def test_closed_signal_and_costs(self):
        r=self.evaluate()['simulation']
        self.assertEqual(r['signal_confirmed_at'],r['entry_at'])
        self.assertEqual(r['outcome'],'TARGET')
        self.assertLess(r['scenarios']['25']['net_r'],r['scenarios']['10']['net_r'])
        self.assertFalse(r['execution_authority'])

    def test_gaps_remain_unknown(self):
        r=self.evaluate(self.frame().drop(self.frame().index[20]))
        self.assertEqual(r['state'],'UNKNOWN_DATA')
        self.assertEqual(r['simulation']['scenarios'],{})

    def test_same_minute_stop_target_ambiguous(self):
        data=self.frame();data.iloc[16]=[10.11,10.5,9.9,10.1,500.]
        self.assertEqual(self.evaluate(data)['state'],'AMBIGUOUS_STOP_TARGET')

    def test_separate_cohorts_and_session(self):
        rows=[self.row(role) for role in ('JUMP_CASE','ACTIVITY','CONTROL')]
        other=self.row(symbol='OLD');other['session_day']='2026-11-12';rows.append(other)
        s=review.summary(rows,SESSION['date'])
        self.assertEqual([g['total'] for g in s['groups'].values()],[1,1,1])
        self.assertFalse(s['automatic_application'])

    def test_late_frozen_and_duplicates_excluded(self):
        late=self.row('ACTIVITY');late['frozen_at']=SESSION['open']
        row=self.row()
        s=review.summary([late,row,copy.deepcopy(row)],SESSION['date'])
        self.assertEqual(s['excluded_duplicate_identities'],1)
        self.assertEqual(sum(g['total'] for g in s['groups'].values()),0)

    def test_old_and_failed_data_not_zero_returns(self):
        old=self.row();old['result']={}
        bad=self.row(symbol='BAD');bad['status']='PARTIAL_DATA'
        g=review.summary([old,bad],SESSION['date'])['groups']['RETROSPECTIVE_JUMPS']
        self.assertEqual(g['states'],dict(NOT_EVALUATED=1,UNKNOWN_DATA=1))
        self.assertIsNone(g['costs']['25']['mean_net_r'])

    def test_nonfinite_return_excluded(self):
        row=self.row();row['result']['end_of_day_entry_review']['simulation']['scenarios']['25']['net_r']=float('nan')
        s=review.summary([row],SESSION['date'])
        self.assertEqual(s['groups']['RETROSPECTIVE_JUMPS']['costs']['25']['missing'],1)
        self.assertIn('محاكاة ORB',' '.join(review.report_lines(s)))


if __name__ == '__main__':
    unittest.main()
