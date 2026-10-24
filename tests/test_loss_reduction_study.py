from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import json,sqlite3,tempfile,unittest
import pandas as pd
import loss_reduction_study as study
from test_decision_outcome_memory import Store
from decision_outcome_memory import DecisionOutcomeMemory
import test_strategy_experiments as fixtures


def plan(local='2026-10-21T15:29:00-04:00',close_hour=16):
    at=study.utc(local);day=at.astimezone(study.NY).date()
    opening=datetime.combine(day,datetime.min.time(),tzinfo=study.NY)+timedelta(hours=9,minutes=30)
    close=datetime.combine(day,datetime.min.time(),tzinfo=study.NY)+timedelta(hours=close_hour)
    rows=[dict(at=(at-timedelta(minutes=i)).isoformat(),low=9.5) for i in (5,4,3,2,1)]
    return dict(protocol=study.PROTOCOL,spec_hash=study.SPEC_HASH,decided_at=at.isoformat(),
        calendar=dict(source='FOCUSED_PREOPEN_ALPACA_CALENDAR',frozen_at=(opening-timedelta(hours=1)).isoformat(),
            session=dict(date=str(day),open=opening.isoformat(),close=close.isoformat())),
        trigger=10.,stop=9.8,decision_feed='iex',stop_context=dict(atr=.2,atr_timeframe='1m',source_feed='iex',low_bars=rows))


def frame(p,feed='iex'):
    _,_,_,start,end,_=study.validate_plan(p)
    data=pd.DataFrame(dict(open=10.05,high=10.3,low=9.9,close=10.2,volume=100.),
        index=pd.date_range(start,end,freq='min',inclusive='left'))
    data.attrs['data_feed']=feed
    return data


def evaluate(p,feed='iex',data=None):
    end=study.validate_plan(p)[4]
    return study.evaluate(p,frame(p,feed) if data is None else data,feed,end+timedelta(minutes=16))


