import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from market_move_research import MoveStore,utc
from prior_day_priority_bus import build,digest,signal_id
from prior_day_entry_study import stock_trace_text,prepare,simulate,PROTOCOL
from research_nomination_audit import audit_snapshot,checks,local_evidence,nomination_text,study_text
from test_prior_day_bus import packet,SESSION
import test_prior_day_entry_study as study_fixtures


def record(symbol='MATCH'):
    signal=packet((symbol,))['signals'][0]
    return dict(session=SESSION,prepared_at=signal['prepared_at'],
        features=dict(signal['features'],symbol=symbol))


class NominationAuditTests(unittest.TestCase):
    def test_actual_membership_and_preopen_price_failure_are_distinct_from_apex_rejection(self):
        candidate=record(); candidate['features']['close']=.49
        published=build([candidate],SESSION,utc('2026-10-17T04:00Z'))
        self.assertEqual(published['signals'],[])
        text='\n'.join(nomination_text('MATCH',SESSION['date'],published,dict(snapshot=candidate)))
        self.assertIn('السهم غير موجود',text); self.assertIn('0.49 دولار',text)
        self.assertIn('0.5–100',text); self.assertIn('ليست سبب رفض',text)
        self.assertNotIn('لم يجتز حجم',text)
        present='\n'.join(nomination_text('MATCH',SESSION['date'],packet(),dict(snapshot=None)))
        self.assertIn('السهم موجود',present); self.assertIn('لا يثبت وصوله',present)

    def test_late_missing_or_tampered_evidence_cannot_explain_an_exclusion(self):
        published=packet(('OTHER',)); candidate=record()
        for snapshot in (None,dict(candidate,prepared_at=SESSION['open']),record('WRONG')):
            text='\n'.join(nomination_text('MATCH',SESSION['date'],published,dict(snapshot=snapshot)))
            self.assertIn('سبب عدم الترشيح غير محسوم',text)
            self.assertNotIn('لم يجتز',text)
        text='\n'.join(nomination_text('MATCH',SESSION['date'],published,dict(snapshot=candidate)))
        self.assertIn('تعارض',text)
        bad=copy.deepcopy(published); bad['signals'][0]['features']['close']=.1
        text='\n'.join(nomination_text('MATCH',SESSION['date'],bad,dict(snapshot=candidate)))
        self.assertIn('غير صالحة',text); self.assertNotIn('السهم غير موجود',text)

    def test_snapshot_replay_matches_exact_packet_and_never_claims_consumption(self):
        records=[record()]; excluded=record('LOW'); excluded['features']['close']=.3; records.append(excluded)
        p=build(records,SESSION,utc('2026-10-17T04:00Z'))
        published=dict(state='PUBLISHED',packet_id=p['packet_id'],frozen_at=p['frozen_at'])
        r=audit_snapshot(records,SESSION,published)
        self.assertEqual(r['state'],'SAVED_PREOPEN_INPUTS_MATCH_PUBLISHED_PACKET')
        self.assertEqual(r['failed_gates'],{'close':1}); self.assertEqual(r['nominated'],1)
        self.assertFalse(r['execution_authority'])
        records[0]['features']['close']=12.
        self.assertEqual(audit_snapshot(records,SESSION,published)['state'],'SNAPSHOT_PACKET_MISMATCH')
        candidate=record(); candidate['features']['range_pct']=float('nan')
        self.assertIn('range_pct',checks(candidate,SESSION,p['frozen_at'])['unknown'])

    def test_immutable_snapshot_archive_survives_next_session_then_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'moves.db'); candidate=record()
            store.save_pattern_features([candidate['features']],SESSION,utc(candidate['prepared_at']))
            future=dict(SESSION,date='2026-10-20',open='2026-10-20T13:30Z',close='2026-10-20T20:00Z')
            store.save_pattern_features([candidate['features']],future,utc('2026-10-19T21:00Z'))
            evidence=local_evidence('MATCH',SESSION['date'],path=store.path)
            self.assertEqual(evidence['snapshot']['features'],candidate['features'])
            changed=dict(candidate['features'],close=90.)
            self.assertFalse(store.save_pattern_features([changed],SESSION,utc('2026-10-17T06:00Z')))
            with store.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM move_pattern_features').fetchone()[0],2)
            late=dict(SESSION,date='2026-11-20',open='2026-11-20T14:30Z',close='2026-11-20T21:00Z')
            store.save_pattern_features([changed],late,utc('2026-11-19T22:00Z'))
            self.assertIsNone(local_evidence('MATCH',SESSION['date'],path=store.path)['snapshot'])

    def test_prepare_only_reads_current_future_snapshots_not_the_whole_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MoveStore(Path(tmp)/'moves.db'); candidate=record()
            store.save_pattern_features([candidate['features']],SESSION,utc(candidate['prepared_at']))
            with store.connect() as conn:
                conn.execute('INSERT INTO move_pattern_features VALUES (?,?,?)',('2026-10-01','BAD','not json'))
            with patch('prior_day_entry_study.publish',return_value={'state':'PUBLISHED'}):
                r=prepare(store,utc('2026-10-17T05:00Z'),paper=True)
            self.assertEqual(r['queued'],1)

    def test_stock_study_costs_are_separate_and_missing_path_is_not_profit(self):
        f=record()['features']; f.update(entry_protocol=PROTOCOL,priority_signal_id=signal_id('MATCH',SESSION['date']),
            snapshot_prepared_at=record()['prepared_at'])
        result=simulate(study_fixtures.PriorDayEntryTests().data(),SESSION)
        row=dict(frozen_at='2026-10-17T04:00Z',status='COMPLETE',features=f,session=SESSION,
            result={'entry_study':result})
        text='\n'.join(study_text('MATCH',SESSION['date'],dict(studies=[row])))
        self.assertIn('0.10%',text); self.assertIn('0.25%',text); self.assertIn('ليست PnL محققة',text)
        row['status']='PARTIAL_DATA'
        text='\n'.join(study_text('MATCH',SESSION['date'],dict(studies=[row])))
        self.assertIn('غير مكتمل',text); self.assertNotIn('0.10%',text)
        row['status']='COMPLETE'; row['frozen_at']=SESSION['open']
        self.assertIn('لا تُعرض نتيجة ربح','\n'.join(study_text('MATCH',SESSION['date'],dict(studies=[row]))))

    def test_trace_reads_full_stage_times_and_mappings_separately_from_latest_events(self):
        identity=signal_id('MATCH',SESSION['date']); stamp='2026-10-19T14:00Z'
        event=dict(signal_id=identity,symbol='MATCH',stage='APEX_RADAR',reason='RESERVED')
        mapping=dict(signal_id=identity,symbol='MATCH',stage='JALWE_EXECUTION',managed_trade_id='t')
        conn=MagicMock(); conn.__enter__.return_value=conn
        cur=MagicMock(); conn.cursor.return_value.__enter__.return_value=cur
        cur.fetchone.return_value=(json.dumps(packet()),)
        cur.fetchall.side_effect=[ [('APEX_RADAR',900,stamp,'2026-10-19T18:00Z')],
            [(json.dumps(event),)],[(json.dumps(mapping),)]]
        with (patch('prior_day_priority_bus.connect',return_value=conn),
              patch('prior_day_entry_study.native_ledger_rows',return_value=[]) as ledger,
              patch('prior_day_entry_study.events',side_effect=AssertionError('write'))):
            text=stock_trace_text('MATCH',SESSION['date'],dsn='fake',research_path='/does/not/exist.db')
        conn.set_session.assert_called_once_with(readonly=True)
        self.assertIn('900 حدث',text); self.assertIn('2026-10-19 17:00:00',text)
        self.assertIn('2026-10-19 21:00:00',text); self.assertIn('RESERVED',text)
        self.assertEqual(ledger.call_args.args[0],[mapping])
        for call in cur.execute.call_args_list:self.assertTrue(call.args[0].lstrip().startswith('SELECT'))


@unittest.skipUnless(os.getenv('TEST_POSTGRES_DSN'),'test PostgreSQL only')
class NominationTracePostgresTests(unittest.TestCase):
    def test_actual_readonly_packet_stage_queries_roundtrip(self):
        import prior_day_priority_bus as bus
        dsn=os.environ['TEST_POSTGRES_DSN']; p=packet()
        bus.publish(p,paper=True,dsn=dsn)
        row=dict(signal_id=signal_id('MATCH',SESSION['date']),symbol='MATCH',stage='APEX_RADAR',
                 trace_key='nomination-query-test',at='2026-10-19T13:31Z',reason='RESERVED')
        bus.events([row],dsn=dsn)
        text=stock_trace_text('MATCH',SESSION['date'],dsn=dsn,research_path='/does/not/exist.db')
        self.assertNotIn('تعذرت قراءة',text)
        self.assertIn('السهم موجود',text); self.assertIn('2026-10-19 16:31:00',text)
        self.assertIn('RESERVED',text); self.assertIn('لا يوجد دليل خروج',text)
