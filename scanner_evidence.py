"""Bounded advisory scanner history shared by APEX/JALWE; never trade authority."""
from datetime import datetime, timezone, timedelta
from contextlib import closing
import hashlib
import json
import logging
import math
import os
import queue
import re
import threading
from zoneinfo import ZoneInfo

LOG=logging.getLogger(__name__)
FIELDS={'symbol','stage','reason','observed_price','bar_at','bar_age_s','trade_at',
        'rank','rank_score','radar_limit','deep_limit','pre_score','confidence',
        'score','minimum','watch_threshold','early_surge_priority','prior_day_priority'}
FIELDS.update({'min_price','max_price','bar_refresh','snapshot_bar_at','snapshot_bar_age_s'})
_queue=queue.Queue(maxsize=32)
_lock=threading.Lock()
_thread=None

def normalize(scan_id,feed,rows,now):
    if not isinstance(scan_id,str) or not 0<len(scan_id)<=200 or now.tzinfo is None:
        raise ValueError('INVALID_SCAN_ID_OR_TIME')
    if len(rows)>20000:raise ValueError('AUDIT_ROW_LIMIT')
    day=now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    clean={}
    for raw in rows:
        if not isinstance(raw,dict):continue
        symbol=raw.get('symbol');stage=raw.get('stage');reason=raw.get('reason')
        if (not isinstance(symbol,str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,19}',symbol)
                or stage not in {'UNIVERSE','DATA','RADAR','PRE','DEEP','RESEARCH','BRIDGE'}
                or not isinstance(reason,str) or not 0<len(reason)<=160):continue
        row={k:v for k,v in raw.items() if k in FIELDS and
             (v is None or type(v) in (str,bool,int,float))}
        row={k:(None if type(v) is float and not math.isfinite(v) else v[:250] if isinstance(v,str) else v)
             for k,v in row.items()}
        clean[json.dumps(row,sort_keys=True,allow_nan=False)]=row
    canonical=sorted(clean)
    batch=hashlib.sha256(json.dumps([scan_id,str(feed),canonical],separators=(',',':')).encode()).hexdigest()
    stamp=now.astimezone(timezone.utc).isoformat()
    return dict(batch=batch,day=day,at=stamp,scan_id=scan_id,feed=str(feed)[:20],rows=[clean[k] for k in canonical])

