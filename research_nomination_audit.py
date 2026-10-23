"""Read saved pre-open eligibility and study evidence; never authorize orders."""
from collections import Counter
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from prior_day_priority_bus import RULE, build, signal_id, utc, valid_signal, validate

GATES={
    'range_pct':('نطاق اليوم السابق',RULE['range_pct'],None,'%'),
    'volume_ratio_previous_day':('حجم اليوم السابق',RULE['volume_ratio_previous_day'],None,'×'),
    'daily_return_pct':('صعود اليوم السابق',RULE['daily_return_pct'],None,'%'),
    'close':('إغلاق اليوم السابق',.5,100.,' دولار'),
    'dollar_volume':('قيمة تداول اليوم السابق',100000.,None,' دولار'),
}
REASONS={
    'RESERVED':'مقعد أولوية مخصص في هذه الدورة',
    'STALE_OR_INELIGIBLE':'بيانات متقادمة أو غير مؤهل للفحص',
    'NOT_ELIGIBLE_SCANNER':'غير موجود ضمن المرشحين المؤهلين في المسح',
    'OUTSIDE_RESERVED_SEATS':'خارج مقاعد الأولوية في هذه الدورة',
    'PRE_CONFIDENCE_BELOW_MIN':'ثقة الفحص الأولي أقل من الحد',
    'SELECTED_DEEP_RESEARCH':'اختير للتحليل العميق',
    'OUTSIDE_DEEP_TOP_N':'خارج عدد مقاعد التحليل العميق',
    'SCORE_BELOW_WATCH_THRESHOLD':'درجة البحث أقل من حد المراقبة',
    'CRITICAL_RESEARCH_RISK':'مخاطر بحثية حرجة',
    'RESEARCH_VERDICT_REJECT':'نتيجة البحث رفض',
    'SHORTLIST_ELIGIBLE':'مؤهل للقائمة المختصرة',
    'PUBLISHED_TO_JALWE':'الإرسال إلى JALWE مؤكد',
    'PUBLISH_NOT_ACKNOWLEDGED':'الإرسال إلى JALWE غير مؤكد',
    'PUBLISH_DISABLED':'إرسال البحث معطل',
    'BRIDGE_UNAVAILABLE':'جسر الإرسال غير متاح',
    'PAPER_SUBMISSION_NOT_FILL_CONFIRMATION':'أمر PAPER مرسل؛ التنفيذ غير مؤكد بهذا الحدث',
}


def reason_text(row):
    reason=str(row.get('reason') or 'غير معلوم')[:300]
    text=REASONS.get(reason,reason)
    if reason in REASONS:text+=' ('+reason+')'
    for key,label in (('confidence','الثقة'),('minimum','الحد الأدنى'),
                      ('score','الدرجة'),('watch_threshold','حد المراقبة'),('rank','الترتيب'),('deep_limit','المقاعد')):
        value=row.get(key)
        if type(value) in (int,float) and math.isfinite(value):text+=f' | {label} {value:g}'
    return text


def saudi(value):
    try:return utc(value).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError,OverflowError):return 'وقت غير معلوم'


def checks(record,session,frozen_at):
    """Describe failures of the existing gates, with unknowns kept separate."""
    if not isinstance(record,dict):return dict(state='MISSING_SNAPSHOT',failed=[],unknown=[])
    f=record.get('features',{}); failed=[]; unknown=[]
    try:
        if not (record['session']==session and f['evidence_day']<session['date']
                and utc(f['available_after'])<=utc(record['prepared_at'])<=utc(frozen_at)<utc(session['open'])):
            return dict(state='SNAPSHOT_NOT_AVAILABLE_AT_FREEZE',failed=[],unknown=[])
    except (KeyError,ValueError,TypeError,OverflowError):
        return dict(state='INVALID_SNAPSHOT_TIMING',failed=[],unknown=[])
    if f.get('source_feed')!='sip':unknown.append('source_feed')
    for code,(_,minimum,maximum,_) in GATES.items():
        v=f.get(code)
        if type(v) not in (int,float) or not math.isfinite(v):unknown.append(code)
        elif v<minimum or (maximum is not None and v>maximum):failed.append(code)
    signal=dict(signal_id=signal_id(f.get('symbol'),session['date']),symbol=f.get('symbol'),
                prepared_at=record.get('prepared_at'),features=f)
    passed=valid_signal(signal,session)
    return dict(state='ELIGIBLE' if passed else 'INELIGIBLE' if failed else 'UNKNOWN',
                failed=failed,unknown=unknown)


