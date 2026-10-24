"""Read-only explanations from native saved evidence; no generated causal claims."""
import json
import math
from pathlib import Path
import re
import sqlite3
from core.decision_diagnostics import decision_details,saudi_time
from core.storage import database_path


FACTOR_NAMES = {'liquidity':'السيولة', 'rvol':'الحجم النسبي', 'vwap':'VWAP',
    'trend':'الاتجاه', 'momentum':'الزخم', 'rsi':'RSI', 'breakout':'قرب الاختراق',
    'compression':'ضيق النطاق', 'news':'درجة الأخبار', 'options_flow':'تدفق الخيارات',
    'market_regime':'حالة السوق'}


def ai_score_details(decision):
    """Explain only newly recorded native factor evidence, never backfill it."""
    evidence=(decision.get('metadata') or {}).get('ai_score_evidence')
    if not isinstance(evidence,dict):
        return ['تفصيل نقاط الذكاء: غير محفوظ لهذا القرار؛ لا يُعاد اختراعه.']
    rows=evidence.get('factor_breakdown')
    if not isinstance(rows,list):
        return ['تفصيل نقاط الذكاء: غير متاح في الدليل المحفوظ.']
    lines=[]
    for side,label in (('bullish','نقاط داعمة'),('bearish','نقاط معاكسة')):
        totals={}
        for row in rows:
            if not isinstance(row,dict) or row.get('side')!=side: continue
            factor=row.get('factor'); points=row.get('weighted_points')
            if (factor not in FACTOR_NAMES or type(points) not in (int,float)
                    or not math.isfinite(points) or points<0): continue
            totals[factor]=totals.get(factor,0)+points
        if totals:
            lines.append(label+': '+' | '.join(FACTOR_NAMES[k]+f' {v:.2f}' for k,v in totals.items()))
    if lines:
        lines.append('الدرجة: سقف 100 لكل جانب، ثم طرح 70% من المعاكس؛ الدرجة ليست احتمال ربح.')
    invalid=evidence.get('invalid_inputs')
    if isinstance(invalid,list) and invalid:
        lines.append('مدخلات غير صالحة مستبعدة: '+', '.join(str(x) for x in invalid)[:300])
    threshold=evidence.get('minimum_buy_score')
    if type(threshold) in (int,float) and math.isfinite(threshold):
        lines.append(f'حد BUY المسجل للذكاء: {threshold:.2f}؛ بقية بوابات الشراء مستقلة.')
    return lines or ['لم تُحسب نقاط عوامل موثوقة في دليل الذكاء المحفوظ.']


def explain(symbol,path=None):
    symbol=str(symbol or '').strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',symbol):
        return 'اكتب /explain SNAP لقراءة آخر قرار محفوظ للسهم.'
    path=Path(path or database_path())
    if not path.exists(): return 'لا توجد قاعدة قرارات محفوظة متاحة.'
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
    conn.row_factory=sqlite3.Row
    try:
        row=conn.execute("SELECT metadata_json,created_at FROM system_events WHERE event_type='APEX_RESEARCH_DECISION' AND json_valid(metadata_json) AND json_extract(metadata_json,'$.symbol')=? ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
        if not row: return f'لا يوجد قرار محفوظ لـ{symbol}؛ لا يمكن استنتاج سبب دخوله أو رفضه.'
        p=json.loads(row['metadata_json']); d=p.get('jalwe') or {}; execution=p.get('execution') or {}
        lines=['🧾 شرح آخر قرار محفوظ — '+symbol,saudi_time(row['created_at']),
            'القرار: '+str(d.get('state','غير مسجل')),'السبب المسجل: '+str(d.get('reason','غير مسجل'))]
        lines+=decision_details(d)
        lines+=ai_score_details(d)
        gates=d.get('gates') or {}
        if isinstance(gates,dict):
            failed=[str(k) for k,v in gates.items() if v is False]
            if failed: lines.append('بوابات مسجلة غير مجتازة: '+', '.join(failed)[:500])
        if execution.get('broker_order_submitted'):
            lines.append('التنفيذ: أُرسل أمر للوسيط؛ الإرسال وحده لا يثبت اكتمال التنفيذ.')
        else: lines.append('التنفيذ: لا يوجد تأكيد إرسال أمر في هذا القرار.')
        trade=execution.get('managed_trade_id')
        if trade:
            try:
                rows=conn.execute('SELECT order_id,action,quantity,fill_price,realized_pnl,event_time FROM strategy_pnl_ledger WHERE trade_id=? AND symbol=? ORDER BY event_time LIMIT 101',(str(trade),symbol)).fetchall()
                exits=[r for r in rows if r['order_id'] and type(r['quantity']) is int and r['quantity']>0 and type(r['fill_price']) in (int,float) and math.isfinite(r['fill_price']) and r['fill_price']>0 and type(r['realized_pnl']) in (int,float) and math.isfinite(r['realized_pnl'])]
                if len(rows)>100: lines.append('سجل التنفيذ تجاوز حد العرض؛ لا يعرض مجموعًا جزئيًا.')
                elif exits:
                    pnl=sum(r['realized_pnl'] for r in exits)
                    lines.append(f'المحقق المسجل لهذه الصفقة قبل رسوم الوسيط: ${pnl:+.2f} | أحداث خروج: {len(exits)}')
                    lines.append('إجراء الخروج المسجل: '+str(exits[-1]['action'] or 'غير مسجل')[:120])
                else: lines.append('لا توجد أحداث خروج محققة مرتبطة بهذه الصفقة في السجل المتاح.')
            except sqlite3.Error: lines.append('سجل التنفيذ غير متاح للتحقق من النتيجة.')
        lines.append('سبب الدخول لا يثبت سبب الخسارة؛ لا تُنسب الخسارة لخبر أو خلل دون دليل خروج مستقل.')
        if p.get('decision_replay_id'): lines.append('إعادة مسار القرار متاحة عبر /replay '+symbol)
        return '\n'.join(lines)[:3800]
    except (sqlite3.Error,ValueError,TypeError,AttributeError):
        return 'تعذر قراءة دليل القرار؛ لم يتم تعديل أي بيانات أو إرسال أوامر.'
    finally: conn.close()
