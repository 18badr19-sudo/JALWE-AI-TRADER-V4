"""Frozen prior-day nominees: offline ORB entry/exit model, never live orders."""
from collections import Counter
from datetime import datetime,timezone,timedelta
import json
import math
from pathlib import Path
import sqlite3

from market_move_research import utc,observe_session,default_path
from prior_day_priority_bus import build,publish,events,signal_id,valid_signal,PROTOCOL as BUS_PROTOCOL

PROTOCOL='prior-day-orb-execution-study-v1'
COST_BPS=(10,25)


def prepare(store,now,*,paper,allow_live=False,clock=None):
    if not paper or allow_live: return dict(state='PAPER_ONLY')
    with store.connect() as conn:
        from prior_day_priority_bus import NY
        rows=conn.execute('''SELECT payload_json FROM move_pattern_features
            WHERE session_day>=? ORDER BY session_day,symbol''',
            (now.astimezone(NY).date().isoformat(),)).fetchall()
    groups={}
    for encoded, in rows:
        value=json.loads(encoded); session=value['session']
        groups.setdefault(session['date'],(session,[]))[1].append(value)
    audit=dict(state='WAITING_PREOPEN_SNAPSHOT',queued=0)
    for session,records in groups.values():
        stamp=clock() if clock else now
        packet=build(records,session,stamp)
        if packet is None: continue
        queued=0
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            stamp=clock() if clock else now
            if stamp>=utc(session['open']): continue
            values=[]
            for signal in packet['signals']:
                f=dict(signal['features'],symbol=signal['symbol'],entry_protocol=PROTOCOL,
                    priority_signal_id=signal['signal_id'],snapshot_prepared_at=signal['prepared_at'])
                values.append(store._episode_values(f,session,'ENTRY_STUDY',signal['signal_id'],stamp,prospective=False))
            before=conn.total_changes
            conn.executemany('''INSERT OR IGNORE INTO move_episodes
                (episode_key,session_day,symbol,role,pair_key,prospective,frozen_at,features_json,session_json)
                VALUES (?,?,?,?,?,?,?,?,?)''',values)
            queued=conn.total_changes-before
            if clock and clock()>=utc(session['open']):
                conn.rollback(); continue
        audit=publish(packet,paper=paper,allow_live=allow_live,clock=clock)
        from research_nomination_audit import audit_snapshot
        audit['snapshot_audit']=audit_snapshot(records,session,audit)
        import logging
        logging.getLogger(__name__).info('RESEARCH_NOMINATION_AUDIT %s',
            json.dumps(audit['snapshot_audit'],sort_keys=True,allow_nan=False))
        audit.update(queued=queued,eligible=len(packet['signals']),session_day=session['date'])
        store.state('prior_day_bus_status',audit)
    return audit


