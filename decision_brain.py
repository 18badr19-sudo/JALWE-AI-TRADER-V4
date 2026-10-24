"""Evidence memory for the existing language-model reviewer, never an order router."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib,json,math

PROTOCOL='jalwe-frozen-experience-brain-v1'
FIELDS=('memory_closed','memory_wins','memory_losses','memory_same_strategy_closed',
        'memory_same_strategy_wins','memory_same_strategy_losses')
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,allow_nan=False).encode()).hexdigest()


def utc(v):
    v=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    if v.tzinfo is None:raise ValueError('NAIVE_TIME')
    return v.astimezone(timezone.utc)


def refresh(store,now,native_path=None):
    """Run only in the independent research child; audit has no broker calls."""
    old=store.state('decision_brain_memory') or {}
    try:
        if old.get('protocol')==PROTOCOL and utc(old['as_of'])<=now<utc(old['as_of'])+timedelta(hours=1):return old
    except (ValueError,KeyError,TypeError):pass
    from trade_chain_audit import audit
    a=audit(path=native_path,limit=20);rows=[]
    for t in a.get('trades',[]):
        try:
            pnl=t.get('realized_pnl_before_broker_fees')
            if (t.get('stage')!='CLOSED' or t.get('ledger_complete') is not True or t.get('findings')
                    or type(pnl) not in (int,float) or not math.isfinite(pnl)):continue
            from trade_factor_review import stamp
            closed=stamp(t['closed_at'],database_time=True)
            if closed is None or closed>now:continue
            rows.append(dict(trade_id=t['trade_id'],closed_at=closed.isoformat(),pnl_before_fees=pnl,
                strategy=(t.get('entry_factors') or {}).get('strategy'),
                execution_snapshot_complete=not bool(t.get('unknown'))))
        except (KeyError,TypeError,ValueError,OverflowError):continue
    # Duplicate identities cannot count twice as experience.
    counts={r['trade_id']:sum(x['trade_id']==r['trade_id'] for x in rows) for r in rows}
    rows=[r for r in rows if counts[r['trade_id']]==1]
    packet=dict(protocol=PROTOCOL,as_of=now.isoformat(),audit_state=a['state'],
        truncated=a.get('truncated',False),rows=rows,execution_authority=False,
        scope='SAVED_CLOSED_LEDGER_BEFORE_FEES_NOT_CAUSAL_OR_CALIBRATED_PROBABILITIES')
    packet['database_naive_time_convention']='ASSUMED_UTC_FROM_SQLITE_STORAGE'
    packet['memory_hash']=digest(packet)
    store.state('decision_brain_memory',packet)
    return packet


def freeze_context(payload,path=None):
    """Fast cached read after native execution; no audit/provider/model construction."""
    from ai_system import read_state
    from core.storage import data_directory
    base=dict(protocol=PROTOCOL,state='MEMORY_UNAVAILABLE',facts={k:None for k in FIELDS},
        execution_authority=False,scope='AFTER_NATIVE_DECISION_COMMENTARY_ONLY')
    try:
        at=utc(payload['timestamp']);p=read_state(path or data_directory()/'market_move_research.db','decision_brain_memory')
        if not p or p.get('protocol')!=PROTOCOL:return base
        proof={k:v for k,v in p.items() if k!='memory_hash'}
        if p.get('memory_hash')!=digest(proof):return base
        read_at=utc(p['as_of'])
        if not read_at<=at<=read_at+timedelta(hours=24):return dict(base,state='MEMORY_STALE_OR_FUTURE')
        if p.get('audit_state') not in {'AUDITED','NO_MANAGED_TRADES'}:return base
        rows=p['rows']
        if not isinstance(rows,list) or len(rows)>20 or len({r['trade_id'] for r in rows})!=len(rows):return base
        if any(utc(r['closed_at'])>read_at or type(r['pnl_before_fees']) not in (int,float)
                or not math.isfinite(r['pnl_before_fees']) for r in rows):return base
        strategy=(payload.get('jalwe') or {}).get('strategy')
        same=[r for r in rows if strategy and r.get('strategy')==strategy]
        facts=dict(zip(FIELDS,(len(rows),sum(r['pnl_before_fees']>0 for r in rows),
            sum(r['pnl_before_fees']<0 for r in rows),len(same),sum(r['pnl_before_fees']>0 for r in same),sum(r['pnl_before_fees']<0 for r in same))))
        out=dict(base,state='FROZEN_SAVED_EXPERIENCE',facts=facts,decision_at=at.isoformat(),symbol=payload['symbol'],
            strategy=strategy,memory_hash=p['memory_hash'],memory_as_of=p['as_of'],
            truncated=p.get('truncated',False),missing_execution_snapshots=sum(not r['execution_snapshot_complete'] for r in rows))
        out['context_hash']=digest(out)
        return out
    except (ValueError,KeyError,TypeError,OverflowError,AttributeError):return base


def model_facts(payload):
    """Only exact frozen decision identity and bounded numeric memory enter Gemini."""
    p=payload.get('decision_brain') or {}
    if not isinstance(p,dict):return {}
    try:
        if (p.get('protocol')!=PROTOCOL or p.get('state')!='FROZEN_SAVED_EXPERIENCE'
                or p.get('context_hash')!=digest({k:v for k,v in p.items() if k!='context_hash'})
                or p.get('symbol')!=payload.get('symbol') or utc(p['decision_at'])!=utc(payload['timestamp'])
                or p.get('strategy')!=(payload.get('jalwe') or {}).get('strategy')):return {}
        f=p['facts']
        if set(f)!=set(FIELDS) or any(type(v) is not int or not 0<=v<=20 for v in f.values()):return {}
        if (f['memory_wins']+f['memory_losses']>f['memory_closed']
                or f['memory_same_strategy_closed']>f['memory_closed']
                or f['memory_same_strategy_wins']+f['memory_same_strategy_losses']>f['memory_same_strategy_closed']):return {}
        return dict(f)
    except (KeyError,ValueError,TypeError):return {}


def status_text(symbol=None,**kwargs):
    from ai_system import snapshot,read_state
    from core.storage import data_directory
    if not symbol:
        p=read_state(kwargs.get('research_path') or data_directory()/'market_move_research.db','decision_brain_memory') or {}
        memory=(f"ذاكرة محفوظة: {len(p['rows'])} صفقة مغلقة | وقتها {p['as_of']}"
            if p.get('protocol')==PROTOCOL and p.get('memory_hash')==digest({k:v for k,v in p.items() if k!='memory_hash'})
            else 'ذاكرة الصفقات المحفوظة غير متاحة؛ ليست صفر صفقات مؤكدة.')
        return '\n'.join(['🧠 عقل JALWE — ذاكرة ومراجعة لغوية',
            memory,
            'يربط أدلة القرار بذاكرة الصفقات، ثم يفحص الاستمرار والاختراق الكاذب واستنفاد الحركة.',
            'Gemini يراجع ضمن الحد المجاني الحالي؛ القرارات غير المراجعة لا تُنسب للنموذج.',
            'لرأي محفوظ يطابق قرار سهم: /brain SNAP',
            'المراجعة تأتي بعد القرار الأصلي؛ لا اعتماد نموذج أو صلاحية أوامر.'])
    symbol=str(symbol).strip().upper();s=snapshot(symbol=symbol,**kwargs);case=s.get('case') or {}
    from ai_system import latest_decision
    from core.storage import database_path
    payload=latest_decision(kwargs.get('native_path') or database_path(),symbol) if case.get('state')!='INVALID_SYMBOL' else None
    p=(payload or {}).get('decision_brain') or {};r=case.get('review') or {}
    lines=['🧠 مراجعة عقل JALWE — '+symbol,'القرار الأصلي: '+str(case.get('state','غير متاح')),
        'كفاية الذاكرة: '+str(p.get('state','لم تُحفظ لهذا القرار'))]
    facts=model_facts(payload or {})
    if facts:lines.append(f"ذاكرة مغلقة: {facts['memory_closed']} | رابحة {facts['memory_wins']} | خاسرة {facts['memory_losses']}؛ قبل الرسوم، وليست احتمال نجاح.")
    if r.get('state')=='SAME_FROZEN_EVIDENCE':
        lines+=['مراجعة النموذج المطابقة: '+r['agreement'],r['summary_ar']]
        for v in r['scenarios']:lines.append(v['scenario']+' | '+v['assessment']+' | '+v['reason_ar'])
        if r['missing_evidence_ar']:lines.append('الناقص: '+'؛ '.join(r['missing_evidence_ar']))
        lines.append('المتابعة المقترحة من النموذج: '+r['next_check_ar'])
    else:lines.append('لم تتوفر مراجعة لغوية مطابقة محفوظة؛ لا نختلق رأيًا أو درجة ثقة.')
    lines.append('الثقة الاحتمالية غير معايرة؛ هذا تفسير لاحق للقرار، وليس موافقة شراء أو إثبات ربحية.')
    return '\n'.join(lines)[:3800]
