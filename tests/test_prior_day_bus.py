import copy
import os
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import prior_day_priority_bus as bus

NOW=bus.utc('2026-10-19T14:00:00Z')
SESSION=dict(date='2026-10-19',open='2026-10-19T13:30:00Z',close='2026-10-19T20:00:00Z')


def packet(symbols=('MATCH',)):
    records=[dict(session=SESSION,prepared_at='2026-10-17T00:00:00Z',features=dict(symbol=s,
        close=10.,dollar_volume=1000000.,range_pct=8.,volume_ratio_previous_day=2.,daily_return_pct=3.,
        source_feed='sip',evidence_day='2026-10-16',available_after='2026-10-16T20:16:00Z')) for s in symbols]
    return bus.build(records,SESSION,bus.utc('2026-10-17T04:00:00Z'))


class PriorityBusTests(unittest.TestCase):
    def test_packet_is_preopen_fixed_conjunction_without_order_authority(self):
        p=packet(); self.assertTrue(bus.validate(p)); self.assertFalse(p['execution_authority'])
        self.assertEqual(p['signals'][0]['signal_id'],bus.signal_id('MATCH',SESSION['date']))
        self.assertIsNone(bus.build([],SESSION,bus.utc(SESSION['open'])))
        self.assertIsNone(bus.build([],SESSION,bus.utc('2026-10-16T00:00:00Z')))

    def test_tampering_future_boolean_and_duplicate_inputs_cannot_apply(self):
        for mutate in (lambda p:p['signals'][0]['features'].update(range_pct=True),
                       lambda p:p['signals'][0]['features'].update(source_feed='iex'),
                       lambda p:p['signals'][0].update(prepared_at=SESSION['open']),
                       lambda p:p['signals'].append(copy.deepcopy(p['signals'][0])),
                       lambda p:p.update(execution_authority=True)):
            p=packet(); mutate(p); p['packet_id']=bus.digest({k:v for k,v in p.items() if k!='packet_id'})
            self.assertFalse(bus.validate(p))
        p=packet(); p['signals'][0]['features']['range_pct']=9
        self.assertFalse(bus.validate(p))

    def test_all_three_bounds_are_required_and_nonfinite_stays_unknown(self):
        p=packet()
        for key in bus.RULE:
            for value in (bus.RULE[key]-.01,None,float('nan'),float('inf')):
                s=copy.deepcopy(p['signals'][0]); s['features'][key]=value
                self.assertFalse(bus.valid_signal(s,SESSION))

    def test_disabled_live_and_expired_never_connect(self):
        with patch.object(bus,'connect') as connect:
            self.assertIsNone(bus.load(NOW,paper=False)[0])
            self.assertIsNone(bus.load(NOW,paper=True,allow_live=True)[0])
            self.assertIsNone(bus.load(bus.utc(bus.END),paper=True)[0])
            with patch.dict(os.environ,{'JALWE_PRIOR_DAY_TRIAL_ENABLED':'false'}):
                self.assertIsNone(bus.load(NOW,paper=True)[0])
            connect.assert_not_called()

    def test_reader_failure_does_not_expose_provider_or_credentials(self):
        with patch.object(bus,'connect',side_effect=RuntimeError('private secret')):
            p,a=bus.load(NOW,paper=True,dsn='test')
        self.assertIsNone(p); self.assertEqual(a['error_type'],'RuntimeError')
        self.assertNotIn('private',str(a))


@unittest.skipUnless(os.getenv('TEST_POSTGRES_DSN'),'test PostgreSQL only')
class PriorityBusPostgresTests(unittest.TestCase):
    def test_shared_roundtrip_immutable_packet_and_event_deduplication(self):
        dsn=os.environ['TEST_POSTGRES_DSN']; p=packet()
        # Only this CI database/test day is modified; never a production DSN.
        first=bus.publish(p,paper=True,dsn=dsn)
        self.assertEqual(first['state'],'PUBLISHED')
        changed=copy.deepcopy(p); changed['signals'][0]['features']['range_pct']=9.
        changed['packet_id']=bus.digest({k:v for k,v in changed.items() if k!='packet_id'})
        self.assertEqual(bus.publish(changed,paper=True,dsn=dsn)['packet_id'],first['packet_id'])
        received,audit=bus.load(NOW,paper=True,dsn=dsn)
        self.assertEqual(received,p); self.assertEqual(audit['state'],'READY')
        self.assertIsNone(bus.load(bus.utc(SESSION['close']),paper=True,dsn=dsn)[0])
        event=dict(signal_id=p['signals'][0]['signal_id'],stage='TEST',trace_key='fixed',at=NOW.isoformat())
        bus.events([event,event],dsn=dsn)
        with bus.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM jalwe_prior_day_events WHERE event_id=%s',(bus.digest([event['signal_id'],'TEST','fixed']),))
                self.assertEqual(cur.fetchone()[0],1)

    def test_publish_crossing_open_rolls_back_transaction(self):
        dsn=os.environ['TEST_POSTGRES_DSN']; p=packet(('OTHER',))
        self.assertEqual(bus.publish(packet(),paper=True,dsn=dsn)['state'],'PUBLISHED')
        p['session']=dict(SESSION,date='2026-10-20',open='2026-10-20T13:30:00Z',close='2026-10-20T20:00:00Z')
        p['signals'][0]['signal_id']=bus.signal_id('OTHER','2026-10-20')
        p['packet_id']=bus.digest({k:v for k,v in p.items() if k!='packet_id'})
        result=bus.publish(p,paper=True,dsn=dsn,clock=lambda:bus.utc(p['session']['open']))
        self.assertEqual(result['state'],'CROSSED_SESSION_OPEN')
        with bus.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM jalwe_prior_day_packets WHERE protocol=%s AND session_day=%s',(bus.PROTOCOL,'2026-10-20'))
                self.assertEqual(cur.fetchone()[0],0)
