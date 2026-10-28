import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from market_research_findings import findings, findings_text, stock_research_text
from market_move_research import MoveStore, utc
from prior_day_entry_study import stock_trace_text
from prior_day_priority_bus import signal_id
import test_market_move_research as fixtures


def packet():
    factors=[dict(codes=[code],known=145,present=count,unknown=1) for code,count in
             [('PRIOR_DAY_RANGE',118),('PRIOR_DAY_VOLUME',66),('PRIOR_DAY_MOMENTUM',49)]]
    return dict(published_at='2026-10-23T03:00:00Z',historical_jump_profiles=dict(common_factors=dict(
        all_jumps=dict(cases=829,min_rise_pct=8),large_jump_groups=[dict(
            min_rise_pct=25,cases=146,unique_symbols=101,factors=factors,prominent_cooccurrences=[])])),
        comparison=dict(pair_episodes=72,complete_pairs=2,unique_days=2),
        patterns=dict(state='COLLECTING_TRAINING',candidates=dict(TEST=dict(
            matched_pairs=0,matched_days=0,complete_pair_coverage=.03))),
        priority_native_outcomes=dict(state='WAITING_NATIVE_EXIT_EVIDENCE',
            realized_pnl_before_broker_fees=0),
        factor_benchmark=dict(retrospective=dict(all_days=dict(rules=dict(ALL_THREE=dict(
            pairs=dict(complete=100,matching_reached=30,control_reached=8)))))))


class FindingsTests(unittest.TestCase):
    def test_known_denominator_not_all_cases_and_no_claim_of_predictive_success(self):
        data=packet(); before=copy.deepcopy(data)
        result=findings(data); text=findings_text(data)
        self.assertIn('118/145 (81.4%)',text)
        self.assertIn('مجهول 1',text)
        self.assertIn('ليست احتمال قفزة',text)
        self.assertIn('2/72',text)
        self.assertIn('40 زوجًا، 10 أيام',text)
        self.assertIn('اختبار على 20 جلسة جديدة',text)
        self.assertIn('للمطابقة 30/100، وللضابطة 8/100',text)
        self.assertFalse(result['pipeline']['native_pnl_available'])
        self.assertIn('الربح غير معلوم وليس صفرًا',text)
        self.assertEqual(data,before)
        self.assertFalse(result['execution_authority'])

    def test_missing_or_unknown_evidence_does_not_become_zero_success_rate(self):
        data=packet(); data['historical_jump_profiles']={}
        text=findings_text(data)
        self.assertIn('لا توجد عينة قفزات مكتملة',text)
        self.assertNotIn('None%',text)
        self.assertIn('لا توجد حزمة',findings_text(None))

    def test_prospective_cost_results_show_missing_paths_and_never_become_paper_profit(self):
        data=packet(); data['entry_study']=dict(total=20,
            states=dict(SIMULATED_EXIT=2,UNKNOWN_DATA=14,NO_ENTRY=3,PENDING=1),
            costs={'10':dict(simulated_exits=2,mean_net_r=-.2),
                   '25':dict(simulated_exits=2,mean_net_r=-.3)})
        text=findings_text(data)
        self.assertIn('بلا دخول 3 | ناقصة/ملتبسة 14',text)
        self.assertIn('متوسط -0.20R من 2',text)
        self.assertIn('متوسط -0.30R من 2',text)
        self.assertIn('الربح غير معلوم وليس صفرًا',text)

    def test_saved_stock_report_reads_all_studies_not_only_packet_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'moves.db'; store=MoveStore(path)
            session=fixtures.session(); now=utc('2026-10-12T21:00Z')
            store.add(fixtures.feature('TEST'),session,'JUMP_CASE','test',now,prospective=False)
            row=store.episodes()[0]
            result=dict(status='COMPLETE',observed_minutes=390,expected_minutes=390,
                jump_path=dict(observed_rise_pct=30.,start_price=10.,peak_price=13.,
                    start_at=session['open'],peak_at=session['close'],profile=dict(
                        prior_day_context=dict(status='AVAILABLE',range_pct=12.,
                            volume_ratio_previous_day=2.5,daily_return_pct=4.),
                        before_context=dict(status='UNAVAILABLE'))))
            store.settle(row['episode_key'],result,now)
            before=store.episodes()[0]['result_json']
            text=stock_research_text('TEST','2026-10-12',path=path)
            self.assertIn('12.00%',text); self.assertIn('2.50×',text)
            self.assertIn('ليست أسبابًا مثبتة',text)
            self.assertIn('السعودية',text)
            self.assertIn('UNAVAILABLE',text)
            self.assertEqual(store.episodes()[0]['result_json'],before)
            self.assertIn('التاريخ بصيغة',stock_research_text('TEST','2026-1-2',path=path))
            missing=Path(tmp)/'absent.db'
            self.assertIn('لا توجد دراسة',stock_research_text('TEST',path=missing))
            self.assertFalse(missing.exists())

    def test_controller_routes_commands_without_sending_or_trading(self):
        import railway_controller as controller
        with (patch('market_move_research.read_packet',return_value=packet()),
              patch('market_research_findings.stock_research_text',return_value='saved study') as read,
              patch('prior_day_entry_study.stock_trace_text',return_value='saved chain') as chain,
              patch('stock_decision_evidence.stock_decision_text',return_value='saved decisions') as native):
            self.assertIn('ماذا عرف',controller.handle('/research_findings'))
            self.assertEqual(controller.handle('/research_stock TEST 2026-10-12'),'saved study\n\nsaved decisions\n\nsaved chain')
            native.assert_called_once_with('TEST','2026-10-12')
            read.assert_called_once_with('TEST','2026-10-12')
            chain.assert_called_once_with('TEST','2026-10-12')


