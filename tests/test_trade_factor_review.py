from copy import deepcopy
from pathlib import Path
import json,sqlite3,unittest
from unittest.mock import patch
import trade_factor_review as review
import trade_chain_audit as audit
import test_trade_chain_and_feature_integrity as fixtures


class TradeFactorReviewTests(unittest.TestCase):
    setUp=fixtures.TradeChainTests.setUp
    insert=fixtures.TradeChainTests.insert
    seed=fixtures.TradeChainTests.seed

    def facts(self):
        d=deepcopy(self.decision)
        d['jalwe'].update(strategy='GAP_AND_GO',setup_grade='A+',ai_score=80.)
        d['jalwe']['metadata'].update(decision_data_feed='iex',decision_features=dict(
            timestamp='2026-10-16T13:55:00Z',rvol=2.,atr=2.,above_vwap=True,liquidity_score=70.))
        return review.extract(self.trade,self.entry,d)

    def row(self,id='one',pnl=-2.):
        return dict(trade_id=id,symbol='TEST',stage='CLOSED',ledger_complete=True,findings=[],
                    unknown=['EXIT_EXECUTION_SNAPSHOT_NOT_SAVED'],realized_pnl_before_broker_fees=pnl,entry_factors=self.facts())

    def test_exact_entry_facts_ignore_a_newer_same_symbol_rejection(self):
        d=deepcopy(self.decision);d['jalwe']['metadata']['decision_features']={'rvol':2.}
        with self.db.connection() as c:
            c.execute("UPDATE system_events SET metadata_json=? WHERE id=1",(json.dumps(d),))
            self.insert(c,'system_events',dict(event_type='APEX_RESEARCH_DECISION',severity='INFO',message='later',
                metadata_json=json.dumps(dict(symbol='TEST',jalwe=dict(metadata=dict(decision_features=dict(rvol=999.))),execution={}))))
        before=self.path.read_bytes();r=audit.audit(self.path)
        self.assertEqual(r['trades'][0]['entry_factors']['features']['rvol'],2.)
        self.assertEqual(before,self.path.read_bytes())

    def test_winner_loser_denominators_exclude_unknown_facts_without_relabeling_them(self):
        loss=self.row();win=self.row('win',3.)
        win['entry_factors']['features']['rvol']=None
        r=review.summarize(dict(state='AUDITED',trades=[loss,win]),calendar={})
        self.assertEqual(r['groups']['LOSS']['flags']['rvol_ge2'],dict(present=1,known=1,unknown=0))
        self.assertEqual(r['groups']['WIN']['flags']['rvol_ge2'],dict(present=0,known=0,unknown=1))
        self.assertEqual(r['groups']['WIN']['pnl_before_fees'],3.)
        self.assertFalse(r['automatic_application']);self.assertIn('UNPROVEN',r['causal_loss_explanation'])

    def test_open_unverified_and_duplicate_ledgers_cannot_manufacture_results(self):
        opened=self.row('open',50.);opened['stage']='OPEN'
        invalid=self.row('invalid',100.);invalid['ledger_complete']=False
        duplicated=self.row('duplicate',-2.)
        flat=self.row('flat',0.)
        r=review.summarize(dict(trades=[opened,invalid,duplicated,deepcopy(duplicated),flat]),calendar={})
        self.assertEqual(r['groups']['WIN']['trades'],0);self.assertEqual(r['groups']['LOSS']['trades'],0)
        self.assertEqual(r['groups']['FLAT']['trades'],1)
        self.assertEqual(r['excluded']['DUPLICATE_TRADE_ID'],2)

    def test_actual_early_close_is_used_and_absent_calendar_does_not_mean_not_late(self):
        row=self.row();row['entry_factors']['entry_at']='2026-11-27T17:46:00Z'
        cal={'2026-11-27':dict(open='2026-11-27T14:30:00Z',close='2026-11-27T18:00:00Z')}
        r=review.summarize(dict(trades=[row]),cal)
        self.assertEqual(r['groups']['LOSS']['flags']['last30'],dict(present=1,known=1,unknown=0))
        self.assertEqual(r['records'][0]['minutes_before_close'],14.)
        r=review.summarize(dict(trades=[row]),{})
        self.assertEqual(r['groups']['LOSS']['flags']['last30']['unknown'],1)

    def test_overnight_means_new_york_day_not_utc_midnight(self):
        t=deepcopy(self.trade);t['created_at']='2026-10-16 23:30:00';t['closed_at']='2026-10-17 00:30:00'
        f=review.extract(t,None,None);self.assertFalse(f['overnight'])
        t['closed_at']='2026-10-17 13:30:00';self.assertTrue(review.extract(t,None,None)['overnight'])

    def test_missing_bool_text_nonfinite_and_overflow_are_unknown_not_real_features(self):
        d=deepcopy(self.decision);d['jalwe']['metadata']['decision_features']=dict(rvol=True,atr=float('nan'),liquidity_score='70',momentum_score=10**1000)
        f=review.extract(self.trade,self.entry,d)
        self.assertTrue(all(f['features'][k] is None for k in ('rvol','atr','liquidity_score','momentum_score')))
        self.assertIsNone(f['stop_distance_atr'])
        json.dumps(f,allow_nan=False)
        f=review.extract(self.trade,None,None);self.assertFalse(f['exact_entry_decision_available'])

    def test_initial_stop_is_used_and_threshold_roundoff_does_not_fake_tight_stop(self):
        t=deepcopy(self.trade);t.update(initial_stop=9.9,current_stop=12.)
        d=deepcopy(self.decision);d['jalwe']['metadata']['decision_features']={'atr':.1}
        f=review.extract(t,self.entry,d);self.assertAlmostEqual(f['stop_distance_pct'],1.)
        row=self.row();row['entry_factors']=f
        r=review.summarize(dict(trades=[row]),{})['groups']['LOSS']['flags']
        self.assertEqual(r['stop_lt_1pct']['present'],0);self.assertEqual(r['stop_lt_atr']['present'],0)

    def test_unrelated_metadata_or_private_text_is_not_exported(self):
        d=deepcopy(self.decision);d['jalwe']['metadata']['secret']='NEVER_EXPORT'
        d['jalwe']['metadata']['decision_features']={'rvol':2.,'secret':'NEVER_EXPORT'}
        self.assertNotIn('NEVER_EXPORT',json.dumps(review.extract(self.trade,self.entry,d)))

    def test_readonly_report_handles_no_evidence_without_claiming_zero_profit(self):
        missing=Path(str(self.path)+'.absent')
        text=review.status_text(missing,missing)
        self.assertIn('غير معلومة',text);self.assertNotIn('$0.00',text);self.assertFalse(missing.exists())
        before=self.path.read_bytes();text=review.status_text(self.path,missing)
        self.assertIn('الخاسرة',text);self.assertIn('غير متاح',text)
        self.assertEqual(before,self.path.read_bytes())
        import railway_controller
        with patch.object(review,'status_text',return_value='saved factors') as read:
            self.assertEqual(railway_controller.handle('/trade_factors'),'saved factors');read.assert_called_once()

    def test_saved_calendar_is_readonly_and_conflicting_dates_are_unknown(self):
        path=Path(str(self.path)+'.calendar')
        self.assertEqual(review.saved_calendar(path),{});self.assertFalse(path.exists())
        s=dict(date='2026-10-16',open='2026-10-16T13:30:00Z',close='2026-10-16T20:00:00Z')
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE move_state(key TEXT,value TEXT)')
            c.execute('INSERT INTO move_state VALUES (?,?)',('jump_session_calendar',json.dumps({'sessions':[s]})))
        before=path.read_bytes();self.assertEqual(review.saved_calendar(path),{'2026-10-16':s});self.assertEqual(before,path.read_bytes())
        with sqlite3.connect(path) as c:c.execute('UPDATE move_state SET value=?',(json.dumps({'sessions':[s,dict(s,close='2026-10-16T17:00:00Z')]}),))
        self.assertEqual(review.saved_calendar(path),{})


if __name__=='__main__':unittest.main()
