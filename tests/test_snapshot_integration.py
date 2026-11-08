import copy,json,sqlite3,unittest
from datetime import timedelta
from unittest.mock import patch
import test_decision_outcome_memory as fixtures
from research_snapshots import compact,expand,load
class SnapshotIntegration(unittest.TestCase):
 setUp=fixtures.OutcomeMemoryTests.setUp
 payload=fixtures.OutcomeMemoryTests.payload
 row=fixtures.OutcomeMemoryTests.row
 def big(self):
  p=self.payload();p['jalwe']['metadata']['market_move_research']={'factor_benchmark':{'data':'x'*50000},'historical_jump_profiles':{'data':'y'*50000},'symbol_evidence':{'known':None,'price':10}}
  return p
 def test_roundtrip_real_record(self):
  p=self.big();original=copy.deepcopy(p);self.memory.record(p,'v1');self.assertEqual(load(self.db,self.row()['payload_json']),p);self.assertEqual(p,original)
 def test_shared_100_decisions(self):
  for i in range(100):
   p=self.big();p['symbol']='X'+str(i);self.memory.record(p,'v1')
  with self.db.connection() as c:
   self.assertEqual(c.execute('select count(*) from research_aggregate_snapshots').fetchone()[0],2)
   self.assertLess(c.execute('select max(length(payload_json)) from decision_outcome_memory').fetchone()[0],2000)
 def test_changed_version_retained(self):
  p=self.big();self.memory.record(p,'v1');old=self.row()['payload_json'];p['jalwe']['metadata']['market_move_research']['factor_benchmark']={'new':1};self.memory.record(p,'v2');self.assertEqual(load(self.db,old),self.big())
 def test_missing_snapshot(self):
  self.memory.record(self.big(),'v1')
  with self.db.connection() as c:c.execute('delete from research_aggregate_snapshots')
  with self.assertRaises(ValueError):load(self.db,self.row()['payload_json'])
 def test_corrupt_snapshot(self):
  self.memory.record(self.big(),'v1')
  with self.db.connection() as c:c.execute("update research_aggregate_snapshots set body_json='{}'")
  with self.assertRaises(ValueError):load(self.db,self.row()['payload_json'])
 def test_atomic_failure(self):
  with patch.object(self.memory.experiments,'record_candidates',side_effect=RuntimeError('fixture')):
   with self.assertRaises(RuntimeError):self.memory.record(self.big(),'v1')
  with self.db.connection() as c:
   self.assertEqual(c.execute('select count(*) from decision_outcome_memory').fetchone()[0],0)
   self.assertEqual(c.execute('select count(*) from research_aggregate_snapshots').fetchone()[0],0)
 def test_legacy_inline(self):
  with self.db.connection() as c:self.assertEqual(expand(c,self.big()),self.big())
 def test_sip_actual_sync_roundtrip(self):
  self.memory.record(self.big(),'v1');self.memory.delayed_sip_research._sync_sources(self.start+timedelta(minutes=1))
  with self.db.connection() as c:
   r=c.execute('select decision_json from delayed_sip_research').fetchone()[0]
   self.assertLess(len(r),2000);self.assertEqual(expand(c,{'jalwe':json.loads(r)})['jalwe'],self.big()['jalwe'])
 def test_repeat_idempotent(self):
  self.assertTrue(self.memory.record(self.big(),'v1'));self.assertFalse(self.memory.record(self.big(),'v1'))
 def test_symbol_unknown_retained(self):
  self.memory.record(self.big(),'v1');p=load(self.db,self.row()['payload_json']);self.assertIsNone(p['jalwe']['metadata']['market_move_research']['symbol_evidence']['known'])
