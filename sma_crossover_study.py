"""Fixed SPY daily 20/50 crossover research; never imported by execution engines."""
from datetime import datetime,timedelta,timezone
import json
import logging
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

PROTOCOL='spy-daily-sma20-50-next-open-v1'
KEY='sma_crossover_study'
NY=ZoneInfo('America/New_York')
LOGGER=logging.getLogger(__name__)


def simulate(rows,sessions):
    base=dict(protocol=PROTOCOL,symbol='SPY',execution_authority=False,
        scope='RETROSPECTIVE_SIMULATION_NOT_BROKER_PNL',initial_capital=100.,
        rule='SMA20_CROSS_SMA50_CLOSED_DAILY_NEXT_SESSION_OPEN_LONG_CASH',
        cost_model='ASSUMED_10_OR_25_BPS_EACH_SIDE_NOT_MEASURED',dividends_included=False)
    if len(sessions)<52: return dict(base,state='INSUFFICIENT_HISTORY')
    expected=[s['date'] for s in sessions]; by_day={}
    try:
        if len(set(expected))!=len(expected) or expected!=sorted(expected): raise ValueError()
        for b in rows:
            stamp=datetime.fromisoformat(b['t'].replace('Z','+00:00'))
            if stamp.tzinfo is None: raise ValueError()
            day=stamp.astimezone(NY).date().isoformat()
            if day not in expected: continue
            if day in by_day: raise ValueError()
            values=[b[k] for k in ('o','h','l','c','v')]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values): raise ValueError()
            o,h,l,c,v=values
            if min(o,h,l,c)<=0 or v<=0 or h<max(o,l,c) or l>min(o,h,c): raise ValueError()
            by_day[day]=b
        missing=[day for day in expected if day not in by_day]
        if missing: return dict(base,state='INCOMPLETE_DAILY_DATA',missing_sessions=len(missing))
    except (ValueError,KeyError,TypeError,OverflowError): return dict(base,state='INVALID_DAILY_DATA')
    bars=[by_day[d] for d in expected]; closes=[b['c'] for b in bars]
    sma20=[None if i<19 else sum(closes[i-19:i+1])/20 for i in range(len(bars))]
    sma50=[None if i<49 else sum(closes[i-49:i+1])/50 for i in range(len(bars))]
    if any(x is not None and not math.isfinite(x) for x in sma20+sma50):
        return dict(base,state='INVALID_DAILY_DATA')
    signals={i:('BUY' if sma20[i]>sma50[i] and sma20[i-1]<=sma50[i-1] else
                'SELL' if sma20[i]<sma50[i] and sma20[i-1]>=sma50[i-1] else 'HOLD') for i in range(50,len(bars))}
    scenarios={}
    for bps in (10,25):
        cost=bps/10000; cash=100.; qty=0.; entry_cost=0.; peak=100.; drawdown=0.; trades=[]; entries=[]
        for i in range(51,len(bars)):
            signal=signals[i-1]; price=bars[i]['o']
            if signal=='BUY' and qty==0:
                entry_cost=cash; qty=cash/(price*(1+cost)); cash=0.
                entries.append(dict(signal_day=expected[i-1],entry_day=expected[i],price=price))
            elif signal=='SELL' and qty>0:
                cash=qty*price*(1-cost); trades.append(dict(exit_day=expected[i],price=price,net_pnl=cash-entry_cost)); qty=0.
            value=cash+qty*bars[i]['c']; peak=max(peak,value); drawdown=max(drawdown,(peak-value)/peak*100)
        value=cash+qty*closes[-1]
        hold=100/(bars[51]['o']*(1+cost))*closes[-1]
        scenarios[str(bps)]=dict(marked_equity=value,return_pct=value-100,
            closed_trades=len(trades),winning_closed_trades=sum(t['net_pnl']>0 for t in trades),
            closed_trade_pnl=sum(t['net_pnl'] for t in trades),open_position=qty>0,
            open_position_marked_value=qty*closes[-1],max_close_to_close_drawdown_pct=drawdown,
            buy_hold_marked_return_pct=hold-100,excess_marked_return_pp=value-hold,
            entries=entries,exits=trades,open_exit_cost_not_charged=True)
    return dict(base,state='SIMULATED',sessions=len(bars),first_test_day=expected[51],
        last_session=expected[-1],latest_closed_signal=signals[len(bars)-1],scenarios=scenarios,
        interpretation='DESCRIPTIVE_SINGLE_SYMBOL_NO_PROMOTION_OR_LIVE_ORDERS')


