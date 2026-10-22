import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from intelligence.shadow_advisor import (AdvisorConfig, ShadowAdvisor, SCHEMA,
    ProviderHTTPError, ReviewParseError, review_error_kind)
from test_shadow_advisor import NOW, payload, review


class ShadowFailureDiagnosticsTests(unittest.TestCase):
    def test_validation_code_and_stage_are_observable_without_accepting_bad_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Mock()
            result = review()
            result['roles'][0]['assessment'] = 'SUPPORTED'
            result['roles'][0]['evidence_refs'] = ['price']
            client.review.return_value = (result, {})
            advisor = ShadowAdvisor(Path(tmp)/'shadow.db', config=AdvisorConfig(True, True, 'fake'),
                                    client=client, clock=lambda: NOW)
            advisor.submit(payload())
            with self.assertLogs('intelligence.shadow_advisor', level='INFO') as logs:
                advisor.process_one()
            row = advisor.status('SNAP')['latest']
            self.assertEqual(row['state'], 'FAILED')
            self.assertEqual(row['error_kind'], 'UNSUPPORTED_ROLE_EVIDENCE')
            self.assertEqual(json.loads(row['result']), None)
            self.assertIn('EVIDENCE_VALIDATION', logs.output[0])
            self.assertFalse(advisor.process_one())
            client.review.assert_called_once()

    def test_unknown_errors_remain_sanitized_and_provider_codes_stay_specific(self):
        self.assertEqual(review_error_kind(ValueError('private-token=must-not-log')), 'ValueError')
        self.assertEqual(review_error_kind(ValueError('INCOMPLETE_GEMINI_RESPONSE')),
                         'INCOMPLETE_GEMINI_RESPONSE')
        self.assertEqual(review_error_kind(ReviewParseError('secret output')), 'INVALID_MODEL_JSON')
        self.assertEqual(review_error_kind(ProviderHTTPError('gemini', 403)), 'GEMINI_HTTP_403')

    def test_provider_schema_requests_all_three_scenarios_and_roles(self):
        for key in ('scenarios', 'roles'):
            self.assertEqual(SCHEMA['properties'][key]['minItems'], 3)
            self.assertEqual(SCHEMA['properties'][key]['maxItems'], 3)
