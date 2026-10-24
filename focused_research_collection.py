"""Pre-open frozen, small SIP collection panel. No decisions, prices or orders inferred."""
from collections import Counter
from datetime import timedelta
import json
import math

from market_move_research import utc, observe_session
from market.retained_research_bars import merge_session_evidence, COLUMNS
from market.research_diagnostics import error_diagnostics

PROTOCOL = 'focused-preopen-sip-collection-v1'
PAIR_LIMIT, NOMINEE_LIMIT, SYMBOL_LIMIT = 4, 4, 12
INTERVAL = timedelta(minutes=15)
DELAY = timedelta(minutes=16)
POST_CLOSE_POLLS, MAX_POLLS = 3, 32
RETENTION = timedelta(days=90)


def selection(rows, session, now):
    """Rank immutable prior-day liquidity only, never results or today's prices."""
    opening = utc(session['open'])
    if now >= opening:
        return []
    eligible=[]
    for row in rows:
        try:
            f=row['features']; value=f['dollar_volume']
            if (f.get('source_feed')!='sip' or f.get('evidence_day','9999')>=session['date']
                    or row['session_day']!=session['date'] or row['session']!=session
                    or not utc(f['available_after'])<=utc(row['frozen_at'])<=now<opening
                    or type(value) not in (int,float) or not math.isfinite(value) or value<=0):
                continue
            if row['role']=='ENTRY_STUDY':
                if not (f.get('entry_protocol')=='prior-day-orb-execution-study-v1'
                        and utc(f['available_after'])<=utc(f['snapshot_prepared_at'])<=utc(row['frozen_at'])):
                    continue
            elif not (row['prospective'] and row['role'] in {'ACTIVITY','CONTROL'}):
                continue
            eligible.append(row)
        except (KeyError,ValueError,TypeError):
            continue
    pairs={}
    for row in eligible:
        if row['prospective']:
            pairs.setdefault(row['pair_key'],[]).append(row)
    groups=[values for values in pairs.values() if len(values)==2
            and {r['role'] for r in values}=={'ACTIVITY','CONTROL'}
            and len({r['symbol'] for r in values})==2]
    groups.sort(key=lambda values:(-min(r['features']['dollar_volume'] for r in values),values[0]['pair_key']))
    chosen=[r for group in groups[:PAIR_LIMIT] for r in sorted(group,key=lambda r:r['role'])]
    nominees=sorted((r for r in eligible if r['role']=='ENTRY_STUDY'),
                    key=lambda r:(-r['features']['dollar_volume'],r['symbol'],r['episode_key']))
    chosen+=nominees[:NOMINEE_LIMIT]
    # Multiple research roles share one provider read; retain every episode link.
    return [dict(episode_key=r['episode_key'],symbol=r['symbol'],role=r['role'],
                 prospective=bool(r['prospective']),pair_key=r['pair_key'],
                 frozen_at=r['frozen_at'],available_after=r['features']['available_after'],
                 prior_dollar_volume=r['features']['dollar_volume']) for r in chosen]