def refresh(store,provider,now):
    """One benchmark read daily; failed reads back off one hour. No execution dependency."""
    previous=store.state(KEY) or {}
    last=previous.get('checked_at')
    if last:
        at=datetime.fromisoformat(last)
        wait=86400 if previous.get('state')=='SIMULATED' else 3600
        if (now-at).total_seconds()<wait: return previous
    try:
        raw=provider.get('https://paper-api.alpaca.markets/v2/calendar',
            {'start':(now.date()-timedelta(days=400)).isoformat(),'end':now.date().isoformat()})
        sessions=[]
        for row in raw:
            close=datetime.fromisoformat(row['date']+'T'+row['close']).replace(tzinfo=NY).astimezone(timezone.utc)
            if close+timedelta(minutes=16)<=now: sessions.append(dict(date=row['date'],close=close))
        sessions=sorted(sessions,key=lambda s:s['date'])[-252:]
        if not sessions: result=dict(protocol=PROTOCOL,state='INSUFFICIENT_HISTORY',execution_authority=False)
        else:
            start=datetime.fromisoformat(sessions[0]['date']).replace(tzinfo=NY).astimezone(timezone.utc)
            data=provider.bars(['SPY'],start,sessions[-1]['close'],'1Day',max_pages=2)
            result=simulate(data.get('SPY',[]),sessions)
    except Exception as exc:
        result=dict(protocol=PROTOCOL,state='DATA_UNAVAILABLE',error_type=type(exc).__name__,execution_authority=False)
    result['checked_at']=now.isoformat(); store.state(KEY,result)
    evidence={k:v for k,v in result.items() if k!='scenarios'}
    evidence['metrics']={bps:{k:v for k,v in data.items() if k not in {'entries','exits'}} for bps,data in result.get('scenarios',{}).items()}
    LOGGER.info('SMA_CROSSOVER_STUDY %s',json.dumps(evidence))
    return result


def status_text(path=None):
    from market_move_research import default_path
    p=Path(path or default_path())
    lines=['📈 اختبار SPY — متوسطا 20/50 يومًا','محاكاة مستقلة؛ لا تنفذ تداولًا ولا تعدّل شروط JALWE.']
    result={}
    if p.exists():
        conn=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
        try:
            row=conn.execute('SELECT value FROM move_state WHERE key=?',(KEY,)).fetchone()
            result=json.loads(row[0]) if row else {}
        except (sqlite3.Error,ValueError): result={'state':'UNAVAILABLE'}
        finally: conn.close()
    lines.append('الحالة: '+str(result.get('state','WAITING_FIRST_READ')))
    if result.get('checked_at'):
        from core.decision_diagnostics import saudi_time
        lines.append('آخر فحص: '+saudi_time(result['checked_at']))
    if result.get('state')=='SIMULATED':
        lines.append(f"الفترة المقاسة: {result['first_test_day']} إلى {result['last_session']} | الرصيد الافتراضي $100")
        for bps,s in result['scenarios'].items():
            lines.append(f"تكلفة مفترضة {bps} نقطة أساس لكل جانب: عائد معلّم {s['return_pct']:+.2f}% | الشراء والاحتفاظ {s['buy_hold_marked_return_pct']:+.2f}% | صفقات مغلقة {s['closed_trades']} | مركز مفتوح: {'نعم' if s['open_position'] else 'لا'}")
        lines.append('الأرباح تشمل تقييم المركز المفتوح إن وجد؛ ليست أرباح وسيط. لا توزيعات نقدية؛ التنفيذ على افتتاح اليوم التالي مفترض.')
        lines.append('النتيجة وصفية لسهم واحد؛ لا تكفي لتفعيل استراتيجية أو إثبات أفضلية.')
    return '\n'.join(lines)[:3800]
