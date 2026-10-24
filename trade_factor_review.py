"""Descriptive comparison of saved entry facts for native closed PAPER trades.

No order/provider calls, training, causal labels or retrospective feature refill.
"""
from collections import Counter
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import json,math,sqlite3

PROTOCOL='native-entry-factors-outcome-description-v1'
NY=ZoneInfo('America/New_York')
FEATURES=('rvol','liquidity_score','volume_acceleration','atr','atr_pct','momentum_score',
          'dollar_volume','rsi','distance_from_vwap_pct')
FLAGS={'iex':'بيانات IEX','grade_a_plus':'درجة A+', 'rvol_ge2':'RVOL عند الدخول ≥2',
    'stop_lt_atr':'الوقف أقل من ATR واحد','stop_lt_1pct':'الوقف أقل من 1%',
    'low_liquidity':'السيولة أقل من 55','above_vwap':'فوق VWAP',
    'overnight':'بقيت لليوم التالي','last30':'الدخول في آخر 30 دقيقة'}


def num(v):
    try:return float(v) if type(v) in (int,float) and math.isfinite(v) else None
    except (TypeError,OverflowError,ValueError):return None


def mapping(v):return v if isinstance(v,dict) else {}


def stamp(v,*,database_time=False):
    try:
        at=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if at.tzinfo is None:
            if not database_time:return None
            at=at.replace(tzinfo=timezone.utc) # SQLite's stored UTC convention.
        return at.astimezone(timezone.utc) if 1900<=at.year<=2200 else None
    except (TypeError,ValueError,OverflowError):return None


def extract(trade,entry,decision):
    d=mapping(mapping(decision).get('jalwe'));m=mapping(d.get('metadata'))
    f=mapping(m.get('decision_features'));q=mapping(m.get('entry_quality'))
    at=stamp(mapping(entry).get('filled_at'),database_time=True)
    source='broker_orders.filled_at'
    if at is None:at=stamp(trade.get('created_at'),database_time=True);source='managed_trades.created_at'
    closed=stamp(trade.get('closed_at'),database_time=True)
    price,stop,atr=(num(v) for v in (trade.get('entry_price'),trade.get('initial_stop'),f.get('atr')))
    distance=price-stop if price is not None and stop is not None and price>stop>0 else None
    ratio=num(distance/atr) if distance is not None and atr is not None and atr>0 else None
    pct=distance/price*100 if distance is not None else None
    boolval=lambda v:v if type(v) is bool else None
    savedstr=lambda v:v if isinstance(v,str) and 0<len(v)<=80 else None
    return dict(protocol=PROTOCOL,exact_entry_decision_available=decision is not None,
        entry_at=at.isoformat() if at else None,entry_time_source=source,
        database_naive_timestamps='ASSUMED_UTC_BY_SQLITE_STORAGE_CONVENTION',
        entry_ny=at.astimezone(NY).isoformat() if at else None,
        overnight=(closed.astimezone(NY).date()>at.astimezone(NY).date()) if closed and at and closed>=at else None,
        strategy=savedstr(d.get('strategy')),setup_grade=savedstr(d.get('setup_grade')),
        decision_feed=savedstr(m.get('decision_data_feed')),market_regime=savedstr(d.get('market_regime')),
        feature_timestamp=f.get('timestamp') if stamp(f.get('timestamp')) else None,
        feature_timeframe=savedstr(mapping(m.get('quality_inputs')).get('timeframe')),
        feature_quality_ok=boolval(f.get('data_quality_ok')),feature_is_stale=boolval(f.get('data_is_stale')),
        above_vwap=boolval(f.get('above_vwap')),
        features={k:num(f.get(k)) for k in FEATURES},
        scores={k:num(d.get(k)) for k in ('ai_score','opportunity_score','session_strategy_score','breakout_score','risk_pct')},
        entry_quote={k:num(q.get(k)) for k in ('spread_pct','spread_r','quote_age_seconds')},
        stop_distance_pct=pct,stop_distance_atr=ratio,
        stop_scope='INITIAL_INTERNAL_STOP_NOT_ROUNDED_BROKER_STOP',
        scope='ORIGINAL_LINKED_ENTRY_FACTS_NOT_RECOMPUTED_OR_CAUSAL')


def saved_calendar(path=None):
    from core.storage import data_directory
    target=Path(path or data_directory()/'market_move_research.db')
    if not target.exists():return {}
    try:
        c=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:row=c.execute("SELECT value FROM move_state WHERE key='jump_session_calendar'").fetchone()
        finally:c.close()
        sessions=json.loads(row[0])['sessions'] if row else []
        if not isinstance(sessions,list) or len(sessions)>200:return {}
        out={};bad=set()
        for s in sessions:
            op,cl=stamp(s.get('open')),stamp(s.get('close'));day=s.get('date')
            if (not op or not cl or not op<cl or op.astimezone(NY).date()!=cl.astimezone(NY).date()
                    or str(op.astimezone(NY).date())!=day):continue
            if day in out and out[day]!=s:bad.add(day)
            out[day]=s
        return {k:v for k,v in out.items() if k not in bad}
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,AttributeError):return {}


