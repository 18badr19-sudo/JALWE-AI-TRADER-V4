"""Telegram inline buttons bind confirmation to one trade and one requesting user."""
from core.manual_sells import ManualSellQueue


def menu(database, requested_by):
    queue = ManualSellQueue(database)
    buttons = []
    for trade_id, trade in database.load_active_managed_trades().items():
        try:
            request = queue.prepare(trade_id, requested_by)
        except ValueError:
            continue
        if request['state'] == 'QUEUED':
            continue
        buttons.append([{'text': f"بيع {trade.symbol} — {trade.remaining_quantity} سهم",
                         'callback_data': 'sell-preview:' + request['request_id']}])
    return {'text': 'اختر صفقة PAPER لبيع الكمية المتبقية.' if buttons else
                   'لا توجد صفقة متاحة للبيع الآن؛ قد تكون مغلقة أو لها طلب بيع قائم.',
            'reply_markup': {'inline_keyboard': buttons}}


def callback(database, data, requested_by):
    queue = ManualSellQueue(database)
    operation, token = str(data).split(':', 1)
    request = queue.get(token, requested_by)
    if operation == 'sell-preview':
        if request['state'] != 'DRAFT':
            return {'text': 'هذا الطلب عولج بالفعل. افتح قائمة البيع لمعرفة الحالة.'}
        return {'text': f"تأكيد بيع {request['symbol']} — {request['quantity']} سهم على PAPER؟\n"
                        'إذا كانت جلسات التداول مغلقة، ينتظر الطلب جلسة مؤهلة. سعر التنفيذ غير مضمون.',
                'reply_markup': {'inline_keyboard': [[
                    {'text': 'تأكيد البيع', 'callback_data': 'sell-confirm:' + token},
                    {'text': 'إلغاء', 'callback_data': 'sell-cancel:' + token}]]}}
    if operation == 'sell-confirm':
        request = queue.confirm(token, requested_by)
        if request['state'] == 'EXPIRED':
            return {'text': 'تغيّرت الكمية؛ افتح قائمة البيع وأكد الكمية الجديدة.'}
        if request['state'] == 'QUEUED':
            return {'text': f"تم تسجيل طلب بيع {request['symbol']}. ينفّذه JALWE عند توفر جلسة وبيانات مؤهلة. الضغط المتكرر لا ينشئ بيعًا آخر."}
        return {'text': 'هذا الطلب عولج بالفعل ولن يتكرر.'}
    if operation == 'sell-cancel':
        queue.cancel_draft(token, requested_by)
        return {'text': 'أُغلق طلب التأكيد. الطلب المؤكد سابقًا لا يُلغى بهذا الزر.'}
    raise ValueError('زر البيع غير معروف.')
