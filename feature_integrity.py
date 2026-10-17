"""Offline audits of native indicator calculations; never changes trading gates."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
import numpy as np

PROTOCOL='native-feature-causality-and-warmup-audit-v1'


def audit_frame(bars,engine=None):
    from intelligence.feature_engine import FeatureEngine
    engine=engine or FeatureEngine()
    base=dict(protocol=PROTOCOL,execution_authority=False,
        scope='OBSERVED_NATIVE_INDICATORS_NOT_ALL_STRATEGY_OR_LLM_LEAKAGE')
    issues=engine.input_issues(bars)
    if issues: return dict(base,state='INVALID_INPUT',input_issues=issues)
    if not 52<=len(bars)<=5000: return dict(base,state='INSUFFICIENT_OR_OVERSIZED_INPUT')
    data=engine._ensure_utc_index(bars)[['open','high','low','close','volume']].copy()
    for name in engine.REQUIRED_COLUMNS: data[name]=data[name].astype(float)
    full=engine.calculate_indicators(data)
    columns=[c for c in full if c not in data.columns]
    if not columns: return dict(base,state='UNAVAILABLE_NO_INDICATORS')
    cuts=sorted({51,max(51,len(data)//2),len(data)-1})
    failures=[]; measured=set()
    for size in cuts:
        prefix=engine.calculate_indicators(data.iloc[:size])
        altered=data.copy()
        altered.iloc[size:,altered.columns.get_indexer(['open','high','low','close'])]*=10.
        altered.iloc[size:,altered.columns.get_indexer(['volume'])]*=1000.
        perturbed=engine.calculate_indicators(altered)
        for column in columns:
            a=prefix[column].to_numpy(dtype=float)
            measured.update([column] if np.isfinite(a).any() else [])
            for label,computed in [('EXTENDED',full),('FUTURE_PERTURBED',perturbed)]:
                b=computed[column].iloc[:size].to_numpy(dtype=float)
                if not np.isclose(a,b,rtol=1e-10,atol=1e-10,equal_nan=True).all():
                    failures.append(dict(column=column,cutoff=data.index[size-1].isoformat(),comparison=label))
    warmup=[]
    for size in sorted({51,min(100,len(data)),min(200,len(data)),len(data)}):
        short=engine.calculate_indicators(data.iloc[-size:])
        differences={}; unknown=[]
        for column in columns:
            a,b=float(full[column].iloc[-1]),float(short[column].iloc[-1])
            if not np.isfinite([a,b]).all(): unknown.append(column); continue
            differences[column]=dict(absolute=abs(a-b),relative=abs(a-b)/max(abs(a),1e-12))
        warmup.append(dict(rows=size,differences=differences,unknown=unknown))
    return dict(base,state='CAUSALITY_DIFFERENCE' if failures else 'OBSERVED_CHECKS_PASSED',
        rows=len(data),first_at=data.index[0].isoformat(),last_at=data.index[-1].isoformat(),
        checked_cutoffs=len(cuts),checked_columns=sorted(measured),
        unmeasured_columns=sorted(set(columns)-measured),failures=failures,warmup=warmup,
        warmup_interpretation='SENSITIVITY_ONLY_NOT_AUTOMATIC_BUG_OR_GATE_CHANGE')


def status_text(path=None):
    from intelligence.decision_replay import default_path
    p=Path(path or default_path())
    lines=['🔬 تدقيق المؤشرات الأصلية','فحص تأثير المستقبل وطول التاريخ؛ لا يغيّر شروط التداول.']
    if not p.exists(): return '\n'.join(lines+['ينتظر أول عينة سوق محفوظة؛ الاختبارات المصطنعة لا تحسب دليل سوق.'])
    conn=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
    try:
        row=conn.execute('SELECT symbol,created,result FROM indicator_audits ORDER BY created DESC LIMIT 1').fetchone()
        if not row: return '\n'.join(lines+['ينتظر أول عينة محفوظة قابلة للفحص.'])
        r=json.loads(row[2]); lines.extend([row[0]+' | '+r['state'],
            'وقت الفحص: '+datetime.fromtimestamp(row[1],ZoneInfo('Asia/Riyadh')).isoformat(),
            f"أعمدة مقاسة: {len(r.get('checked_columns',[]))} | فروق سببية: {len(r.get('failures',[]))}"])
        if r.get('warmup'):
            short=r['warmup'][0]
            sensitive=[k for k,v in short['differences'].items() if v['absolute']>1e-10]
            lines.append(f"مقارنة آخر {short['rows']} شمعة بالتاريخ المتاح: فروق في "+(', '.join(sensitive) or 'لا شيء مقاس'))
        lines.append('اختلاف طول التاريخ حساسية تحتاج دراسة، وليس خطأ مثبتًا. نجاح العينة لا يثبت سلامة كل الاستراتيجيات.')
    except (sqlite3.Error,ValueError,KeyError): lines.append('لا يوجد دليل قابل للقراءة بعد.')
    finally: conn.close()
    return '\n'.join(lines)[:3800]
