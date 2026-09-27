"""Read-only health observation across the two Railway services.

Heartbeat means the APEX process is alive, not that its analysis is profitable
or that market data/providers are healthy. No broker or process-control access.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import closing

logger = logging.getLogger(__name__)


def _connect(database_url):
    import psycopg2
    return psycopg2.connect(database_url, connect_timeout=5,
                            options='-c statement_timeout=5000')


class ApexHeartbeat:
    def __init__(self, database_url=None):
        self.database_url = database_url or os.getenv('DATABASE_URL', '').strip()
        self.phase = 'STARTING'
        self._stop = threading.Event()

    def start(self):
        if self.database_url:
            threading.Thread(target=self._run, name='apex-heartbeat', daemon=True).start()

    def set_phase(self, phase):
        self.phase = phase

    def stop(self):
        self._stop.set()

    def publish(self):
        with closing(_connect(self.database_url)) as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('''CREATE TABLE IF NOT EXISTS service_heartbeats (
                        service_name TEXT PRIMARY KEY,
                        heartbeat_at TIMESTAMPTZ NOT NULL,
                        phase TEXT NOT NULL,
                        phase_started_at TIMESTAMPTZ NOT NULL
                    )''')
                    cur.execute('''INSERT INTO service_heartbeats
                        (service_name, heartbeat_at, phase, phase_started_at)
                        VALUES ('APEX', CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (service_name) DO UPDATE SET
                            heartbeat_at = CURRENT_TIMESTAMP,
                            phase_started_at = CASE
                                WHEN service_heartbeats.phase <> EXCLUDED.phase
                                THEN CURRENT_TIMESTAMP
                                ELSE service_heartbeats.phase_started_at END,
                            phase = EXCLUDED.phase''', (self.phase,))

    def _run(self):
        while not self._stop.is_set():
            try:
                self.publish()
            except Exception:
                # Do not expose database URLs/credentials in logs or alerts.
                logger.warning('APEX heartbeat could not be saved; retrying later.')
            self._stop.wait(30)


def read_apex_health(database_url=None):
    url = database_url or os.getenv('DATABASE_URL', '').strip()
    if not url:
        return {'state': 'UNKNOWN'}
    try:
        with closing(_connect(url)) as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT phase,
                    EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - heartbeat_at)),
                    EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - phase_started_at))
                    FROM service_heartbeats WHERE service_name = 'APEX' ''')
                row = cur.fetchone()
        if row is None:
            return {'state': 'UNKNOWN'}
        phase, age, phase_age = row
        age = float(age)
        return {'state': 'FRESH' if 0 <= age <= 120 else 'STALE',
                'age_seconds': age, 'phase': phase,
                'phase_age_seconds': float(phase_age)}
    except Exception:
        return {'state': 'UNKNOWN'}


def apex_status_text(health):
    state = health.get('state')
    if state == 'STALE':
        return '⚠️ APEX: النبض متأخر؛ قد تكون الخدمة أو الاتصال متوقفًا'
    if state != 'FRESH':
        return '❔ APEX: تعذّر التحقق من الخدمة المستقلة'
    phase = health.get('phase')
    if phase == 'ERROR':
        return '⚠️ APEX: العملية حية، لكن آخر دورة واجهت خطأ'
    if phase == 'SCANNING' and health.get('phase_age_seconds', 0) > 1800:
        return '⚠️ APEX: العملية حية، ودورة الفحص مستمرة منذ أكثر من 30 دقيقة'
    labels = {'SCANNING': 'يفحص السوق', 'WAITING': 'بانتظار الفحص التالي',
              'STARTING': 'يبدأ التشغيل'}
    return '🟢 APEX: نبض حديث — ' + labels.get(phase, 'خدمة مستقلة')
