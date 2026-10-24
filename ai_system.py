"""One read-only view of native decisions, saved model reviews and research ML.

No SDK, provider request, training trigger, table initialization or order endpoint.
"""
from datetime import datetime,timezone
import json
import hashlib
from pathlib import Path
import re
import sqlite3

from core.storage import data_directory,database_path

PROTOCOL='jalwe-unified-ai-evidence-v1'


def read_rows(path,sql,args=()):
    target=Path(path)
    if not target.exists():return None
    try:
        conn=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        conn.row_factory=sqlite3.Row
        try:return [dict(r) for r in conn.execute(sql,args)]
        finally:conn.close()
    except (sqlite3.Error,OSError):return None


def read_state(path,key):
    rows=read_rows(path,'SELECT value FROM move_state WHERE key=?',(key,))
    try:
        value=json.loads(rows[0]['value']) if rows else None
        return value if isinstance(value,dict) else None
    except (ValueError,TypeError):return None


def latest_decision(path,symbol):
    # Bounded Python decoding also handles historical JSON NaN in optional facts.
    # Invalid numeric facts are still rejected by frozen_evidence, never imputed.
    rows=read_rows(path,"""SELECT metadata_json,created_at FROM system_events
        WHERE event_type='APEX_RESEARCH_DECISION' ORDER BY id DESC LIMIT 500""")
    for row in rows or []:
        try:
            p=json.loads(row['metadata_json'])
            if isinstance(p,dict) and p.get('symbol')==symbol:return p
        except (ValueError,TypeError):continue
    return None


def matching_review(path,payload):
    if payload is None:return dict(state='NO_NATIVE_DECISION')
    from intelligence.shadow_advisor import frozen_evidence,validate_review,PROMPT_SPEC
    try:
        stamp=datetime.fromisoformat(str(payload['timestamp']).replace('Z','+00:00'))
        if stamp.tzinfo is None:return dict(state='UNVERIFIED_DECISION_TIME')
        expected=frozen_evidence(payload,stamp.timestamp())
    except (KeyError,ValueError,TypeError,AttributeError):expected=None
    if expected is None:return dict(state='NATIVE_EVIDENCE_INELIGIBLE_FOR_SHADOW')
    rows=read_rows(path,"SELECT * FROM shadow_reviews WHERE symbol=? AND state='ANALYZED' ORDER BY created DESC LIMIT 30",
                   (payload['symbol'],))
    for row in rows or []:
        try:
            evidence=json.loads(row['evidence'])
            if row.get('prompt_spec')!=PROMPT_SPEC or evidence!=expected:continue
            result=json.loads(row['result']);validate_review(result,evidence)
            return dict(state='SAME_FROZEN_EVIDENCE',created_at=row['created'],provider=row.get('provider'),
                        model=row.get('model'),roles=result['roles'],agreement=result['agreement'],
                        summary_ar=result['summary_ar'],scenarios=result['scenarios'],
                        missing_evidence_ar=result['missing_evidence_ar'],next_check_ar=result['next_check_ar'],
                        scope='EQUIVALENT_SAVED_FACTS_NOT_UNIQUE_FILL_OR_TRADE_ID',execution_authority=False)
        except (ValueError,TypeError,KeyError):continue
    return dict(state='NO_MATCHING_REVIEW',scope='SAME_SYMBOL_ALONE_IS_NOT_A_MATCH')


