import unittest
from core.decision_diagnostics import saudi_time, decision_details


class DecisionDiagnosticsTests(unittest.TestCase):
    def test_sql_utc_and_aware_timestamps_display_explicit_saudi_time(self):
        for value in ('2026-10-14 18:28:07','2026-10-14T18:28:07Z','2026-10-14T14:28:07-04:00'):
            self.assertEqual(saudi_time(value),'2026-10-14 21:28 السعودية')
        self.assertEqual(saudi_time('invalid'),'وقت غير متاح')

    def test_rtac_age_and_tv_router_threshold_use_saved_evidence(self):
        rtac={'metadata':{'feature_diagnostics':{'latest_bar_age_minutes':38.128,'max_bar_age_minutes':15}}}
        self.assertIn('38.1 دقيقة | الحد 15','\n'.join(decision_details(rtac)))
        tv={'reason':'Market regime / strategy router rejected the setup.','market_regime':'SIDEWAYS',
            'metadata':{'strategy_router_evidence':{'score':75.,'minimum_score':82.,'approved':False}}}
        text='\n'.join(decision_details(tv))
        self.assertIn('درجة 75.00 | المطلوب 82',text);self.assertIn('SIDEWAYS',text)

    def test_legacy_warning_is_used_without_guessing_new_thresholds(self):
        old={'reason':'Market regime / strategy router rejected the setup.',
             'warnings':['Best strategy did not meet regime minimum score 82.']}
        self.assertIn('الحد المسجل 82','\n'.join(decision_details(old)))
        old['warnings']=[]
        self.assertEqual(decision_details(old),[])

    def test_invalid_numeric_evidence_does_not_create_a_claim(self):
        d={'metadata':{'strategy_router_evidence':{'approved':False,'score':float('nan'),'minimum_score':82}}}
        self.assertEqual(decision_details(d),[])
