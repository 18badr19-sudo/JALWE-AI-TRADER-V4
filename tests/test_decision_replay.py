import copy
from contextlib import nullcontext
from dataclasses import fields, MISSING
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core.models import FeatureSnapshot, SignalAction, RiskDecision
from intelligence.ai_engine import AIAnalysis
from intelligence.decision_engine import DecisionEngine, DecisionState
from intelligence.opportunity_engine import OpportunityAnalysis, OpportunityGrade, StrategyName
from intelligence.strategy_router import RoutedOpportunity
from intelligence.session_strategy_engine import SessionStrategyAnalysis, SessionStrategyCandidate, SessionStrategyName
from intelligence.trigger_engine import TriggerDecision, TriggerState
from intelligence.breakout_confirmation_engine import BreakoutConfirmation, BreakoutConfirmationState
from market.market_regime import MarketRegime, MarketRegimeResult
from market.session_features import SessionFeatures
from intelligence import decision_replay as audit


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.capsules=[]
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'replays.db'

    def engine(self,kind='READY',rows=60):
        e=object.__new__(DecisionEngine); e._breakout_latches={}
        frame=pd.DataFrame(dict(open=[10.]*rows,high=[10.3]*rows,low=[9.9]*rows,
            close=[10.2]*rows,volume=[1000]*rows),index=pd.date_range(self.now-timedelta(minutes=5*rows),periods=rows,freq='5min'))
        frame.attrs['data_feed']='iex'
        features=FeatureSnapshot('TEST',timestamp=self.now-timedelta(minutes=5),price=10.2,
            data_quality_ok=True,data_is_stale=False,rvol=2.,volume=1000,dollar_volume=10000.,liquidity_score=50.)
        if kind=='STALE': features.timestamp=self.now-timedelta(hours=2)
        e.market_data=NS(get_bars=Mock(return_value=frame),get_last_price=Mock(return_value=10.2),
            get_latest_quote=Mock(return_value=dict(bid=10.19,ask=10.2,timestamp=self.now,feed='iex')))
        e.feature_engine=NS(diagnose_input=Mock(return_value=dict(valid_rows=rows,required_rows=51,missing_columns=[])),
            build=Mock(return_value=features))
        def news(f): f.news_score=41.
        def options(f): f.options_flow_score=55.
        e.news_engine=NS(apply_to_features=Mock(side_effect=news))
        e.options_engine=NS(apply_to_features=Mock(side_effect=options))
        e.external_research_bridge=NS(get_latest_research=Mock(return_value=None))
        e.ai_engine=NS(evaluate=Mock(return_value=AIAnalysis('TEST',SignalAction.BUY,95.,.8,.8,95.,5.)))
        opportunity=OpportunityAnalysis('TEST',kind!='OPP_REJECT',StrategyName.MOMENTUM_BREAKOUT,
            95.,OpportunityGrade.A,1.,10.1,9.1)
        e.opportunity_engine=NS(minimum_score=70.,evaluate=Mock(return_value=opportunity))
        market=MarketRegimeResult(MarketRegime.BULL_TREND,.9)
        e.market_context_engine=NS(get_regime_result=Mock(return_value=market))
        routed=RoutedOpportunity('TEST',True,opportunity.strategy,95.,95.,OpportunityGrade.A,1.,1.,
            market.regime,.9,10.1,9.1,minimum_score=72.)
        e.strategy_router=NS(route=Mock(return_value=routed))
        values={f.name:None for f in fields(SessionFeatures) if f.default is MISSING and f.default_factory is MISSING}
        values.update(symbol='TEST',session_date=self.now.date().isoformat(),current_price=10.2,
            premarket_volume=100,regular_volume=1000,opening_range_ready=True)
        e.session_feature_engine=NS(build=Mock(return_value=SessionFeatures(**values)))
        strategy=SessionStrategyAnalysis('TEST',True,SessionStrategyName.OPENING_RANGE_BREAKOUT,95.,10.1,9.1,
            candidates=[SessionStrategyCandidate(SessionStrategyName.OPENING_RANGE_BREAKOUT,95.,True,10.1,9.1)])
        e.session_strategy_engine=NS(minimum_score=75.,evaluate=Mock(return_value=strategy))
        trigger=TriggerDecision('TEST',TriggerState.WAITING_FOR_TRIGGER if kind=='WATCHING' else TriggerState.ENTRY_CONFIRMED,
            kind!='WATCHING',10.2,10.1,9.1,reason='recorded trigger')
        e.trigger_engine=NS(evaluate=Mock(return_value=trigger))
        e.breakout_engine=NS(evaluate=Mock(return_value=BreakoutConfirmation('TEST',
            BreakoutConfirmationState.WAITING if kind=='LATCH' else BreakoutConfirmationState.CONFIRMED,
            kind!='LATCH',10.1,9.1,close_price=10.2,score=95.,reason='recorded breakout')))
        e.risk_engine=NS(evaluate=Mock(return_value=RiskDecision(kind!='RISK_REJECT','recorded risk',
            quantity=2,risk_pct=1.,stop_price=9.1,target_1=12.4,target_2=13.5,target_3=14.6)))
        return e

    def capture(self,e):
        # Correctness cases must not depend on CI host scheduling. The dedicated
        # budget-exhaustion test supplies its own advancing mocked clock.
        clock=(nullcontext() if isinstance(audit.time.perf_counter,Mock)
               else patch.object(audit.time,'perf_counter',return_value=0.))
        with clock, patch.dict(os.environ,{'JALWE_DECISION_REPLAY_ENABLED':'true'}), \
             patch('intelligence.decision_replay.enqueue',side_effect=self.capsules.append), \
             patch('market_move_research.read_packet',return_value=None), \
             patch('intelligence.decision_engine.database.save_feature_snapshot',return_value=None), \
             patch('strategy_experiments.get_strategy_experiments',return_value=NS(choose=lambda value,**kwargs:(value,{'mode':'BASELINE'}))):
            return e.analyze('TEST')

    def test_ready_watching_stale_opportunity_and_risk_rejection_replay_same_native_flow(self):
        for kind,state in (('READY','READY_FOR_PAPER_EXECUTION'),('WATCHING','WATCHING'),
                           ('STALE','REJECTED'),('OPP_REJECT','REJECTED'),('RISK_REJECT','REJECTED')):
            with self.subTest(kind=kind):
                self.capsules=[]; engine=self.engine(kind); decision=self.capture(engine)
                self.assertEqual(decision.state.value,state)
                self.assertEqual(len(self.capsules),1)
                before=copy.deepcopy(engine._breakout_latches)
                with patch('market_move_research.read_packet',side_effect=AssertionError('no storage read')), \
                     patch('intelligence.decision_engine.database.save_feature_snapshot',side_effect=AssertionError('no write')), \
                     patch('strategy_experiments.get_strategy_experiments',side_effect=AssertionError('no policy read')), \
                     patch('market.market_data.get_market_data',side_effect=AssertionError('no SDK')), \
                     patch('socket.create_connection',side_effect=AssertionError('no network')):
                    result=audit.replay(self.capsules[0])
                self.assertEqual(result['state'],'MATCH',result)
                self.assertEqual(result['native_state'],state)
                self.assertEqual(result['calls'],result['recorded_calls'])
                self.assertEqual(engine._breakout_latches,before)
                self.assertFalse(result['execution_authority'])
                self.assertEqual(decision.replay_id,self.capsules[0]['id'])

    def test_news_and_options_mutations_survive_replay_without_requesting_them_again(self):
        engine=self.engine(); self.capture(engine); capsule=self.capsules[0]
        changed=[e for e in capsule['events'] if e['mutations']]
        self.assertTrue(any(e['label']=='news_engine.apply_to_features' for e in changed))
        self.assertTrue(any(e['label']=='options_engine.apply_to_features' for e in changed))
        for component in audit.COMPONENTS:
            for method in vars(getattr(engine,component)).values():
                if isinstance(method,Mock): method.side_effect=AssertionError('must not re-enter native dependency')
        self.assertEqual(audit.replay(capsule)['state'],'MATCH')

    def test_waiting_latch_creation_and_recheck_are_replayed_with_recorded_clock_and_state(self):
        engine=self.engine('LATCH'); self.capture(engine)
        self.assertIn('TEST',engine._breakout_latches)
        self.assertEqual(audit.replay(self.capsules[-1])['state'],'MATCH')
        self.capture(engine)
        self.assertEqual(audit.replay(self.capsules[-1])['state'],'MATCH')

    def test_version_mismatch_corruption_and_missing_calls_cannot_claim_a_match(self):
        self.capture(self.engine()); original=self.capsules[0]
        corrupt=copy.deepcopy(original); corrupt['events'][0]['label']='invented'
        self.assertEqual(audit.replay(corrupt)['state'],'INVALID_CAPSULE')
        version=copy.deepcopy(original); version['code_key']='old'; version['id']=audit.digest({k:v for k,v in version.items() if k!='id'})
        self.assertEqual(audit.replay(version)['state'],'VERSION_UNAVAILABLE')
        missing=copy.deepcopy(original); missing['events'].pop(); missing['id']=audit.digest({k:v for k,v in missing.items() if k!='id'})
        self.assertEqual(audit.replay(missing)['state'],'UNREPLAYABLE')

    def test_live_disabled_capture_failure_and_unknown_types_never_change_native_decision(self):
        for env in ({'JALWE_DECISION_REPLAY_ENABLED':'false'},):
            with patch.dict(os.environ,env),patch('intelligence.decision_replay.Transcript') as recorder:
                callback=Mock(return_value='native result')
                self.assertEqual(audit.capture(NS(),callback,'TEST'),'native result')
                recorder.assert_not_called()
        engine=self.engine()
        with patch('intelligence.decision_replay.Codec.encode',side_effect=ValueError('unsupported')):
            decision=self.capture(engine)
        self.assertEqual(decision.state,DecisionState.READY_FOR_PAPER_EXECUTION)
        self.assertEqual(self.capsules,[])
        with self.assertRaises(audit.CaptureUnavailable): audit.Codec().encode({'api_key':'do-not-save'})
        with self.assertRaises(audit.CaptureUnavailable): audit.Codec().encode('Authorization: Bearer private')

    def test_store_retains_source_identity_read_only_report_and_duplicate_is_idempotent(self):
        decision=self.capture(self.engine()); capsule=self.capsules[0]
        store=audit.ReplayStore(self.path)
        self.assertEqual(store.save(capsule)['state'],'MATCH')
        self.assertEqual(store.save(capsule)['state'],'MATCH')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM decision_replays').fetchone()[0],1)
            source=conn.execute('SELECT coordinator_source FROM replay_sources').fetchone()[0]
            self.assertIn('def _analyze_native',source)
        text=audit.status_text('TEST',self.path)
        self.assertIn('MATCH',text); self.assertIn('استجابات المراحل محفوظة',text)
        self.assertEqual(decision.state,DecisionState.READY_FOR_PAPER_EXECUTION)

    def test_capture_budget_exhaustion_skips_recording_without_changing_decision(self):
        stamps=iter(i*.1 for i in range(1000))
        with patch.object(audit.time,'perf_counter',side_effect=lambda:next(stamps)):
            result=self.capture(self.engine())
        self.assertEqual(result.state,DecisionState.READY_FOR_PAPER_EXECUTION)
        self.assertEqual(self.capsules,[])

    def test_malformed_input_reason_is_saved_without_fabricated_stop_context(self):
        engine=self.engine()
        engine.feature_engine.build.return_value.data_quality_ok=False
        engine.feature_engine.diagnose_input.return_value['input_issues']=['DUPLICATE_TIMESTAMP']
        decision=self.capture(engine)
        self.assertIn('DUPLICATE_TIMESTAMP',decision.reason)
        self.assertIsNone(decision.metadata['research_stop_context']['model_low'])
        engine.ai_engine.evaluate.assert_not_called()

    def test_normal_500_bar_capture_is_bounded_and_replayable(self):
        # This tests transcript fidelity and size, not machine-dependent speed.
        with patch.object(audit.time,'perf_counter',return_value=0.):
            self.capture(self.engine(rows=500))
        self.assertEqual(len(self.capsules),1)
        capsule=self.capsules[0]
        self.assertLess(len(audit.canonical(capsule).encode()),audit.MAX_BYTES)
        self.assertLess(len(capsule['blobs']),5)
        self.assertEqual(audit.replay(capsule)['state'],'MATCH')


if __name__=='__main__': unittest.main()