class StockTraceTests(unittest.TestCase):
    def query(self,events,exits):
        connection=MagicMock(); connection.__enter__.return_value=connection
        cursor=MagicMock(); connection.cursor.return_value.__enter__.return_value=cursor
        cursor.fetchone.return_value=None
        latest={r['stage']:r for r in events}
        cursor.fetchall.side_effect=[
            [(stage,1,'2026-10-12T14:00Z','2026-10-12T14:00Z') for stage in latest],
            [(json.dumps(r),) for r in latest.values()],
            [(json.dumps(r),) for r in events if r['stage']=='JALWE_EXECUTION']]
        with (patch('prior_day_priority_bus.connect',return_value=connection),
              patch('prior_day_entry_study.native_ledger_rows',return_value=exits) as ledger,
              patch('prior_day_entry_study.events',side_effect=AssertionError('audit write'))):
            text=stock_trace_text('TEST','2026-10-12',dsn='test-only')
        connection.set_session.assert_called_once_with(readonly=True)
        args=cursor.execute.call_args.args
        self.assertTrue(args[0].lstrip().startswith('SELECT'))
        self.assertEqual(args[1],(signal_id('TEST','2026-10-12'),))
        return text,ledger

    def test_submitted_order_is_not_fill_or_zero_profit(self):
        identity=signal_id('TEST','2026-10-12')
        events=[dict(signal_id=identity,symbol='TEST',stage='JALWE_EXECUTION',
                     managed_trade_id='t',broker_order_id='buy')]
        text,_=self.query(events,[])
        self.assertIn('وحده ليس تنفيذًا',text)
        self.assertIn('الربح غير معلوم',text)
        self.assertNotIn('$+0.00',text)

    def test_native_partial_exits_are_separate_from_simulation_and_other_symbols(self):
        identity=signal_id('TEST','2026-10-12')
        events=[dict(signal_id=identity,symbol='TEST',stage='JALWE_EXECUTION',managed_trade_id='t')]
        exits=[dict(signal_id=identity,symbol='TEST',quantity=2,realized_pnl_before_broker_fees=1.25),
               dict(signal_id=identity,symbol='OTHER',quantity=5,realized_pnl_before_broker_fees=100.)]
        text,_=self.query(events,exits)
        self.assertIn('$+1.25',text)
        self.assertIn('قد تكون مخارج جزئية',text)
        self.assertNotIn('101.25',text)
