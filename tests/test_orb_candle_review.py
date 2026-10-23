import unittest

import pandas as pd

import prior_day_entry_study as study
import test_prior_day_entry_study as fixtures


class OrbCandleReviewTests(unittest.TestCase):
    def setUp(self):
        self.data=fixtures.PriorDayEntryTests().data()
        self.session=fixtures.SESSION

    def test_reverse_or_shuffled_transport_order_cannot_change_modelled_trade(self):
        expected=study.simulate(self.data,self.session)
        for candidate in (self.data.iloc[::-1],self.data.sample(frac=1,random_state=17)):
            before=candidate.copy(deep=True)
            self.assertEqual(study.simulate(candidate,self.session),expected)
            pd.testing.assert_frame_equal(candidate,before)

    def test_valid_numeric_strings_and_timezone_describe_the_same_trade(self):
        expected=study.simulate(self.data,self.session)
        self.assertEqual(study.simulate(self.data.astype(str),self.session),expected)
        local=self.data.copy(); local.index=local.index.tz_convert('America/New_York')
        self.assertEqual(study.simulate(local,self.session),expected)

    def test_off_grid_row_is_not_a_completed_minute_in_the_model(self):
        extra=self.data.iloc[[15]].copy()
        extra.index=extra.index+pd.Timedelta(seconds=30)
        extra.iloc[0]=[10.,11.,1.,10.,1000.]
        sample=pd.concat([self.data.iloc[:15],extra,self.data.iloc[15:]])
        self.assertEqual(study.simulate(sample,self.session),study.simulate(self.data,self.session))

    def test_boolean_price_or_volume_never_becomes_a_real_quote_or_trade(self):
        for column in ('open','high','low','close','volume'):
            data=self.data.astype(object)
            data.iloc[10,data.columns.get_loc(column)]=True
            value=study.simulate(data,self.session)
            self.assertEqual(value['state'],'UNKNOWN_DATA')
            self.assertEqual(value['scenarios'],{})

    def test_current_whole_session_policy_blocks_even_gaps_after_a_known_exit(self):
        completed=study.simulate(self.data,self.session)
        self.assertEqual(completed['state'],'SIMULATED_EXIT')
        self.assertLess(pd.Timestamp(completed['exit_at']),self.data.index[-1])
        for minute in (self.data.index[3],self.data.index[-1]):
            incomplete=study.simulate(self.data.drop(minute),self.session)
            self.assertEqual(incomplete['state'],'UNKNOWN_DATA')
            self.assertEqual(incomplete['scenarios'],{})

    def test_cancel_after_range_low_break_remains_the_original_strategy_policy(self):
        data=self.data.copy()
        data.iloc[15]=[10.,10.01,9.90,10.,100.]
        data.iloc[16]=[10.01,10.11,10.01,10.10,1000.]
        data.iloc[17]=[10.11,10.13,10.,10.11,500.]
        data.iloc[18]=[10.11,10.5,10.11,10.4,100.]
        result=study.simulate(data,self.session)
        self.assertEqual(result['state'],'INVALIDATED_BEFORE_ENTRY')
        self.assertEqual(result['scenarios'],{})
