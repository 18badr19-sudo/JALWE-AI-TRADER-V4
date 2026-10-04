"""Read-only historical target evidence; never submits or changes orders."""
from datetime import datetime, timezone
from decimal import Decimal
import time
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('وقت التنفيذ غير موثوق: المنطقة الزمنية مفقودة.')
    return result.astimezone(timezone.utc)


def audit_trades(symbol, start, end, targets, fetch_page, *, max_pages=30, seconds=40):
    """Follow every page; incomplete or malformed coverage cannot prove no touch."""
    target_prices = [Decimal(str(value)) for value in targets]
    if any(not price.is_finite() or price <= 0 for price in target_prices):
        raise ValueError('أهداف الصفقة غير صالحة.')
    result = {'count': 0, 'high': None, 'high_at': None,
              'touches': [None] * len(targets), 'complete': False, 'invalid': 0}
    token = None
    seen = set()
    deadline = time.monotonic() + seconds
    for _ in range(max_pages):
        if time.monotonic() >= deadline:
            break
        payload = fetch_page({'symbols': symbol, 'start': start.isoformat(),
            'end': end.isoformat(), 'feed': 'iex', 'sort': 'asc', 'limit': 10000,
            'page_token': token}, max(1, min(5, deadline - time.monotonic())))
        raw = payload.get('trades')
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError('استجابة سجل التداولات غير صالحة.')
        rows = raw.get(symbol, [])
        if not isinstance(rows, list):
            raise ValueError('صفحات سجل التداولات غير صالحة.')
        for row in rows:
            try:
                price = Decimal(str(row['p']))
                at = timestamp(row['t'])
                if not price.is_finite() or price <= 0 or not start <= at <= end:
                    raise ValueError('Invalid historical trade')
            except (KeyError, TypeError, ValueError, ArithmeticError):
                result['invalid'] += 1
                continue
            result['count'] += 1
            if result['high'] is None or price > Decimal(str(result['high'])):
                result['high'], result['high_at'] = str(price), at.isoformat()
            for index, target in enumerate(target_prices):
                previous = result['touches'][index]
                if price >= target and (previous is None or at < timestamp(previous)):
                    result['touches'][index] = at.isoformat()
        token = payload.get('next_page_token')
        if not token:
            result['complete'] = result['invalid'] == 0
            break
        if token in seen:
            break
        seen.add(token)
    return result


def report(trade, start, end, evidence):
    ny_time = lambda value: timestamp(value).astimezone(NY).strftime('%Y-%m-%d %H:%M:%S NY')
    lines = ['🔎 فحص أهداف ' + trade.symbol + ' — قراءة فقط',
        'المصدر: IEX؛ لا يشمل السوق بالكامل.',
        'من: ' + ny_time(start.isoformat()), 'إلى: ' + ny_time(end.isoformat()),
        f'الدخول الفعلي: ${trade.entry_price:.4f}',
        f'الوقف المسجل الآن: ${trade.current_stop:.4f}',
        f'الكمية المتبقية الآن: {trade.remaining_quantity}',
        f"عدد التداولات المقروءة: {evidence['count']}"]
    if evidence['high'] is not None:
        lines.append(f"أعلى سعر: ${evidence['high']} — {ny_time(evidence['high_at'])}")
    complete = evidence['complete'] and evidence['count'] > 0
    lines.append('التغطية: ' + ('اكتملت صفحات الفترة المطلوبة' if complete else 'غير كافية للجزم بعدم الوصول'))
    for index, target in enumerate((trade.target_1, trade.target_2, trade.target_3)):
        touch = evidence['touches'][index]
        status = ('ظهر وصول في السجل: ' + ny_time(touch)) if touch else (
            'لم يظهر وصول في الفترة المطلوبة' if complete else 'غير محسوم')
        lines.append(f'T{index + 1}: ${target:.4f} — {status}')
    lines.append('هذا سجل تاريخي؛ لا يثبت أن البوت تلقّى السعر في نفس اللحظة أو أن أمر البيع تنفّذ.')
    return '\n'.join(lines)