def simulate(data,session):
    base=dict(protocol=PROTOCOL,scope='SIMULATED_NOT_REALIZED_PNL',execution_authority=False,
        entry_rule='CLOSED_1M_ORB15_VOLUME_1_5_NEXT_OPEN',target_rule='ONE_EXIT_AT_2R',
        cost_model='ASSUMED_ALL_IN_SPREAD_SLIPPAGE_FEES_NOT_MEASURED',cost_bps_per_side=list(COST_BPS))
    quality=observe_session(data,session)
    if quality['status']!='COMPLETE': return dict(base,state='UNKNOWN_DATA',scenarios={},missing_minutes=quality.get('missing_minutes'))
    # The coverage validator sorts/coerces a copy. Use the same canonical
    # expected minutes for modelling, rather than the unvalidated raw order.
    import pandas as pd
    expected=pd.date_range(utc(session['open']),utc(session['close']),freq='min',inclusive='left')
    bars=data.loc[data.index.isin(expected),['open','high','low','close','volume']].copy()
    if bars.map(lambda v:type(v).__name__ in {'bool','bool_'}).any().any():
        return dict(base,state='UNKNOWN_DATA',scenarios={},reason='BOOLEAN_OHLCV')
    bars.index=pd.to_datetime(bars.index,utc=True)
    bars=bars.apply(pd.to_numeric,errors='coerce').sort_index()
    if len(bars)<17: return dict(base,state='INSUFFICIENT_SESSION',scenarios={})
    trigger,stop=float(bars.high.iloc[:15].max()),float(bars.low.iloc[:15].min())
    base.update(trigger=trigger,stop=stop,opening_range_end=(utc(session['open'])+timedelta(minutes=15)).isoformat())
    if trigger<=stop: return dict(base,state='INVALID_RANGE',scenarios={})
    signal=None
    for i in range(15,len(bars)):
        b=bars.iloc[i]
        if b.low<=stop: return dict(base,state='INVALIDATED_BEFORE_ENTRY',scenarios={})
        mean=float(bars.volume.iloc[i-5:i].mean())
        wick=(b.high-max(b.open,b.close))/(b.high-b.low) if b.high>b.low else 1.
        if trigger<float(b.close)<=trigger*1.015 and mean>0 and b.volume>=1.5*mean and wick<=.35:
            signal=i; break
    if signal is None: return dict(base,state='NO_ENTRY',scenarios={})
    if signal+1==len(bars): return dict(base,state='NO_NEXT_BAR',signal_at=bars.index[signal].isoformat(),scenarios={})
    entry_index=signal+1; entry=float(bars.open.iloc[entry_index]); risk=entry-stop
    base.update(signal_at=bars.index[signal].isoformat(),entry_at=bars.index[entry_index].isoformat(),
        signal_confirmed_at=(bars.index[signal]+timedelta(minutes=1)).isoformat(),
        modelled_entry=entry,modelled_fill='NOT_CONFIRMED_BY_QUOTE_OR_QUEUE')
    if entry>trigger*1.015: return dict(base,state='NO_FILL_CHASE_LIMIT',scenarios={})
    if risk<=0: return dict(base,state='NO_FILL_STOP_GAP',scenarios={})
    if entry<trigger: return dict(base,state='NO_FILL_TRIGGER_LOST',scenarios={})
    target=entry+2*risk; base['target']=target
    outcome,exit_price,exit_at='SESSION_CLOSE',float(bars.close.iloc[-1]),bars.index[-1]
    for stamp,b in bars.iloc[entry_index:].iterrows():
        if b.open<=stop:
            outcome,exit_price,exit_at='STOP_GAP',float(b.open),stamp; break
        if b.open>=target:
            outcome,exit_price,exit_at='TARGET_AT_OPEN',target,stamp; break
        if b.low<=stop and b.high>=target:
            return dict(base,state='AMBIGUOUS_STOP_TARGET',exit_at=stamp.isoformat(),scenarios={})
        if b.low<=stop:
            outcome,exit_price,exit_at='STOP',stop,stamp; break
        if b.high>=target:
            outcome,exit_price,exit_at='TARGET',target,stamp; break
    scenarios={}
    for bps in COST_BPS:
        cost=bps/10000
        net=exit_price*(1-cost)-entry*(1+cost)
        scenarios[str(bps)]=dict(net_r=net/risk,net_per_share=net,assumed_bps_per_side=bps)
    return dict(base,state='SIMULATED_EXIT',outcome=outcome,modelled_exit=exit_price,
                exit_at=exit_at.isoformat(),scenarios=scenarios)


def summary(rows):
    studies=[r for r in rows if r['role']=='ENTRY_STUDY' and r['features'].get('entry_protocol')==PROTOCOL]
    labels=Counter(); results=[]
    for row in studies:
        result=row['result'].get('entry_study',{})
        label=('PENDING' if row['status']=='PENDING' else 'UNKNOWN_DATA' if row['status']!='COMPLETE'
               else result.get('state','UNKNOWN_DATA'))
        labels[label]+=1
        if row['status']=='COMPLETE' and result.get('state')=='SIMULATED_EXIT': results.append(result)
    costs={}
    for bps in COST_BPS:
        measured=[r['scenarios'][str(bps)]['net_r'] for r in results]
        costs[str(bps)]=dict(simulated_exits=len(measured),positive=sum(v>0 for v in measured),
            negative=sum(v<0 for v in measured),mean_net_r=sum(measured)/len(measured) if measured else None)
    return dict(protocol=PROTOCOL,total=len(studies),states=dict(labels),costs=costs,
                execution_authority=False,scope='SIMULATED_NOT_REALIZED_PNL')


def trace_decision(research,payload):
    """Correlate native decisions/orders; never alter their payload or authority."""
    metadata=getattr(research,'metadata',{}) or {}; proof=metadata.get('prior_day_signal')
    if not isinstance(proof,dict) or proof.get('protocol')!=BUS_PROTOCOL: return
    if not valid_signal(proof.get('signal',{}),proof.get('session',{})): return
    if (str(getattr(research,'symbol','')).upper()!=proof['signal']['symbol']
            or payload.get('symbol')!=proof['signal']['symbol']): return
    signal=proof['signal']; now=datetime.now(timezone.utc).isoformat()
    decision=payload.get('jalwe',{}); execution=payload.get('execution',{})
    row=dict(signal_id=signal['signal_id'],symbol=signal['symbol'],stage='JALWE_DECISION',
        trace_key=str(getattr(research,'created_at',''))+'|'+str(payload.get('timestamp',now)),at=now,reason=decision.get('reason'),state=decision.get('state'),
        report_at=str(getattr(research,'created_at','')),scan_id=proof.get('scan_id'))
    rows=[row]
    if execution.get('broker_order_submitted'):
        rows.append(dict(row,stage='JALWE_EXECUTION',trace_key=str(execution.get('broker_order_id')),broker_order_id=execution.get('broker_order_id'),
            managed_trade_id=execution.get('managed_trade_id'),state=execution.get('orchestrator_state'),
            reason='PAPER_SUBMISSION_NOT_FILL_CONFIRMATION'))
    events(rows)
    return rows


