"""Read-only audit of saved native trade ownership, fills, PnL and stop state.

Saved mismatches are review findings, not proof of a broker/runtime defect.
No calls to Alpaca, model providers, order engines or Telegram exist here.
"""
from collections import Counter
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time

PROTOCOL='native-trade-chain-evidence-audit-v1'


def obj(value):
    try:
        value=json.loads(value) if isinstance(value,str) else value
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError): return {}


def number(value):
    return float(value) if type(value) in (int,float) and math.isfinite(value) else None


def inspect_trade(trade,entry,exits,orders,decision):
    findings=[]; unknown=[]
    initial,remaining=number(trade.get('initial_quantity')),number(trade.get('remaining_quantity'))
    if initial is None or remaining is None or not 0<=remaining<=initial:
        findings.append('INVALID_POSITION_QUANTITIES')
    filled_entry=None
    if entry:
        if entry.get('symbol')!=trade['symbol'] or str(entry.get('side')).upper()!='BUY':
            findings.append('ENTRY_ORDER_IDENTITY_MISMATCH')
        filled_entry=number(entry.get('filled_price'))
        if number(entry.get('filled_quantity'))!=initial: findings.append('ENTRY_QUANTITY_SNAPSHOT_DIFF')
        if filled_entry is None or filled_entry<=0: unknown.append('ENTRY_FILL_PRICE_UNAVAILABLE')
        elif number(trade.get('entry_price')) is not None and not math.isclose(filled_entry,float(trade['entry_price']),abs_tol=1e-6,rel_tol=1e-8):
            findings.append('ENTRY_PRICE_SNAPSHOT_DIFF')
    else: unknown.append('ENTRY_ORDER_SNAPSHOT_MISSING')
    valid=[]; by_order=Counter(); notionals=Counter()
    for row in exits:
        qty,price,basis,pnl=(number(row.get(k)) for k in ('quantity','fill_price','entry_price','realized_pnl'))
        if row.get('symbol')!=trade['symbol'] or not row.get('order_id') or qty is None or qty<=0 or not qty.is_integer() or price is None or price<=0 or basis is None or basis<=0 or pnl is None:
            findings.append('INVALID_EXIT_LEDGER_EVIDENCE'); continue
        valid.append(row); by_order[row['order_id']]+=int(qty); notionals[row['order_id']]+=qty*price
        if abs(pnl-(price-basis)*qty)>.011: findings.append('EXIT_PNL_ARITHMETIC_DIFF')
        if number(trade.get('entry_price')) is not None and not math.isclose(basis,float(trade['entry_price']),rel_tol=1e-8,abs_tol=1e-6):
            findings.append('EXIT_BASIS_SNAPSHOT_DIFF')
    for order_id,quantity in by_order.items():
        order=orders.get(order_id)
        if order is None: unknown.append('EXIT_ORDER_SNAPSHOT_MISSING'); continue
        if order.get('symbol')!=trade['symbol'] or str(order.get('side')).upper()!='SELL': findings.append('EXIT_ORDER_IDENTITY_MISMATCH')
        if number(order.get('filled_quantity'))!=quantity: findings.append('EXIT_CUMULATIVE_QUANTITY_DIFF')
        broker_average=number(order.get('filled_price'))
        if broker_average is None or broker_average<=0: unknown.append('EXIT_AVERAGE_PRICE_UNAVAILABLE')
        elif abs(broker_average-notionals[order_id]/quantity)>1e-6: findings.append('EXIT_AVERAGE_PRICE_SNAPSHOT_DIFF')
    exit_qty=sum(r['quantity'] for r in valid)
    if initial is not None and remaining is not None and exit_qty!=initial-remaining:
        findings.append('LEDGER_POSITION_QUANTITY_DIFF')
    if trade.get('stage')=='CLOSED' and remaining!=0: findings.append('CLOSED_WITH_REMAINING_QUANTITY')
    metadata=obj(trade.get('metadata_json')); floor=number(trade.get('initial_stop'))
    if trade.get('t1_completed'): floor=number(trade.get('entry_price'))
    if trade.get('t2_completed'): floor=number(trade.get('target_1'))
    if metadata.get('t3_completed'): floor=number(trade.get('target_2'))
    stop=number(trade.get('current_stop'))
    if remaining is not None and remaining>0:
        if floor is None or stop is None: unknown.append('CURRENT_STOP_UNAVAILABLE')
        elif stop+1e-8<floor: findings.append('CURRENT_STOP_BELOW_RECORDED_COMPLETED_STAGE_FLOOR')
        if metadata.get('protective_stop_active') is not True: unknown.append('PROTECTION_NOT_CONFIRMED_IN_SAVED_STATE')
        else:
            protected_qty=number(metadata.get('protective_stop_quantity')); protected_price=number(metadata.get('protective_stop_price'))
            if protected_qty is None or protected_price is None: unknown.append('PROTECTION_DETAILS_MISSING')
            elif protected_qty!=remaining or (stop is not None and protected_price+1e-8<stop):
                findings.append('PROTECTION_SNAPSHOT_DIFF')
    if decision is None: unknown.append('EXACT_ENTRY_DECISION_LINK_MISSING')
    native=obj((decision or {}).get('jalwe')); execution=obj((decision or {}).get('execution'))
    reference=number(native.get('entry_price'))
    slippage=(filled_entry-reference)/reference*10000 if filled_entry is not None and filled_entry>0 and reference is not None and reference>0 else None
    timing={}
    for label,start,end in (
        ('entry_quote_check_to_submission_seconds',obj(obj(native.get('metadata')).get('entry_quality')).get('checked_at'),(entry or {}).get('submitted_at')),
        ('submission_to_fill_seconds',(entry or {}).get('submitted_at'),(entry or {}).get('filled_at'))):
        if start is None or end is None: continue
        try:
            a=datetime.fromisoformat(str(start).replace('Z','+00:00'));b=datetime.fromisoformat(str(end).replace('Z','+00:00'))
            if a.tzinfo is None or b.tzinfo is None: raise ValueError()
            seconds=(b-a).total_seconds()
            if seconds<0: unknown.append('TIMESTAMP_ORDER_CONFLICT')
            else: timing[label]=seconds
        except (ValueError,TypeError): unknown.append('TIMESTAMP_EVIDENCE_INVALID')
    actual_entry=number(trade.get('entry_price'));initial_stop=number(trade.get('initial_stop'))
    risk=(actual_entry-initial_stop)*initial if actual_entry is not None and initial_stop is not None and initial is not None and actual_entry>initial_stop>0 and initial>0 else None
    # This is deviation from recorded decision reference, not measured quote/queue slippage.
    return dict(trade_id=trade['trade_id'],symbol=trade['symbol'],stage=trade['stage'],
        created_at=trade.get('created_at'),closed_at=trade.get('closed_at'),
        state='REVIEW_SAVED_MISMATCH' if findings else 'INCOMPLETE_EVIDENCE' if unknown else 'SAVED_EVIDENCE_CONSISTENT',
        findings=sorted(set(findings)),unknown=sorted(set(unknown)),
        initial_quantity=initial,remaining_quantity=remaining,ledger_exit_quantity=exit_qty,
        exit_events=len(valid),realized_pnl_before_broker_fees=sum(r['realized_pnl'] for r in valid),
        ledger_complete=len(valid)==len(exits) and initial is not None and remaining is not None and exit_qty==initial-remaining,
        entry_fill_price=filled_entry,decision_entry_reference=reference,
        fill_vs_decision_reference_bps=slippage,reference_deviation_is_measured_execution_cost=False,
        decision_state=native.get('state'),decision_reason=native.get('reason'),
        replay_id=(decision or {}).get('decision_replay_id'),
        timing=timing,initial_risk_dollars=risk,
        stop_snapshot=stop,stage_floor_snapshot=floor,exit_actions=sorted({str(r.get('action')) for r in valid}),
        scope='SAVED_SNAPSHOTS_NOT_CURRENT_BROKER_STATE_OR_CAUSAL_LOSS_DIAGNOSIS')


