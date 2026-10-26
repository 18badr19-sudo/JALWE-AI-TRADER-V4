import unittest
from market_jump_common import summarize_common, PROTOCOL
from market_research_findings import findings

class CommonTriplesTests(unittest.TestCase):
    def test_unknown_not_absent_and_triples_retained(self):
        rows=[]
        for i in range(3):
            row={'symbol':f'S{i}', 'session_day':'2026-10-22','result':{'jump_path':{'observed_rise_pct':30}}}
            factors=[{'code':c,'state':'PRESENT'} for c in ('PRIOR_DAY_RANGE','PRIOR_DAY_VOLUME','PRIOR_DAY_MOMENTUM')]
            if i==2: factors.pop()
            rows.append((row,{'common_protocol':PROTOCOL,'prior_day_factors':factors}))
        result=summarize_common(rows,{})
        group=result['large_jump_groups'][0]
        triple=next(r for r in group['triple_cooccurrences'] if len(r['codes'])==3)
        self.assertEqual((triple['known'],triple['present'],triple['unknown']),(2,2,1))
        self.assertFalse(result['execution_authority'])

    def test_minute_factors_visible_without_claiming_prediction(self):
        result=findings({'historical_jump_profiles':{'common_factors':{'all_jumps':{'cases':3,'min_rise_pct':8,'factors':[{'codes':['VOLUME_BUILDUP'],'known':2,'present':1,'unknown':1}]}}}})
        self.assertEqual(result['selected_jump_group']['factors'][0]['prevalence_pct'],50)
        self.assertFalse(result['automatic_application'])

    def test_controls_missing_not_zero_and_invalid_counts_rejected(self):
        from market_research_findings import controlled_factor_rows
        self.assertEqual(controlled_factor_rows({}),[])
        p={'factor_benchmark':{'retrospective':{'all_days':{'rules':{'RANGE8':{'pairs':{'complete':10,'matching_reached':6,'control_reached':2}}}}}}}
        rows=controlled_factor_rows(p)
        self.assertEqual(rows[0]['difference_pp'],40)
        self.assertFalse(rows[0]['execution_authority'])
        p['factor_benchmark']['retrospective']['all_days']['rules']['RANGE8']['pairs']['complete']=True
        self.assertEqual(controlled_factor_rows(p),[])

    def test_research_price_boundaries_and_control_separation(self):
        import market_factor_benchmark as b
        def f(price):
            return dict(symbol='TEST',close=price,dollar_volume=200000,source_feed='sip',range_pct=9.,volume_ratio_previous_day=3.,daily_return_pct=4.)
        for price in (.1,.49,.5,1.,15.,100.):
            self.assertIsNotNone(b.record(f(price)))
        for price in (.099,100.01,True,float('nan'),float('inf')):
            self.assertIsNone(b.record(f(price)))
        self.assertNotEqual(b.record(f(.49))['group'][0],b.record(f(.5))['group'][0])
        self.assertNotEqual(b.PREFIX,'daily_factor_cohort_v1:')
        self.assertNotEqual(b.DEFINITION_KEY,'daily_factor_definition')
