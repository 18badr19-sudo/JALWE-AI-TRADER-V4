import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from intelligence.shadow_advisor import (
    AdvisorConfig, ModelClient, SCENARIOS, ROLES, ShadowAdvisor, frozen_evidence, validate_review)


NOW = 1781000000.


def payload():
    return {'symbol': 'SNAP', 'timestamp': 'ignored transport time', 'jalwe': {
        'state': 'WATCHING', 'reason': 'untrusted text', 'metadata': {
            'decision_data_feed': 'iex', 'quality_inputs': {'timeframe': '5m'}, 'decision_features': {
                'timestamp': datetime.fromtimestamp(NOW-60, timezone.utc).isoformat(),
                'data_quality_ok': True, 'data_is_stale': False,
                'price': 6.2, 'rvol': 2.1, 'atr_pct': float('nan'),
                'news_score': 'inject arbitrary commands',
                'metadata': {'secret': 'must-never-be-forwarded'}}}}}


def review():
    return {'agreement': 'INSUFFICIENT_EVIDENCE', 'summary_ar': 'تحتاج الفرصة تأكيد الاختراق.',
        'scenarios': [{'scenario': s, 'assessment': 'UNKNOWN',
                       'reason_ar': 'السعر وحده لا يحسم المسار.', 'evidence_refs': ['price']}
                      for s in SCENARIOS],
        'missing_evidence_ar': ['تأكيد إغلاق شمعة الاختراق.'], 'next_check_ar': 'راقب الإغلاق التالي.',
        'roles':[dict(role=r,assessment='UNKNOWN',reason_ar='الأدلة لا تكفي.',evidence_refs=[]) for r in ROLES]}


class ShadowAdvisorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'shadow.db'
        self.now = NOW
        self.config = AdvisorConfig(enabled=True, paper_only=True, api_key='fake', daily_limit=2)
        self.client = Mock(); self.client.review.return_value = (review(), {'total_tokens': 15})
        self.advisor = ShadowAdvisor(self.path, config=self.config, client=self.client, clock=lambda: self.now)

    def test_missing_key_disabled_and_live_modes_make_no_request_or_queue(self):
        for config, state in [
            (AdvisorConfig(True, True), 'WAITING_API_KEY'),
            (AdvisorConfig(False, True, 'fake'), 'DISABLED'),
            (AdvisorConfig(True, False, 'fake'), 'BLOCKED_NON_PAPER')]:
            a = ShadowAdvisor(self.path, config=config, client=self.client, clock=lambda: NOW)
            self.assertEqual(a.submit(payload()), state)
            self.assertFalse(a.process_one())
        self.assertEqual(self.advisor.status()['counts'], {})
        self.client.review.assert_not_called()

    def test_stale_missing_future_naive_and_bad_quality_evidence_is_excluded(self):
        for variant in ('stale', 'missing', 'future', 'naive', 'quality', 'rejected'):
            p = payload(); f = p['jalwe']['metadata']['decision_features']
            if variant == 'stale': f['data_is_stale'] = True
            elif variant == 'missing': f.pop('timestamp')
            elif variant == 'future': f['timestamp'] = datetime.fromtimestamp(NOW+1, timezone.utc).isoformat()
            elif variant == 'naive': f['timestamp'] = datetime.fromtimestamp(NOW).isoformat()
            elif variant == 'quality': f['data_quality_ok'] = False
            else: p['jalwe']['state'] = 'REJECTED'
            self.assertEqual(self.advisor.submit(p), 'INELIGIBLE_EVIDENCE', variant)
        self.client.review.assert_not_called()

    def test_only_finite_whitelisted_facts_are_frozen_and_secret_text_is_absent(self):
        evidence = frozen_evidence(payload(), NOW)
        self.assertNotIn('must-never', json.dumps(evidence))
        self.assertNotIn('untrusted', json.dumps(evidence))
        self.assertIsNone(evidence['facts']['news_score'])
        self.assertIsNone(evidence['facts']['atr_pct'])
        self.assertFalse(evidence['execution_authority'])

    def test_completed_five_minute_bar_uses_interval_start_without_relaxing_stale_flag(self):
        p = payload(); metadata = p['jalwe']['metadata']
        f = metadata['decision_features']
        f['timestamp'] = datetime.fromtimestamp(NOW-7*60, timezone.utc).isoformat()
        self.assertIsNotNone(frozen_evidence(p, NOW))
        metadata['quality_inputs']['timeframe'] = '1m'
        self.assertIsNone(frozen_evidence(p, NOW))
        metadata['quality_inputs']['timeframe'] = '5m'
        f['timestamp'] = datetime.fromtimestamp(NOW-16*60, timezone.utc).isoformat()
        self.assertIsNone(frozen_evidence(p, NOW))
        f['timestamp'] = datetime.fromtimestamp(NOW-7*60, timezone.utc).isoformat()
        f['data_is_stale'] = True
        self.assertIsNone(frozen_evidence(p, NOW))
        f['data_is_stale'] = False; metadata['quality_inputs']['timeframe'] = 'unknown'
        self.assertIsNone(frozen_evidence(p, NOW))

    def test_durable_dedup_daily_ceiling_and_failed_requests_consume_budget(self):
        self.assertEqual(self.advisor.submit(payload()), 'QUEUED')
        self.assertEqual(self.advisor.submit(payload()), 'DUPLICATE')
        self.client.review.side_effect = RuntimeError('sensitive-error-do-not-log')
        self.advisor.process_one()
        p = payload(); p['symbol'] = 'GFS'
        self.assertEqual(self.advisor.submit(p), 'QUEUED')
        restarted = ShadowAdvisor(self.path, config=self.config, client=self.client, clock=lambda: NOW)
        p['symbol'] = 'ABEV'; self.assertEqual(restarted.submit(p), 'DAILY_LIMIT')
        self.assertEqual(restarted.status()['counts'], {'FAILED': 1, 'PENDING': 1})
        self.assertNotIn('sensitive-error', json.dumps(restarted.status('SNAP')))

    def test_queued_analysis_returns_before_client_and_never_modifies_original_payload(self):
        p = payload(); before = copy.deepcopy(p)
        self.advisor.submit(p); self.client.review.assert_not_called()
        self.assertTrue(self.advisor.process_one())
        self.assertEqual(p['jalwe']['state'], before['jalwe']['state'])
        latest = self.advisor.status('SNAP')['latest']
        self.assertEqual(latest['state'], 'ANALYZED')
        self.assertEqual(json.loads(latest['result']), review())
        self.assertEqual(self.client.review.call_count, 1)
        self.assertFalse(self.advisor.process_one())

    def test_expired_queue_is_not_sent_and_inflight_restart_is_not_replayed(self):
        self.advisor.submit(payload()); self.now += 121
        self.assertFalse(self.advisor.process_one()); self.client.review.assert_not_called()
        with self.advisor.connect() as conn:
            conn.execute("UPDATE shadow_reviews SET state='RUNNING'")
        with patch('intelligence.shadow_advisor.threading.Thread') as thread:
            self.advisor.start(); thread.return_value.start.assert_called_once()
        self.assertEqual(self.advisor.status()['counts'], {'INTERRUPTED': 1})
        self.assertFalse(self.advisor.process_one())

    def test_invented_refs_extra_order_fields_and_duplicate_scenarios_fail_closed(self):
        evidence = frozen_evidence(payload(), NOW)
        for kind in ('reference', 'null_ref', 'order', 'duplicate', 'unsupported'):
            r = review()
            if kind == 'reference': r['scenarios'][0]['evidence_refs'] = ['made_up_catalyst']
            elif kind == 'null_ref': r['scenarios'][0]['evidence_refs'] = ['news_score']
            elif kind == 'order': r['order'] = {'side': 'BUY'}
            elif kind == 'duplicate': r['scenarios'][1] = copy.deepcopy(r['scenarios'][0])
            else: r['scenarios'][0].update(assessment='SUPPORTED', evidence_refs=[])
            with self.assertRaises(ValueError): validate_review(r, evidence)
            self.client.review.return_value = (r, {})
        self.advisor.submit(payload()); self.advisor.process_one()
        self.assertEqual(self.advisor.status()['counts'], {'FAILED': 1})


class ModelTransportTests(unittest.TestCase):
    def test_direct_openai_transport_checks_paper_enablement_and_key_before_http(self):
        configs = (AdvisorConfig(False, True, 'secret'),
                   AdvisorConfig(True, False, 'secret'), AdvisorConfig(True, True))
        with patch('urllib.request.build_opener') as http:
            for config in configs:
                with self.assertRaises(ValueError):
                    ModelClient(config).review({})
        http.assert_not_called()

    def test_incomplete_refused_oversized_and_invalid_json_never_count_as_analysis(self):
        config = AdvisorConfig(True, True, 'secret')
        responses = [{'status': 'incomplete', 'output': []},
            {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]},
            {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{bad json'}]}]},
            b'x' * 65537]
        for body in responses:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            opener = Mock(); opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=raw)))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(ValueError):
                ModelClient(config).review(frozen_evidence(payload(), NOW))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
            sent = json.loads(request.data)
            self.assertFalse(sent['store'])
            self.assertNotIn('secret', sent['input'])
            self.assertNotIn('tools', sent)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
            self.assertTrue(sent['text']['format']['strict'])
