"""Durable manual-close requests; only the single watcher submits broker orders."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc)


class ManualSellQueue:
    def __init__(self, database):
        self.database = database
        with database.connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS manual_sell_requests (
                request_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL,
                symbol TEXT NOT NULL, quantity INTEGER NOT NULL,
                requested_by TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(trade_id) REFERENCES managed_trades(trade_id))''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_manual_sell_per_trade
                ON manual_sell_requests(trade_id) WHERE state IN ('DRAFT','QUEUED')''')

    def prepare(self, trade_id, requested_by):
        current = now()
        with self.database.connection() as conn:
            conn.execute("UPDATE manual_sell_requests SET state='EXPIRED',updated_at=? WHERE state='DRAFT' AND expires_at<?", (current.isoformat(), current.isoformat()))
            existing = conn.execute("SELECT * FROM manual_sell_requests WHERE trade_id=? AND state IN ('DRAFT','QUEUED')", (trade_id,)).fetchone()
            if existing:
                if existing['requested_by'] != str(requested_by):
                    raise ValueError('يوجد طلب بيع لهذا السهم من مستخدم آخر.')
                return dict(existing)
            trade = conn.execute('SELECT * FROM managed_trades WHERE trade_id=?', (trade_id,)).fetchone()
            if not trade or trade['remaining_quantity'] <= 0:
                raise ValueError('الصفقة مغلقة بالفعل.')
            metadata = json.loads(trade['metadata_json'] or '{}')
            if trade['pending_action'] or metadata.get('exit_submission'):
                raise ValueError('يوجد بيع قيد التنفيذ؛ انتظر نتيجة الوسيط.')
            row = dict(request_id=uuid4().hex, trade_id=trade_id, symbol=trade['symbol'],
                quantity=trade['remaining_quantity'], requested_by=str(requested_by), state='DRAFT',
                created_at=current.isoformat(), expires_at=(current+timedelta(minutes=5)).isoformat(),
                updated_at=current.isoformat(), reason='')
            conn.execute('INSERT INTO manual_sell_requests VALUES (:request_id,:trade_id,:symbol,:quantity,:requested_by,:state,:created_at,:expires_at,:updated_at,:reason)', row)
            return row

    def get(self, request_id, requested_by=None):
        with self.database.connection() as conn:
            row = conn.execute('SELECT * FROM manual_sell_requests WHERE request_id=?', (request_id,)).fetchone()
        if not row or requested_by is not None and row['requested_by'] != str(requested_by):
            raise ValueError('طلب البيع غير متاح لهذا المستخدم.')
        return dict(row)

    def confirm(self, request_id, requested_by):
        current = now().isoformat()
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM manual_sell_requests WHERE request_id=? AND requested_by=?', (request_id,str(requested_by))).fetchone()
            if not row:
                raise ValueError('طلب البيع غير متاح لهذا المستخدم.')
            if row['state'] != 'DRAFT':
                return dict(row)
            if row['expires_at'] < current:
                raise ValueError('انتهت صلاحية التأكيد؛ افتح قائمة البيع من جديد.')
            trade = conn.execute('SELECT * FROM managed_trades WHERE trade_id=?', (row['trade_id'],)).fetchone()
            metadata = json.loads(trade['metadata_json'] or '{}') if trade else {}
            if not trade or trade['remaining_quantity'] != row['quantity'] or trade['remaining_quantity'] <= 0:
                conn.execute("UPDATE manual_sell_requests SET state='EXPIRED',updated_at=? WHERE request_id=?", (current,request_id))
                # Commit invalidation, then return a refresh-required state.
                return dict(row, state='EXPIRED')
            if trade['pending_action'] or metadata.get('exit_submission'):
                raise ValueError('يوجد بيع قيد التنفيذ؛ انتظر نتيجته ثم أعد الطلب.')
            conn.execute("UPDATE manual_sell_requests SET state='QUEUED',updated_at=? WHERE request_id=? AND state='DRAFT'", (current,request_id))
            return dict(row, state='QUEUED', updated_at=current)

    def pending_for(self, trade_id):
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM manual_sell_requests WHERE trade_id=? AND state='QUEUED'", (trade_id,)).fetchone()
        return dict(row) if row else None

    def cancel_draft(self, request_id, requested_by):
        self.get(request_id, requested_by)
        with self.database.connection() as conn:
            conn.execute("UPDATE manual_sell_requests SET state='CANCELED',updated_at=? WHERE request_id=? AND state='DRAFT'", (now().isoformat(),request_id))

    def finish_closed(self):
        with self.database.connection() as conn:
            conn.execute("""UPDATE manual_sell_requests SET state='DONE',reason='POSITION_CLOSED',updated_at=?
                WHERE state='QUEUED' AND trade_id IN (SELECT trade_id FROM managed_trades WHERE remaining_quantity=0)""", (now().isoformat(),))
