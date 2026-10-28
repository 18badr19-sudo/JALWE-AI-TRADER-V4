"""Read-only stock/day native decision evidence, independent of nomination membership."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


def stock_decision_text(symbol, day, *, path=None):
    symbol = str(symbol).strip().upper()
    try:
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol):
            raise ValueError()
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=ZoneInfo('America/New_York'))
        if start.date().isoformat() != day:
            raise ValueError()
    except (TypeError, ValueError):
        return 'رمز السهم أو التاريخ غير صالح.'
    if path is None:
        from core.config import settings
        path = settings.DATABASE_PATH
    target = Path(path)
    if not target.exists():
        return 'قرارات JALWE الأصلية: قاعدة الأدلة غير متاحة.'
    lower = start.astimezone(timezone.utc)
    upper = (start + timedelta(days=1)).astimezone(timezone.utc)
    try:
        conn = sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
        conn.row_factory = sqlite3.Row
        try:
            # Timestamp text may use Z or offsets: compare instants in SQL.
            rows = conn.execute('''SELECT source_state,reason,decided_at,payload_json
                FROM decision_outcome_memory WHERE symbol=?
                AND julianday(decided_at)>=julianday(?) AND julianday(decided_at)<julianday(?)
                ORDER BY julianday(decided_at),decision_key LIMIT 2001''',
                (symbol,lower.isoformat(),upper.isoformat())).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 'قرارات JALWE الأصلية: تعذرت قراءة الأدلة؛ السبب غير محسوم.'
    lines = [f'🧾 قرارات JALWE الأصلية {symbol} — {day} NY']
    if not rows:
        return '\n'.join(lines+['لا يوجد قرار محفوظ في اليوم المحدد؛ لا يثبت ذلك رفض APEX أو عدم التداول.'])
    capped = len(rows)>2000
    rows = rows[:2000]
    def saudi(stamp):
        return datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%H:%M:%S')
    try:
        lines.append(f'تغطية السجل: {len(rows)} قرار | أول {saudi(rows[0]["decided_at"])} | آخر {saudi(rows[-1]["decided_at"])} السعودية.')
        for reason,count in Counter(r['reason'] for r in rows).most_common(5):
            lines.append(f'• {count}: {str(reason)[:250]}')
        latest=rows[-1]
        payload=json.loads(latest['payload_json'])
        jalwe=payload.get('jalwe') or {}
        metadata=jalwe.get('metadata') or {}
        lines.append('آخر حالة محفوظة: '+str(latest['source_state']))
        gates=jalwe.get('gates') or {}
        if gates:
            lines.append('البوابات المسجلة: '+', '.join(f'{k}={v}' for k,v in gates.items())[:700])
        proof=metadata.get('bar_source_evidence') or {}
        if proof:
            fetch=proof.get('fetch') or {}; completion=proof.get('completion') or {}
            lines.append('مصدر الشموع: '+str(proof.get('feed','غير معلوم'))+
                ' | أحدث صف مرجع: '+str(fetch.get('provider_latest_bar_start','غير معلوم'))+
                ' | أحدث شمعة مكتملة: '+str(completion.get('latest_bar_start','غير معلوم')))
            lines.append('صفوف غير مكتملة/توقيت مستبعدة: '+str(completion.get('excluded_rows','غير معلوم'))+
                ' | صفوف رقمية محذوفة: '+str(fetch.get('numeric_rows_removed','غير معلوم')))
        else:
            lines.append('تفاصيل جلب الشموع غير محفوظة لهذا القرار؛ لا تُستنتج من قراءة لاحقة.')
    except (ValueError,TypeError,AttributeError):
        lines.append('تفاصيل آخر قرار غير صالحة للقراءة؛ لم تُستبدل ببيانات لاحقة.')
    if capped:lines.append('وصلت القراءة حد 2000؛ التغطية جزئية والأعداد تخص الجزء المقروء.')
    lines.append('البوابة غير المجتازة قد تكون غير مفحوصة؛ أسباب الرفض مأخوذة من القرار فقط. غياب شمعة لا يثبت غياب التداول، والقرار ليس تنفيذًا أو ربحًا.')
    return '\n'.join(lines)
