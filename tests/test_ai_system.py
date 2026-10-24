from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

import ai_system
from intelligence.shadow_advisor import ShadowAdvisor
from test_gemini_shadow import config
from test_shadow_advisor import NOW,payload,review
from test_research_model_lab import days
from intelligence.research_model_lab import evaluate


class UnifiedAITests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.native=self.root/'native.db';self.research=self.root/'research.db';self.shadow=self.root/'shadow.db'
        self.now=datetime.fromtimestamp(NOW,timezone.utc)
        self.kw=dict(native_path=self.native,research_path=self.research,shadow_path=self.shadow,now=self.now)

    def native_decision(self,p):
        with sqlite3.connect(self.native) as c:
            c.execute('CREATE TABLE IF NOT EXISTS system_events (id INTEGER PRIMARY KEY,event_type TEXT,metadata_json TEXT,created_at TEXT)')
            c.execute('INSERT INTO system_events(event_type,metadata_json,created_at) VALUES (?,?,?)',
                      ('APEX_RESEARCH_DECISION',json.dumps(p),self.now.isoformat()))

    def test_missing_sources_are_unknown_and_not_created(self):
        result=ai_system.snapshot(**self.kw)
        self.assertEqual(result['factor_learning']['state'],'UNAVAILABLE')
        self.assertIsNone(result['shadow_counts'])
        self.assertFalse(self.native.exists());self.assertFalse(self.shadow.exists());self.assertFalse(self.research.exists())
        self.assertIn('غير متاح',ai_system.status_text(**self.kw))
        self.assertEqual(result['new_provider_calls'],0)

    def test_matching_review_requires_exact_sent_facts_not_same_symbol(self):
        p=payload();p['timestamp']=self.now.isoformat()
        self.native_decision(p)
        client=Mock();client.review.return_value=(review(),{})
        advisor=ShadowAdvisor(self.shadow,config=config(),client=client,clock=lambda:NOW)
        advisor.submit(p);advisor.process_one();client.review.reset_mock()
        before={p:p.read_bytes() for p in (self.native,self.shadow)}
        result=ai_system.snapshot(symbol='SNAP',**self.kw)
        self.assertEqual(result['case']['review']['state'],'SAME_FROZEN_EVIDENCE')
        client.review.assert_not_called()
        for path,value in before.items():self.assertEqual(path.read_bytes(),value)
        changed=deepcopy(p);changed['jalwe']['metadata']['decision_features']['price']=7.
        self.native_decision(changed)
        result=ai_system.snapshot(symbol='SNAP',**self.kw)
        self.assertEqual(result['case']['review']['state'],'NO_MATCHING_REVIEW')
        self.assertIn('غير متاح',ai_system.status_text('SNAP',**self.kw))

    def test_old_prompt_corrupt_result_and_missing_timestamp_do_not_match(self):
        p=payload();p['timestamp']=self.now.isoformat();self.native_decision(p)
        client=Mock();client.review.return_value=(review(),{})
        advisor=ShadowAdvisor(self.shadow,config=config(),client=client,clock=lambda:NOW)
        advisor.submit(p);advisor.process_one()
        with sqlite3.connect(self.shadow) as c:c.execute("UPDATE shadow_reviews SET prompt_spec='old'")
        self.assertEqual(ai_system.snapshot(symbol='SNAP',**self.kw)['case']['review']['state'],'NO_MATCHING_REVIEW')
        p.pop('timestamp');self.native_decision(p)
        self.assertEqual(ai_system.snapshot(symbol='SNAP',**self.kw)['case']['review']['state'],'NATIVE_EVIDENCE_INELIGIBLE_FOR_SHADOW')

    def test_last_saved_factor_run_not_loose_trade_snapshot_count(self):
        with sqlite3.connect(self.native) as c:
            c.execute('CREATE TABLE learning_runs (id INTEGER PRIMARY KEY,learning_active INTEGER,eligible_trades INTEGER,changed_factors INTEGER,created_at TEXT)')
            c.execute('INSERT INTO learning_runs VALUES (1,0,8,0,?)',(self.now.isoformat(),))
        value=ai_system.snapshot(**self.kw)['factor_learning']
        self.assertEqual((value['state'],value['eligible_trades']),('WAITING_RECORDED',8))
        self.assertIn('LAST_SAVED',value['scope'])

    def test_json_artifact_expiration_and_tamper_are_separate_from_execution(self):
        artifact=evaluate(days(),self.now,'fixture')['artifact']
        value={'artifact':artifact,'state':'BACKTEST_RECORDED'}
        with sqlite3.connect(self.research) as c:
            c.execute('CREATE TABLE move_state (key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT INTO move_state VALUES (?,?)',('ai_model_lab_status',json.dumps(value)))
        self.assertEqual(ai_system.snapshot(**self.kw)['artifact_status'],'RESEARCH_ONLY')
        later=dict(self.kw,now=self.now+timedelta(days=8))
        self.assertEqual(ai_system.snapshot(**later)['artifact_status'],'EXPIRED')
        value['artifact']['coefficients'][0]+=1
        with sqlite3.connect(self.research) as c:c.execute('UPDATE move_state SET value=?',(json.dumps(value),))
        self.assertEqual(ai_system.snapshot(**self.kw)['artifact_status'],'UNVERIFIED')
        self.assertFalse(ai_system.snapshot(**self.kw)['execution_authority'])

    def test_malformed_state_and_sql_symbol_are_not_executed(self):
        with sqlite3.connect(self.research) as c:
            c.execute('CREATE TABLE move_state (key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT INTO move_state VALUES (?,?)',('ai_model_lab_status','[]'))
        self.assertEqual(ai_system.snapshot(**self.kw)['model_lab'],{})
        self.assertEqual(ai_system.snapshot(symbol="X' OR 1=1",**self.kw)['case']['state'],'INVALID_SYMBOL')

    def test_malformed_artifact_and_decision_are_unverified_not_exceptions(self):
        with sqlite3.connect(self.research) as c:
            c.execute('CREATE TABLE move_state (key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT INTO move_state VALUES (?,?)',('ai_model_lab_status',json.dumps({'artifact':['bad']})))
        self.native_decision({'symbol':'SNAP','jalwe':['bad']})
        value=ai_system.snapshot(symbol='SNAP',**self.kw)
        self.assertEqual(value['artifact_status'],'UNVERIFIED')
        self.assertEqual(value['case']['state'],'NO_SAVED_DECISION_IN_WINDOW')
        self.assertIn('500',value['case']['lookup_scope'])

    def test_controller_routes_readonly_commands_without_provider_or_order_calls(self):
        import railway_controller as controller
        with patch.object(ai_system,'status_text',return_value='unified saved evidence') as status:
            self.assertEqual(controller.handle('/ai_system'),'unified saved evidence')
            self.assertEqual(controller.handle('/ai_case SNAP'),'unified saved evidence')
            self.assertEqual(status.call_args.args,('SNAP',))


if __name__=='__main__':unittest.main()
