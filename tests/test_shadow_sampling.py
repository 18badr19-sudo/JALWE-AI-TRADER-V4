"""Session sampling uses a fake clock/SQLite and never invokes the provider."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from intelligence.shadow_advisor import ShadowAdvisor, AdvisorConfig
from test_gemini_shadow import config
from test_shadow_advisor import payload


class ShadowSamplingTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'sampling.db'
        self.client=Mock()
        self.at('2026-10-23T09:35:00')
        self.advisor=ShadowAdvisor(self.path, config=config(spread_reviews=True),
                                   client=self.client,clock=lambda:self.now)

    def at(self, local):
        self.now=datetime.fromisoformat(local).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()

    def p(self, symbol='ONE'):
        p=payload();p['symbol']=symbol
        p['jalwe']['metadata']['decision_features']['timestamp']=datetime.fromtimestamp(self.now-60,timezone.utc).isoformat()
        return p

    def test_three_distinct_windows_and_symbols_with_daily_hard_cap(self):
        self.assertEqual(self.advisor.submit(self.p()),'QUEUED')
        self.assertEqual(self.advisor.submit(self.p('TWO')),'REVIEW_WINDOW_LIMIT')
        self.at('2026-10-23T11:40:00')
        self.assertEqual(self.advisor.submit(self.p()),'SYMBOL_ALREADY_SAMPLED')
        self.assertEqual(self.advisor.submit(self.p('TWO')),'QUEUED')
        self.at('2026-10-23T13:50:00')
        self.assertEqual(self.advisor.submit(self.p('THREE')),'QUEUED')
        self.assertEqual(self.advisor.submit(self.p('FOUR')),'DAILY_LIMIT')
        self.client.review.assert_not_called()

    def test_restart_failed_requests_and_old_records_consume_budget(self):
        self.advisor.submit(self.p())
        with self.advisor.connect() as c: c.execute("UPDATE shadow_reviews SET state='FAILED'")
        restarted=ShadowAdvisor(self.path,config=config(spread_reviews=True),client=self.client,clock=lambda:self.now)
        self.assertEqual(restarted.submit(self.p('TWO')),'REVIEW_WINDOW_LIMIT')
        self.assertEqual(restarted.status()['counts'],{'FAILED':1})

    def test_no_catchup_burst_when_earlier_windows_unused(self):
        self.at('2026-10-23T15:00:00')
        self.assertEqual(self.advisor.submit(self.p()),'QUEUED')
        self.assertEqual(self.advisor.submit(self.p('TWO')),'REVIEW_WINDOW_LIMIT')

    def test_weekend_preopen_and_close_do_not_queue(self):
        for stamp in ('2026-10-23T09:29:59','2026-10-23T16:00:00','2026-10-24T10:00:00'):
            self.at(stamp)
            self.assertEqual(self.advisor.submit(self.p()),'OUTSIDE_REVIEW_WINDOW')
        self.client.review.assert_not_called()

    def test_dst_and_new_session_budget(self):
        self.advisor.submit(self.p())
        self.at('2026-11-02T09:30:00')
        self.assertEqual(datetime.fromtimestamp(self.now,timezone.utc).hour,14)
        self.assertEqual(self.advisor.submit(self.p()),'QUEUED')

    def test_expired_pending_is_not_sent_and_does_not_release_slot(self):
        self.advisor.submit(self.p());self.now+=121
        self.assertFalse(self.advisor.process_one())
        self.assertEqual(self.advisor.submit(self.p('TWO')),'REVIEW_WINDOW_LIMIT')
        self.client.review.assert_not_called()

    def test_same_evidence_dedup_still_precedes_sampling(self):
        p=self.p();self.advisor.submit(p)
        self.assertEqual(self.advisor.submit(p),'DUPLICATE')

    def test_concurrent_reservations_cannot_overbook_a_window(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda i:self.advisor.submit(self.p('CASE'+str(i))),range(6)))
        self.assertEqual(results.count('QUEUED'),1)
        self.assertEqual(results.count('REVIEW_WINDOW_LIMIT'),5)
        self.assertEqual(self.advisor.status()['counts'],{'PENDING':1})
        self.client.review.assert_not_called()

    def test_success_cache_needs_no_new_slot_even_outside_windows(self):
        from test_shadow_advisor import review
        self.client.review.return_value=(review(),{})
        p=self.p();self.assertEqual(self.advisor.submit(p),'QUEUED')
        self.assertTrue(self.advisor.process_one())
        # Same facts are still fresh at close, and a cache hit makes no new call.
        self.at('2026-10-23T15:59:59')
        q=self.p('LATE');self.assertEqual(self.advisor.submit(q),'QUEUED')
        self.assertTrue(self.advisor.process_one())
        self.now+=2
        self.assertEqual(self.advisor.submit(q),'CACHE_HIT')
        self.assertFalse(self.advisor.process_one())
        self.assertEqual(self.client.review.call_count,2)

    def test_default_configuration_spreads_gemini_reviews(self):
        self.assertTrue(AdvisorConfig().spread_reviews)
        self.assertEqual(self.advisor.status()['sampling_policy'],'NY_REGULAR_THIRDS_DISTINCT_SYMBOLS')


if __name__=='__main__': unittest.main()