def audit(path=None,symbol=None,limit=20):
    from core.storage import database_path
    path=Path(path or database_path()); symbol=str(symbol or '').strip().upper()
    base=dict(protocol=PROTOCOL,execution_authority=False,checked_at=datetime.now(timezone.utc).isoformat(),trades=[])
    if symbol and not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',symbol): return dict(base,state='INVALID_SYMBOL')
    if not path.exists(): return dict(base,state='DATABASE_UNAVAILABLE')
    limit=max(1,min(int(limit),50))
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5); conn.row_factory=sqlite3.Row
    try:
        deadline=time.monotonic()+10.
        conn.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        conn.execute('BEGIN') # one consistent read snapshot, no schema or data writes
        trade_rows=conn.execute('SELECT * FROM managed_trades '+('WHERE symbol=? ' if symbol else '')+'ORDER BY created_at DESC,trade_id DESC LIMIT ?',
            (symbol,limit+1) if symbol else (limit+1,)).fetchall()
        results=[]
        wanted={r['trade_id'] for r in trade_rows[:limit]}
        decision_by_identity={}
        decision_rows=[]
        if wanted:
            marks=','.join('?' for _ in wanted)
            # One scan, not one full historical JSON scan per trade.
            decision_rows=conn.execute("SELECT metadata_json FROM system_events WHERE event_type='APEX_RESEARCH_DECISION' AND CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json,'$.execution.managed_trade_id') END IN ("+marks+") ORDER BY id DESC LIMIT 501",tuple(wanted)).fetchall()
            for row in decision_rows:
                p=obj(row[0]);e=obj(p.get('execution'))
                identity=(e.get('managed_trade_id'),e.get('broker_order_id'))
                if e.get('broker_order_submitted') is True: decision_by_identity.setdefault(identity,p)
        for encoded in trade_rows[:limit]:
            t=dict(encoded)
            entry=conn.execute('SELECT * FROM broker_orders WHERE order_id=?',(t.get('entry_order_id'),)).fetchone()
            exits=[dict(r) for r in conn.execute('SELECT * FROM strategy_pnl_ledger WHERE trade_id=? ORDER BY event_time,event_key LIMIT 1001',(t['trade_id'],))]
            if len(exits)>1000:
                results.append(dict(trade_id=t['trade_id'],symbol=t['symbol'],stage=t['stage'],state='EXIT_LIMIT_REACHED',findings=[],unknown=['TRUNCATED_EXIT_EVIDENCE']));continue
            orders={}
            for order_id in {r.get('order_id') for r in exits if r.get('order_id')}:
                order=conn.execute('SELECT * FROM broker_orders WHERE order_id=?',(order_id,)).fetchone()
                if order: orders[order_id]=dict(order)
            # Match native entry by BOTH managed trade identity and entry order.
            # A newer rejection for this symbol can never replace its entry reason.
            p=decision_by_identity.get((t['trade_id'],t.get('entry_order_id')))
            if p and (p.get('symbol')!=t['symbol'] or obj(p.get('execution')).get('broker_order_submitted') is not True): p=None
            result=inspect_trade(t,dict(entry) if entry else None,exits,orders,p)
            results.append(result)
        return dict(base,state='AUDITED' if results else 'NO_MANAGED_TRADES',trades=results,
            states=dict(Counter(r['state'] for r in results)),entry_link_limit_reached=len(decision_rows)>=501,truncated=len(trade_rows)>limit,
            loss_cause='UNPROVEN_NO_MARKET_COUNTERFACTUAL',fees='NOT_AVAILABLE_NOT_ASSUMED_ZERO')
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError) as exc:
        return dict(base,state='EVIDENCE_UNAVAILABLE',error_type=type(exc).__name__)
    finally: conn.close()


