import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from stock_decision_evidence import stock_decision_text

class StockDecisionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'evidence.db'
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE decision_outcome_memory (decision_key TEXT,symbol TEXT,source_state TEXT,reason TEXT,decided_at TEXT,payload_json TEXT)')
    def add(self,key,at,metadata=None):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO decision_outcome_memory VALUES (?,?,?,?,?,?)',
                (key,'TEST','WATCHING','Waiting for trigger',at,json.dumps({'jalwe':{'gates':{'trigger':False},'metadata':metadata or {}}})))
    def text(self):return stock_decision_text('TEST','2026-10-27',path=self.path)
    def test_new_york_day_and_mixed_offsets(self):
        self.add('outside','2026-10-27T03:59:59Z')
        self.add('start','2026-10-27T04:00:00Z')
        self.add('late','2026-10-28T03:59:59+00:00')
        self.add('next','2026-10-28T04:00:00Z')
        before=self.path.read_bytes();text=self.text()
        self.assertIn('2 قرار',text);self.assertIn('trigger=False',text)
        self.assertEqual(before,self.path.read_bytes())
    def test_old_evidence_is_unknown_not_inferred(self):
        self.add('x','2026-10-27T14:00:00Z')
        self.assertIn('غير محفوظة',self.text())
    def test_retained_fetch_and_completion_evidence(self):
        self.add('x','2026-10-27T14:00:00Z',{'bar_source_evidence':{'feed':'iex',
            'fetch':{'provider_latest_bar_start':'2026-10-27T13:55Z','numeric_rows_removed':0},
            'completion':{'latest_bar_start':'2026-10-27T13:30Z','excluded_rows':1}}})
        text=self.text();self.assertIn('13:55Z',text);self.assertIn('13:30Z',text)
    def test_empty_and_invalid_requests_never_claim_rejection(self):
        self.assertIn('لا يثبت',self.text())
        self.assertIn('غير صالح',stock_decision_text("TEST';DROP",'2026-10-27',path=self.path))
    def test_malformed_latest_payload_keeps_counts(self):
        self.add('x','2026-10-27T14:00:00Z')
        with sqlite3.connect(self.path) as c:c.execute("UPDATE decision_outcome_memory SET payload_json='bad'")
        self.assertIn('غير صالحة',self.text())