def audit_snapshot(records,session,published):
    """Check that retained producer inputs reproduce the actual published list.

    This is an eligibility replay only, not a recomputation of market indicators
    or evidence that APEX consumed a nomination.
    """
    base=dict(state='UNAVAILABLE',session_day=session['date'],execution_authority=False)
    if published.get('state')!='PUBLISHED' or not published.get('frozen_at'):return base
    try:
        replay=build(records,session,utc(published['frozen_at']))
        if not replay or replay['packet_id']!=published.get('packet_id'):
            return dict(base,state='SNAPSHOT_PACKET_MISMATCH')
        results=[checks(r,session,replay['frozen_at']) for r in records]
        return dict(base,state='SAVED_PREOPEN_INPUTS_MATCH_PUBLISHED_PACKET',
            packet_id=replay['packet_id'],frozen_at=replay['frozen_at'],records=len(records),
            nominated=len(replay['signals']),states=dict(Counter(r['state'] for r in results)),
            failed_gates=dict(Counter(c for r in results for c in r['failed'])),
            scope='ELIGIBILITY_REPLAY_NOT_APEX_CONSUMPTION_OR_ENTRY')
    except (KeyError,ValueError,TypeError,OverflowError):return base


def local_evidence(symbol,day,*,path=None):
    from market_move_research import default_path
    target=Path(path or default_path())
    if not target.exists():return dict(state='MISSING_DATABASE',snapshot=None,studies=[])
    try:
        conn=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:
            row=conn.execute('''SELECT payload_json FROM move_pattern_features
                WHERE session_day=? AND symbol=?''',(day,symbol)).fetchone()
            studies=conn.execute('''SELECT frozen_at,status,features_json,session_json,result_json,checked_at
                FROM move_episodes WHERE session_day=? AND symbol=? AND role='ENTRY_STUDY'
                AND prospective=0 LIMIT 2''',(day,symbol)).fetchall()
        finally:conn.close()
        return dict(state='READ',snapshot=json.loads(row[0]) if row else None,
            studies=[dict(frozen_at=r[0],status=r[1],features=json.loads(r[2]),session=json.loads(r[3]),
                result=json.loads(r[4] or '{}'),checked_at=r[5]) for r in studies])
    except (sqlite3.Error,TypeError,ValueError):return dict(state='UNAVAILABLE',snapshot=None,studies=[])


