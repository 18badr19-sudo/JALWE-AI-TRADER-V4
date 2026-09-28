import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from intelligence.external_research_bridge import ExternalResearchBridge, ExternalResearch


@unittest.skipUnless(os.getenv('TEST_POSTGRES_DSN'), 'requires isolated test PostgreSQL')
class BridgePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        self.dsn = os.environ['TEST_POSTGRES_DSN']
        self.schema = 'test_bridge_' + uuid.uuid4().hex
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('CREATE SCHEMA ' + self.schema)
        self.connector = patch.object(ExternalResearchBridge, '_connect_postgres',
            lambda bridge: psycopg2.connect(self.dsn, options='-c search_path=' + self.schema))
        self.connector.start()
        self.bridge = ExternalResearchBridge(database_url=self.dsn)

    def tearDown(self):
        import psycopg2
        self.connector.stop()
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    def publish_deployed_apex_schema(self, symbol='AUDC', source='APEX'):
        with self.bridge._connect_postgres() as conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS external_research_latest (
                    symbol TEXT, source TEXT, news_score DOUBLE PRECISION,
                    sentiment TEXT, catalyst TEXT, confidence DOUBLE PRECISION,
                    summary TEXT, market_bias TEXT, technical_notes TEXT,
                    headlines_json TEXT, risk_flags_json TEXT, created_at TEXT,
                    metadata_json TEXT, PRIMARY KEY(symbol, source))''')
                cur.execute('''INSERT INTO external_research_latest VALUES
                    (%s,%s,NULL,'NEUTRAL',NULL,.95,'current','MIXED','note',
                     '[]','[]',%s,%s)''', (symbol, source,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps({'verdict': 'WATCH', 'research_score': 60})))

    def test_current_apex_packet_visible_to_watcher_and_decision_lookup(self):
        self.publish_deployed_apex_schema()
        reports = self.bridge.list_recent_research(source='APEX')
        self.assertEqual([r.symbol for r in reports], ['AUDC'])
        self.assertLess(self.bridge.get_age_minutes(reports[0]), 1)
        report = self.bridge.get_latest_research('AUDC', source='APEX')
        self.assertEqual(report.metadata['verdict'], 'WATCH')
        self.assertEqual(report.technical_notes, ['note'])
        self.assertAlmostEqual(report.confidence, .95)

    def test_newest_packet_wins_without_duplicate_symbol_source(self):
        self.bridge.publish_research(ExternalResearch(symbol='AUDC', summary='old',
            created_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat()))
        self.publish_deployed_apex_schema()
        reports = self.bridge.list_recent_research(source='APEX')
        self.assertEqual(len(reports), 1)
        self.assertEqual(self.bridge.count(), 1)
        self.assertEqual(reports[0].summary, 'current')
        self.assertEqual(self.bridge.get_latest_research('AUDC').summary, 'current')

    def test_canonical_only_installation_still_reads_reports(self):
        self.bridge.publish_research(ExternalResearch(symbol='TEST', summary='canonical'))
        self.assertEqual(self.bridge.get_latest_research('TEST').summary, 'canonical')
        self.assertEqual(len(self.bridge.list_recent_research(source='APEX')), 1)

    def test_source_filter_applies_to_both_schemas(self):
        self.publish_deployed_apex_schema(source='OTHER')
        self.assertEqual(self.bridge.list_recent_research(source='APEX'), [])
        self.assertIsNone(self.bridge.get_latest_research('AUDC', source='APEX'))


if __name__ == '__main__':
    unittest.main()
