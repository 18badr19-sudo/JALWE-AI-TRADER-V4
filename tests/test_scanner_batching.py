import os
import queue
import unittest
from datetime import datetime, timezone
from datetime import timedelta
from contextlib import closing
from unittest.mock import Mock, patch

import scanner_evidence as evidence


class ScannerBatchingTests(unittest.TestCase):
    def packet(self, index=0, count=1):
        return dict(batch=str(index), day='2026-10-29', at='2026-10-29T10:00:00+00:00',
                    scan_id=str(index), feed='iex', rows=[dict(symbol='QA',
                    stage='RADAR', reason='TEST')]*count)

    def test_packet_and_row_bounds_are_independent_and_released_on_read(self):
        pending=evidence.EvidenceQueue(maxsize=2,max_rows=3)
        pending.put_nowait(self.packet(count=2))
        with self.assertRaises(queue.Full):pending.put_nowait(self.packet(count=2))
        pending.put_nowait(self.packet(count=1))
        with self.assertRaises(queue.Full):pending.put_nowait(self.packet(count=0))
        self.assertEqual(pending.pending_rows,3)
        self.assertEqual(len(pending.get_batch()),2)
        self.assertEqual(pending.pending_rows,0)
        pending.task_done();pending.task_done();pending.join()

    def test_worker_groups_writes_and_acknowledges_only_after_commit(self):
        pending=evidence.EvidenceQueue()
        packets=[self.packet(i) for i in range(40)]
        for packet in packets:pending.put_nowait(packet)
        results=[dict(state='SAVED',rows=1,scan_id=str(i)) for i in range(32)]
        def commit(batch):
            self.assertEqual(batch,packets[:32])
            self.assertEqual(pending.unfinished_tasks,40)
            return results
        with patch.object(evidence,'_queue',pending), \
             patch.object(evidence,'write_many',side_effect=commit), \
             patch.object(evidence.LOG,'info') as logged:
            evidence._work_once()
        self.assertEqual(logged.call_count,32)
        self.assertEqual(pending.qsize(),8)
        self.assertEqual(pending.unfinished_tasks,8)

    def test_failed_group_never_logs_saved_or_blocks_later_groups(self):
        pending=evidence.EvidenceQueue()
        pending.put_nowait(self.packet())
        with patch.object(evidence,'_queue',pending), \
             patch.object(evidence,'write_many',side_effect=TimeoutError()), \
             patch.object(evidence.LOG,'info') as saved, \
             patch.object(evidence.LOG,'warning') as warning:
            evidence._work_once()
        saved.assert_not_called();warning.assert_called_once()
        self.assertEqual(pending.unfinished_tasks,0)
        pending.put_nowait(self.packet(1))
        self.assertEqual(len(pending.get_batch()),1)
        pending.task_done()

    def test_bulk_input_limits_fail_before_database_connection(self):
        with self.assertRaises(ValueError):evidence.write_many([self.packet()]*33)
        with self.assertRaises(ValueError):evidence.write_many([self.packet(count=40001)])
        self.assertEqual(evidence.write_many([]),[])

    def test_sixty_four_small_packets_survive_paused_writer(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.dict(os.environ, {'DATABASE_URL': 'dummy'}), \
             patch.object(evidence, '_thread', worker):
            statuses = [evidence.submit('burst', 'iex', [dict(symbol=f'QA{i}',
                stage='RADAR', reason='TEST')]) for i in range(64)]
            try:
                self.assertEqual(statuses, ['QUEUED_NOT_CONFIRMED'] * 64)
            finally:
                while not evidence._queue.empty():
                    evidence._queue.get_nowait()
                    evidence._queue.task_done()


@unittest.skipUnless(os.getenv('TEST_POSTGRES_DSN'),'isolated test Postgres required')
class ScannerBatchingPostgresTests(unittest.TestCase):
    def test_group_uses_one_connection_and_preserves_retry_first_last(self):
        from prior_day_priority_bus import connect
        now=datetime.now(timezone.utc)
        row=dict(symbol='QABATCH',stage='RADAR',reason='BATCH_TEST',observed_price=11.)
        newer=evidence.normalize('batch-new-'+now.isoformat(),'iex',[row],now)
        older=evidence.normalize('batch-old-'+now.isoformat(),'iex',
            [dict(row,observed_price=10.)],now-timedelta(minutes=1))
        dsn=os.environ['TEST_POSTGRES_DSN']
        try:
            with patch('prior_day_priority_bus.connect',wraps=connect) as opened:
                result=evidence.write_many([newer,older,newer],dsn=dsn)
                self.assertEqual(opened.call_count,1)
            self.assertEqual([r['state'] for r in result],['SAVED','SAVED','DUPLICATE'])
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute("SELECT observations,first_json,last_json FROM apex_scan_evidence WHERE symbol='QABATCH' AND reason='BATCH_TEST'")
                    count,first,last=c.fetchone()
            import json
            self.assertEqual(count,2)
            self.assertEqual(json.loads(first)['observed_price'],10.)
            self.assertEqual(json.loads(last)['observed_price'],11.)
        finally:
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute("DELETE FROM apex_scan_evidence WHERE symbol='QABATCH' AND reason='BATCH_TEST'")
                    c.execute('DELETE FROM apex_scan_evidence_batches WHERE batch IN (%s,%s)',(newer['batch'],older['batch']))

    def test_second_packet_failure_rolls_back_first_packet_and_retry_can_save(self):
        from prior_day_priority_bus import connect
        now=datetime.now(timezone.utc)
        row=dict(symbol='QAROLLBACK',stage='RADAR',reason='BATCH_TEST')
        first=evidence.normalize('rollback-first-'+now.isoformat(),'iex',[row],now)
        bad=evidence.normalize('rollback-bad-'+now.isoformat(),'iex',[row],now)
        bad['rows']=[dict(symbol='QAROLLBACK')]
        dsn=os.environ['TEST_POSTGRES_DSN']
        seed=evidence.normalize('rollback-schema-'+now.isoformat(),'iex',[],now)
        evidence.write(seed,dsn=dsn)
        try:
            with self.assertRaises(KeyError):evidence.write_many([first,bad],dsn=dsn)
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute('SELECT COUNT(*) FROM apex_scan_evidence_batches WHERE batch IN (%s,%s)',(first['batch'],bad['batch']))
                    self.assertEqual(c.fetchone()[0],0)
            self.assertEqual(evidence.write(first,dsn=dsn)['state'],'SAVED')
        finally:
            with closing(connect(dsn)) as conn,conn:
                with conn.cursor() as c:
                    c.execute("DELETE FROM apex_scan_evidence WHERE symbol='QAROLLBACK' AND reason='BATCH_TEST'")
                    c.execute('DELETE FROM apex_scan_evidence_batches WHERE batch IN (%s,%s,%s)',(first['batch'],bad['batch'],seed['batch']))

