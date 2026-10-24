"""Prospectively frozen, research-only time/stop comparison. No provider or broker SDK."""
from datetime import datetime,timedelta,timezone
from collections import Counter
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib,json,math,sqlite3,logging
import pandas as pd

NY=ZoneInfo('America/New_York')
PROTOCOL='paired-cutoff30-common-entry-structure-stop-v1'
RULES=('CURRENT','CUTOFF30','STRUCTURE','CUTOFF30_STRUCTURE')
SPEC=dict(protocol=PROTOCOL,cutoff_minutes=30,atr_buffer=.25,prior_bars=5,
    risk_dollars=1.,cash_dollars=100.,target_r=2.,costs_each_side=[.001,.0025],
    horizon='FIRST_FULL_MINUTE_TO_MIN_60M_OR_REGULAR_CLOSE',
    entry='COMMON_BASELINE_CLOSED_TRIGGER_THEN_NEXT_OPEN',
    baseline='NATIVE_ELIGIBLE_SELECTED_CANDIDATE_PROXY_NOT_BROKER_FILLS',
    execution_authority=False,automatic_promotion=False)
SPEC_HASH=hashlib.sha256(json.dumps(SPEC,sort_keys=True).encode()).hexdigest()


def utc(value):
    value=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError('NAIVE_TIME')
    return value.astimezone(timezone.utc)


def numeric(value):
    try:return type(value) in (int,float) and math.isfinite(value)
    except (OverflowError,TypeError,ValueError):return False


def frozen_session(at,path=None):
    """Reuse an immutable pre-open Alpaca calendar manifest; no new API request."""
    from core.storage import data_directory
    target=Path(path or data_directory()/'market_move_research.db')
    if not target.exists():return None
    try:
        conn=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.05)
        try:row=conn.execute('SELECT manifest_json FROM focused_collection_sessions WHERE session_day=?',
                            (at.astimezone(NY).date().isoformat(),)).fetchone()
        finally:conn.close()
        if not row:return None
        p=json.loads(row[0]);s=p['session']
        if (p.get('protocol')!='focused-preopen-sip-collection-v1'
                or not utc(p['frozen_at'])<utc(s['open'])<=at<utc(s['close'])
                or s['date']!=at.astimezone(NY).date().isoformat()):return None
        return dict(session=s,frozen_at=p['frozen_at'],source='FOCUSED_PREOPEN_ALPACA_CALENDAR',
                    manifest_hash=hashlib.sha256(row[0].encode()).hexdigest())
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):return None


def stop_context(bars,atr,timeframe,feed):
    """Record only decision-time evidence. Evaluate independently checks its dates."""
    try:
        rows=[dict(at=t.isoformat(),low=float(low) if math.isfinite(float(low)) else None) for t,low in zip(bars.index[-5:],bars['low'].tail(5))]
    except (AttributeError,TypeError,ValueError,KeyError,OverflowError):rows=[]
    return dict(atr=float(atr) if numeric(atr) else None,
                atr_timeframe=timeframe,source_feed=feed,low_bars=rows)


def validate_plan(plan):
    if plan.get('protocol')!=PROTOCOL or plan.get('spec_hash')!=SPEC_HASH:raise ValueError('UNFROZEN_PROTOCOL')
    at=utc(plan['decided_at']);cal=plan['calendar']
    if not isinstance(cal,dict):raise ValueError('MISSING_PREOPEN_CALENDAR')
    s=cal['session']
    opening,close=utc(s['open']),utc(s['close'])
    if (cal['source']!='FOCUSED_PREOPEN_ALPACA_CALENDAR'
            or not utc(cal['frozen_at'])<opening<=at<close
            or opening.astimezone(NY).date().isoformat()!=s['date']
            or close.astimezone(NY).date()!=opening.astimezone(NY).date()
            or at.astimezone(NY).date()!=opening.astimezone(NY).date()
            or any(t.second or t.microsecond for t in (opening,close))
            or not 0<(close-opening).total_seconds()<=390*60):raise ValueError('INVALID_CALENDAR')
    trigger,stop=plan['trigger'],plan['stop']
    if not all(numeric(v) for v in (trigger,stop)) or not trigger>stop>0:raise ValueError('INVALID_PLAN')
    c=plan['stop_context']
    if not isinstance(c,dict):raise ValueError('MISSING_STOP_CONTEXT')
    atr=c['atr'];rows=c['low_bars']
    minutes={'1m':1,'5m':5,'15m':15}.get(c['atr_timeframe'])
    if (not minutes or not numeric(atr) or atr<=0 or c['source_feed']!=plan['decision_feed']
            or plan['decision_feed'] not in {'iex','sip'} or len(rows)!=5):raise ValueError('MISSING_STOP_CONTEXT')
    times=[utc(r['at']) for r in rows]
    if (len(set(times))!=5 or times!=sorted(times) or times[-1]+timedelta(minutes=minutes)>at
            or times[-1]+timedelta(minutes=minutes+max(3,minutes))<at
            or any(b-a!=timedelta(minutes=minutes) for a,b in zip(times,times[1:]))
            or any(not numeric(r['low']) or r['low']<=0 for r in rows)):
        raise ValueError('INVALID_OR_FUTURE_STOP_CONTEXT')
    structural=min(stop,min(r['low'] for r in rows)-.25*atr)
    if structural<=0:raise ValueError('INVALID_STRUCTURE_STOP')
    start=pd.Timestamp(at).ceil('min').to_pydatetime()
    end=min(start+timedelta(minutes=60),close)
    return trigger,stop,structural,start,end,close