def write(packet,*,dsn=None):
    """One atomic batch; duplicate retries do not add observation counts."""
    from prior_day_priority_bus import connect
    from psycopg2.extras import execute_values
    with closing(connect(dsn)) as conn, conn:
        with conn.cursor() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS apex_scan_evidence_batches
                (batch TEXT PRIMARY KEY,seen_at TIMESTAMPTZ NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS apex_scan_evidence
                (day TEXT NOT NULL,symbol TEXT NOT NULL,stage TEXT NOT NULL,reason TEXT NOT NULL,
                 first_at TIMESTAMPTZ NOT NULL,last_at TIMESTAMPTZ NOT NULL,observations BIGINT NOT NULL,
                 first_json TEXT NOT NULL,last_json TEXT NOT NULL,
                 PRIMARY KEY(day,symbol,stage,reason))''')
            c.execute('INSERT INTO apex_scan_evidence_batches VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING batch',
                      (packet['batch'],packet['at']))
            if not c.fetchone():return dict(state='DUPLICATE',rows=0)
            grouped={}
            for row in packet['rows']:
                key=(row['symbol'],row['stage'],row['reason'])
                # Same stage/reason appears once per batch, even if input repeats.
                grouped.setdefault(key,row)
            values=[]
            for (symbol,stage,reason),row in grouped.items():
                payload=json.dumps(dict(row,scan_id=packet['scan_id'],feed=packet['feed'],at=packet['at']),allow_nan=False)
                values.append((packet['day'],symbol,stage,reason,packet['at'],packet['at'],1,payload,payload))
            if values:
                execute_values(c,'''INSERT INTO apex_scan_evidence VALUES %s
                    ON CONFLICT(day,symbol,stage,reason) DO UPDATE SET
                    first_json=CASE WHEN EXCLUDED.first_at<apex_scan_evidence.first_at THEN EXCLUDED.first_json ELSE apex_scan_evidence.first_json END,
                    last_json=CASE WHEN EXCLUDED.last_at>=apex_scan_evidence.last_at THEN EXCLUDED.last_json ELSE apex_scan_evidence.last_json END,
                    first_at=LEAST(apex_scan_evidence.first_at,EXCLUDED.first_at),
                    last_at=GREATEST(apex_scan_evidence.last_at,EXCLUDED.last_at),
                    observations=apex_scan_evidence.observations+1''',values,page_size=256)
            # Time-bounded retained history, independent of research trial expiry.
            c.execute('DELETE FROM apex_scan_evidence WHERE day<%s',
                      ((datetime.fromisoformat(packet['day']).date()-timedelta(days=14)).isoformat(),))
            c.execute("DELETE FROM apex_scan_evidence_batches WHERE seen_at<CURRENT_TIMESTAMP-INTERVAL '14 days'")
    return dict(state='SAVED',rows=len(values))

def _work():
    while True:
        packet=_queue.get()
        try:
            result=write(packet)
            LOG.info('SCANNER_EVIDENCE_SAVED %s',json.dumps(dict(result,scan_id=packet['scan_id'])))
        except Exception as exc:
            LOG.warning('SCANNER_EVIDENCE_UNAVAILABLE %s',type(exc).__name__)
        finally:_queue.task_done()

def submit(scan_id,feed,rows):
    if not os.getenv('DATABASE_URL',''):return 'NO_SHARED_DATABASE'
    try:
        packet=normalize(scan_id,feed,rows,datetime.now(timezone.utc))
        if not packet['rows']:return 'NO_VALID_ROWS'
        global _thread
        with _lock:
            if _thread is None or not _thread.is_alive():
                _thread=threading.Thread(target=_work,name='scanner-evidence',daemon=True);_thread.start()
        _queue.put_nowait(packet)
        return 'QUEUED_NOT_CONFIRMED'
    except queue.Full:
        LOG.warning('SCANNER_EVIDENCE_QUEUE_FULL evidence_gap=true')
        return 'QUEUE_FULL'
    except Exception as exc:
        LOG.warning('SCANNER_EVIDENCE_INVALID %s',type(exc).__name__)
        return 'INVALID'

def stock_scan_text(symbol,day,*,dsn=None):
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,19}',symbol):return 'رمز غير صالح.'
    try:
        if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day:raise ValueError()
    except (TypeError,ValueError):return 'تاريخ غير صالح.'
    title=f'🔎 فرز APEX العام {symbol} — {day} NY'
    if not (dsn or os.getenv('DATABASE_URL','')):return title+'\nقاعدة سجل الفرز غير متاحة؛ السبب غير معلوم.'
    try:
        from prior_day_priority_bus import connect
        with closing(connect(dsn)) as conn, conn:
            conn.set_session(readonly=True)
            with conn.cursor() as c:
                c.execute('''SELECT stage,reason,first_at,last_at,observations,first_json,last_json
                    FROM apex_scan_evidence WHERE day=%s AND symbol=%s
                    ORDER BY first_at,stage,reason LIMIT 65''',(day,symbol))
                rows=c.fetchall()
        if not rows:return title+'\nلا توجد أدلة فرز محفوظة؛ لا يثبت ذلك الرفض أو عدم التداول. الحفظ يبدأ من الإصدار الجديد.'
        def at(v):
            return datetime.fromisoformat(str(v).replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%H:%M:%S')
        lines=[title]
        labels={'observed_price':'السعر','min_price':'أدنى سعر مسموح','max_price':'أعلى سعر مسموح','rank':'الترتيب','deep_limit':'مقاعد التحليل','radar_limit':'مقاعد الرادار','pre_score':'درجة الفحص','bar_age_s':'عمر الشمعة بالثواني','bar_refresh':'حالة تحديث الشمعة'}
        for stage,reason,first,last,count,first_json,last_json in rows[:64]:
            lines.append(f'• {stage}: {reason} | أول {at(first)} | آخر {at(last)} السعودية | {count} رصد')
            for label,encoded in [('أول رصد',first_json),('آخر رصد',last_json)]:
                r=json.loads(encoded)
                facts=[f'{name}: {r[k]}' for k,name in labels.items() if k in r and r[k] is not None]
                if facts:lines.append('  '+label+' — '+' | '.join(facts))
        if len(rows)>64:lines.append('وصلت القراءة حد 64 سببًا؛ التغطية جزئية.')
        lines.append('ملخص أول وآخر رصد لكل سبب، وليس تسلسل كل دورة. يشمل قبل الحركة وبعدها؛ لا يثبت السبب وقت بداية القفزة. الاحتفاظ 14 يومًا وقد توجد فجوات حفظ؛ لا يُستنتج تنفيذ أو ربح.')
        return '\n'.join(lines)
    except Exception as exc:
        return title+'\nتعذرت قراءة سجل الفرز ('+type(exc).__name__+')؛ السبب غير معلوم.'
