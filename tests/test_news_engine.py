import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from alpaca.data.models.news import NewsSet
from intelligence.news_engine import NewsEngine


class NewsEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = NewsEngine.__new__(NewsEngine)
        self.engine.lookback_hours = 24
        self.engine.limit = 50
        self.engine.client = Mock()

    def article(self, article_id=1, age_hours=1):
        timestamp = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        return dict(id=article_id, headline='TEST raises guidance',
                    summary='Strong demand', created_at=timestamp,
                    updated_at=timestamp, symbols=['TEST'], source='test',
                    url=None, author='test', content='', images=[])

    def respond(self, articles):
        # Use the actual SDK model, not a mock with a fictional .news field.
        self.engine.client.get_news.return_value = NewsSet(
            {'news': articles, 'next_page_token': None})

    def test_sdk_news_reaches_analysis_and_duplicates_count_once(self):
        article = self.article()
        self.respond([article, article])
        result = self.engine.analyze('test')
        self.assertTrue(result.available)
        self.assertEqual(result.article_count, 1)
        self.assertGreater(result.score, 50)
        self.assertTrue(result.catalyst_detected)
        self.assertEqual(result.catalyst_type, 'GUIDANCE')

    def test_empty_response_is_available_without_fabricated_score(self):
        self.respond([])
        result = self.engine.analyze('TEST')
        self.assertTrue(result.available)
        self.assertEqual(result.article_count, 0)
        self.assertIsNone(result.score)

    def test_failed_request_is_unavailable(self):
        self.engine.client.get_news.side_effect = RuntimeError('offline')
        result = self.engine.analyze('TEST')
        self.assertFalse(result.available)
        self.assertIsNone(result.score)

    def test_unrecognized_response_is_unavailable_not_empty(self):
        self.engine.client.get_news.return_value = object()
        self.assertFalse(self.engine.analyze('TEST').available)

    def test_news_outside_requested_window_is_excluded(self):
        self.respond([self.article(1, -1), self.article(2, 25), self.article(3, 1)])
        result = self.engine.analyze('TEST')
        self.assertEqual([item.article_id for item in result.articles], ['3'])


if __name__ == '__main__':
    unittest.main()