def evaluate(plan,frame,feed,as_of):
    base=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,state='UNKNOWN',reason='MISSING_EVIDENCE',
        execution_authority=False,automatic_promotion=False,observation_feed=feed,
        scope='COMMON_ENTRY_RESEARCH_PROXY_NOT_REALIZED_PNL',rules=None)
    try:trigger,stop,structural,start,end,close=validate_plan(plan)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError) as e:
        return dict(base,reason=str(e) if isinstance(e,ValueError) else 'MISSING_FROZEN_INPUTS')
    if feed not in {'iex','sip'}:return dict(base,reason='INVALID_OBSERVATION_FEED')
    delay=timedelta(minutes=16) if feed=='sip' else timedelta()
    if as_of<end+delay:return dict(base,reason='WAITING_WINDOW_OR_SIP_DELAY')
    expected=int((end-start).total_seconds()/60)
    base.update(window_start=start.isoformat(),window_end=end.isoformat(),expected_minutes=expected,
                regular_close=close.isoformat(),current_stop=stop,structural_stop=structural)
    if (not isinstance(frame,pd.DataFrame) or frame.attrs.get('data_feed')!=feed
            or not isinstance(frame.index,pd.DatetimeIndex) or frame.index.tz is None
            or frame.index.hasnans or not frame.columns.is_unique
            or not {'open','high','low','close','volume'}.issubset(frame.columns)):
        return dict(base,reason='INVALID_OBSERVATION_FRAME')
    data=frame.loc[(frame.index>=start)&(frame.index<end)]
    base.update(observed_minutes=len(data))
    index=pd.date_range(start,end, freq='min',inclusive='left')
    if not data.index.equals(index):return dict(base,reason='MISSING_DUPLICATE_OR_UNORDERED_MINUTES')
    for row in data[['open','high','low','close','volume']].itertuples(index=False,name=None):
        # numpy scalars from numeric frames are ordinary Python values here.
        if any(not numeric(v) for v in row):return dict(base,reason='INVALID_OHLCV')
        o,h,l,c,v=row
        if not 0<l<=min(o,c)<=max(o,c)<=h or v<0:return dict(base,reason='INVALID_OHLCV')
    evidence=[dict(at=t.isoformat(),values=[float(v) for v in row]) for t,row in zip(data.index,data[['open','high','low','close','volume']].itertuples(index=False,name=None))]
    base.update(bar_evidence=evidence,data_hash=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest(),observed_at=as_of.isoformat())
    signal=None
    no_entry='NO_ACTIVATION'
    for i,(_,bar) in enumerate(data.iterrows()):
        if bar.low<=stop:
            if bar.close>=trigger:return dict(base,reason='AMBIGUOUS_BASELINE_ACTIVATION')
            no_entry='BASELINE_INVALIDATED_BEFORE_ENTRY';break
        if bar.close>=trigger:
            if i+1<len(data):signal=i+1
            else:no_entry='NO_NEXT_FULL_MINUTE'
            break
    rules={}
    for cost in SPEC['costs_each_side']:
        key=str(cost);cost_rules={}
        if signal is None:
            for rule in RULES:cost_rules[rule]=dict(traded=False,outcome=no_entry,net_pnl=0.,quantity=0)
        else:
            stamp=data.index[signal];raw=float(data.open.iloc[signal]);fill=raw*(1+cost)
            # The common entry must pass the original current-stop/2R plan for both arms.
            if not stop<fill<trigger+2*(trigger-stop):
                for rule in RULES:cost_rules[rule]=dict(traded=False,outcome='COMMON_ENTRY_OUTSIDE_PLAN',net_pnl=0.,quantity=0)
            else:
                for rule in RULES:
                    if rule.startswith('CUTOFF30') and stamp.to_pydatetime()>=close-timedelta(minutes=30):
                        cost_rules[rule]=dict(traded=False,outcome='BLOCKED_BY_PREDECLARED_CUTOFF',net_pnl=0.,quantity=0);continue
                    floor=structural if 'STRUCTURE' in rule else stop
                    qty=max(0,min(math.floor(1./(fill-floor)),math.floor(100./fill)))
                    if qty==0:
                        cost_rules[rule]=dict(traded=False,outcome='NO_ALLOCATABLE_SHARES',net_pnl=0.,quantity=0);continue
                    # 2R from the common cost-inclusive entry, separate for each frozen stop.
                    target=fill+2*(fill-floor);exit_raw=float(data.close.iloc[-1]);outcome='TIME_EXIT'
                    for _,bar in data.iloc[signal:].iterrows():
                        if bar.low<=floor and bar.high>=target:
                            return dict(base,reason='AMBIGUOUS_STOP_TARGET')
                        if bar.low<=floor:exit_raw=min(floor,float(bar.open));outcome='STOP';break
                        if bar.high>=target:exit_raw=target;outcome='TARGET';break
                    cost_rules[rule]=dict(traded=True,outcome=outcome,quantity=qty,entry_at=stamp.isoformat(),
                        entry_fill=fill,stop=floor,target=target,risk_dollars=qty*(fill-floor),
                        exit_fill=exit_raw*(1-cost),net_pnl=qty*(exit_raw*(1-cost)-fill))
        rules[key]=cost_rules
    return dict(base,state='COMPLETE',reason='COMPLETE_PAIRED_PATH',rules=rules)