def native_ledger_rows(mappings,path):
    """Read broker-reconciled native exits; never adopt simulated study results."""
    by_trade={}
    for mapping in mappings:
        trade=mapping.get('managed_trade_id')
        if trade and mapping.get('signal_id'):
            by_trade.setdefault(trade,[]).append(mapping)
    ids=[t for t,values in by_trade.items() if len({m['signal_id'] for m in values})==1]
    if not ids or not Path(path).exists(): return []
    rows=[]
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    conn.row_factory=sqlite3.Row
    try:
        for offset in range(0,len(ids),200):
            batch=ids[offset:offset+200]
            for value in conn.execute('SELECT * FROM strategy_pnl_ledger WHERE trade_id IN ('+','.join('?' for _ in batch)+')',batch):
                record=dict(value); mapping=by_trade[record['trade_id']][0]
                pnl=record['realized_pnl']
                if type(pnl) not in (int,float) or not math.isfinite(pnl): continue
                price=record['fill_price']; quantity=record['quantity']
                if (type(quantity) is not int or quantity<=0 or not record['order_id']
                        or type(price) not in (int,float) or not math.isfinite(price) or price<=0): continue
                stamp=utc(record['event_time']).isoformat()
                rows.append(dict(signal_id=mapping['signal_id'],symbol=record['symbol'],stage='JALWE_REALIZED_EXIT',
                    trace_key=record['event_key'],at=stamp,managed_trade_id=record['trade_id'],
                    broker_order_id=record['order_id'],quantity=record['quantity'],fill_price=record['fill_price'],
                    realized_pnl_before_broker_fees=pnl,reason='NATIVE_BROKER_RECONCILED_LEDGER'))
    finally:
        conn.close()
    return rows


def collect_realized(*,dsn=None,database_path=None):
    from prior_day_priority_bus import connect
    from core.config import settings
    import os
    if not (dsn or os.getenv('DATABASE_URL','')): return dict(state='WAITING_SHARED_DATABASE')
    try:
        with connect(dsn) as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute("SELECT payload_json FROM jalwe_prior_day_events WHERE stage='JALWE_EXECUTION' AND created_at>CURRENT_TIMESTAMP-INTERVAL '30 days' ORDER BY created_at DESC LIMIT 1000")
                mappings=[json.loads(row[0]) for row in cur.fetchall()]
        rows=native_ledger_rows(mappings,database_path or settings.DATABASE_PATH)
        events(rows,dsn=dsn)
        return dict(state='NATIVE_RECONCILED_EXITS' if rows else 'WAITING_NATIVE_EXIT_EVIDENCE',
            exit_fill_events=len(rows),unique_trades=len({r['managed_trade_id'] for r in rows}),
            realized_pnl_before_broker_fees=sum(r['realized_pnl_before_broker_fees'] for r in rows),
            mapping_limit_reached=len(mappings)==1000,simulation_included=False)
    except Exception as exc:
        return dict(state='UNAVAILABLE',error_type=type(exc).__name__)


