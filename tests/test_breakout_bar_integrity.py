import unittest
import pandas as pd
from intelligence.breakout_confirmation_engine import BreakoutConfirmationEngine
from intelligence.trigger_engine import TriggerDecision, TriggerState

class BreakoutIntegrityTests(unittest.TestCase):
    def bars(self):
        f=pd.DataFrame(dict(open=[9.99]*25,high=[10.025]*25,low=[9.985]*25,
            close=[9.995]*25,volume=[100.]*25),index=pd.date_range('2026-10-27T13:30Z',periods=25,freq='5min'))
        f.iloc[-1]=[9.99,10.025,9.985,10.02,200.]
        return f
    def evaluate(self,f):
        return BreakoutConfirmationEngine().evaluate(TriggerDecision('TEST',TriggerState.ENTRY_CONFIRMED,True,10.02,10.,9.8),f)
    def test_corrupt_latest_does_not_reuse_previous_confirming_candle(self):
        f=self.bars();bad=f.iloc[-1:].copy();bad.index+=pd.Timedelta(minutes=5);bad['close']=float('nan')
        self.assertFalse(self.evaluate(pd.concat([f,bad])).approved_for_entry)
    def test_invalid_ohlcv_duplicates_and_timestamp_block(self):
        for col,value in [('high',9.9),('volume',float('inf')),('close',True),('low',-1)]:
            with self.subTest(col=col,value=value):
                f=self.bars();f[col]=f[col].astype(object);f.iloc[-1,f.columns.get_loc(col)]=value
                self.assertFalse(self.evaluate(f).approved_for_entry)
        f=self.bars();self.assertFalse(self.evaluate(pd.concat([f,f.iloc[-1:]])).approved_for_entry)
        f=self.bars();f.index=list(f.index[:-1])+['bad'];self.assertFalse(self.evaluate(f).approved_for_entry)
    def test_valid_sorted_and_reversed_frames_agree(self):
        f=self.bars();a=self.evaluate(f);self.assertTrue(a.approved_for_entry)
        self.assertEqual(a,self.evaluate(f.iloc[::-1]))
