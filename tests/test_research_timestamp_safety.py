import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

from intelligence.external_research_bridge import ExternalResearchBridge


class ResearchTimestampSafetyTests(unittest.TestCase):
    def setUp(self):
        self.bridge = ExternalResearchBridge.__new__(ExternalResearchBridge)
        self.now = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)

    def test_future_research_cannot_remain_fresh_until_its_clock_catches_up(self):
        for future in (timedelta(seconds=6), timedelta(days=1)):
            report = NS(created_at=(self.now+future).isoformat())
            with self.subTest(future=future), \
                 patch('intelligence.external_research_bridge.utc_now', return_value=self.now):
                self.assertIsNone(self.bridge.get_age_minutes(report))
                self.assertFalse(self.bridge.is_fresh(report))

    def test_small_clock_skew_timezone_and_legacy_utc_timestamps_are_supported(self):
        reports = [(self.now+timedelta(seconds=2), 0),
                   (self.now-timedelta(minutes=10), 10),
                   ((self.now-timedelta(minutes=10)).astimezone(timezone(timedelta(hours=3))), 10),
                   ((self.now-timedelta(minutes=10)).replace(tzinfo=None), 10)]
        with patch('intelligence.external_research_bridge.utc_now', return_value=self.now):
            for at, age in reports:
                with self.subTest(at=at):
                    report = NS(created_at=at.isoformat())
                    self.assertEqual(self.bridge.get_age_minutes(report), age)
                    self.assertTrue(self.bridge.is_fresh(report))
            self.assertFalse(self.bridge.is_fresh(NS(created_at=(self.now-timedelta(minutes=31)).isoformat())))