def stock_trace_text(symbol,day,*,dsn=None,database_path=None,research_path=None):
    """Read one exact frozen signal and its native ledger; never write events."""
    from market_research_findings import SYMBOL
    from prior_day_priority_bus import connect
    from core.config import settings
    import os
    from research_nomination_audit import local_evidence,nomination_text,study_text,saudi,reason_text
    symbol=str(symbol).strip().upper()
    try:
        if not SYMBOL.fullmatch(symbol) or datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day:
            return 'رمز السهم أو التاريخ غير صالح.'
    except (ValueError,TypeError):return 'رمز السهم أو التاريخ غير صالح.'
    local=local_evidence(symbol,day,path=research_path)
    lines=[f'🔗 سلسلة الترشيح {symbol} — {day} (أدلة محفوظة)']
    model=study_text(symbol,day,local)
    if not (dsn or os.getenv('DATABASE_URL','')):
        return '\n'.join(lines+['قاعدة تتبع الترشيح غير متاحة؛ وصوله إلى APEX غير معلوم.']+model)
    try:
        identity=signal_id(symbol,day)
        with connect(dsn) as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute('''SELECT payload_json FROM jalwe_prior_day_packets
                    WHERE protocol=%s AND session_day=%s''',(BUS_PROTOCOL,day))
                saved=cur.fetchone()
                packet=json.loads(saved[0]) if saved else None
                # Aggregate the full available history, not the last batch.
                cur.execute('''SELECT stage,COUNT(*),MIN(created_at),MAX(created_at)
                    FROM jalwe_prior_day_events WHERE signal_id=%s GROUP BY stage
                    ORDER BY stage LIMIT 21''',(identity,))
                timeline=cur.fetchall()
                cur.execute('''SELECT payload_json FROM (
                    SELECT DISTINCT ON (stage) stage,payload_json,created_at,event_id
                    FROM jalwe_prior_day_events WHERE signal_id=%s
                    ORDER BY stage,created_at DESC,event_id DESC) latest
                    ORDER BY stage LIMIT 21''',(identity,))
                latest_rows=cur.fetchall()
                cur.execute('''SELECT payload_json FROM jalwe_prior_day_events
                    WHERE signal_id=%s AND stage='JALWE_EXECUTION'
                    ORDER BY created_at DESC,event_id DESC LIMIT 1001''',(identity,))
                raw=cur.fetchall()
        lines.extend(nomination_text(symbol,day,packet,local))
        rows=[json.loads(r[0]) for r in latest_rows[:20]]
        rows=[r for r in rows if r.get('signal_id')==identity and r.get('symbol')==symbol]
        by_stage={r['stage']:r for r in rows}
        labels={'APEX_RADAR':'رادار APEX','APEX_PRE':'الفحص الأولي','APEX_DEEP':'التحليل العميق',
                'APEX_RESEARCH':'نتيجة البحث','APEX_BRIDGE':'الإرسال إلى JALWE',
                'JALWE_DECISION':'قرار JALWE','JALWE_EXECUTION':'إرسال أمر PAPER'}
        for stage,count,first,last in timeline[:20]:
            if stage not in by_stage:continue
            latest=by_stage[stage]
            lines.append(f"• {labels.get(stage,stage)}: {count} حدث | أول {saudi(first)} | آخر {saudi(last)} السعودية.")
            lines.append('  آخر حالة/سبب محفوظ: '+str(latest.get('state') or '')+' '+reason_text(latest))
        if not timeline:lines.append('لا توجد أحداث ربط متاحة؛ هذا لا يثبت أن السهم رُفض أو لم يُتداول.')
        elif not any(r.get('stage')=='APEX_RADAR' for r in rows):
            lines.append('أول وصول إلى رادار APEX غير مثبت في الأحداث المقروءة.')
        decisions=[r for r in rows if r.get('stage')=='JALWE_DECISION']
        if decisions:
            latest=decisions[0]
            lines.append('آخر قرار محفوظ: '+str(latest.get('state','غير معلوم'))+' — '+str(latest.get('reason','غير معلوم'))[:300])
        mappings=[json.loads(r[0]) for r in raw[:1000]]
        mappings=[r for r in mappings if r.get('signal_id')==identity and r.get('symbol')==symbol
                  and r.get('stage')=='JALWE_EXECUTION']
        exits=native_ledger_rows(mappings,database_path or settings.DATABASE_PATH)
        exits=[r for r in exits if r['signal_id']==identity and r['symbol']==symbol]
        if exits:
            lines.append(f"مخارج PAPER مؤكدة بالسجل: {len(exits)} تنفيذ، {sum(r['quantity'] for r in exits)} سهم؛ المحقق قبل رسوم الوسيط ${sum(r['realized_pnl_before_broker_fees'] for r in exits):+.2f}.")
            lines.append('قد تكون مخارج جزئية؛ لا يثبت ذلك إغلاق كامل المركز. رسوم الوسيط غير محسومة هنا.')
        else:lines.append('لا يوجد دليل خروج PAPER مرتبط في السجل المقروء؛ أمر الشراء المرسل وحده ليس تنفيذًا، والربح غير معلوم.')
        if len(raw)>1000 or len(timeline)>20 or len(latest_rows)>20:
            lines.append('وصلت القراءة حد مراحل أو أوامر الربط؛ التغطية غير كاملة.')
        lines.append('التوقيت يغطي أول وآخر حدث محفوظ لكل مرحلة؛ غياب مرحلة لا يثبت الرفض. قد تكون أحداث أقدم من مدة الاحتفاظ غير متاحة.')
        lines.append('أحداث الفحص ليست صفقات؛ لا تتضمن نتائج المحاكاة في الربح المحقق.')
        return '\n'.join(lines+model)
    except Exception as exc:
        return '\n'.join(lines+['ربط PAPER للسهم: تعذرت قراءة الأدلة ('+type(exc).__name__+')؛ النتيجة غير معلومة.']+model)