def nomination_text(symbol,day,packet,local):
    if not packet:return ['قائمة الترشيح الأصلية غير متاحة؛ عضوية السهم وسبب عدم ترشيحه غير معلومين.']
    if not validate(packet) or packet['session']['date']!=day:
        return ['قائمة الترشيح غير صالحة أو تخص جلسة أخرى؛ لا يُحسم الترشيح منها.']
    nominated=next((s for s in packet['signals'] if s['symbol']==symbol),None)
    lines=['قائمة الترشيح المجمدة قبل الافتتاح: '+('السهم موجود.' if nominated else 'السهم غير موجود؛ هذا ليس رفضًا من APEX.')]
    lines.append('وقت تجميد القائمة: '+saudi(packet['frozen_at'])+' السعودية.')
    lines.append('مرجع القائمة: '+packet['packet_id'][:16]+'.')
    if nominated:
        lines.append('الشروط الأصلية للترشيح مجتازة؛ وجوده بالقائمة لا يثبت وصوله إلى APEX أو شراءه.')
        return lines
    record=local.get('snapshot')
    if record and record.get('features',{}).get('symbol')!=symbol:record=None
    result=checks(record,packet['session'],packet['frozen_at'])
    if result['state'] in {'MISSING_SNAPSHOT','SNAPSHOT_NOT_AVAILABLE_AT_FREEZE','INVALID_SNAPSHOT_TIMING'}:
        lines.append('مدخلات السهم المتاحة عند تجميد القائمة غير محفوظة أو غير صالحة؛ سبب عدم الترشيح غير محسوم.')
        return lines
    lines.append('إعادة فحص مدخلات ما قبل الافتتاح المحفوظة؛ ليست سبب رفض مسجلًا من APEX:')
    lines.append('وقت حفظ المدخلات: '+saudi(record['prepared_at'])+' السعودية.')
    for code in result['failed']:
        label,minimum,maximum,suffix=GATES[code]; bound=f'{minimum:g}–{maximum:g}' if maximum is not None else f'≥{minimum:g}'
        lines.append(f"• لم يجتز {label}: {record['features'][code]:.4g}{suffix}؛ المطلوب {bound}{suffix}.")
    if result['unknown']:lines.append('مدخلات غير معلومة: '+', '.join(result['unknown'])+'.')
    if result['state']=='ELIGIBLE':lines.append('تعارض: المدخلات المحفوظة تجتاز الشروط والسهم غير موجود بالقائمة؛ يحتاج مراجعة ولا يُنسب سبب استبعاد.')
    elif not result['failed'] and not result['unknown']:lines.append('سبب عدم الترشيح غير محسوم من المدخلات المقروءة.')
    return lines


def study_text(symbol,day,local):
    """Only a genuinely sealed pre-open entry study can report modelled returns."""
    from prior_day_entry_study import PROTOCOL
    studies=local.get('studies',[])
    if len(studies)!=1:return ['اختبار الدخول المسبق للسهم: لا توجد عينة مجمدة موحدة متاحة؛ لم يُحسم أداؤه.']
    row=studies[0]; session=row['session']; features=row['features']
    try:
        if not (session['date']==day and features.get('entry_protocol')==PROTOCOL
                and features.get('symbol')==symbol and features.get('priority_signal_id')==signal_id(symbol,day)
                and utc(features['available_after'])<=utc(features['snapshot_prepared_at'])
                    <=utc(row['frozen_at'])<utc(session['open'])):
            raise ValueError('NOT_PREOPEN')
    except (KeyError,ValueError,TypeError,OverflowError):
        return ['اختبار الدخول المسبق للسهم: توقيت أو هوية العينة غير صالح؛ لا تُعرض نتيجة ربح.']
    lines=['🧪 عينة دخول مستقلة مجمدة: '+saudi(row['frozen_at'])+' السعودية.']
    result=row['result'].get('entry_study',{})
    if row['status']!='COMPLETE':return lines+['مسار الدقائق غير مكتمل؛ الدخول والوقف والربح غير محسومة.']
    state=result.get('state','UNKNOWN_DATA')
    lines.append('نتيجة النموذج: '+state+'؛ محاكاة وليست صفقة منفذة.')
    if result.get('protocol')!=PROTOCOL or state!='SIMULATED_EXIT':return lines
    lines.append('سبب خروج النموذج: '+str(result.get('outcome','غير معلوم'))+'.')
    for key,label in (('entry_at','دخول النموذج'),('exit_at','خروج النموذج')):
        lines.append(label+': '+saudi(result.get(key))+' السعودية.')
    for key,label in (('modelled_entry','الدخول'),('stop','الوقف'),('target','الهدف'),('modelled_exit','الخروج')):
        value=result.get(key)
        if type(value) in (int,float) and math.isfinite(value):lines.append(f'• {label}: ${value:.4f}')
    for bps in (10,25):
        value=result.get('scenarios',{}).get(str(bps),{}).get('net_r')
        if type(value) in (int,float) and math.isfinite(value):
            lines.append(f'تكلفة مفترضة {bps/100:.2f}% لكل جانب: {value:+.2f}R؛ ليست PnL محققة.')
    return lines
