"""Outcome-blind historical sampling and ORB simulations, never native trades."""
from collections import Counter,defaultdict
from datetime import datetime,timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from market_move_research import bucket,daily_features,default_path,utc
from prior_day_priority_bus import RULE

PROTOCOL='archival-prior-day-orb-v1'
SCOPE='RETROSPECTIVE_RECONSTRUCTION_NOT_PREOPEN_RECORD_OR_REALIZED_PNL'
DAYS,PAIRS_PER_DAY=10,2
FOCUS=('SHFS','2026-10-22')


def token(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def prior_inputs(symbol,indexed,prior,older):
    bars=[indexed.get(symbol,{}).get(s['date']) for s in (older,prior)]
    if any(b is None for b in bars):return None
    try:return daily_features(symbol,bars,prior,older)
    except (ValueError,TypeError,KeyError,OverflowError):return None


def matches(f):
    return all(f[k]>=v for k,v in RULE.items())


def selection(daily,sessions,now):
    """Selection uses two previous sessions only, including the control pairing.

    Current active-catalog membership is a survivorship limitation. No target-day
    high, return, volume, minute outcome or saved jump ranking selects pairs.
    """
    from market_factor_benchmark import daily_index
    indexed=daily_index(daily); plans=[]
    calendar=sorted(sessions,key=lambda s:s['date'])
    eligible=[i for i,s in enumerate(calendar) if i>=2
              and utc(s['close'])+timedelta(minutes=16)<=now][-DAYS:]
    for i in eligible:
        session=calendar[i]; prior,older=calendar[i-1],calendar[i-2]
        groups=defaultdict(lambda:([],[])); inputs={}; excluded=0
        for symbol in sorted(indexed):
            f=prior_inputs(symbol,indexed,prior,older)
            if f is None or utc(f['available_after'])>=utc(session['open']):excluded+=1;continue
            if not .5<=f['close']<=100 or f['dollar_volume']<100000:excluded+=1;continue
            inputs[symbol]=f
            groups[bucket(f)][0 if matches(f) else 1].append(symbol)
        pairs=[]
        for yes,no in groups.values():
            order=lambda s:token([PROTOCOL,session['date'],s])
            pairs.extend(zip(sorted(yes,key=order),sorted(no,key=order)))
        pairs=sorted(pairs,key=lambda p:token([PROTOCOL,session['date'],list(p)]))[:PAIRS_PER_DAY]
        plans.append(dict(session=session,pairs=[list(p) for p in pairs],
            inputs={s:inputs[s] for pair in pairs for s in pair},excluded_inputs=excluded,
            universe_symbols=len(indexed),selection='PRIOR_INPUTS_ONLY_REBUILT_AFTER_SESSION'))
    # The explicitly requested winner is isolated from factor/control statistics.
    focus_session=next((s for s in calendar if s['date']==FOCUS[1]),None)
    focus=None
    if focus_session is not None and utc(focus_session['close'])+timedelta(minutes=16)<=now:
        i=calendar.index(focus_session)
        f=prior_inputs(FOCUS[0],indexed,calendar[i-1],calendar[i-2]) if i>=2 else None
        focus=dict(session=focus_session,symbol=FOCUS[0],features=f)
    return plans,focus


def prepare(store,daily,sessions,now):
    plans,focus=selection(daily,sessions,now); queued=0
    for plan in plans+([dict(focus=focus)] if focus else []):
        is_focus='focus' in plan
        session=plan['focus']['session'] if is_focus else plan['session']
        mark='archive_orb:'+PROTOCOL+':'+session['date']+(':focus' if is_focus else ':pairs')
        if store.state(mark) is not None:continue
        values=[]; selections=[]
        if is_focus:
            focus=plan['focus']; f=focus['features']
            selections=[(dict(f or {},symbol=focus['symbol']),'FOCUS',token([PROTOCOL,'FOCUS',session['date'],focus['symbol']]))]
        else:
            for case,control in plan['pairs']:
                pair=token([PROTOCOL,session['date'],case,control])
                selections.extend([(plan['inputs'][case],'MATCH',pair),(plan['inputs'][control],'CONTROL',pair)])
        for f,group,pair in selections:
            f=dict(f,archive_protocol=PROTOCOL,archive_group=group,reconstructed_at=now.isoformat(),
                   archive_scope=SCOPE,archive_rule=RULE,
                   selection='EXPLICIT_POST_MOVE_FOCUS' if is_focus else plan['selection'])
            values.append(store._episode_values(f,session,'ARCHIVE_STUDY',pair,now,prospective=False))
        manifest=dict(protocol=PROTOCOL,created_at=now.isoformat(),session_day=session['date'],
            scope=SCOPE,explicit_focus=is_focus,selected=len(values),
            pairs=plan.get('pairs',[]),excluded_inputs=plan.get('excluded_inputs'),
            universe_symbols=plan.get('universe_symbols'),execution_authority=False)
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM move_state WHERE key=?',(mark,)).fetchone():continue
            before=conn.total_changes
            conn.executemany('''INSERT OR IGNORE INTO move_episodes
                (episode_key,session_day,symbol,role,pair_key,prospective,frozen_at,features_json,session_json)
                VALUES (?,?,?,?,?,?,?,?,?)''',values)
            queued+=conn.total_changes-before
            conn.execute('INSERT INTO move_state VALUES (?,?)',(mark,json.dumps(manifest,allow_nan=False)))
    return dict(protocol=PROTOCOL,queued=queued,historical_sessions=len(plans),
        pairs_per_day=PAIRS_PER_DAY,explicit_focus=list(FOCUS),scope=SCOPE,execution_authority=False)


def run_model(data,session):
    from prior_day_entry_study import simulate
    return dict(simulate(data,session),archive_protocol=PROTOCOL,archive_scope=SCOPE,
                original_bot_decision='UNKNOWN_NOT_RECONSTRUCTED')


def summary(rows):
    selected=[r for r in rows if r['role']=='ARCHIVE_STUDY' and not r['prospective']
              and r['features'].get('archive_protocol')==PROTOCOL]
    def model(r):return r['result'].get('archive_entry',{})
    def label(r):return ('PENDING' if r['status']=='PENDING' else 'UNKNOWN_DATA' if r['status']!='COMPLETE'
        else model(r).get('state','UNKNOWN_DATA'))
    groups={}
    for group in ('MATCH','CONTROL','FOCUS'):
        sample=[r for r in selected if r['features'].get('archive_group')==group]
        costs={}
        for bps in ('10','25'):
            values=[model(r).get('scenarios',{}).get(bps,{}).get('net_r') for r in sample
                if label(r)=='SIMULATED_EXIT' and model(r).get('archive_protocol')==PROTOCOL]
            values=[v for v in values if type(v) in (int,float) and math.isfinite(v)]
            costs[bps]=dict(simulated_exits=len(values),positive=sum(v>0 for v in values),negative=sum(v<0 for v in values),
                mean_net_r=sum(values)/len(values) if values else None)
        groups[group]=dict(total=len(sample),states=dict(Counter(label(r) for r in sample)),costs=costs)
    pairs=defaultdict(dict)
    for row in selected:
        group=row['features'].get('archive_group')
        if group in {'MATCH','CONTROL'}:pairs[(row['session_day'],row['pair_key'])].setdefault(group,[]).append(row)
    complete=0; differences={'10':[],'25':[]}
    for pair in pairs.values():
        if set(pair)!={'MATCH','CONTROL'} or any(len(v)!=1 for v in pair.values()):continue
        a,b=pair['MATCH'][0],pair['CONTROL'][0]
        if a['symbol']==b['symbol'] or any(r['status']!='COMPLETE' for r in (a,b)):continue
        complete+=1
        if any(label(r)!='SIMULATED_EXIT' or model(r).get('archive_protocol')!=PROTOCOL for r in (a,b)):continue
        for bps in differences:
            av,bv=(model(r).get('scenarios',{}).get(bps,{}).get('net_r') for r in (a,b))
            if all(type(v) in (int,float) and math.isfinite(v) for v in (av,bv)):differences[bps].append(av-bv)
    return dict(protocol=PROTOCOL,scope=SCOPE,total=len(selected),groups=groups,
        pairs_selected=len(pairs),pairs_complete_paths=complete,
        paired_both_traded={k:dict(pairs=len(v),mean_difference_r=sum(v)/len(v) if v else None) for k,v in differences.items()},
        limits=['CURRENT_ACTIVE_CATALOG_SURVIVORSHIP','RETROSPECTIVE_NOT_FROZEN_BEFORE_SESSION',
                'ASSUMED_COSTS_NOT_MEASURED_FILLS','PAIRED_RETURN_SUBSET_REQUIRES_BOTH_MODELS_ENTER'],
        execution_authority=False,automatic_application=False)


def summary_text(value):
    lines=['📚 اختبار الأرشيف — إعادة دراسة تاريخية',
        f"العينة: {value.get('total',0)} سهم/جلسة | أزواج مقارنة {value.get('pairs_complete_paths',0)}/{value.get('pairs_selected',0)} بمسارات مكتملة."]
    for group,label in (('MATCH','مطابقة العوامل'),('CONTROL','أسهم المقارنة'),('FOCUS','SHFS المختار بعد القفزة — منفصل')):
        sample=value.get('groups',{}).get(group,{})
        if not sample.get('total'):continue
        states=sample['states']; no_entry=sum(v for k,v in states.items() if k.startswith('NO_') or k=='INVALIDATED_BEFORE_ENTRY')
        unknown=sum(v for k,v in states.items() if k not in {'PENDING','SIMULATED_EXIT','INVALIDATED_BEFORE_ENTRY'} and not k.startswith('NO_'))
        lines.append(f"• {label}: {sample['total']} | مخارج محاكاة {states.get('SIMULATED_EXIT',0)} | بلا دخول {no_entry} | ناقصة/ملتبسة {unknown} | تنتظر {states.get('PENDING',0)}")
        for bps,cost in sample['costs'].items():
            mean=cost['mean_net_r']
            lines.append(f"  تكلفة مفترضة {int(bps)/100:.2f}% لكل جانب: "+(f"متوسط {mean:+.2f}R من {cost['simulated_exits']} مخارج" if mean is not None else 'النتيجة غير معلومة؛ لا مخارج محاكاة قابلة للقياس.'))
    lines.extend(['العوامل من اليومين السابقين فقط؛ اختيار أزواج المقارنة لا يقرأ نتيجة يوم الاختبار.',
        'العينة من كتالوج الأسهم المتاحة حاليًا؛ لا تشمل ضمانًا للأسهم المشطوبة تاريخيًا.',
        'اختبار بعد الحدث بتكاليف مفترضة؛ لا يستعيد قرار البوت القديم ولا يُحسب ربح PAPER أو تدريبًا مستقبليًا.'])
    return '\n'.join(lines)


def stock_text(symbol,day,*,path=None):
    from market_research_findings import SYMBOL
    from research_nomination_audit import GATES,saudi
    symbol=str(symbol).strip().upper()
    try:
        if not SYMBOL.fullmatch(symbol) or datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day:return ''
    except (ValueError,TypeError):return ''
    target=Path(path or default_path())
    if not target.exists():return ''
    try:
        conn=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:raw=conn.execute('''SELECT features_json,session_json,result_json,status,frozen_at FROM move_episodes
            WHERE symbol=? AND session_day=? AND role='ARCHIVE_STUDY' AND prospective=0 LIMIT 4''',(symbol,day)).fetchall()
        finally:conn.close()
        rows=[dict(features=json.loads(r[0]),session=json.loads(r[1]),result=json.loads(r[2] or '{}'),status=r[3],at=r[4]) for r in raw]
        rows=[r for r in rows if r['features'].get('archive_protocol')==PROTOCOL and r['features'].get('symbol')==symbol]
        rows.sort(key=lambda r:r['features'].get('archive_group')!='FOCUS')
        if not rows:return ''
        row=rows[0]; f=row['features']; lines=[f'📚 إعادة اختبار الأرشيف {symbol} — {day}',
            'أُعدت بعد الجلسة: '+saudi(row['at'])+' السعودية؛ ليست مدخلات الترشيح الأصلية.']
        for code,(label,minimum,maximum,suffix) in GATES.items():
            v=f.get(code)
            if type(v) in (int,float) and math.isfinite(v):
                passed=v>=minimum and (maximum is None or v<=maximum)
                lines.append(f"• {label}: {v:.4g}{suffix} | "+('يجتاز الشرط الحالي' if passed else 'لا يجتاز الشرط الحالي'))
            else:lines.append(f'• {label}: غير معلوم من الأرشيف المقروء.')
        if f.get('selection')=='EXPLICIT_POST_MOVE_FOCUS':lines.append('عينة اختيرت بعد مشاهدة القفزة؛ مستبعدة من مقارنة العوامل والضوابط.')
        result=row['result'].get('archive_entry',{})
        if row['status']!='COMPLETE':return '\n'.join(lines+['المحاكاة تنتظر مسار دقائق كامل؛ لا نتيجة ربح محسومة.'])
        lines.append('نتيجة النموذج: '+str(result.get('state','UNKNOWN_DATA'))+' | '+str(result.get('outcome','')))
        for key,label in (('entry_at','دخول النموذج'),('exit_at','خروج النموذج')):
            if result.get(key):lines.append(label+': '+saudi(result[key])+' السعودية.')
        for key,label in (('modelled_entry','دخول'),('stop','وقف'),('target','هدف'),('modelled_exit','خروج')):
            v=result.get(key)
            if type(v) in (int,float) and math.isfinite(v):lines.append(f'• {label}: ${v:.4f}')
        if result.get('archive_protocol')==PROTOCOL and result.get('state')=='SIMULATED_EXIT':
            for bps in ('10','25'):
                v=result.get('scenarios',{}).get(bps,{}).get('net_r')
                if type(v) in (int,float) and math.isfinite(v):lines.append(f'تكلفة مفترضة {int(bps)/100:.2f}% لكل جانب: {v:+.2f}R.')
        lines.append('محاكاة مستقلة؛ لا تثبت تنفيذ شراء أو سبب استبعاد قديم أو ربح PAPER.')
        return '\n'.join(lines)
    except (sqlite3.Error,KeyError,TypeError,ValueError):return 'إعادة اختبار الأرشيف: تعذرت قراءة الأدلة؛ النتيجة غير معلومة.'
