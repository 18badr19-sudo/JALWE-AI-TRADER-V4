"""Readable saved research evidence. No model fitting, policy or order authority."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

LABELS = {'PRIOR_DAY_RANGE':'نطاق حركة اليوم السابق ≥8%',
          'PRIOR_DAY_VOLUME':'حجم اليوم السابق ≥2× اليوم الذي قبله',
          'PRIOR_DAY_MOMENTUM':'صعود اليوم السابق ≥3%'}
SYMBOL = re.compile(r'[A-Z][A-Z0-9.\-]{0,11}\Z')


def findings(packet):
    """Keep selected-winner prevalence, controls, validation and fills separate."""
    from market_move_patterns import MIN_TRAIN_PAIRS, MIN_TRAIN_DAYS, MIN_COVERAGE, VALIDATION_DAYS
    common=packet.get('historical_jump_profiles',{}).get('common_factors',{})
    groups=[common.get('all_jumps',{})]+common.get('large_jump_groups',[])
    selected=next((g for g in groups if g.get('min_rise_pct')==25 and g.get('cases',0)),
                  common.get('all_jumps',{}))
    factors=[]
    for row in selected.get('factors',[]):
        codes=row.get('codes',[])
        if len(codes)!=1 or codes[0] not in LABELS: continue
        known,present=row.get('known'),row.get('present')
        if type(known) is not int or type(present) is not int or not 0<=present<=known: continue
        factors.append(dict(code=codes[0],label=LABELS[codes[0]],present=present,known=known,
            unknown=row.get('unknown'),prevalence_pct=100*present/known if known else None))
    policy=packet.get('patterns',{})
    native=packet.get('priority_native_outcomes',{})
    return dict(protocol='research-findings-v1',scope='DESCRIPTION_NOT_CAUSE_OR_TRADE_ADVICE',
        source_time=packet.get('published_at'),selected_jump_group=dict(
            min_rise_pct=selected.get('min_rise_pct'),cases=selected.get('cases',0),
            unique_symbols=selected.get('unique_symbols'),factors=factors,
            cooccurrences=selected.get('prominent_cooccurrences',[])[:3]),
        coverage=packet.get('diagnostics',{}).get('coverage_totals',{}),
        forward_comparison=packet.get('comparison',{}),
        training=dict(state=policy.get('state','UNAVAILABLE'),candidates=policy.get('candidates',{}),
            required_pairs=MIN_TRAIN_PAIRS,required_days=MIN_TRAIN_DAYS,required_coverage=MIN_COVERAGE,
            validation_sessions_seen=policy.get('validation_sessions_seen',0),
            required_validation_sessions=VALIDATION_DAYS),
        daily_control_comparison=dict(
            retrospective=packet.get('factor_benchmark',{}).get('retrospective',{}).get('all_days',{}).get('rules',{}).get('ALL_THREE',{}),
            forward=packet.get('factor_benchmark',{}).get('forward',{}).get('evidence',{}).get('rules',{}).get('ALL_THREE',{})),
        pipeline=dict(priority=packet.get('priority_link',{}),trace=packet.get('priority_trace',{}),
            native=native,native_pnl_available=native.get('state')=='NATIVE_RECONCILED_EXITS'),
        simulation=packet.get('entry_study',{}),automatic_application=False,execution_authority=False)


def findings_text(packet):
    if not packet: return '🧠 خلاصة الباحث: لا توجد حزمة بحث حديثة متاحة.'
    value=findings(packet); group=value['selected_jump_group']
    lines=['🧠 ماذا عرف الباحث؟']
    lines.append(f"قافزون ≥{group['min_rise_pct']}%: {group['cases']} سهم/جلسة مكتملة؛ اختيار بعد الحركة."
                 if group['cases'] else 'لا توجد عينة قفزات مكتملة في الحزمة المقروءة.')
    for row in sorted(group['factors'],key=lambda r:-(r['prevalence_pct'] or 0)):
        pct='غير معلوم' if row['prevalence_pct'] is None else f"{row['prevalence_pct']:.1f}%"
        lines.append(f"• {row['label']}: {row['present']}/{row['known']} ({pct}) | مجهول {row['unknown']}")
    for row in group['cooccurrences']:
        if all(c in LABELS for c in row.get('codes',[])):
            lines.append('• اجتماع '+ ' + '.join(LABELS[c] for c in row['codes'])+
                         f": {row['present']}/{row['known']} | مجهول {row['unknown']}")
    lines.append('هذه نسبة ظهور الصفة بين قافزين مختارين؛ ليست احتمال قفزة لسهم يحمل الصفة ولا نسبة نجاح شراء.')
    comp=value['forward_comparison']; training=value['training']
    lines.append(f"اختبار المسار المسبق: {comp.get('complete_pairs',0)}/{comp.get('pair_episodes',0)} زوج مكتمل، خلال {comp.get('unique_days',0)} أيام.")
    labels={'COLLECTING_TRAINING':'يجمع بيانات التدريب','WAITING_FRESH_VALIDATION':'ينتظر الاختبار اللاحق',
            'ACTIVE_PRIORITY':'أولوية فحص فقط','FAILED_VALIDATION':'لم يجتز الاختبار'}
    lines.append(f"التعلم: {labels.get(training['state'],training['state'])}. شرط التدريب لكل قاعدة: {training['required_pairs']} زوجًا، {training['required_days']} أيام، وتغطية ≥{training['required_coverage']:.0%}.")
    for code,row in training['candidates'].items():
        lines.append(f"  {code}: أزواج مؤثرة {row.get('matched_pairs',0)} | أيام {row.get('matched_days',0)} | تغطية {row.get('complete_pair_coverage',0):.1%}")
    lines.append(f"بعد اجتياز التدريب: اختبار على {training['required_validation_sessions']} جلسة جديدة؛ المسجلة حتى الآن {training['validation_sessions_seen']}. اكتمالها وحده لا يضمن اجتياز التقييم.")
    for key,label in (('retrospective','المقارنة اليومية التاريخية'),('forward','المقارنة اليومية المستقبلية')):
        pairs=value['daily_control_comparison'][key].get('pairs',{})
        complete=pairs.get('complete',0)
        if complete:
            lines.append(f"{label} للشروط الثلاثة: بلغ أعلى اليوم +8% للمطابقة {pairs['matching_reached']}/{complete}، وللضابطة {pairs['control_reached']}/{complete}. ليست ترتيب وقف/هدف أو ربح صفقة.")
    native=value['pipeline']['native']
    if value['pipeline']['native_pnl_available']:
        lines.append(f"PAPER المرتبط: {native.get('unique_trades',0)} صفقة لها مخارج مسجلة؛ المحقق قبل رسوم الوسيط ${native['realized_pnl_before_broker_fees']:+.2f}.")
    else: lines.append('PAPER المرتبط: لم يتوفر دليل خروج مرتبط؛ الربح غير معلوم وليس صفرًا.')
    lines.extend(['التعلم والمقارنة يستمران دون تغيير بوابات الشراء أو المخاطر تلقائيًا.',
                  'للسهم بالتفصيل: /research_stock SHFS 2026-10-22'])
    return '\n'.join(lines)


def stock_research_text(symbol, day=None, *, path=None):
    """Read the saved full stock/session study, not only the latest examples."""
    from market_move_research import default_path
    symbol=str(symbol).upper().strip()
    if not SYMBOL.fullmatch(symbol): return 'اكتب رمزًا صحيحًا: /research_stock SHFS 2026-10-22'
    if day is not None:
        try:
            if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day: raise ValueError()
        except (TypeError,ValueError): return 'التاريخ بصيغة YYYY-MM-DD.'
    target=Path(path or default_path())
    if not target.exists(): return f'لا توجد دراسة محفوظة متاحة لـ{symbol}.'
    try:
        conn=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        conn.row_factory=sqlite3.Row
        try:
            rows=[dict(r) for r in conn.execute('''SELECT * FROM move_episodes
                WHERE symbol=? AND role='JUMP_CASE' AND prospective=0
                  AND (? IS NULL OR session_day=?) ORDER BY session_day DESC LIMIT 31''',(symbol,day,day))]
        finally: conn.close()
        if not rows: return f'لا توجد دراسة قفزة محفوظة لـ{symbol}'+(f' بتاريخ {day}.' if day else '.')
        day=rows[0]['session_day']; rows=[r for r in rows if r['session_day']==day]
        if len(rows)>30: return f'{symbol} {day}: عدد نسخ الدراسة يتجاوز حد القراءة؛ لم يُحسم دليل موحّد.'
        for row in rows: row['result']=json.loads(row['result_json'] or '{}')
        completed=[r for r in rows if r['status']=='COMPLETE']
        candidates=completed or rows
        paths={json.dumps(r['result'].get('jump_path',{}),sort_keys=True) for r in candidates}
        if len(paths)>1: return f'{symbol} {day}: نسخ دراسة متعارضة؛ لا تُستخلص عوامل موثوقة منها.'
        row=candidates[0]; result=row['result']; path_value=result.get('jump_path',{})
        profile=path_value.get('profile',{})
        lines=[f'🔎 ماذا عرف الباحث عن {symbol} — {day}؟',f"حالة الدراسة: {row['status']} | الشموع الصحيحة {result.get('observed_minutes','غير معلوم')}/{result.get('expected_minutes','غير معلوم')}"]
        if row.get('checked_at'):
            from market_move_research import utc
            lines.append('وقت قراءة الدراسة: '+utc(row['checked_at']).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M')+' السعودية.')
        rise=path_value.get('observed_rise_pct')
        if type(rise) in (int,float) and math.isfinite(rise):
            lines.append(f"المسار المرصود: ${path_value['start_price']:.4f} → ${path_value['peak_price']:.4f} ({rise:+.2f}%).")
            from market_move_research import utc
            lines.append('البداية المرصودة '+utc(path_value['start_at']).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%H:%M')+
                         '، القمة '+utc(path_value['peak_at']).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%H:%M')+' السعودية؛ ترتيب الحركة داخل الدقيقة غير معلوم.')
        prior=profile.get('prior_day_context',{})
        for key,label,suffix in (('range_pct','نطاق حركة اليوم السابق','%'),('volume_ratio_previous_day','حجمه مقارنة باليوم الذي قبله','×'),('daily_return_pct','صعود إغلاق اليوم السابق','%')):
            v=prior.get(key) if prior.get('status')=='AVAILABLE' else None
            lines.append(f"• {label}: "+(f'{v:.2f}{suffix}' if type(v) in (float,int) and math.isfinite(v) else 'غير معلوم'))
        before=profile.get('before_context',{})
        lines.append(f"سياق العشرين دقيقة قبل البداية: {before.get('status','UNAVAILABLE')}.")
        if before.get('status') in {'RAW_20M_COMPLETE','SAVED_DERIVED_CONTEXT_ONLY'}:
            for key,label,suffix in (('return_pct','تغير السعر','%'),('volume_acceleration','تسارع الحجم','×'),('range_pct','نطاق الحركة','%')):
                v=before.get(key)
                if type(v) in (float,int) and math.isfinite(v):lines.append(f'• {label}: {v:.2f}{suffix}')
            above=before.get('close_above_vwap')
            lines.append('• الإغلاق فوق/عند VWAP السياق: '+('نعم' if above is True else 'لا' if above is False else 'غير معلوم'))
        news=result.get('pre_move_news',{})
        lines.append('الأخبار: '+(f"{len(news.get('articles',[]))} خبر ضمن نافذة المصدر المحفوظة." if news.get('status')=='AVAILABLE' else 'غير متاحة.'))
        if news.get('status')=='AVAILABLE':
            keywords=sorted({word for article in news.get('articles',[]) for word in article.get('keywords',[]) if isinstance(word,str)})
            lines.append('الكلمات الوصفية المحفوظة: '+(', '.join(keywords) if keywords else 'لا كلمات مصنفة في نافذة المصدر.'))
        lines.append('البداية اختيرت بعد مشاهدة الحركة؛ هذه صفات مرصودة وليست أسبابًا مثبتة. الدراسة الناقصة لا تدخل إثبات أفضلية.')
        return '\n'.join(lines)
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError):
        return f'{symbol}: تعذرت قراءة أدلة الدراسة المحفوظة؛ النتيجة غير معلومة.'
