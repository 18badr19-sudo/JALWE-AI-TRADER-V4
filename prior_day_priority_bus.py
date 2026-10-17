"""Shared advisory protocol for pre-open prior-day PAPER research priority.

This module is kept byte-identical in APEX/JALWE. No broker API exists here.
"""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROTOCOL='prior-day-apex-priority-v1'
START='2026-10-17T02:53:52+00:00'
END='2026-10-31T02:53:52+00:00'
NY=ZoneInfo('America/New_York')
RULE={'range_pct':8.,'volume_ratio_previous_day':2.,'daily_return_pct':3.}


def utc(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('UNZONED_TIME')
    return parsed.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def signal_id(symbol,day):
    return digest([PROTOCOL,day,symbol])


def enabled(paper,allow_live=False):
    return paper is True and not allow_live and os.getenv('JALWE_PRIOR_DAY_TRIAL_ENABLED','true').strip().lower() in {'true','1','on','yes'}


def valid_signal(signal,session):
    try:
        f=signal['features']; symbol=signal['symbol']
        return (isinstance(symbol,str) and 0<len(symbol)<=20 and symbol==symbol.upper()
            and signal['signal_id']==signal_id(symbol,session['date']) and f['source_feed']=='sip'
            and f['evidence_day']<session['date'] and utc(f['available_after'])<=utc(signal['prepared_at'])<utc(session['open'])
            and all(type(f.get(k)) in (int,float) and math.isfinite(f[k]) and f[k]>=n for k,n in RULE.items())
            and type(f.get('close')) in (int,float) and .5<=f['close']<=100
            and type(f.get('dollar_volume')) in (int,float) and math.isfinite(f['dollar_volume']) and f['dollar_volume']>=100000)
    except (KeyError,TypeError,ValueError,OverflowError):
        return False


def validate(packet):
    try:
        session=packet['session']; signals=packet['signals']
        core={k:v for k,v in packet.items() if k!='packet_id'}
        return (packet['protocol']==PROTOCOL and packet['rule']==RULE and packet['execution_authority'] is False
            and utc(START)<=utc(packet['frozen_at'])<utc(session['open'])<utc(session['close'])
            and utc(session['close'])<=utc(END) and len(signals)<=1000
            and len({s['symbol'] for s in signals})==len(signals)
            and all(valid_signal(s,session) and utc(s['prepared_at'])<=utc(packet['frozen_at']) for s in signals)
            and packet['packet_id']==digest(core))
    except (KeyError,TypeError,ValueError,OverflowError):
        return False


def build(records,session,now):
    if not utc(START)<=now<utc(session['open'])<utc(END): return None
    signals=[]
    for value in records:
        try:
            f=value['features']; signal=dict(signal_id=signal_id(f['symbol'],session['date']),symbol=f['symbol'],
                prepared_at=value['prepared_at'],features={k:f[k] for k in (*RULE,'close','dollar_volume','source_feed','evidence_day','available_after')})
            if value['session']==session and valid_signal(signal,session) and utc(signal['prepared_at'])<=now:
                signals.append(signal)
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    packet=dict(protocol=PROTOCOL,rule=RULE,session=session,frozen_at=now.isoformat(),
                signals=sorted(signals,key=lambda s:s['symbol']),execution_authority=False)
    packet['packet_id']=digest(packet)
    return packet if validate(packet) else None


def connect(dsn=None):
    import psycopg2
    return psycopg2.connect(dsn or os.getenv('DATABASE_URL',''),connect_timeout=3,
                           options='-c statement_timeout=2000')


def publish(packet,*,paper,allow_live=False,dsn=None,clock=None):
    if not enabled(paper,allow_live) or not packet or not validate(packet):
        return dict(state='DISABLED_OR_INVALID')
    if not (dsn or os.getenv('DATABASE_URL','')): return dict(state='WAITING_SHARED_DATABASE')
    try:
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS jalwe_prior_day_packets
                    (protocol TEXT NOT NULL,session_day TEXT NOT NULL,packet_id TEXT NOT NULL,
                     payload_json TEXT NOT NULL,PRIMARY KEY(protocol,session_day))''')
                cur.execute('''CREATE TABLE IF NOT EXISTS jalwe_prior_day_events
                    (event_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,stage TEXT NOT NULL,
                     created_at TIMESTAMPTZ NOT NULL,payload_json TEXT NOT NULL)''')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_prior_day_event_time ON jalwe_prior_day_events(stage,created_at)')
                cur.execute('''INSERT INTO jalwe_prior_day_packets VALUES (%s,%s,%s,%s)
                    ON CONFLICT(protocol,session_day) DO NOTHING''',
                    (PROTOCOL,packet['session']['date'],packet['packet_id'],json.dumps(packet,allow_nan=False)))
                cur.execute('SELECT payload_json FROM jalwe_prior_day_packets WHERE protocol=%s AND session_day=%s',
                            (PROTOCOL,packet['session']['date']))
                stored=json.loads(cur.fetchone()[0])
                if not validate(stored): raise ValueError('INVALID_EXISTING_PRIORITY_PACKET')
                if clock and clock()>=utc(packet['session']['open']):
                    conn.rollback(); return dict(state='CROSSED_SESSION_OPEN')
                cur.execute("DELETE FROM jalwe_prior_day_events WHERE created_at<CURRENT_TIMESTAMP-INTERVAL '30 days'")
        return dict(state='PUBLISHED',packet_id=stored['packet_id'],session_day=stored['session']['date'],
                    signals=len(stored['signals']),frozen_at=stored['frozen_at'])
    except Exception as exc:
        return dict(state='UNAVAILABLE',error_type=type(exc).__name__)


def load(now,*,paper,allow_live=False,dsn=None):
    if not enabled(paper,allow_live) or not utc(START)<=now<utc(END): return None,dict(state='DISABLED_OR_EXPIRED')
    if not (dsn or os.getenv('DATABASE_URL','')): return None,dict(state='WAITING_SHARED_DATABASE')
    try:
        with connect(dsn) as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute('SELECT payload_json FROM jalwe_prior_day_packets WHERE protocol=%s AND session_day=%s',
                            (PROTOCOL,now.astimezone(NY).date().isoformat()))
                row=cur.fetchone()
        if row is None: return None,dict(state='WAITING_FROZEN_PACKET')
        packet=json.loads(row[0])
        if not validate(packet): return None,dict(state='INVALID_PACKET')
        if not utc(packet['session']['open'])<=now<utc(packet['session']['close']):
            return None,dict(state='OUTSIDE_FROZEN_SESSION')
        return packet,dict(state='READY',packet_id=packet['packet_id'],signals=len(packet['signals']))
    except Exception as exc:
        return None,dict(state='UNAVAILABLE',error_type=type(exc).__name__)


def events(rows,*,dsn=None):
    """Bounded audit writes only. Caller must already have a validated signal."""
    if not rows or not (dsn or os.getenv('DATABASE_URL','')): return
    try:
        values=[]
        if len(rows)>1000: return
        for row in rows:
            key=digest([row['signal_id'],row['stage'],row['trace_key']])
            values.append((key,row['signal_id'],row['stage'],row['at'],json.dumps(row,allow_nan=False)))
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_values
                execute_values(cur,'INSERT INTO jalwe_prior_day_events VALUES %s ON CONFLICT(event_id) DO NOTHING',values,page_size=128)
    except Exception:
        # Audit availability can never authorize entry or stop the scanner.
        return


def trace_summary(*,dsn=None):
    if not (dsn or os.getenv('DATABASE_URL','')): return dict(state='WAITING_SHARED_DATABASE')
    try:
        with connect(dsn) as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute("SELECT stage,COUNT(*),COUNT(DISTINCT signal_id),MAX(created_at) FROM jalwe_prior_day_events WHERE created_at>CURRENT_TIMESTAMP-INTERVAL '3 days' GROUP BY stage")
                rows=cur.fetchall()
        return dict(state='RECORDED' if rows else 'WAITING_MARKET_EVIDENCE',
                    stages={r[0]:dict(events=r[1],signals=r[2]) for r in rows},last_evidence=max((r[3].isoformat() for r in rows),default=None))
    except Exception as exc:
        return dict(state='UNAVAILABLE',error_type=type(exc).__name__)