class StudyMathTests(unittest.TestCase):
    def test_cutoff_is_checked_at_common_entry_not_just_decision(self):
        p=plan();r=evaluate(p)
        for rules in r['rules'].values():
            self.assertTrue(rules['CURRENT']['traded'])
            self.assertEqual(rules['CUTOFF30']['outcome'],'BLOCKED_BY_PREDECLARED_CUTOFF')
        p=plan('2026-10-21T15:28:00-04:00')
        self.assertTrue(evaluate(p)['rules']['0.001']['CUTOFF30']['traded'])

    def test_early_close_and_dst_use_actual_frozen_calendar(self):
        for local,hour in [('2026-11-27T12:29:00-05:00',13),('2026-11-27T12:28:00-05:00',13),
                           ('2026-10-21T15:29:00-04:00',16),('2026-11-05T15:29:00-05:00',16)]:
            p=plan(local,hour);r=evaluate(p)
            self.assertEqual(r['state'],'COMPLETE')
            self.assertEqual(r['rules']['0.001']['CUTOFF30']['traded'],':28:' in local)
            self.assertEqual(study.utc(r['window_end']),study.utc(p['calendar']['session']['close']))

    def test_same_entry_fixed_risk_smaller_quantity_and_independent_cost_reference(self):
        p=plan('2026-10-21T14:00:00-04:00');r=evaluate(p)
        self.assertEqual(r['state'],'COMPLETE')
        for cost in study.SPEC['costs_each_side']:
            rules=r['rules'][str(cost)];a,b=rules['CURRENT'],rules['STRUCTURE']
            self.assertEqual(a['entry_fill'],b['entry_fill']);self.assertEqual(a['entry_at'],b['entry_at'])
            self.assertLess(b['quantity'],a['quantity'])
            for v in rules.values():
                self.assertLessEqual(v['risk_dollars'],1.)
                self.assertLessEqual(v['quantity']*v['entry_fill'],100.)
                self.assertAlmostEqual(v['target'],v['entry_fill']+2*(v['entry_fill']-v['stop']))
                expected=v['quantity']*(10.2*(1-cost)-10.05*(1+cost))
                self.assertAlmostEqual(v['net_pnl'],expected)
        self.assertFalse(r['execution_authority']);self.assertFalse(r['automatic_promotion'])
        self.assertEqual(len(r['bar_evidence']),60);self.assertEqual(len(r['data_hash']),64)

    def test_wider_stop_can_hold_while_current_stops_without_adding_risk(self):
        p=plan('2026-10-21T14:00:00-04:00');data=frame(p)
        data.loc[data.index[1],'low']=9.7
        r=evaluate(p,data=data)['rules']['0.001']
        self.assertEqual(r['CURRENT']['outcome'],'STOP')
        self.assertEqual(r['STRUCTURE']['outcome'],'TIME_EXIT')
        self.assertLess(r['CURRENT']['net_pnl'],0);self.assertGreater(r['STRUCTURE']['net_pnl'],0)

    def test_cutoff_can_miss_a_winner_and_prevent_a_loss(self):
        p=plan();data=frame(p)
        positive=evaluate(p,data=data)['rules']['0.001']
        self.assertGreater(positive['CURRENT']['net_pnl'],0)
        self.assertEqual(positive['CUTOFF30']['net_pnl'],0)
        data.loc[data.index[1],'low']=9.7
        negative=evaluate(p,data=data)['rules']['0.001']
        self.assertLess(negative['CURRENT']['net_pnl'],0)
        self.assertEqual(negative['CUTOFF30']['net_pnl'],0)

    def test_partial_duplicate_unordered_invalid_and_wrong_feed_remain_unknown(self):
        p=plan();good=frame(p)
        cases=[good.iloc[:-1],pd.concat([good,good.iloc[-1:]]),good.iloc[::-1],good.set_axis(good.index.tz_localize(None))]
        for column,value in [('low',float('nan')),('volume',True),('open','10'),('high',8.),('volume',-1.)]:
            bad=good.copy().astype(object);bad.loc[bad.index[2],column]=value;cases.append(bad)
        wrong=good.copy();wrong.attrs['data_feed']='sip';cases.append(wrong)
        for data in cases:
            r=evaluate(p,data=data);self.assertEqual(r['state'],'UNKNOWN');self.assertIsNone(r['rules'])

    def test_future_or_missing_stop_context_and_unknown_calendar_never_make_zero_pnl(self):
        for kind in ('future','naive','missing','atr_bool','atr_nan','wrong_feed','duplicate','old_protocol','bad_calendar_seconds'):
            p=plan();end=study.validate_plan(p)[4];data=frame(p)
            if kind=='future':p['stop_context']['low_bars'][-1]['at']=p['decided_at']
            elif kind=='naive':p['stop_context']['low_bars'][-1]['at']='2026-10-21T19:28:00'
            elif kind=='missing':p['calendar']=None
            elif kind=='atr_bool':p['stop_context']['atr']=True
            elif kind=='atr_nan':p['stop_context']['atr']=float('nan')
            elif kind=='wrong_feed':p['stop_context']['source_feed']='sip'
            elif kind=='duplicate':p['stop_context']['low_bars'][-1]=p['stop_context']['low_bars'][0]
            elif kind=='bad_calendar_seconds':p['calendar']['session']['close']='2026-10-21T20:00:30Z'
            else:p['protocol']='old'
            r=study.evaluate(p,data,'iex',end+timedelta(minutes=20))
            self.assertEqual(r['state'],'UNKNOWN',kind);self.assertIsNone(r['rules'])

    def test_same_bar_order_is_unknown_even_if_cutoff_arm_would_skip(self):
        p=plan();data=frame(p);data.loc[data.index[1],['high','low']]=[11.,9.7]
        self.assertEqual(evaluate(p,data=data)['reason'],'AMBIGUOUS_STOP_TARGET')

    def test_sip_delay_and_feed_are_independent_of_original_decision_feed(self):
        p=plan();data=frame(p,'sip');end=study.validate_plan(p)[4]
        self.assertEqual(study.evaluate(p,data,'sip',end+timedelta(minutes=15))['state'],'UNKNOWN')
        r=study.evaluate(p,data,'sip',end+timedelta(minutes=16))
        self.assertEqual(r['state'],'COMPLETE');self.assertEqual(r['observation_feed'],'sip')
        self.assertEqual(p['decision_feed'],'iex')

    def test_prices_after_close_cannot_change_result(self):
        p=plan();data=frame(p);before=evaluate(p,data=data)
        data.loc[data.index[-1]+timedelta(minutes=1)]=[10.,999.,.01,500.,1000.]
        self.assertEqual(before,evaluate(p,data=data))

    def test_no_baseline_activation_is_a_known_no_trade_not_an_unknown(self):
        p=plan();data=frame(p).assign(open=9.9,high=9.99,low=9.85,close=9.95)
        r=evaluate(p,data=data)
        self.assertEqual(r['state'],'COMPLETE')
        self.assertTrue(all(not v['traded'] and v['net_pnl']==0 for v in r['rules']['0.001'].values()))

    def test_decision_context_helper_never_mutates_or_raises_for_bad_optional_atr(self):
        p=plan();data=frame(p);before=data.copy()
        for atr in (None,float('nan'),True,'bad',10**1000):
            c=study.stop_context(data,atr,'1m','iex');self.assertIsNone(c['atr'])
        pd.testing.assert_frame_equal(before,data)


