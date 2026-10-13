"""Independent stock-move studies. Packets are advisory; no orders or gate changes."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

PROTOCOL = 'independent-market-moves-v1'
NY = ZoneInfo('America/New_York')
UP_PCT, DOWN_PCT = 8.0, 3.0


def utc(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return value.astimezone(timezone.utc)


def positive(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def valid_bar(row):
    if not isinstance(row, dict):
        return False
    values = [positive(row.get(k)) for k in ('o', 'h', 'l', 'c', 'v')]
    return (all(v is not None for v in values) and values[1] >= max(values[0], values[2], values[3])
            and values[2] <= min(values[0], values[3]))


def daily_features(symbol, bars, session, previous_session):
    """Two completed daily bars; never silently substitute a missing prior session."""
    by_day = {}
    for bar in bars:
        if valid_bar(bar):
            day = utc(bar['t']).astimezone(NY).date().isoformat()
            if day in by_day:
                raise ValueError('Duplicate daily evidence')
            by_day[day] = bar
    current = by_day.get(session['date'])
    prior = by_day.get(previous_session['date'])
    if current is None or prior is None:
        return None
    features = dict(symbol=symbol, evidence_day=session['date'],
        available_after=(utc(session['close']) + timedelta(minutes=16)).isoformat(),
        close=float(current['c']), dollar_volume=float(current['c']) * float(current['v']),
        volume_ratio_previous_day=float(current['v']) / float(prior['v']),
        daily_return_pct=100 * (float(current['c']) / float(prior['c']) - 1),
        range_pct=100 * (float(current['h']) - float(current['l'])) / float(current['c']),
        source_feed='sip', adjustment='split', volume_scope='completed_daily_bar',
        news_evidence_available=False)
    if not all(math.isfinite(features[k]) for k in
               ('close', 'dollar_volume', 'volume_ratio_previous_day', 'daily_return_pct', 'range_pct')):
        return None
    return features


def bucket(features):
    # Match controls using evidence available before either future outcome.
    return (int(math.log2(features['close'])), int(math.log10(features['dollar_volume'])))


def activity(features):
    return (0.5 <= features['close'] <= 100 and features['dollar_volume'] >= 100000
            and features['volume_ratio_previous_day'] >= 1.5 and features['range_pct'] >= 3)


def stable_key(symbol, day):
    return hashlib.sha256(f'{PROTOCOL}|{day}|{symbol}'.encode()).hexdigest()


def choose_pairs(features, target_day, limit=8):
    if limit <= 0:
        return []
    eligible = [f for f in features if 0.5 <= f['close'] <= 100 and f['dollar_volume'] >= 100000]
    cases = sorted((f for f in eligible if activity(f)), key=lambda f: stable_key(f['symbol'], target_day))
    controls = sorted((f for f in eligible if not activity(f)), key=lambda f: stable_key(f['symbol'], target_day))
    used, pairs = set(), []
    for case in cases:
        matches = [c for c in controls if c['symbol'] not in used and bucket(c) == bucket(case)]
        if matches:
            control = matches[0]
            used.add(control['symbol'])
            pairs.append((case, control))
        if len(pairs) >= limit:
            break
    return pairs


def _before_move(data, up_at):
    before = data.loc[data.index < up_at].tail(20) if up_at is not None else data.iloc[:0]
    result = {'status':'UNAVAILABLE_LESS_THAN_20M', 'bars':len(before)}
    if len(before) == 20:
        recent, earlier = float(before['volume'].tail(5).mean()), float(before['volume'].head(15).mean())
        typical = (before['high'] + before['low'] + before['close']) / 3
        vwap = float((typical * before['volume']).sum() / before['volume'].sum())
        result = dict(status='COMPLETE',bars=20,cutoff=up_at.isoformat(),source_feed='sip',
            scope='RETROSPECTIVE_CONTEXT_NOT_LIVE_FEATURES',
            return_pct=100*(float(before['close'].iloc[-1])/float(before['open'].iloc[0])-1),
            volume_acceleration=recent/earlier,vwap=vwap,
            close_above_vwap=bool(float(before['close'].iloc[-1]) >= vwap))
    return result


def _opening_prefix(data, expected, end, duplicates):
    """Describe only consecutive valid bars from the actual session open to first gap."""
    gaps = expected.difference(data.index)
    cutoff = gaps[0] if len(gaps) else end
    prefix = data.loc[data.index < cutoff]
    result = dict(status='UNAVAILABLE',scope='CONTIGUOUS_PREFIX_ONLY_NOT_FULL_SESSION_OR_PNL',
                  cutoff=cutoff.isoformat(),bars=len(prefix),observed_barrier_order=None)
    if duplicates or prefix.empty:
        return result
    baseline = float(prefix['open'].iloc[0])
    up = prefix.loc[prefix['high'] >= baseline*(1+UP_PCT/100)]
    down = prefix.loc[prefix['low'] <= baseline*(1-DOWN_PCT/100)]
    up_at, down_at = (None if x.empty else x.index[0] for x in (up, down))
    order = ('AMBIGUOUS' if up_at is not None and up_at == down_at else
             'UP8_FIRST' if up_at is not None and (down_at is None or up_at < down_at) else
             'DOWN3_FIRST' if down_at is not None else 'NO_BARRIER_OBSERVED_IN_PREFIX')
    result.update(status='OBSERVED_PREFIX',observed_barrier_order=order,
        up_at=up_at.isoformat() if up_at is not None else None,
        down_at=down_at.isoformat() if down_at is not None else None,
        observed_prefix_mfe_pct=100*(float(prefix['high'].max())/baseline-1),
        observed_prefix_mae_pct=100*(float(prefix['low'].min())/baseline-1),
        pre_move_context=_before_move(prefix,up_at))
    return result


def observe_session(frame, session):
    """Conservative whole-session path, including early-close calendars; never fill gaps."""
    start, end = utc(session['open']), utc(session['close'])
    expected = pd.date_range(start, end, freq='min', inclusive='left')
    data = frame.copy()
    from market_move_diagnostics import VERSION
    from market.research_diagnostics import coverage_diagnostics
    required = ['open', 'high', 'low', 'close', 'volume']
    if not set(required).issubset(data.columns) or not isinstance(data.index, pd.DatetimeIndex):
        return {'status': 'PARTIAL_DATA', 'missing_minutes': len(expected), 'outcome': None,
                'diagnostic_version': VERSION, 'data_diagnostics': {'reason':'MISSING_OHLCV_OR_TIMESTAMP_SCHEMA'}}
    if data.index.tz is None:
        return {'status': 'PARTIAL_DATA', 'missing_minutes': len(expected), 'outcome': None,
                'diagnostic_version': VERSION, 'data_diagnostics': {'reason':'NAIVE_TIMESTAMP'}}
    data.index = pd.to_datetime(data.index, utc=True, errors='coerce')
    for col in required:
        data[col] = pd.to_numeric(data[col], errors='coerce')
    # Provider end bounds can be inclusive; outside-session rows do not describe this path.
    in_window = data.index[(data.index >= start) & (data.index < end)]
    duplicates = int(in_window.duplicated().sum())
    finite = data[required].map(lambda x: pd.notna(x) and math.isfinite(x)).all(axis=1)
    valid = (finite & (data[required] > 0).all(axis=1)
        & (data['high'] >= data[['open', 'low', 'close']].max(axis=1))
        & (data['low'] <= data[['open', 'close']].min(axis=1)))
    data = data.loc[valid & data.index.isin(expected)].sort_index()
    data = data.loc[~data.index.duplicated(keep='first')]
    missing = len(expected.difference(data.index))
    result = dict(status='PARTIAL_DATA', source_feed='sip', expected_minutes=len(expected),
                  observed_minutes=len(data), missing_minutes=missing, duplicates=duplicates,
                  outcome=None, scope='OBSERVED_MOVEMENT_NOT_TRADE_PNL')
    diagnostics = coverage_diagnostics(frame, data, start, end)
    stamps = pd.to_datetime(frame.index, utc=True, errors='coerce').dropna()
    absent = expected.difference(stamps.floor('min').unique())
    diagnostics['first_absent_minutes'] = [stamp.isoformat() for stamp in absent[:2]]
    if duplicates:
        diagnostics['reason'] = 'INVALID_OR_DUPLICATE_BARS'
    result.update(diagnostic_version=VERSION, data_diagnostics=diagnostics)
    if missing or duplicates or len(data) != len(expected):
        result['prefix_context'] = _opening_prefix(data,expected,end,duplicates)
        return result
    baseline = float(data['open'].iloc[0])
    up = data.loc[data['high'] >= baseline * (1 + UP_PCT/100)]
    down = data.loc[data['low'] <= baseline * (1 - DOWN_PCT/100)]
    up_at, down_at = (None if x.empty else x.index[0] for x in (up, down))
    outcome = ('AMBIGUOUS' if up_at is not None and up_at == down_at else
               'UP8_FIRST' if up_at is not None and (down_at is None or up_at < down_at) else
               'DOWN3_FIRST' if down_at is not None else 'NO_BARRIER')
    result.update(status='COMPLETE', outcome=outcome, reference_open=baseline,
        up_threshold_pct=UP_PCT, down_threshold_pct=DOWN_PCT,
        up_at=up_at.isoformat() if up_at is not None else None,
        down_at=down_at.isoformat() if down_at is not None else None,
        observed_mfe_pct=100 * (float(data['high'].max()) / baseline - 1),
        observed_mae_pct=100 * (float(data['low'].min()) / baseline - 1))
    result['pre_move_context'] = _before_move(data,up_at)
    return result


def comparison(rows):
    """Paired forward evidence only; chronological holdout never selects a new rule."""
    prospective = [r for r in rows if r['prospective'] and r['role'] in {'ACTIVITY', 'CONTROL'}]
    groups = {}
    for row in prospective:
        groups.setdefault(row['pair_key'], {})[row['role']] = row
    pairs = []
    for g in groups.values():
        if set(g) != {'ACTIVITY', 'CONTROL'}:
            continue
        if (g['ACTIVITY']['session_day'] != g['CONTROL']['session_day'] or
            any(r['status'] != 'COMPLETE' or r['result'].get('outcome') not in
                {'UP8_FIRST', 'DOWN3_FIRST', 'NO_BARRIER'} for r in g.values())):
            continue
        pairs.append((g['ACTIVITY']['session_day'],
                      int(g['ACTIVITY']['result'].get('outcome') == 'UP8_FIRST') -
                      int(g['CONTROL']['result'].get('outcome') == 'UP8_FIRST')))
    days = sorted({d for d, _ in pairs})
    result = dict(state='WAITING_FORWARD_DATA', pair_episodes=len(groups), complete_pairs=len(pairs),
                  unique_days=len(days), automatic_application=False,
                  metric='UP8_FIRST_FREQUENCY_DIFFERENCE_NOT_PNL',
                  complete_pair_coverage=len(pairs)/len(groups) if groups else 0)
    if len(days) < 10 or len(pairs) < 40 or result['complete_pair_coverage'] < .9:
        return result
    split = len(days) * 2 // 3
    train_days, validation_days = days[:split], days[split:]
    # At least five whole days in each split.
    if len(train_days) < 5 or len(validation_days) < 5 or any(
            sum(d in selected for d, _ in pairs) < 20 for selected in (train_days, validation_days)):
        return result
    def daily_edges(selected):
        return [sum(v for d, v in pairs if d == day)/sum(d == day for d, _ in pairs) for day in selected]
    train, validation = daily_edges(train_days), daily_edges(validation_days)
    avg = sum(validation)/len(validation)
    # Bounded day-level Hoeffding bound (99%, independent days assumption).
    # A constant tiny validation sample must not imply zero uncertainty.
    bound = avg - math.sqrt(2*math.log(100)/len(validation))
    result.update(train_days=len(train_days), validation_days=len(validation_days),
                  train_edge=sum(train)/len(train), validation_edge=avg,
                  validation_lower_bound=bound, uncertainty_method='DAY_LEVEL_HOEFFDING_99',
                  uncertainty_assumption='INDEPENDENT_DAYS_NOT_PROVEN')
    result['state'] = 'VALIDATED_RESEARCH_ASSOCIATION' if result['train_edge'] > 0 and bound > 0 else 'NO_VALIDATED_EDGE'
    return result


class MoveStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS move_episodes (
                episode_key TEXT PRIMARY KEY, session_day TEXT NOT NULL, symbol TEXT NOT NULL,
                role TEXT NOT NULL, pair_key TEXT NOT NULL, prospective INTEGER NOT NULL,
                frozen_at TEXT NOT NULL, features_json TEXT NOT NULL, session_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', result_json TEXT, checked_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS move_packets (
                    packet_id TEXT PRIMARY KEY, published_at TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS move_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_move_due ON move_episodes(status, session_day);''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _episode_values(features, session, role, pair_key, now, *, prospective):
        if prospective and not (utc(features['available_after']) <= now < utc(session['open'])):
            raise ValueError('Prospective evidence was unavailable or cohort froze after open')
        if role not in {'ACTIVITY', 'CONTROL', 'HOT_CASE'} or (role == 'HOT_CASE' and prospective):
            raise ValueError('Invalid study role')
        key = stable_key(features['symbol']+'|'+role+'|'+pair_key, session['date'])
        return (key, session['date'], features['symbol'], role, pair_key, int(prospective), now.isoformat(),
                json.dumps(features, allow_nan=False), json.dumps(session))

    def add(self, features, session, role, pair_key, now, *, prospective):
        values = self._episode_values(features, session, role, pair_key, now, prospective=prospective)
        with self.connect() as conn:
            inserted = conn.execute('''INSERT OR IGNORE INTO move_episodes
                (episode_key, session_day, symbol, role, pair_key, prospective, frozen_at,
                 features_json, session_json) VALUES (?,?,?,?,?,?,?,?,?)''',
                values).rowcount
        return bool(inserted)

    def freeze_cohort(self, pairs, session, now, *, clock=None):
        """Commit a complete cohort and its restart marker in one transaction."""
        def cohort_values(at):
            values = []
            for case, control in pairs:
                if case['symbol'] == control['symbol']:
                    raise ValueError('A prospective pair needs distinct symbols')
                pair = stable_key(case['symbol']+'|prospective', session['date'])
                for features, role in ((case,'ACTIVITY'), (control,'CONTROL')):
                    values.append(self._episode_values(features,session,role,pair,at,prospective=True))
            return values
        values = cohort_values(now)
        if not values:
            return False
        mark = 'frozen:'+session['date']
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM move_state WHERE key=?', (mark,)).fetchone():
                return False
            # Do not rewrite legacy evidence if an older version crashed mid-freeze.
            if conn.execute('SELECT 1 FROM move_episodes WHERE session_day=? AND prospective=1',
                            (session['date'],)).fetchone():
                raise ValueError('Unmarked prospective evidence already exists')
            # SQLite can wait for another writer; recheck after obtaining the lock.
            if clock is not None:
                now = clock()
                if now >= utc(session['open']):
                    return False
                values = cohort_values(now)
            conn.executemany('''INSERT INTO move_episodes
                (episode_key, session_day, symbol, role, pair_key, prospective, frozen_at,
                 features_json, session_json) VALUES (?,?,?,?,?,?,?,?,?)''', values)
            conn.execute('INSERT INTO move_state VALUES (?,?)', (mark,json.dumps(dict(
                created_at=now.isoformat(),prior_day=pairs[0][0]['evidence_day']))))
            if clock is not None and clock() >= utc(session['open']):
                conn.rollback()
                return False
        return True

    def episodes(self):
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM move_episodes ORDER BY session_day, episode_key')]
        for row in rows:
            row['features'] = json.loads(row['features_json'])
            row['session'] = json.loads(row['session_json'])
            row['result'] = json.loads(row['result_json'] or '{}')
        return rows

    def settle(self, key, result, now, *, retry=False):
        if result['status'] not in {'COMPLETE', 'PARTIAL_DATA', 'DATA_UNAVAILABLE'}:
            raise ValueError('Invalid observation status')
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM move_episodes WHERE episode_key=?', (key,)).fetchone()
            if row is None:
                return False
            if row['status'] != 'PENDING':
                from market_move_diagnostics import observation_due
                evidence = dict(row)
                evidence['session'] = json.loads(row['session_json'])
                evidence['result'] = json.loads(row['result_json'] or '{}')
                if not retry or not observation_due(evidence, now):
                    return False
            changed = conn.execute('''UPDATE move_episodes SET status=?, result_json=?, checked_at=?, attempts=attempts+1
                WHERE episode_key=? AND status=? AND attempts=?''',
                (result['status'], json.dumps(result, allow_nan=False), now.isoformat(), key,
                 row['status'], row['attempts'])).rowcount
        return bool(changed)

    def state(self, key, value=None):
        with self.connect() as conn:
            if value is not None:
                conn.execute('INSERT OR REPLACE INTO move_state VALUES (?,?)', (key, json.dumps(value)))
                return value
            row = conn.execute('SELECT value FROM move_state WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def publish(self, now, health):
        cutoff = (now.astimezone(NY).date()-timedelta(days=90)).isoformat()
        with self.connect() as conn:
            conn.execute('DELETE FROM move_episodes WHERE session_day < ?', (cutoff,))
        rows = self.episodes()
        from market_move_diagnostics import aggregate
        counts = Counter(r['status'] for r in rows)
        day = health.get('completed_session')
        examples = [r for r in rows if r['session_day'] <= day] if day else [r for r in rows if r['status'] != 'PENDING']
        cases = sorted(examples, key=lambda r: (r['session_day'], r['role'] == 'HOT_CASE',
                                               r['status'] == 'COMPLETE', r['episode_key']), reverse=True)[:12]
        packet = dict(protocol=PROTOCOL, scope='RESEARCH_ONLY_NOT_TRADE_SIGNAL',
            published_at=now.isoformat(), source_feed='sip', automatic_application=False,
            health=health, counts=dict(counts), prospective_episodes=sum(r['prospective'] for r in rows),
            hot_case_episodes=sum(r['role'] == 'HOT_CASE' for r in rows),
            comparison=comparison(rows), latest_cases=[dict(symbol=r['symbol'], session_day=r['session_day'],
                role=r['role'], prospective=bool(r['prospective']), status=r['status'],
                frozen_features=r['features'], result=r['result']) for r in cases],
            diagnostics=aggregate(rows, now),
            forward_cohort=[dict(symbol=r['symbol'], role=r['role'], session_day=r['session_day'],
                frozen_at=r['frozen_at'], evidence_day=r['features'].get('evidence_day'),
                evaluate_after=(utc(r['session']['close'])+timedelta(minutes=16)).isoformat())
                for r in rows if r['prospective'] and r['status'] == 'PENDING'][-16:],
            limits=['Regular-session completed studies', 'Current Alpaca active universe, not historical delisted universe',
                    'Missing minutes remain inconclusive', 'News failures remain unavailable, never neutral scores',
                    'Retrospective SIP context was not necessarily available live',
                    'Research thresholds 8% up/3% down are not trading targets or stops'])
        key = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO move_packets VALUES (?,?,?)',
                         (key, now.isoformat(), json.dumps(packet, allow_nan=False)))
            # Bounded durable store: episodes 90 days, recent packet versions 30.
            conn.execute('DELETE FROM move_packets WHERE packet_id NOT IN (SELECT packet_id FROM move_packets ORDER BY published_at DESC LIMIT 30)')
        return key, packet


def default_path():
    from core.storage import data_directory
    return data_directory() / 'market_move_research.db'


def read_packet(symbol=None, *, now=None, path=None):
    now = now or datetime.now(timezone.utc)
    try:
        target = Path(path or default_path())
        if not target.exists():
            return None
        conn = sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
        try:
            row = conn.execute('SELECT packet_id, published_at, payload_json FROM move_packets ORDER BY published_at DESC LIMIT 1').fetchone()
        finally:
            conn.close()
        if row is None or not 0 <= (now-utc(row[1])).total_seconds() <= 3600:
            return None
        packet = json.loads(row[2])
        if (not isinstance(packet, dict) or packet.get('protocol') != PROTOCOL
                or packet.get('automatic_application') is not False
                or packet.get('scope') != 'RESEARCH_ONLY_NOT_TRADE_SIGNAL'
                or packet.get('source_feed') != 'sip' or packet.get('published_at') != row[1]
                or not isinstance(packet.get('latest_cases'), list)):
            return None
        result = dict(packet_id=row[0], published_at=row[1], scope=packet['scope'],
                      protocol=PROTOCOL, automatic_application=False, health=packet['health'],
                      counts=packet['counts'], comparison=packet['comparison'],
                      prospective_episodes=packet['prospective_episodes'], hot_case_episodes=packet['hot_case_episodes'])
        if any(not isinstance(result[k],dict) for k in ('health','counts','comparison')):
            return None
        result['cases'] = [c for c in packet['latest_cases'] if symbol is None or c['symbol'] == symbol]
        result['diagnostics'] = packet.get('diagnostics', {})
        result['forward_cohort'] = [c for c in packet.get('forward_cohort', []) if symbol is None or c['symbol'] == symbol]
        return result
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


def report_text(packet):
    if packet is None:
        return '🔬 مسار أبحاث الحركات: ينتظر تقريرًا حديثًا.'
    counts, comp = packet['counts'], packet['comparison']
    health = packet['health']
    states = {'READY':'يعمل', 'DATA_UNAVAILABLE':'تعذر جلب البيانات', 'STARTING':'يبدأ'}
    roles = {'HOT_CASE':'دراسة صعود لاحقة', 'ACTIVITY':'عينة نشاط مسبقة', 'CONTROL':'عينة مقارنة'}
    outcomes = {'UP8_FIRST':'صعود 8% أولًا', 'DOWN3_FIRST':'هبوط 3% أولًا',
                'NO_BARRIER':'لم يبلغ العتبتين', 'AMBIGUOUS':'ترتيب غير محسوم'}
    comparisons = {'WAITING_FORWARD_DATA':'ينتظر عينة كافية', 'NO_VALIDATED_EDGE':'لم تثبت أفضلية',
                   'VALIDATED_RESEARCH_ASSOCIATION':'ارتباط بحثي اجتاز المقارنة'}
    lines = ['🔬 مسار أبحاث الحركات — مستقل', f"الحالة: {states.get(health.get('state'), 'غير متاح')}",
        f"آخر جلسة مكتملة: {health.get('completed_session', 'غير متاح')}",
        f"كتالوج الأصول: {health.get('universe_count', 'غير متاح')} | شموع يومية لأسهم: {health.get('daily_symbols', 'غير متاح')}",
        'الأعداد من السجل المحفوظ حتى 90 يومًا؛ ليست صفقات منفذة.',
        f"عينات مجمدة قبل الجلسة: {packet['prospective_episodes']} | دراسات صعود لاحقة: {packet['hot_case_episodes']}",
        f"مكتملة: {counts.get('COMPLETE', 0)} | تنتظر: {counts.get('PENDING', 0)} | ناقصة/غير متاحة: {counts.get('PARTIAL_DATA',0)+counts.get('DATA_UNAVAILABLE',0)}",
        f"مقارنات مستقبلية مكتملة: {comp['complete_pairs']} على {comp['unique_days']} أيام | {comparisons.get(comp['state'], 'غير متاح')}"]
    diagnostics = packet.get('diagnostics', {})
    if diagnostics:
        from market.research_diagnostics import diagnostic_text
        lines.append('تشخيص النقص: '+diagnostic_text(diagnostics.get('reasons', {})))
        if 'unique_incomplete_symbol_days' in diagnostics:
            lines.append(f"السجلات الناقصة تخص {diagnostics['unique_incomplete_symbol_days']} سهم/جلسة مختلفة.")
        lines.append(f"إعادة فحص مؤهلة الآن: {diagnostics.get('retry_eligible', 0)} | حد المحاولات: 3 خلال 48 ساعة")
        labels = {'NO_TRADES_IN_FEED':'بلا صفقات في المصدر',
                  'ONLY_NON_PRICE_UPDATING_TRADES':'صفقات لا تحدد سعر الشمعة',
                  'REGULAR_TRADES_WITHOUT_BAR':'صفقات عادية دون شمعة',
                  'UNKNOWN_TRADE_CONDITIONS':'علامات صفقات غير محسومة',
                  'INCOMPLETE_TRADE_READ':'صفحات صفقات غير مكتملة',
                  'TRADE_READ_UNAVAILABLE':'تعذر قراءة الصفقات',
                  'CONFLICTING_TRADE_SAMPLES':'عينات متعارضة'}
        if diagnostics.get('sampled_minutes'):
            lines.append('عينات دقائق مختلفة: '+' | '.join(f"{labels.get(k,k)} {v}" for k,v in diagnostics['sampled_minutes'].items()))
            lines.append('العينات لا تفسر بقية الدقائق، ولا تعوّض شموعًا ناقصة.')
    cohort = packet.get('forward_cohort', [])
    if cohort:
        at = utc(min(c['evaluate_after'] for c in cohort)).astimezone(ZoneInfo('Asia/Riyadh'))
        lines.append('أقرب تقييم للعينة بعد إتاحة البيانات: '+at.strftime('%Y-%m-%d %H:%M')+' السعودية')
    for c in packet.get('cases', [])[:5]:
        result = c['result']
        lines.append(f"• {c['symbol']} {c['session_day']} | {roles.get(c['role'], c['role'])} | {outcomes.get(result.get('outcome'), 'غير محسوم')}")
        if result.get('status') == 'COMPLETE':
            lines.append(f"  أعلى حركة من الافتتاح {result['observed_mfe_pct']:+.2f}% | أدنى حركة {result['observed_mae_pct']:+.2f}%")
        context = result.get('pre_move_context', {})
        if context.get('status') == 'COMPLETE':
            lines.append(f"  قبل الحركة 20 دقيقة: تغير {context['return_pct']:+.2f}% | تسارع حجم {context['volume_acceleration']:.2f}x | فوق VWAP: {'نعم' if context['close_above_vwap'] else 'لا'}")
        news = result.get('pre_move_news', {})
        if news.get('status') == 'AVAILABLE':
            keywords = sorted({k for a in news.get('articles', []) for k in a.get('keywords', [])})
            lines.append(f"  أخبار مقبولة زمنيًا: {news.get('count',0)} | كلمات وصفية: {', '.join(keywords) or 'لا كلمات مصنفة'}")
        if result.get('status') == 'PARTIAL_DATA':
            lines.append(f"  دقائق ناقصة: {result.get('missing_minutes', 'غير متاح')}؛ تقييم الجلسة الكاملة غير مكتمل.")
            prefix = result.get('prefix_context', {})
            if prefix.get('status') == 'OBSERVED_PREFIX':
                order = outcomes.get(prefix.get('observed_barrier_order'), 'لم يبلغ العتبتين في الجزء المرصود')
                lines.append(f"  الجزء المتصل قبل أول فجوة: {prefix['bars']} دقيقة | {order}؛ وصف جزئي فقط.")
                context = prefix.get('pre_move_context', {})
                if context.get('status') == 'COMPLETE':
                    lines.append(f"  قبل الصعود في هذا الجزء: تغير {context['return_pct']:+.2f}% | تسارع حجم {context['volume_acceleration']:.2f}x")
    lines.append('عتبات الحركة 8%/3% بحثية؛ ليست أهداف صفقة أو أرباحًا فعلية. النتائج لا تغيّر بوابات الدخول تلقائيًا.')
    return '\n'.join(lines)