def snapshot(*,native_path=None,research_path=None,shadow_path=None,now=None,symbol=None):
    now=now or datetime.now(timezone.utc)
    native_path=native_path or database_path()
    research_path=research_path or data_directory()/'market_move_research.db'
    shadow_path=shadow_path or data_directory()/'shadow_ai_reviews.db'
    out=dict(protocol=PROTOCOL,checked_at=now.isoformat(),execution_authority=False,
             native_ai_mode='DETERMINISTIC_SCORING_WITH_BOUNDED_CLOSED_TRADE_MULTIPLIERS',
             gemini_mode='SAVED_SHADOW_REVIEW_ONLY',new_provider_calls=0)
    learning=read_rows(native_path,'SELECT * FROM learning_runs ORDER BY id DESC LIMIT 1')
    if learning:
        row=learning[0]
        out['factor_learning']=dict(state='ACTIVE_RECORDED' if row['learning_active'] else 'WAITING_RECORDED',
            eligible_trades=row['eligible_trades'],changed_factors=row['changed_factors'],
            as_of=row['created_at'],scope='LAST_SAVED_LEARNING_RUN_NOT_A_FRESH_RECOMPUTATION')
    else:out['factor_learning']=dict(state='UNAVAILABLE')
    out['collection']=read_state(research_path,'focused_collection_status') or {}
    out['pattern_learning']=read_state(research_path,'pattern_policy') or {}
    out['model_lab']=read_state(research_path,'ai_model_lab_status') or {}
    out['entry_quality_study']=read_state(research_path,'entry_quality_status') or {}
    out['brain_memory']=read_state(research_path,'decision_brain_memory') or {}
    artifact=out['model_lab'].get('artifact') or {}
    if not isinstance(artifact,dict):artifact={'invalid_structure':True}
    out['artifact_status']='NO_ARTIFACT'
    if artifact:
        try:
            expires=datetime.fromisoformat(artifact['expires_at'])
            created=datetime.fromisoformat(artifact['created_at'])
            proof={k:v for k,v in artifact.items() if k!='artifact_hash'}
            checksum=hashlib.sha256(json.dumps(proof,sort_keys=True,allow_nan=False).encode()).hexdigest()
            if (artifact.get('artifact_hash')!=checksum or expires.tzinfo is None or created.tzinfo is None
                    or not created<=now or expires<=created):
                out['artifact_status']='UNVERIFIED'
            else:out['artifact_status']='EXPIRED' if now>=expires else 'RESEARCH_ONLY'
        except (ValueError,KeyError,TypeError):out['artifact_status']='UNVERIFIED'
    counts=read_rows(shadow_path,'SELECT state,COUNT(*) n FROM shadow_reviews GROUP BY state')
    out['shadow_counts']={r['state']:r['n'] for r in counts} if counts is not None else None
    out['gaps']=[
        'FULL_QUALITY_FORWARD_DATA_REQUIRED',
        'CLASSIFIER_BACKTEST_DOES_NOT_VALIDATE_NATIVE_TRADE_PNL',
        'FRESH_FORWARD_VALIDATION_AND_CALIBRATION_NOT_ESTABLISHED',
        'SHADOW_NEWS_ROLE_HAS_NO_VERIFIED_ARTICLE_INPUTS',
        'NO_AUTOMATIC_MODEL_PROMOTION']
    if symbol:
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',symbol):
            out['case']=dict(state='INVALID_SYMBOL')
        else:
            payload=latest_decision(native_path,symbol)
            decision=(payload or {}).get('jalwe') or {}
            if not isinstance(decision,dict):decision={}
            metadata=decision.get('metadata') or {}
            if not isinstance(metadata,dict):metadata={}
            out['case']=dict(symbol=symbol,lookup_scope='LATEST_500_NATIVE_DECISION_EVENTS',
                state=decision.get('state','NO_SAVED_DECISION_IN_WINDOW'),
                at=(payload or {}).get('timestamp'),reason=decision.get('reason'),
                ai_score=decision.get('ai_score'),gates=decision.get('gates') or {},
                factor_evidence=metadata.get('ai_score_evidence') or {},
                review=matching_review(shadow_path,payload))
    return out