class LossReductionStudy:
    def __init__(self,db,calendar_reader=None):
        self.db=db;self.calendar_reader=calendar_reader or frozen_session
        with db.connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS loss_reduction_trials (
                trial_key TEXT PRIMARY KEY,decision_key TEXT NOT NULL,symbol TEXT NOT NULL,
                decided_at TEXT NOT NULL,protocol TEXT NOT NULL,plan_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_loss_study_parent ON loss_reduction_trials(decision_key);
                CREATE TABLE IF NOT EXISTS loss_reduction_results (
                trial_key TEXT NOT NULL,feed TEXT NOT NULL,checked_at TEXT NOT NULL,result_json TEXT NOT NULL,
                PRIMARY KEY(trial_key,feed));''')
        logging.getLogger(__name__).info('LOSS_REDUCTION_STUDY_READY protocol=%s cutoff_minutes=30 risk_dollars=1 cash_dollars=100 execution_authority=false',PROTOCOL)

    def record(self,c,trial_key,decision_key,payload,candidate):
        at=utc(payload['timestamp']);m=payload['jalwe'].get('metadata') or {}
        try:cal=self.calendar_reader(at)
        except Exception:cal=None
        plan=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,spec=SPEC,decided_at=at.isoformat(),calendar=cal,
            decision_feed=m.get('decision_data_feed'),stop_context=m.get('loss_study_stop_context'),
            trigger=candidate.get('trigger_price'),stop=candidate.get('invalidation_price'))
        def safe(value):
            if isinstance(value,dict):return {k:safe(v) for k,v in value.items()}
            if isinstance(value,list):return [safe(v) for v in value]
            return None if isinstance(value,float) and not math.isfinite(value) else value
        plan=safe(plan)
        c.execute('INSERT OR IGNORE INTO loss_reduction_trials VALUES (?,?,?,?,?,?)',
            (trial_key,decision_key,payload['symbol'],at.isoformat(),PROTOCOL,json.dumps(plan,allow_nan=False,default=str)))

    def observe(self,c,decision_key,frame,feed,as_of):
        for row in c.execute('SELECT * FROM loss_reduction_trials WHERE decision_key=? AND protocol=?',(decision_key,PROTOCOL)).fetchall():
            old=c.execute('SELECT result_json FROM loss_reduction_results WHERE trial_key=? AND feed=?',(row['trial_key'],feed)).fetchone()
            if old and json.loads(old[0]).get('state')=='COMPLETE':continue
            result=evaluate(json.loads(row['plan_json']),frame,feed,as_of)
            logging.getLogger(__name__).info('LOSS_REDUCTION_STUDY_OBSERVATION %s',json.dumps(dict(trial_key=row['trial_key'],feed=feed,state=result['state'],reason=result['reason'],execution_authority=False)))
            c.execute('INSERT OR REPLACE INTO loss_reduction_results VALUES (?,?,?,?)',
                (row['trial_key'],feed,as_of.isoformat(),json.dumps(result,allow_nan=False)))

    def text(self):
        with self.db.connection() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM loss_reduction_trials WHERE protocol=? ORDER BY decided_at DESC LIMIT 5001',(PROTOCOL,))]
            results={(r['trial_key'],r['feed']):json.loads(r['result_json']) for r in c.execute(
                'SELECT r.* FROM loss_reduction_results r JOIN loss_reduction_trials t USING(trial_key) WHERE t.protocol=? ORDER BY t.decided_at DESC LIMIT 10002',(PROTOCOL,))}
        lines=['🔬 مقارنة تقليل الخسارة — بحث فقط','دخول موحد؛ منع آخر 30 دقيقة من الإغلاق الفعلي؛ الوقف الحالي مقابل قاع 5 شموع ناقص 0.25 ATR.',
               'مخاطرة افتراضية $1 وسقف كاش $100؛ خروج كامل عند 2R أو نهاية النافذة/الجلسة، وليس خروج البوت المرحلي.']
        if len(rows)>5000:return '\n'.join(lines+['تجاوز حد العرض؛ لا يُعرض متوسط جزئي.'])
        lines.append(f'نوافذ مسجلة {len(rows)} | أيام {len({utc(r["decided_at"]).astimezone(NY).date() for r in rows})} | أسهم مختلفة {len({r["symbol"] for r in rows})}')
        for feed in ('iex','sip'):
            known=[(r,results[(r['trial_key'],feed)]) for r in rows if results.get((r['trial_key'],feed),{}).get('state')=='COMPLETE']
            seen=sum((r['trial_key'],feed) in results for r in rows)
            lines.append(f'{feed}: مكتملة {len(known)} | غير محسومة {seen-len(known)} | تنتظر قراءة {len(rows)-seen}')
            reasons=Counter(results[(r['trial_key'],feed)]['reason'] for r in rows
                if (r['trial_key'],feed) in results and results[(r['trial_key'],feed)].get('state')!='COMPLETE')
            labels={'MISSING_PREOPEN_CALENDAR':'تقويم مجمد غير متاح','MISSING_STOP_CONTEXT':'سياق الوقف الأصلي غير متاح',
                'MISSING_FROZEN_INPUTS':'مدخلات أصلية ناقصة','MISSING_DUPLICATE_OR_UNORDERED_MINUTES':'دقائق ناقصة أو مكررة أو غير مرتبة',
                'INVALID_OR_FUTURE_STOP_CONTEXT':'شموع الوقف قديمة أو غير صالحة أو لم تكتمل وقت القرار',
                'WAITING_WINDOW_OR_SIP_DELAY':'تنتظر اكتمال النافذة أو إتاحة SIP المؤخر',
                'AMBIGUOUS_STOP_TARGET':'ترتيب الهدف والوقف داخل الدقيقة غير محسوم',
                'AMBIGUOUS_BASELINE_ACTIVATION':'ترتيب التفعيل والإلغاء غير محسوم'}
            if reasons:lines.append('أسباب عدم الحسم: '+ ' | '.join(f'{labels.get(k,"دليل غير صالح")}: {v}' for k,v in sorted(reasons.items())))
            for cost in SPEC['costs_each_side']:
                if not known:continue
                values=[v['rules'][str(cost)] for _,v in known]
                baseline=sum(v['CURRENT']['net_pnl'] for v in values)
                changes=[]
                for rule,label in (('CUTOFF30','التوقيت'),('STRUCTURE','الوقف'),('CUTOFF30_STRUCTURE','معًا')):
                    total=sum(v[rule]['net_pnl'] for v in values)
                    avoided=sum(v['CURRENT']['traded'] and not v[rule]['traded'] and v['CURRENT']['net_pnl']<0 for v in values)
                    missed=sum(v['CURRENT']['traded'] and not v[rule]['traded'] and v['CURRENT']['net_pnl']>0 for v in values)
                    changes.append(f'{label}: فرق ${total-baseline:+.2f} | خسائر متجنبة {avoided} | أرباح فائتة {missed}')
                lines.append(f'تكلفة مفترضة {cost*100:.2f}% لكل جانب | الحالي ${baseline:+.2f} | '+'؛ '.join(changes))
        lines+=['المفقود والملتبس يبقيان غير محسومين؛ IEX وSIP منفصلان وقد تتكرر الأسهم.','هذه مقارنة وصفية؛ لا إثبات ربحية أو اعتماد تلقائي أو تغيير شراء/وقف.']
        return '\n'.join(lines)[:3700]


def read_report(path=None):
    """Command reader: no migrations, writes, provider constructors or training."""
    from contextlib import contextmanager
    from core.storage import database_path
    target=Path(path or database_path())
    if not target.exists():return 'مقارنة تقليل الخسارة: لا توجد بيانات محفوظة متاحة.'
    class Reader:
        @contextmanager
        def connection(self):
            c=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
            c.row_factory=sqlite3.Row
            try:yield c
            finally:c.close()
    try:
        obj=LossReductionStudy.__new__(LossReductionStudy);obj.db=Reader()
        return obj.text()
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
        return 'تعذر قراءة المقارنة؛ النتيجة غير متاحة ولم تتغير أي بيانات.'
