from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from market_jump_paths import VERSION, candidates, jump_path
from market_jump_requests import (REQUEST_SYMBOLS, queue_requested, requested_keys,
                                  requested_bounds, schedule_studies, request_summary, valid_bound)
from market_move_research import MoveStore, utc, observe_session, read_packet, report_text, stable_key
from market_move_research_loop import MoveResearchWorker
import test_market_move_research as fixtures


class RequestedJumpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = MoveStore(Path(self.tmp.name)/'moves.db')
        self.now = utc('2026-10-14T05:00:00Z')
        self.sessions = [fixtures.session(d) for d in
            ('2026-10-02','2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09',
             '2026-10-12','2026-10-13','2026-10-14')]

    def test_explicit_six_symbols_thirty_sessions_need_no_portfolio_or_daily_prices(self):
        manifest = queue_requested(self.store,self.sessions,self.now)
        rows = self.store.episodes()
        self.assertEqual(manifest['selected'],30)
        self.assertEqual(len(rows),30)
        self.assertEqual({r['symbol'] for r in rows},set(REQUEST_SYMBOLS))
        self.assertTrue(all(r['role']=='JUMP_CASE' and not r['prospective'] for r in rows))
        self.assertTrue(all('close' not in r['features'] for r in rows))
        self.assertEqual(request_summary(rows,self.store.state('jump_request'))['counts'],{'PENDING':30})

    def test_requests_respect_completed_calendar_and_availability(self):
        now = utc('2026-10-06T20:15:59Z')
        keys = requested_keys(self.sessions,now)
        self.assertEqual({d for _,d in keys},{'2026-10-05'})
        keys = requested_keys(self.sessions,now+timedelta(seconds=1))
        self.assertEqual({d for _,d in keys},{'2026-10-05','2026-10-06'})

    def test_old_episode_reused_unchanged_and_request_deduplicates_after_restart(self):
        s = self.sessions[1]
        f = dict(fixtures.feature('MI'),jump_protocol=VERSION)
        pair = stable_key('MI|'+VERSION,s['date'])
        self.store.add(f,s,'JUMP_CASE',pair,self.now,prospective=False)
        before = self.store.episodes()[0]
        self.store.settle(before['episode_key'],dict(status='DATA_UNAVAILABLE',outcome=None),self.now)
        before = self.store.episodes()[0]
        queue_requested(self.store,self.sessions,self.now)
        queue_requested(MoveStore(self.store.path),self.sessions,self.now)
        self.assertEqual(len(self.store.episodes()),30)
        self.assertEqual(before,next(r for r in self.store.episodes() if r['episode_key']==before['episode_key']))

    def row(self, symbol, day='2026-10-09', bound=10, prospective=False, status='PENDING'):
        return dict(symbol=symbol,session_day=day,features={'daily_screen_bound_pct':bound},
                    prospective=prospective,role='ACTIVITY' if prospective else 'JUMP_CASE',
                    status=status,attempts=0,episode_key=symbol+day)

    def test_forward_first_eight_requested_seats_leave_discovery_budget(self):
        rows = [self.row('R'+str(i)) for i in range(15)]+[self.row('D'+str(i),bound=100+i) for i in range(15)]
        keys = {(r['symbol'],r['session_day']) for r in rows if r['symbol'].startswith('R')}
        selected = schedule_studies(rows,keys)
        self.assertEqual(len(selected),12)
        self.assertEqual(sum(r['symbol'].startswith('R') for r in selected),8)
        self.assertEqual([r['symbol'] for r in selected[-4:]],['D14','D13','D12','D11'])
        forward = [self.row('F'+str(i),prospective=True) for i in range(20)]
        self.assertTrue(all(r['prospective'] for r in schedule_studies(rows+forward,keys)))
        self.assertEqual(len(schedule_studies(rows+forward,keys)),12)

    def test_strongest_bound_read_first_but_new_observations_precede_retries(self):
        rows = [self.row('OLDER_BIG','2026-10-05',1000),self.row('NEW_SMALL','2026-10-13',10),
                self.row('RETRY','2026-10-13',2000,status='PARTIAL_DATA'),self.row('NAN',bound=float('nan'))]
        ordered = schedule_studies(rows,set())
        self.assertEqual([r['symbol'] for r in ordered],['OLDER_BIG','NEW_SMALL','NAN','RETRY'])
        self.assertEqual(ordered,schedule_studies(list(reversed(rows)),set()))

    def test_daily_overlay_prioritizes_quiet_requested_jump_without_rewriting_store(self):
        queue_requested(self.store,self.sessions,self.now)
        before = self.store.episodes()
        bars = [dict(fixtures.bar(s['date']),o=2.34,h=2.34,l=2.34,c=2.34,v=597)
                for s in self.sessions if s['date']<='2026-10-09']
        event = next(b for b in bars if b['t'].startswith('2026-10-07'))
        event.update(o=2.34,h=7.02,l=2.22,c=5.07,v=29811117)
        bounds = requested_bounds({'PFAI':bars},self.sessions,self.now)
        self.assertGreater(bounds['PFAI|2026-10-07'],200)
        selected = schedule_studies(before,requested_keys(self.sessions,self.now),bounds=bounds)
        self.assertEqual((selected[0]['symbol'],selected[0]['session_day']),('PFAI','2026-10-07'))
        self.assertEqual(self.store.episodes(),before)

    def test_overlay_cannot_affect_forward_or_unrequested_lane(self):
        forward = self.row('F',prospective=True,bound=5)
        other = self.row('OTHER',bound=10)
        requested = self.row('MI',bound=0)
        bounds = {'F|2026-10-09':100000,'OTHER|2026-10-09':100000,'MI|2026-10-09':500}
        selected = schedule_studies([other,requested,forward],{('MI','2026-10-09')},bounds=bounds)
        self.assertEqual(selected,[forward,requested,other])
        self.assertEqual(other['features']['daily_screen_bound_pct'],10)

    def test_invalid_or_overflowing_priority_values_cannot_interrupt_scheduling(self):
        for value in (True,'1000',float('nan'),float('inf'),-1,10**1000):
            self.assertEqual(valid_bound(value),0.)
        rows = [self.row('HUGE',bound=10**1000),self.row('VALID',bound=20)]
        self.assertEqual(schedule_studies(rows,set())[0]['symbol'],'VALID')

    def test_quiet_before_jump_and_price_outside_trade_range_are_researchable(self):
        days = self.sessions[:2]
        bars = [dict(fixtures.bar(days[0]['date']),v=1),
                dict(fixtures.bar(days[1]['date']),h=15.,v=100000)]
        self.assertEqual(len(list(candidates(['QUIET'],{'QUIET':bars},days,self.now))),1)
        tiny = [dict(r,o=.1,h=.2,l=.09,c=.1,v=2000000) for r in bars]
        self.assertEqual(len(list(candidates(['TINY'],{'TINY':tiny},days,self.now))),1)
        tiny[-1]['v'] = 1; tiny[0]['v'] = 1
        self.assertEqual(list(candidates(['TINY'],{'TINY':tiny},days,self.now)),[])

    def test_daily_read_failure_does_not_block_explicit_intraday_studies(self):
        provider = Mock(); provider.calendar.return_value = self.sessions; provider.assets.return_value = ['OTHER']
        provider.daily.side_effect = RuntimeError('no daily data')
        provider.intraday.side_effect = lambda symbol,s,now:fixtures.frame(s)
        provider.news_before.return_value = {'status':'AVAILABLE','articles':[]}
        packet = MoveResearchWorker(self.store,provider).cycle(self.now)
        self.assertEqual(packet['health']['reason'],'DAILY_READ_FAILED_REQUESTED_STUDIES_CONTINUE')
        self.assertEqual(packet['requested_jumps']['counts'],{'COMPLETE':12,'PENDING':18})
        self.assertEqual(packet['comparison']['complete_pairs'],0)
        self.assertEqual(packet['prospective_episodes'],0)
        self.assertFalse(packet['automatic_application'])

    def test_partial_huge_paths_cannot_lead_completed_ranking_and_report_is_separate(self):
        queue_requested(self.store,self.sessions,self.now)
        rows = self.store.episodes()
        one = rows[0]; s = one['session']; data = fixtures.frame(s)
        data.iloc[40] = [10.,10.,8.,9.,200.]; data.iloc[70] = [10.,15.,9.,12.,1000.]
        result = observe_session(data,s); result['jump_path'] = jump_path(data,s,full_session_complete=True)
        self.store.settle(one['episode_key'],result,self.now)
        second = rows[1]
        self.store.settle(second['episode_key'],dict(status='PARTIAL_DATA',outcome=None,missing_minutes=1,
            jump_path={'status':'OBSERVED_SEGMENT','strong_jump':True,'observed_rise_pct':10000}),self.now)
        _,packet = self.store.publish(self.now,{'state':'READY','completed_session':'2026-10-13'})
        self.assertEqual(len(packet['strongest_completed_jumps']),1)
        self.assertEqual(packet['strongest_completed_jumps'][0]['symbol'],one['symbol'])
        loaded = read_packet(path=self.store.path,now=self.now)
        self.assertEqual(loaded['requested_jumps']['counts'],{'COMPLETE':1,'PARTIAL_DATA':1,'PENDING':28})
        text = report_text(loaded)
        self.assertIn('طلب دراسة 2026-10-05 إلى 2026-10-09',text)
        self.assertIn('ليس ترتيب السوق كله',text)
        self.assertNotIn('+10000.00%',text)
        self.assertEqual(packet['comparison']['complete_pairs'],0)


if __name__=='__main__':
    unittest.main()
