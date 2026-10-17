import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from intelligence import shadow_advisor as shadow
import test_shadow_advisor as fixtures
import test_gemini_shadow as gemini


class ShadowRolesCacheTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'shadow.db'
        self.client=Mock(); self.client.review.return_value=(fixtures.review(),{'total_tokens':15})
        self.advisor=shadow.ShadowAdvisor(self.path,config=gemini.config(),client=self.client,clock=lambda:fixtures.NOW)

    def test_exact_body_prompt_version_provider_and_response_are_persisted_without_key(self):
        self.advisor.submit(fixtures.payload()); self.advisor.process_one()
        row=self.advisor.status('SNAP')['latest']
        self.assertEqual(row['prompt_spec'],shadow.PROMPT_SPEC)
        request=json.loads(row['request_json'])
        self.assertEqual(request,shadow.request_body(gemini.config(),json.loads(row['evidence'])))
        self.assertEqual(request['system_instruction'],shadow.INSTRUCTIONS)
        self.assertEqual(json.loads(row['raw_output']),fixtures.review())
        self.assertNotIn('gemini-test-key',json.dumps(dict(row)))
        self.assertFalse(request['store']); self.assertNotIn('tools',request)

    def test_successful_cache_reuse_survives_restart_without_another_call_or_daily_slot(self):
        p=fixtures.payload(); self.advisor.submit(p); self.advisor.process_one()
        restarted=shadow.ShadowAdvisor(self.path,config=gemini.config(),client=self.client,clock=lambda:fixtures.NOW)
        self.assertEqual(restarted.submit(p),'CACHE_HIT')
        self.assertEqual(restarted.status()['cache_hits'],1)
        self.assertFalse(restarted.process_one())
        self.client.review.assert_called_once()
        self.assertEqual(restarted.status()['counts'],{'ANALYZED':1})
        self.assertEqual(len(json.loads(restarted.status('SNAP')['latest']['result'])['roles']),3)

    def test_changed_prompt_or_evidence_is_never_mislabeled_as_cached(self):
        p=fixtures.payload(); self.advisor.submit(p); self.advisor.process_one()
        with patch.object(shadow,'PROMPT_SPEC','new-schema-version'):
            self.assertEqual(self.advisor.submit(p),'QUEUED')
        p['jalwe']['metadata']['decision_features']['price']=6.3
        self.assertEqual(self.advisor.submit(p),'QUEUED')
        self.assertEqual(self.advisor.status()['counts'],{'ANALYZED':1,'PENDING':2})

    def test_corrupt_cached_result_never_reenters_provider(self):
        p=fixtures.payload(); self.advisor.submit(p); self.advisor.process_one()
        with self.advisor.connect() as conn: conn.execute("UPDATE shadow_reviews SET result='{}'")
        self.assertEqual(self.advisor.submit(p),'INVALID_CACHED_REVIEW')
        self.assertFalse(self.advisor.process_one()); self.client.review.assert_called_once()

    def test_score_cannot_become_verified_news_or_cross_role_evidence(self):
        p=fixtures.payload(); p['jalwe']['metadata']['decision_features']['news_score']=90.
        evidence=shadow.frozen_evidence(p,fixtures.NOW)
        for change in ('catalyst','duplicate','cross_role','extra_order'):
            result=fixtures.review()
            if change=='catalyst': result['roles'][0].update(assessment='SUPPORTED',evidence_refs=['news_score'])
            elif change=='duplicate': result['roles'][1]=copy.deepcopy(result['roles'][0])
            elif change=='cross_role': result['roles'][1]['evidence_refs']=['news_score']
            else: result['roles'][2]['order']={'side':'BUY'}
            with self.assertRaises(ValueError): shadow.validate_review(result,evidence)

    def test_bad_json_response_is_archived_and_request_tampering_never_calls_provider(self):
        self.advisor.submit(fixtures.payload())
        self.client.review.side_effect=shadow.ReviewParseError('not-json')
        self.advisor.process_one()
        latest=self.advisor.status('SNAP')['latest']
        self.assertEqual(latest['state'],'FAILED'); self.assertEqual(latest['raw_output'],'not-json')
        p=fixtures.payload(); p['jalwe']['metadata']['decision_features']['price']=6.3
        self.advisor.submit(p)
        with self.advisor.connect() as conn: conn.execute("UPDATE shadow_reviews SET request_json='{}' WHERE state='PENDING'")
        self.advisor.process_one(); self.client.review.assert_called_once()
        self.assertEqual(self.advisor.status()['counts'],{'FAILED':2})

    def test_stored_current_prompt_is_required_and_legacy_pending_is_not_sent(self):
        self.advisor.submit(fixtures.payload())
        with self.advisor.connect() as conn: conn.execute('UPDATE shadow_reviews SET prompt_spec=NULL')
        self.assertFalse(self.advisor.process_one()); self.client.review.assert_not_called()
        with patch('intelligence.shadow_advisor.threading.Thread'): self.advisor.start()
        self.assertEqual(self.advisor.status()['counts'],{'PROMPT_CHANGED':1})


if __name__=='__main__': unittest.main()
