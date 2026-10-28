import json
import os
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
import scanner_evidence as evidence

class ScannerEvidenceTests(unittest.TestCase):
    def row(self):return dict(symbol='QAUTC',stage='RADAR',reason='SELECTED_RADAR',rank=2,observed_price=10.,secret='never-save')
    def test_normalization_whitelists_and_deduplicates_without_mutating(self):
        row=self.row();p=evidence.normalize('scan','iex',[row,row],datetime(2026,10,28,1,tzinfo=timezone.utc))
        self.assertEqual(p['day'],'2026-10-27');self.assertEqual(len(p['rows']),1)
        self.assertNotIn('secret',p['rows'][0]);self.assertIn('secret',row)
        self.assertEqual(p['batch'],evidence.normalize('scan','iex',[row],datetime(2026,10,28,2,tzinfo=timezone.utc))['batch'])
    def test_invalid_inputs_and_nonfinite_do_not_form_valid_facts(self):
        row=self.row();row['observed_price']=float('inf')
        p=evidence.normalize('s','iex',[row,dict(symbol="BAD';",stage='DATA',reason='x')],datetime.now(timezone.utc))
        self.assertEqual(len(p['rows']),1);self.assertIsNone(p['rows'][0]['observed_price'])
        with self.assertRaises(ValueError):evidence.normalize('s','iex',[],datetime.now())
        with self.assertRaises(ValueError):evidence.normalize('s','iex',[row]*20001,datetime.now(timezone.utc))
    def test_missing_database_does_not_start_worker_or_infer_rejection(self):
        with patch.dict(os.environ,{},clear=True),patch.object(evidence.threading,'Thread') as thread:
            self.assertEqual(evidence.submit('s','iex',[self.row()]),'NO_SHARED_DATABASE')
            self.assertIn('غير متاحة',evidence.stock_scan_text('QAUTC','2026-10-27'))
            thread.assert_not_called()
    def test_queue_pressure_is_reported_without_blocking_scanner(self):
        with patch.dict(os.environ,{'DATABASE_URL':'fake'}),patch.object(evidence,'_thread',Mock()),patch.object(evidence._queue,'put_nowait',side_effect=evidence.queue.Full):
            self.assertEqual(evidence.submit('s','iex',[self.row()]),'QUEUE_FULL')
    def test_reader_uses_readonly_and_keeps_absence_unknown(self):
        conn=Mock();conn.__enter__=Mock(return_value=conn);conn.__exit__=Mock(return_value=False)
        cursor=Mock();cursor.__enter__=Mock(return_value=cursor);cursor.__exit__=Mock(return_value=False)
        conn.cursor.return_value=cursor;cursor.fetchall.return_value=[]
        with patch('prior_day_priority_bus.connect',return_value=conn):
            text=evidence.stock_scan_text('QAUTC','2026-10-27',dsn='fake')
        conn.set_session.assert_called_once_with(readonly=True);conn.close.assert_called_once()
        self.assertIn('لا يثبت',text);self.assertTrue(cursor.execute.call_args.args[0].lstrip().startswith('SELECT'))

@unittest.skipUnless(os.getenv('TEST_POSTGRES_DSN'),'isolated test Postgres required')
class ScannerEvidencePostgresTests(unittest.TestCase):
    def test_atomic_retry_and_out_of_order_first_last(self):
        from prior_day_priority_bus import connect
        from contextlib import closing
        now=datetime.now(timezone.utc);row=dict(symbol='QAUTC',stage='RADAR',reason='UNIT_TEST',rank=2,observed_price=11.)
        newer=evidence.normalize('unit-new-'+now.isoformat(),'iex',[row],now)
        older=evidence.normalize('unit-old-'+now.isoformat(),'iex',[dict(row,observed_price=10.)],now-timedelta(minutes=1))
        dsn=os.environ['TEST_POSTGRES_DSN']
        try:
            self.assertEqual(evidence.write(newer,dsn=dsn)['state'],'SAVED')
            self.assertEqual(evidence.write(newer,dsn=dsn)['state'],'DUPLICATE')
            evidence.write(older,dsn=dsn)
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute("SELECT observations,first_json,last_json FROM apex_scan_evidence WHERE day=%s AND symbol='QAUTC' AND reason='UNIT_TEST'",(newer['day'],))
                    count,first,last=c.fetchone()
            self.assertEqual(count,2);self.assertEqual(json.loads(first)['observed_price'],10.)
            self.assertEqual(json.loads(last)['observed_price'],11.)
            self.assertIn('UNIT_TEST',evidence.stock_scan_text('QAUTC',newer['day'],dsn=dsn))
        finally:
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute("DELETE FROM apex_scan_evidence WHERE symbol='QAUTC' AND reason='UNIT_TEST'")
                    c.execute('DELETE FROM apex_scan_evidence_batches WHERE batch IN (%s,%s)',(newer['batch'],older['batch']))