class CalendarAndIntegrationTests(unittest.TestCase):
    setUp=fixtures.StrategyExperimentTests.setUp
    payload=fixtures.StrategyExperimentTests.payload
    analysis=fixtures.StrategyExperimentTests.analysis
    bars=fixtures.StrategyExperimentTests.bars

    def configured(self):
        p=plan('2026-10-02T09:45:00-04:00');payload=self.payload()
        payload['jalwe']['metadata']['loss_study_stop_context']=p['stop_context']
        self.engine.loss_study.calendar_reader=lambda at:deepcopy(p['calendar'])
        return p,payload

    def test_freezes_one_selected_candidate_and_deduplicates_without_retrofit(self):
        p,payload=self.configured();self.memory.record(payload,'study')
        with self.db.connection() as c:
            row=dict(c.execute('SELECT * FROM loss_reduction_trials').fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM loss_reduction_trials').fetchone()[0],1)
        self.engine.loss_study.calendar_reader=lambda at:None
        self.assertFalse(self.memory.record(payload,'study'))
        with self.db.connection() as c:self.assertEqual(row,dict(c.execute('SELECT * FROM loss_reduction_trials').fetchone()))

    def test_ineligible_candidates_do_not_become_study_opportunities(self):
        _,payload=self.configured();payload['jalwe']['gates']['opportunity']=False
        self.memory.record(payload,'ineligible')
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM loss_reduction_trials').fetchone()[0],0)

    def test_iex_observation_is_added_without_changing_baseline_simulation(self):
        p,payload=self.configured();self.memory.record(payload,'iex-study');data=self.bars();data.attrs['data_feed']='iex'
        self.memory.market_data=type('Reader',(),{'get_observation_bars':lambda _,*args:data})()
        self.memory.update_due(now=study.utc(payload['timestamp'])+timedelta(minutes=75))
        with self.db.connection() as c:
            r=json.loads(c.execute('SELECT result_json FROM loss_reduction_results').fetchone()[0])
            self.assertEqual(r['state'],'COMPLETE')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM strategy_preferences').fetchone()[0],0)
        self.assertIn('iex: مكتملة 1',self.engine.loss_study.text())
        self.assertIn('sip: مكتملة 0',self.engine.loss_study.text())

    def test_sip_reuses_existing_observation_and_preserves_decision_feed(self):
        from unittest.mock import Mock
        p,payload=self.configured();self.memory.record(payload,'sip-study')
        data=self.bars();data.attrs['data_feed']='sip';market=Mock();market.get_research_observation_bars.return_value=data
        self.memory.delayed_sip_research.market_data=market
        self.memory.delayed_sip_research.update_due(now=study.utc(payload['timestamp'])+timedelta(minutes=90))
        with self.db.connection() as c:
            row=c.execute("SELECT * FROM loss_reduction_results WHERE feed='sip'").fetchone()
            self.assertIsNotNone(row);self.assertEqual(json.loads(row['result_json'])['state'],'COMPLETE')
            self.assertEqual(json.loads(c.execute('SELECT plan_json FROM loss_reduction_trials').fetchone()[0])['decision_feed'],'iex')
        market.get_observation_bars.assert_not_called()

    def test_missing_data_stays_unknown_then_complete_is_idempotent(self):
        p,payload=self.configured();self.memory.record(payload,'recover')
        end=study.utc(payload['timestamp'])+timedelta(minutes=60);data=self.bars();data.attrs['data_feed']='iex'
        with self.db.connection() as c:
            key=c.execute('SELECT decision_key FROM loss_reduction_trials').fetchone()[0]
            self.engine.loss_study.observe(c,key,data.iloc[:-1],'iex',end)
            self.assertEqual(json.loads(c.execute('SELECT result_json FROM loss_reduction_results').fetchone()[0])['state'],'UNKNOWN')
            self.engine.loss_study.observe(c,key,data,'iex',end)
            saved=tuple(c.execute('SELECT * FROM loss_reduction_results').fetchone())
            self.engine.loss_study.observe(c,key,data,'iex',end+timedelta(hours=1))
            self.assertEqual(saved,tuple(c.execute('SELECT * FROM loss_reduction_results').fetchone()))

    def test_read_command_creates_no_files_or_changes(self):
        missing=Path(str(self.db.path)+'.absent')
        self.assertIn('لا توجد',study.read_report(missing));self.assertFalse(missing.exists())
        _,payload=self.configured();self.memory.record(payload,'read')
        before=Path(self.db.path).read_bytes();self.assertIn('بحث فقط',study.read_report(self.db.path))
        self.assertEqual(before,Path(self.db.path).read_bytes())
        import railway_controller
        with patch.object(study,'read_report',return_value='saved study') as read:
            self.assertEqual(railway_controller.handle('/loss_study'),'saved study');read.assert_called_once()

    def test_calendar_reader_is_readonly_preopen_and_uses_exact_date(self):
        path=Path(str(self.db.path)+'.calendar');p=plan()
        self.assertIsNone(study.frozen_session(study.utc(p['decided_at']),path));self.assertFalse(path.exists())
        manifest=dict(protocol='focused-preopen-sip-collection-v1',session=p['calendar']['session'],frozen_at=p['calendar']['frozen_at'])
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE focused_collection_sessions(session_day TEXT,manifest_json TEXT)')
            c.execute('INSERT INTO focused_collection_sessions VALUES (?,?)',(p['calendar']['session']['date'],json.dumps(manifest)))
        before=path.read_bytes();found=study.frozen_session(study.utc(p['decided_at']),path)
        self.assertEqual(found['session'],p['calendar']['session']);self.assertEqual(before,path.read_bytes())
        self.assertIsNone(study.frozen_session(study.utc(p['decided_at'])+timedelta(days=1),path))
        manifest['frozen_at']=p['decided_at']
        with sqlite3.connect(path) as c:c.execute('UPDATE focused_collection_sessions SET manifest_json=?',(json.dumps(manifest),))
        self.assertIsNone(study.frozen_session(study.utc(p['decided_at']),path))


if __name__=='__main__':unittest.main()