class FocusedCollector:
    def __init__(self, store, provider):
        self.store,self.provider=store,provider
        with store.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS focused_collection_sessions (
                session_day TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,
                last_attempt TEXT, polls INTEGER NOT NULL DEFAULT 0,
                closed_polls INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS focused_collection_bars (
                session_day TEXT NOT NULL,symbol TEXT NOT NULL,evidence_json TEXT NOT NULL,
                coverage_json TEXT NOT NULL,expires_at TEXT NOT NULL,
                PRIMARY KEY(session_day,symbol));''')

    def freeze(self, session, now, *, clock=None):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM focused_collection_sessions WHERE session_day=?',
                            (session['date'],)).fetchone(): return False
            rows=[]
            for value in conn.execute('''SELECT * FROM move_episodes WHERE session_day=?
                    AND (prospective=1 OR role='ENTRY_STUDY') ORDER BY episode_key''',(session['date'],)):
                row=dict(value);row['features']=json.loads(row['features_json']);row['session']=json.loads(row['session_json'])
                rows.append(row)
            stamp=clock() if clock else now
            members=selection(rows,session,stamp)
            if not members: return False
            manifest=dict(protocol=PROTOCOL,session=session,frozen_at=stamp.isoformat(),members=members,
                selection='PRIOR_DAY_LIQUIDITY_WITHIN_ORIGINAL_FROZEN_PAIRS_AND_NOMINEES',
                execution_authority=False,training_authority=False)
            conn.execute('INSERT INTO focused_collection_sessions(session_day,manifest_json) VALUES (?,?)',
                         (session['date'],json.dumps(manifest,sort_keys=True,allow_nan=False)))
            if clock and clock()>=utc(session['open']):
                conn.rollback();return False
        return True

    def manifest(self, now):
        with self.store.connect() as conn:
            rows=[json.loads(r[0]) for r in conn.execute(
                'SELECT manifest_json FROM focused_collection_sessions ORDER BY session_day')]
        current=[r for r in rows if utc(r['session']['open'])<=now<=utc(r['session']['close'])+timedelta(hours=48)]
        return current[-1] if current else next((r for r in rows if utc(r['session']['open'])>now),None)

    def evidence(self, row, now):
        with self.store.connect() as conn:
            saved=conn.execute('''SELECT evidence_json FROM focused_collection_bars
                WHERE session_day=? AND symbol=? AND expires_at>=?''',
                (row['session_day'],row['symbol'],now.isoformat())).fetchone()
        value=json.loads(saved[0]) if saved else None
        if value and (utc(value.get('window_start'))!=utc(row['session']['open'])
                      or utc(value.get('window_end'))!=utc(row['session']['close'])):
            return None
        return value

    def capture(self, now):
        """Reserve one paced batch before IO; failure never fabricates a capture."""
        with self.store.connect() as conn:
            conn.execute('DELETE FROM focused_collection_bars WHERE expires_at<?',(now.isoformat(),))
            conn.execute('DELETE FROM focused_collection_sessions WHERE session_day<?',
                         ((now-RETENTION).date().isoformat(),))
        manifest=self.manifest(now)
        if manifest is None: return dict(state='WAITING_PREOPEN_PANEL',captured_symbols=0),{}
        session=manifest['session'];opening,closing=utc(session['open']),utc(session['close'])
        end=min(closing,(now-DELAY).replace(second=0,microsecond=0))
        if end<=opening: return dict(state='WAITING_DELAYED_MINUTES',captured_symbols=0),{}
        symbols=sorted({m['symbol'] for m in manifest['members']})
        if not 1<=len(symbols)<=SYMBOL_LIMIT: raise ValueError('INVALID_FOCUSED_PANEL_SIZE')
        full=end==closing
        # Durable cadence and post-close ceilings survive process restarts.
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM focused_collection_sessions WHERE session_day=?',(session['date'],)).fetchone()
            if (row['polls']>=MAX_POLLS or full and row['closed_polls']>=POST_CLOSE_POLLS
                    or row['last_attempt'] and now<utc(row['last_attempt'])+INTERVAL):
                return dict(state='PACED_OR_BUDGETED',captured_symbols=0),{}
            conn.execute('UPDATE focused_collection_sessions SET last_attempt=?,polls=polls+1,closed_polls=closed_polls+? WHERE session_day=?',
                         (now.isoformat(),int(full),session['date']))
        try:
            frames=self.provider.intraday_prefix_many(symbols,session,now)
            if set(frames)!=set(symbols): raise ValueError('INVALID_FOCUSED_BATCH')
        except Exception as exc:
            d=error_diagnostics(exc)
            return dict(state='DATA_UNAVAILABLE',captured_symbols=len(symbols),**d,
                        halt_reads=d['reason'] in {'AUTHENTICATION_FAILED','FORBIDDEN','RATE_LIMITED',
                            'PROVIDER_SERVER_ERROR','PROVIDER_TIMEOUT','SUBSCRIPTION_REQUIRED'}),{}
        completed_frames={};probes=2
        for symbol in symbols:
            raw=frames[symbol]
            if raw.attrs.get('data_feed')!='sip' or raw.attrs.get('adjustment')!='split':
                raise ValueError('FOCUSED_FEED_MISMATCH')
            raw=raw.loc[(raw.index>=opening)&(raw.index<end)].copy()
            # bool is not OHLCV, even though pandas can coerce it to 1/0.
            for column in COLUMNS:
                raw[column]=raw[column].map(lambda v:None if type(v).__name__ in {'bool','bool_'} else v)
            with self.store.connect() as conn:
                saved=conn.execute('SELECT evidence_json,coverage_json FROM focused_collection_bars WHERE session_day=? AND symbol=?',
                                   (session['date'],symbol)).fetchone()
            previous=json.loads(saved[0]) if saved else None
            merged,evidence=merge_session_evidence(raw,previous,symbol=symbol,start=opening,end=closing,checked_at=now)
            prefix=dict(date=session['date'],open=opening.isoformat(),close=end.isoformat())
            quality=observe_session(merged,prefix)
            diagnostics=quality.get('data_diagnostics',{})
            samples=(json.loads(saved[1]).get('gap_samples',[]) if saved else [])
            by_minute={s['minute']:s for s in samples if s.get('complete')}
            absent=diagnostics.get('first_absent_minutes',[])
            for at in absent:
                if probes<=0: break
                if at in by_minute: continue
                probes-=1
                try: sample=self.provider.gap_trades(symbol,utc(at),now)
                except Exception as exc: sample=dict(minute=at,complete=False,classification='TRADE_READ_UNAVAILABLE',**error_diagnostics(exc))
                by_minute[at]=sample
            # Keep bounded diagnostic samples; samples do NOT repair missing bars.
            coverage=dict(as_of=now.isoformat(),mature_end=end.isoformat(),
                mature_expected_minutes=quality.get('expected_minutes',0),
                observed_minutes=quality.get('observed_minutes',0),
                full_session_complete=full and quality['status']=='COMPLETE',
                data_diagnostics=diagnostics,gap_samples=list(by_minute.values())[-8:],
                scope='RETRIEVAL_COVERAGE_NOT_TRAINING_SUCCESS_OR_PNL')
            with self.store.connect() as conn:
                conn.execute('INSERT OR REPLACE INTO focused_collection_bars VALUES (?,?,?,?,?)',
                    (session['date'],symbol,json.dumps(evidence,allow_nan=False),
                     json.dumps(coverage,allow_nan=False),(closing+RETENTION).isoformat()))
            if full: completed_frames[(symbol,session['open'],session['close'])]=raw
        return dict(state='CAPTURED',captured_symbols=len(symbols),window_end=end.isoformat(),
                    full_session_available=full),completed_frames

    def summary(self, now):
        manifest=self.manifest(now)
        if manifest is None: return dict(protocol=PROTOCOL,state='WAITING_PREOPEN_PANEL',symbols=0)
        session=manifest['session'];symbols={m['symbol'] for m in manifest['members']}
        with self.store.connect() as conn:
            rows={r['symbol']:json.loads(r['coverage_json']) for r in conn.execute(
                'SELECT * FROM focused_collection_bars WHERE session_day=?',(session['date'],)) if r['symbol'] in symbols}
            session_row=conn.execute('SELECT polls,closed_polls FROM focused_collection_sessions WHERE session_day=?',(session['date'],)).fetchone()
        end=min(utc(session['close']),(now-DELAY).replace(second=0,microsecond=0))
        minutes=max(0,int((end-utc(session['open'])).total_seconds()/60))
        expected=minutes*len(symbols)
        observed=sum(r['observed_minutes'] for r in rows.values())
        complete={symbol for symbol,r in rows.items() if r['full_session_complete']}
        pairs={}
        for m in manifest['members']:
            if m['prospective']: pairs.setdefault(m['pair_key'],set()).add(m['symbol'])
        reasons=Counter();samples=Counter()
        for r in rows.values():
            reason=r['data_diagnostics'].get('reason','UNKNOWN');reasons[reason]+=1
            for sample in r['gap_samples']: samples[sample.get('classification','UNKNOWN')]+=1
        return dict(protocol=PROTOCOL,state='WAITING_SESSION' if not minutes else 'COLLECTING_OR_RECHECKING',
            session_day=session['date'],frozen_at=manifest['frozen_at'],symbols=len(symbols),pairs=len(pairs),
            nominees=sum(m['role']=='ENTRY_STUDY' for m in manifest['members']),
            mature_expected_minutes=expected,observed_minutes=observed,
            coverage_pct=round(100*observed/expected,2) if expected else None,
            complete_paths=len(complete),complete_pairs=sum(len(v)==2 and v<=complete for v in pairs.values()),
            reasons=dict(reasons),gap_sample_classes=dict(samples),polls=session_row['polls'],
            closed_polls=session_row['closed_polls'],max_closed_polls=POST_CLOSE_POLLS,
            oldest_saved_read_at=min((r['as_of'] for r in rows.values()),default=None),
            latest_capture_at=max((r['as_of'] for r in rows.values()),default=None),
            no_price_inference=True,execution_authority=False,training_authority=False)
