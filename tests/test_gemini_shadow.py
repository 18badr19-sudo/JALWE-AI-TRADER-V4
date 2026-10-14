import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from intelligence.shadow_advisor import (AdvisorConfig, GEMINI_FREE_MODEL,
    GeminiClient, GeminiQuotaExhausted, ModelClient, ShadowAdvisor, frozen_evidence)
from test_shadow_advisor import NOW, payload, review


def config(**changes):
    return replace(AdvisorConfig(True, True, 'gemini-test-key', GEMINI_FREE_MODEL, 3,
                                 'gemini', True), **changes)


def response():
    return {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
        {'type': 'text', 'text': json.dumps(review(), ensure_ascii=False)}]}],
        'usage': {'total_input_tokens': 100, 'total_output_tokens': 250, 'total_tokens': 350}}


def opener_for(body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=raw)))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    return opener


class GeminiTransportTests(unittest.TestCase):
    def test_native_structured_request_keeps_key_out_of_url_prompt_and_openai(self):
        opener = opener_for(response())
        with patch('urllib.request.build_opener', return_value=opener):
            result, usage = GeminiClient(config()).review(frozen_evidence(payload(), NOW))
        self.assertEqual(result, review()); self.assertEqual(usage['total_tokens'], 350)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://generativelanguage.googleapis.com/v1beta/interactions')
        self.assertNotIn('gemini-test-key', request.full_url)
        self.assertEqual(request.get_header('X-goog-api-key'), 'gemini-test-key')
        self.assertIsNone(request.get_header('Authorization'))
        sent = json.loads(request.data)
        self.assertFalse(sent['store']); self.assertNotIn('tools', sent)
        self.assertEqual(sent['generation_config']['max_output_tokens'], 2000)
        self.assertEqual(sent['response_format']['mime_type'], 'application/json')
        self.assertFalse(sent['response_format']['schema']['additionalProperties'])
        self.assertNotIn('gemini-test-key', sent['input'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)

    def test_non_free_model_unknown_provider_missing_key_and_unconfirmed_tier_make_no_http_call(self):
        with patch('urllib.request.build_opener') as http:
            for c in (config(free_tier_confirmed=False), config(model='gemini-4-argon'),
                      config(provider='typo'), config(api_key=''), config(enabled=False), config(paper_only=False)):
                with self.assertRaises(ValueError): GeminiClient(c).review({})
            with self.assertRaises(ValueError): ModelClient(config()).review({})
        http.assert_not_called()

    def test_quota_and_auth_failures_never_use_another_host_or_model(self):
        for status in (429, 403, 402, 500):
            opener = Mock(); opener.open.side_effect = urllib.error.HTTPError('secret URL', status, 'secret message', None, None)
            with patch('urllib.request.build_opener', return_value=opener), \
                 self.assertRaises(GeminiQuotaExhausted if status == 429 else ValueError) as error:
                GeminiClient(config()).review({})
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(opener.open.call_count, 1)
            self.assertIn('googleapis.com', opener.open.call_args.args[0].full_url)

    def test_partial_refused_tool_only_extra_output_oversized_and_invalid_json_are_not_success(self):
        missing = {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [{'type': 'refusal'}]}]}
        tool = {'status': 'completed', 'steps': [{'type': 'tool_call', 'content': [{'type': 'text', 'text': '{}'}]}]}
        extra = response(); extra['steps'][0]['content'].append({'type': 'text', 'text': '{}'})
        invalid = response(); invalid['steps'][0]['content'][0]['text'] = 'not json'
        for body in ({'status': 'incomplete'}, missing, tool, extra, invalid, b'x'*65537):
            with patch('urllib.request.build_opener', return_value=opener_for(body)), self.assertRaises(ValueError):
                GeminiClient(config()).review({})

    def test_provider_specific_environment_never_reuses_openai_key_or_model(self):
        env = {'JALWE_SHADOW_AI_ENABLED': 'true', 'JALWE_SHADOW_AI_PROVIDER': 'gemini',
               'OPENAI_API_KEY': 'must-not-use-paid-key', 'JALWE_SHADOW_AI_MODEL': 'paid-model'}
        with patch.dict(os.environ, env, clear=True):
            c = AdvisorConfig.environment()
            self.assertEqual(c.model, GEMINI_FREE_MODEL); self.assertFalse(c.api_key)
            self.assertEqual(c.daily_limit, 3)
            self.assertEqual(c.state(), 'WAITING_FREE_TIER_CONFIRMATION')
        with patch.dict(os.environ, dict(env, GEMINI_API_KEY='free-key', JALWE_GEMINI_FREE_TIER_CONFIRMED='true'), clear=True):
            self.assertEqual(AdvisorConfig.environment().state(), 'SHADOW_ONLY')


class GeminiQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'reviews.db'; self.now = NOW
        self.client = Mock()
        self.advisor = ShadowAdvisor(self.path, config=config(), client=self.client, clock=lambda: self.now)

    def test_quota_pause_is_durable_and_does_not_block_native_payload_or_call_again(self):
        p = payload(); original = copy.deepcopy(p)
        self.assertEqual(self.advisor.submit(p), 'QUEUED')
        self.client.review.side_effect = GeminiQuotaExhausted('no quota')
        self.assertTrue(self.advisor.process_one())
        self.assertEqual(self.advisor.status()['mode'], 'GEMINI_QUOTA_PAUSED')
        restarted = ShadowAdvisor(self.path, config=config(), client=self.client, clock=lambda: self.now)
        self.assertFalse(restarted.process_one())
        self.assertEqual(restarted.submit(p), 'GEMINI_QUOTA_PAUSED')
        self.assertEqual(self.client.review.call_count, 1)
        self.assertEqual(p['jalwe']['state'], original['jalwe']['state'])
        self.assertEqual(restarted.status()['counts'], {'QUOTA_EXHAUSTED': 1})
        self.now += 86401
        self.assertEqual(restarted.mode(), 'SHADOW_ONLY')

    def test_provider_switch_cannot_resend_pending_gemini_work_to_paid_openai(self):
        self.advisor.submit(payload())
        openai = ShadowAdvisor(self.path, config=AdvisorConfig(True, True, 'paid-key'),
                              client=self.client, clock=lambda: NOW)
        self.assertFalse(openai.process_one())
        with patch('intelligence.shadow_advisor.threading.Thread'):
            openai.start()
        self.assertEqual(openai.status()['counts'], {'CONFIG_CHANGED': 1})
        self.client.review.assert_not_called()

    def test_success_is_attributed_to_gemini_and_fabricated_evidence_stays_failed(self):
        self.client.review.return_value = (review(), {'total_tokens': 10})
        self.advisor.submit(payload()); self.advisor.process_one()
        latest = self.advisor.status('SNAP')['latest']
        self.assertEqual(latest['provider'], 'gemini'); self.assertEqual(latest['model'], GEMINI_FREE_MODEL)
        self.assertEqual(latest['state'], 'ANALYZED')
        invalid = review(); invalid['scenarios'][0]['evidence_refs'] = ['invented_news']
        self.client.review.return_value = (invalid, {})
        p = payload(); p['symbol'] = 'GFS'
        self.advisor.submit(p); self.advisor.process_one()
        self.assertEqual(self.advisor.status('GFS')['latest']['state'], 'FAILED')

    def test_legacy_schema_preserves_old_records_and_provider_identity(self):
        legacy = self.path.parent/'legacy.db'
        with sqlite3.connect(legacy) as conn:
            conn.execute('CREATE TABLE shadow_reviews (id TEXT PRIMARY KEY,symbol TEXT,session TEXT,created REAL,state TEXT,evidence TEXT,result TEXT,usage TEXT,error_kind TEXT,finished REAL)')
            conn.execute('INSERT INTO shadow_reviews VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('old', 'SNAP', '2026-06-09', NOW, 'ANALYZED', '{}', '{}', '{}', None, NOW))
        migrated = ShadowAdvisor(legacy, config=config(), client=self.client, clock=lambda: NOW)
        row = migrated.status('SNAP')['latest']
        self.assertEqual(row['provider'], 'openai'); self.assertEqual(row['model'], 'legacy')
        self.assertEqual(row['state'], 'ANALYZED')

    def test_http_failure_retains_only_safe_status_in_storage_logs_and_telegram(self):
        from intelligence.shadow_advisor import advisor_status_text
        for provider in ('gemini', 'openai'):
            for code in (401, 403, 404, 500):
                with self.subTest(provider=provider, code=code):
                    cfg = config() if provider == 'gemini' else AdvisorConfig(True, True, 'fake')
                    path = self.path.parent / (provider + str(code) + '.db')
                    a = ShadowAdvisor(path, config=cfg, clock=lambda: self.now)
                    a.submit(payload())
                    opener = Mock()
                    opener.open.side_effect = urllib.error.HTTPError(
                        'https://secret.invalid/key=credential', code, 'private response', None, None)
                    with patch('urllib.request.build_opener', return_value=opener), \
                         self.assertLogs('intelligence.shadow_advisor', level='INFO') as logs:
                        self.assertTrue(a.process_one())
                    row = a.status('SNAP')['latest']
                    safe = provider.upper() + '_HTTP_' + str(code)
                    self.assertEqual(row['state'], 'FAILED')
                    self.assertEqual(row['error_kind'], safe)
                    with patch('core.storage.data_directory', return_value=path.parent), \
                         patch('intelligence.shadow_advisor.ShadowAdvisor', return_value=a):
                        message = advisor_status_text('SNAP')
                    combined = json.dumps(row) + str(logs.output) + message
                    self.assertIn(safe, message)
                    for private in ('credential', 'secret.invalid', 'private response'):
                        self.assertNotIn(private, combined)
                    self.assertEqual(opener.open.call_count, 1)