def status_text(symbol=None,path=None):
    r=audit(path,symbol,limit=5 if symbol else 10)
    lines=['🧾 تدقيق سلسلة الصفقة — سجلات JALWE','الحالة: '+r['state']]
    for t in r['trades']:
        lines.append(t['symbol']+' | '+t['stage']+' | '+t['state'])
        if 'realized_pnl_before_broker_fees' in t:
            lines.append(f"  محقق مسجل قبل الرسوم: ${t['realized_pnl_before_broker_fees']:+.2f} | اكتمال الكميات: {'نعم' if t['ledger_complete'] else 'غير مثبت'}")
        if t.get('decision_reason'): lines.append('  سبب الدخول المسجل: '+str(t['decision_reason'])[:160])
        if t.get('fill_vs_decision_reference_bps') is not None: lines.append(f"  فرق التنفيذ عن مرجع القرار: {t['fill_vs_decision_reference_bps']:+.1f} نقطة أساس؛ ليس قياس تكلفة التنفيذ كاملة")
        if t.get('exit_actions'): lines.append('  الخروج المسجل: '+', '.join(t['exit_actions']))
        if t.get('findings'): lines.append('  فروق محفوظة للمراجعة: '+', '.join(t['findings']))
        if t.get('unknown'): lines.append('  أدلة ناقصة: '+', '.join(t['unknown']))
    if r.get('truncated'): lines.append('العرض محدود؛ ليس سجل الحساب كاملًا.')
    lines.append('السجل لا يثبت حالة الوسيط الحالية أو سبب حركة السعر. الرسوم غير متاحة؛ لا تحسب صفرًا.')
    return '\n'.join(lines)[:3800]


def emit_audit(path):
    result=audit(path,limit=20)
    print('NATIVE_TRADE_CHAIN_AUDIT '+json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
    return result
