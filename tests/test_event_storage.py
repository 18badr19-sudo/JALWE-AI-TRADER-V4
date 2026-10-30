"""Exercise the real event writer with SQLite, without importing app singletons."""
import ast
import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional


def writer_class():
    root = Path(__file__).resolve().parents[1]
    path = root / 'core' / 'database.py'
    if not path.exists():
        path = root / 'database.py'
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Database')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'log_event')
    module = ast.Module(body=[ast.ClassDef(name='Writer', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[])
    namespace = dict(json=json, Any=Any, Optional=Optional)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['Writer']


class EventStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE system_events(event_type TEXT, severity TEXT, message TEXT, metadata_json TEXT)')
        self.writer = writer_class()()
        @contextmanager
        def connection():
            with self.conn:
                yield self.conn
        self.writer.connection = connection

    def tearDown(self):
        self.conn.close()

    def save(self, payload, event='APEX_RESEARCH_DECISION'):
        self.writer.log_event(event, 'decision evidence', 'WARNING', payload)
        row = self.conn.execute('SELECT * FROM system_events ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(row[:3], (event, 'WARNING', 'decision evidence'))
        return row[3]

    def payload(self):
        return {'symbol': 'TEST', 'apex': {'score': 83}, 'jalwe': {'state': 'WATCHING', 'metadata': {'market_move_research': {'factor_benchmark': {'daily': 'x' * 50000}, 'historical_jump_profiles': {'profiles': 'y' * 50000}, 'symbol_evidence': {'rvol': 2.5}, 'published_at': '2026-10-30'}}}}

    def test_only_aggregate_fields_removed_from_persisted_decision(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        expected = copy.deepcopy(payload)
        research = expected['jalwe']['metadata']['market_move_research']
        research.pop('factor_benchmark')
        research.pop('historical_jump_profiles')
        saved = self.save(payload)
        self.assertEqual(json.loads(saved), expected)
        self.assertEqual(payload, before)
        self.assertLess(len(saved), len(json.dumps(payload)) / 100)

    def test_other_events_keep_full_payload_bytes(self):
        payload = self.payload()
        self.assertEqual(self.save(payload, 'BROKER_ORDER'), json.dumps(payload, default=str))

    def test_each_field_can_be_absent_or_null(self):
        for key in ('factor_benchmark', 'historical_jump_profiles'):
            payload = self.payload()
            r = payload['jalwe']['metadata']['market_move_research']
            r.pop(key)
            other = next(k for k in ('factor_benchmark', 'historical_jump_profiles') if k != key)
            r[other] = None
            saved = json.loads(self.save(payload))['jalwe']['metadata']['market_move_research']
            self.assertNotIn(key, saved)
            self.assertNotIn(other, saved)
            self.assertEqual(saved['symbol_evidence'], {'rvol': 2.5})

    def test_missing_and_malformed_nested_containers_preserved(self):
        for value in (None, [], 'text', 5, True):
            for payload in ({'jalwe': value}, {'jalwe': {'metadata': value}}, {'jalwe': {'metadata': {'market_move_research': value}}}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.save(payload), json.dumps(payload, default=str))
        self.assertEqual(self.save({'symbol': 'TEST'}), json.dumps({'symbol': 'TEST'}))

    def test_default_and_non_dict_root_compatibility(self):
        for value in (None, {}, [], ['item'], 'text', 42):
            self.assertEqual(self.save(value), json.dumps(value or {}, default=str))

    def test_identically_named_fields_elsewhere_are_preserved(self):
        payload = self.payload()
        payload['factor_benchmark'] = {'keep': True}
        payload['jalwe']['metadata']['historical_jump_profiles'] = ['keep']
        saved = json.loads(self.save(payload))
        self.assertEqual(saved['factor_benchmark'], {'keep': True})
        self.assertEqual(saved['jalwe']['metadata']['historical_jump_profiles'], ['keep'])

    def test_custom_values_still_use_default_str(self):
        payload = self.payload()
        payload['jalwe']['metadata']['market_move_research']['symbol_evidence'] = Path('evidence')
        self.assertEqual(json.loads(self.save(payload))['jalwe']['metadata']['market_move_research']['symbol_evidence'], 'evidence')

    def test_failed_write_does_not_mutate_caller(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        self.conn.execute('DROP TABLE system_events')
        with self.assertRaises(sqlite3.OperationalError):
            self.save(payload)
        self.assertEqual(payload, before)

if __name__ == '__main__':
    unittest.main()