def summarize(audit,calendar=None):
    calendar=saved_calendar() if calendar is None else calendar
    trades=audit.get('trades') or [];counts=Counter(t.get('trade_id') for t in trades)
    selected=[];excluded=Counter()
    for t in trades:
        pnl=num(t.get('realized_pnl_before_broker_fees'))
        if counts[t.get('trade_id')]!=1:excluded['DUPLICATE_TRADE_ID']+=1;continue
        if t.get('stage')!='CLOSED':excluded['OPEN_NOT_REALIZED']+=1;continue
        if t.get('ledger_complete') is not True or t.get('findings') or pnl is None:
            excluded['INCOMPLETE_OR_MISMATCHED_LEDGER']+=1;continue
        facts=mapping(t.get('entry_factors'));features=mapping(facts.get('features'))
        at=stamp(facts.get('entry_at'));s=calendar.get(str(at.astimezone(NY).date())) if at else None
        op,cl=stamp(mapping(s).get('open')),stamp(mapping(s).get('close'))
        minutes=(cl-at).total_seconds()/60 if at and op and cl and op<=at<cl else None
        rvol=num(features.get('rvol'));liq=num(features.get('liquidity_score'))
        pct,ratio=(num(facts.get(k)) for k in ('stop_distance_pct','stop_distance_atr'))
        feed=facts.get('decision_feed');grade=facts.get('setup_grade')
        flags=dict(iex=feed=='iex' if feed in {'iex','sip'} else None,
            grade_a_plus=grade=='A+' if grade in {'A+','A','B','C'} else None,
            rvol_ge2=rvol>=2 if rvol is not None and rvol>=0 else None,
            low_liquidity=liq<55 if liq is not None and 0<=liq<=100 else None,
            stop_lt_atr=ratio<1-1e-9 if ratio is not None else None,
            stop_lt_1pct=pct<1-1e-9 if pct is not None else None,
            above_vwap=facts.get('above_vwap'),overnight=facts.get('overnight'),
            last30=minutes<=30 if minutes is not None else None)
        selected.append(dict(trade_id=t['trade_id'],symbol=t['symbol'],pnl_before_fees=pnl,
            outcome='WIN' if pnl>0 else 'LOSS' if pnl<0 else 'FLAT',
            minutes_before_close=minutes,entry_factors=facts,flags=flags,
            unknown_execution_evidence=t.get('unknown') or []))
    groups={}
    for name in ('WIN','LOSS','FLAT'):
        rows=[r for r in selected if r['outcome']==name]
        groups[name]=dict(trades=len(rows),pnl_before_fees=sum(r['pnl_before_fees'] for r in rows),
            flags={k:dict(present=sum(r['flags'].get(k) is True for r in rows),
                known=sum(type(r['flags'].get(k)) is bool for r in rows),
                unknown=sum(type(r['flags'].get(k)) is not bool for r in rows)) for k in FLAGS},
            strategies=dict(Counter(r['entry_factors'].get('strategy') or 'UNKNOWN' for r in rows)))
    return dict(protocol=PROTOCOL,checked_at=datetime.now(timezone.utc).isoformat(),
        state='DESCRIPTIVE_ONLY' if selected else 'NO_ELIGIBLE_CLOSED_LEDGER',records=selected,groups=groups,
        source_audit_state=audit.get('state'),source_truncated=audit.get('truncated',False),excluded=dict(excluded),
        calendar_scope='SAVED_CALENDAR_READ_RETROSPECTIVELY_NOT_ORIGINAL_DECISION_INPUT',
        overnight_scope='POST_ENTRY_HOLDING_DESCRIPTION_NOT_PREDICTIVE_ENTRY_FEATURE',
        execution_authority=False,automatic_application=False,causal_loss_explanation='UNPROVEN',
        predictive_edge='NOT_ESTABLISHED',fees='UNAVAILABLE_NOT_ASSUMED_ZERO')


def review(path=None,research_path=None):
    from trade_chain_audit import audit
    return summarize(audit(path=path,limit=20),saved_calendar(research_path))


def status_text(path=None,research_path=None):
    r=review(path,research_path);w,l=r['groups']['WIN'],r['groups']['LOSS']
    if r['state']=='NO_ELIGIBLE_CLOSED_LEDGER':
        return 'عوامل الصفقات: لا توجد أدلة كافية لصفقات مغلقة قابلة للمقارنة؛ النتيجة غير معلومة، وليست ربحًا صفرًا.'
    shown=lambda v:f"{v['present']}/{v['known']} معلوم" if v['known'] else 'غير متاح'
    lines=['🔎 العامل المشترك في صفقات JALWE — وصف من السجل',
        f"رابحة {w['trades']} | خاسرة {l['trades']} | صافي العينة المقروءة قبل الرسوم ${sum(g['pnl_before_fees'] for g in r['groups'].values()):+.2f}"]
    for k,label in FLAGS.items():
        a,b=w['flags'][k],l['flags'][k]
        lines.append(f"• {label}: الخاسرة {shown(b)} | الرابحة {shown(a)} | مجهول {a['unknown']+b['unknown']}")
    if r['source_truncated']:lines.append('العرض لآخر 20 صفقة فقط؛ ليست كل الصفقات.')
    if r['excluded']:lines.append('سجلات مستبعدة من المقارنة: '+str(sum(r['excluded'].values())))
    lines+=['المقام هو الصفقات التي توفرت لها الصفة؛ المجهول لا يُحسب غيابًا.',
        'البقاء لليوم التالي وصف لما حدث بعد الدخول؛ ليس عامل دخول تنبؤيًا.',
        'هذه صفات صفقات منفذة، منفصلة عن الأسهم التي قفزت في البحث؛ العينة الصغيرة لا تثبت سبب الخسارة أو أفضلية شراء.',
        'الأرقام من ledger محفوظ؛ تفاصيل تنفيذ قديمة ورسوم الوسيط قد تكون ناقصة. لا تغيير للشروط أو التداول.']
    return '\n'.join(lines)[:3800]