def status_text(symbol=None,**kwargs):
    symbol=str(symbol or '').strip().upper() or None
    value=snapshot(symbol=symbol,**kwargs)
    labels={'WAITING_CLOSED_PANELS':'ينتظر جلسات العينة المكتملة',
        'BLOCKED_INCOMPLETE_DATA':'متوقف بسبب نقص البيانات',
        'BLOCKED_CLASS_VARIATION':'ينتظر تنوع النتائج',
        'BACKTEST_RECORDED':'مقارنة زمنية بحثية محفوظة',
        'BLOCKED_NONCONVERGENCE':'لم يتقارب التدريب؛ لم يُحفظ نموذج',
        'BLOCKED_INPUT_CONTRACT':'مدخلات غير صالحة؛ لم يُدرّب',
        'BLOCKED_DUPLICATE_EVIDENCE':'بيانات مكررة؛ لم يُدرّب'}
    lines=['🧠 JALWE — البوت والذكاء في عرض واحد',
        'التنفيذ: بوابات JALWE وإدارة المخاطرة المستقلة. Gemini يفسّر فقط.',
        'APEX يرشح؛ الباحث يجمع ويختبر؛ لا تصويت بين الأجزاء يسمح بتجاوز بوابة.']
    learning=value['factor_learning']
    if learning.get('eligible_trades') is not None:
        lines.append(f"تعلم أوزان العوامل: {learning['state']} | مؤهلة في آخر تشغيل {learning['eligible_trades']} | {learning['as_of']}")
    else:lines.append('تعلم أوزان العوامل: الدليل المحفوظ غير متاح.')
    collection=value['collection']
    if collection.get('symbols'):
        pct=collection.get('coverage_pct');pct='غير معلوم' if pct is None else f'{pct:.1f}%'
        lines.append(f"جمع الباحث {collection.get('session_day')}: {collection['symbols']} سهم | تغطية محفوظة {pct}")
    lab=value['model_lab'];state=lab.get('state','UNAVAILABLE')
    lines.append('مختبر النموذج المحلي: '+labels.get(state,state))
    if lab:
        lines.append(f"جلسات مكتملة متاحة {lab.get('available_sessions',0)}/{lab.get('required_sessions',16)} | الحالة بتاريخ {lab.get('checked_at','غير مسجل')}")
        if lab.get('experiment_id'):lines.append('إصدار التجربة: '+lab['experiment_id'][:16])
        metrics=lab.get('metrics') or {}
        if metrics:
            lines.append(f"خطأ Brier: النموذج {metrics['candidate_brier']:.4f} | خط أساس ثابت {metrics['constant_baseline_brier']:.4f}؛ الأقل أفضل لهذا الحدث البحثي.")
            lines.append('هذه ليست مقارنة بأرباح البوت ولا اختبارًا لتنبؤات مسجلة قبل النتيجة.')
    lines.append('حالة النموذج المحفوظ: '+value['artifact_status']+'؛ لا صلاحية تنفيذ.')
    lines.append(f"ذاكرة المراجعة اللغوية: {len(value['brain_memory'].get('rows',[]))} صفقة محفوظة | /brain")
    quality=value['entry_quality_study']
    if quality:
        lines.append(f"تجربة الدرجة والسيولة: {quality.get('state','UNAVAILABLE')} | مرشحون {quality.get('recorded',0)} | التفاصيل /quality_study")
    case=value.get('case')
    if case:
        lines += [f"السهم {case.get('symbol',symbol)}: {case['state']}"]
        lines.append('نطاق البحث: آخر 500 حدث قرار محفوظ؛ الأقدم قد لا يظهر.')
        if case.get('at'):lines.append('وقت القرار المحفوظ: '+str(case['at']))
        if case.get('reason'):lines.append('السبب المسجل: '+str(case['reason'])[:200])
        review=case.get('review') or {}
        if review.get('state')=='SAME_FROZEN_EVIDENCE':
            lines.append('تفسير '+str(review['provider'])+' يطابق الأدلة المجمدة: '+review['agreement'])
        else:lines.append('التفسير المطابق: غير متاح؛ لا نربط تحليلًا آخر بالسهم لمجرد تطابق الرمز.')
        lines.append('تفصيل القرار: /explain '+symbol)
    lines += ['النواقص: اكتمال البيانات، تحقق مستقبلي ومعايرة، وأخبار موثقة لمحلل الأخبار.',
              'حد Gemini المجاني محفوظ؛ هذا العرض لا يطلب تحليلًا جديدًا ولا يغيّر الشراء أو الوقف.']
    return '\n'.join(lines)[:3800]
