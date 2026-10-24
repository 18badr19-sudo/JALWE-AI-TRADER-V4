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
    if 'targeted_bar_recheck' in frame.attrs:
        diagnostics['targeted_bar_recheck'] = frame.attrs['targeted_bar_recheck']
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
                CREATE TABLE IF NOT EXISTS move_minute_evidence (
                    episode_key TEXT PRIMARY KEY, expires_at TEXT NOT NULL, evidence_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_move_evidence_expiry ON move_minute_evidence(expires_at);
                CREATE TABLE IF NOT EXISTS move_pattern_features (
                    session_day TEXT NOT NULL, symbol TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(session_day,symbol));
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
        if role not in {'ACTIVITY', 'CONTROL', 'HOT_CASE', 'JUMP_CASE','ENTRY_STUDY','ARCHIVE_STUDY'} or (role in {'HOT_CASE','JUMP_CASE','ENTRY_STUDY','ARCHIVE_STUDY'} and prospective):
            raise ValueError('Invalid study role')
        if role=='ARCHIVE_STUDY':
            from archival_entry_research import PROTOCOL as ARCHIVE_PROTOCOL
            if (features.get('archive_protocol')!=ARCHIVE_PROTOCOL
                    or features.get('archive_group') not in {'MATCH','CONTROL','FOCUS'}
                    or now<utc(session['close'])+timedelta(minutes=16)):
                raise ValueError('Archive study requires a completed historical session')
        if role=='ENTRY_STUDY' and not (features.get('entry_protocol')=='prior-day-orb-execution-study-v1'
                and utc(features['available_after'])<=utc(features['snapshot_prepared_at'])<=now<utc(session['open'])):
            raise ValueError('Entry study was not frozen before open')
        if role=='ENTRY_STUDY':
            from prior_day_priority_bus import valid_signal
            if not valid_signal(dict(symbol=features['symbol'],signal_id=features.get('priority_signal_id'),
                    prepared_at=features['snapshot_prepared_at'],features=features),session):
                raise ValueError('Invalid entry study nomination')
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

    def enqueue_jumps(self, studies, mark, now, lookback_days):
        """Commit a historical manifest in one write, with its crash-safe marker."""
        if self.state(mark) is not None:
            return self.state(mark)
        values = [self._episode_values(feat,session,'JUMP_CASE',
            stable_key(feat['symbol']+'|'+feat['jump_protocol'],session['date']),now,prospective=False)
            for feat,session in studies]
        manifest = dict(created_at=now.isoformat(),selected=len(values),lookback_days=lookback_days)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            saved = conn.execute('SELECT value FROM move_state WHERE key=?',(mark,)).fetchone()
            if saved:
                return json.loads(saved['value'])
            conn.executemany('''INSERT OR IGNORE INTO move_episodes
                (episode_key,session_day,symbol,role,pair_key,prospective,frozen_at,features_json,session_json)
                VALUES (?,?,?,?,?,?,?,?,?)''',values)
            conn.execute('INSERT INTO move_state VALUES (?,?)',(mark,json.dumps(manifest)))
        return manifest

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
            from market_move_patterns import register_frozen_validation
            register_frozen_validation(conn,values)
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

    def save_pattern_features(self, features, session, now, *, clock=None):
        """Persist prior-day inputs once, before open, for all analysis candidates."""
        mark = 'pattern_features:'+session['date']
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM move_state WHERE key=?',(mark,)).fetchone():
                return False
            stamp = clock() if clock else now
            if stamp >= utc(session['open']):
                return False
            values = [(session['date'],f['symbol'],json.dumps(dict(features=f,session=session,
                prepared_at=stamp.isoformat()),allow_nan=False)) for f in features
                if utc(f['available_after']) <= stamp]
            conn.executemany('INSERT INTO move_pattern_features VALUES (?,?,?)',values)
            conn.execute('INSERT INTO move_state VALUES (?,?)',(mark,json.dumps(dict(prepared_at=stamp.isoformat()))))
            if clock and clock() >= utc(session['open']):
                conn.rollback()
                return False
            # Keep immutable pre-open inputs for nomination audits after the
            # next snapshot is prepared. Consumers still select exact days.
            cutoff=(utc(session['open'])-timedelta(days=30)).date().isoformat()
            conn.execute('DELETE FROM move_pattern_features WHERE session_day < ?',(cutoff,))
        return True

    def retained_evidence(self, key, now):
        with self.connect() as conn:
            row = conn.execute('SELECT evidence_json FROM move_minute_evidence WHERE episode_key=? AND expires_at>=?',
                               (key,now.isoformat())).fetchone()
        return json.loads(row[0]) if row else None

    def prune_retained_evidence(self, now):
        # Bound each cleanup transaction; raw retry evidence is not kept for 90 days.
        with self.connect() as conn:
            return conn.execute('''DELETE FROM move_minute_evidence WHERE episode_key IN
                (SELECT episode_key FROM move_minute_evidence WHERE expires_at<? LIMIT 1000)''',
                (now.isoformat(),)).rowcount

    def settle(self, key, result, now, *, retry=False, expected_attempts=None, bar_evidence=None):
        if result['status'] not in {'COMPLETE', 'PARTIAL_DATA', 'DATA_UNAVAILABLE'}:
            raise ValueError('Invalid observation status')
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM move_episodes WHERE episode_key=?', (key,)).fetchone()
            if row is None:
                return False
            if expected_attempts is not None and row['attempts'] != expected_attempts:
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
            if changed:
                from market_move_diagnostics import MAX_ATTEMPTS, RETRY_HORIZON
                anchor = utc(row['frozen_at']) if row['role'] in {'JUMP_CASE','ARCHIVE_STUDY'} else utc(json.loads(row['session_json'])['close'])
                expires = anchor+RETRY_HORIZON
                if result['status']=='COMPLETE' or row['attempts']+1>=MAX_ATTEMPTS or expires<now:
                    conn.execute('DELETE FROM move_minute_evidence WHERE episode_key=?',(key,))
                elif bar_evidence is not None:
                    conn.execute('INSERT OR REPLACE INTO move_minute_evidence VALUES (?,?,?)',
                                 (key,expires.isoformat(),json.dumps(bar_evidence,allow_nan=False)))
        return bool(changed)

    def state(self, key, value=None):
        with self.connect() as conn:
            if value is not None:
                conn.execute('INSERT OR REPLACE INTO move_state VALUES (?,?)', (key, json.dumps(value)))
                return value
            row = conn.execute('SELECT value FROM move_state WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def enrich_jump_profile(self,key,expected_result,profile,now):
        """Add descriptive evidence only; never reopen or replace an outcome."""
        import copy
        updated = copy.deepcopy(expected_result)
        if not isinstance(updated.get('jump_path'),dict):
            return False
        updated['jump_path']['profile'] = profile
        encoded = json.dumps(updated,allow_nan=False)
        with self.connect() as conn:
            row = conn.execute('SELECT role,prospective,status,result_json,session_json FROM move_episodes WHERE episode_key=?',(key,)).fetchone()
            if (row is None or row['role']!='JUMP_CASE' or row['prospective'] or
                    row['status'] not in {'COMPLETE','PARTIAL_DATA'} or
                    utc(json.loads(row['session_json'])['close'])+timedelta(minutes=16)>now or
                    json.loads(row['result_json'] or '{}')!=expected_result):
                return False
            return bool(conn.execute('UPDATE move_episodes SET result_json=? WHERE episode_key=? AND result_json=?',
                                     (encoded,key,row['result_json'])).rowcount)

    def publish(self, now, health, *, patterns=None):
        from prior_day_entry_study import summary as entry_summary
        from archival_entry_research import summary as archive_summary
        from market_factor_statistics import news_summary
        cutoff = (now.astimezone(NY).date()-timedelta(days=90)).isoformat()
        with self.connect() as conn:
            conn.execute('DELETE FROM move_episodes WHERE session_day < ?', (cutoff,))
        rows = self.episodes()
        from market_move_diagnostics import aggregate
        counts = Counter(r['status'] for r in rows)
        jump_cutoff = (now.astimezone(NY).date()-timedelta(days=30)).isoformat()
        jumps = [r for r in rows if r['role']=='JUMP_CASE' and r['session_day'] >= jump_cutoff]
        from market_jump_requests import request_summary
        requested = request_summary(rows,self.state('jump_request'))
        from market_jump_profiles import summarize_profiles
        profiles = summarize_profiles(jumps)
        strongest = sorted((r for r in jumps if r['status']=='COMPLETE'
            and r['result'].get('jump_path',{}).get('status')=='COMPLETE'
            and r['result']['jump_path'].get('strong_jump') is True),
            key=lambda r:(-r['result']['jump_path']['observed_rise_pct'],r['symbol'],r['session_day']))[:6]
        day = health.get('completed_session')
        examples = [r for r in rows if r['session_day'] <= day] if day else [r for r in rows if r['status'] != 'PENDING']
        examples = [r for r in examples if r['role']!='ARCHIVE_STUDY']
        cases = sorted(examples, key=lambda r: (r['session_day'], {'JUMP_CASE':2,'HOT_CASE':1}.get(r['role'],0),
            {'COMPLETE':3,'PARTIAL_DATA':2,'DATA_UNAVAILABLE':1,'PENDING':0}.get(r['status'],0),
            r['episode_key']), reverse=True)[:12]
        packet = dict(protocol=PROTOCOL, scope='RESEARCH_ONLY_NOT_TRADE_SIGNAL',
            published_at=now.isoformat(), source_feed='sip', automatic_application=False,
            health=health, focused_collection=self.state('focused_collection_status') or {}, counts=dict(counts), prospective_episodes=sum(r['prospective'] for r in rows),
            hot_case_episodes=sum(r['role'] == 'HOT_CASE' for r in rows),
            historical_jumps=dict(episodes=len(jumps),counts=dict(Counter(r['status'] for r in jumps)),
                confirmed=sum(r['status']=='COMPLETE' and r['result'].get('jump_path',{}).get('strong_jump') is True for r in jumps),
                lookback_days=health.get('jump_lookback_days',30)),
            requested_jumps=requested,
            historical_jump_profiles=profiles,
            strongest_completed_jumps=[dict(symbol=r['symbol'],session_day=r['session_day'],
                path=r['result']['jump_path']) for r in strongest],
            comparison=comparison(rows), latest_cases=[dict(symbol=r['symbol'], session_day=r['session_day'],
                role=r['role'], prospective=bool(r['prospective']), status=r['status'],
                frozen_features=r['features'], result=r['result']) for r in cases],
            diagnostics=aggregate(rows, now),
            patterns=patterns or {'state':'UNAVAILABLE'},
            factor_benchmark=self.state('daily_factor_benchmark') or {},
            news_statistics=news_summary(rows),
            priority_link=self.state('prior_day_bus_status') or {},
            entry_study=entry_summary(rows),
            archive_study=archive_summary(rows),
            priority_trace=self.state('prior_day_trace_summary') or {},
            priority_native_outcomes=self.state('prior_day_native_outcomes') or {},
            forward_cohort=[dict(symbol=r['symbol'], role=r['role'], session_day=r['session_day'],
                frozen_at=r['frozen_at'], evidence_day=r['features'].get('evidence_day'),
                evaluate_after=(utc(r['session']['close'])+timedelta(minutes=16)).isoformat())
                for r in rows if r['prospective'] and r['status'] == 'PENDING'][-16:],
            limits=['Regular-session completed studies', 'Active US equity research catalog plus explicit symbol/day requests; not a full delisted universe',
                    'Missing minutes remain inconclusive', 'News failures remain unavailable, never neutral scores',
                    'Retrospective SIP context was not necessarily available live',
                    'Research thresholds 8% up/3% down are not trading targets or stops'])
        from market_research_findings import findings
        packet['findings'] = findings(packet)
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
        result['focused_collection'] = packet.get('focused_collection', {})
        result['diagnostics'] = packet.get('diagnostics', {})
        result['patterns'] = packet.get('patterns', {})
        result['findings'] = packet.get('findings', {})
        result['factor_benchmark'] = packet.get('factor_benchmark', {})
        result['news_statistics'] = packet.get('news_statistics', {})
        result['priority_link'] = packet.get('priority_link', {})
        result['entry_study'] = packet.get('entry_study', {})
        result['archive_study'] = packet.get('archive_study', {})
        result['priority_trace'] = packet.get('priority_trace', {})
        result['priority_native_outcomes'] = packet.get('priority_native_outcomes', {})
        result['historical_jumps'] = packet.get('historical_jumps', {})
        result['requested_jumps'] = packet.get('requested_jumps', {})
        result['historical_jump_profiles'] = packet.get('historical_jump_profiles', {})
        result['strongest_completed_jumps'] = [c for c in packet.get('strongest_completed_jumps', [])
                                              if symbol is None or c['symbol']==symbol]
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
    roles = {'HOT_CASE':'دراسة صعود لاحقة', 'JUMP_CASE':'دراسة مسار قفزة تاريخية','ENTRY_STUDY':'اختبار دخول للمطابقات',
             'ACTIVITY':'عينة نشاط مسبقة', 'CONTROL':'عينة مقارنة'}
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
    knowledge=packet.get('findings',{}).get('selected_jump_group',{})
    if knowledge.get('cases'):
        lines.append(f"🧠 صفات مشتركة بين {knowledge['cases']} قفزة مكتملة ≥{knowledge['min_rise_pct']}%:")
        for fact in knowledge.get('factors',[]):
            lines.append(f"  {fact['label']}: {fact['present']}/{fact['known']} | مجهول {fact['unknown']}")
        lines.append('نسبة ظهور بين قافزين مختارين، وليست نسبة نجاح شراء. التفاصيل: /research_findings')
    focus=packet.get('focused_collection',{})
    if focus.get('symbols'):
        coverage=focus.get('coverage_pct')
        coverage='ينتظر الدقائق المتاحة' if coverage is None else f"{coverage:.1f}%"
        lines += [f"🎯 جمع مركز قبل الافتتاح — {focus['session_day']}: {focus['symbols']} سهم | {focus['pairs']} أزواج | مرشحون {focus['nominees']}",
            f"تغطية محفوظة للدقائق المتاحة: {coverage} | مسارات جلسة كاملة {focus['complete_paths']}/{focus['symbols']} | أزواج مكتملة {focus['complete_pairs']}/{focus['pairs']}",
            'حفظ SIP متأخر أثناء الجلسة؛ النقص مجهول، ولا يُملأ بسعر مفترض. اكتمال الجمع لا يثبت أفضلية أو نجاح تدريب.']
        if focus.get('reasons'):
            labels={'MISSING_PROVIDER_MINUTES':'دقائق غير مرجعة','EMPTY_PROVIDER_WINDOW':'نافذة فارغة',
                    'INVALID_OR_DUPLICATE_BARS':'شموع غير صالحة أو متعارضة','COMPLETE':'الدقائق المتاحة مكتملة'}
            lines.append('حالة نوافذ العينة: '+' | '.join(f"{labels.get(k,k)} {v}" for k,v in focus['reasons'].items()))
        failed=focus.get('latest_batch',{})
        if failed.get('state')=='DATA_UNAVAILABLE':
            lines.append('آخر جلب للعينة تعذر: '+str(failed.get('reason','غير محسوم'))+'؛ التغطية المعروضة من الدقائق المحفوظة.')
    jumps = packet.get('historical_jumps',{})
    batch = health.get('study_batch',{})
    if batch:
        lines.append(f"آخر دفعة أبحاث: عولجت {batch['processed']} من {batch['selected']} | الحد {batch['limit']} | الزمن {batch['elapsed_seconds']:.0f} ثانية")
        lines.append('جلب شموع مشترك حسب الجلسة؛ سرعة المعالجة لا تعني اكتمال البيانات أو ضمان انتهاء الطابور.')
    if 'completed_session_forward_total' in health:
        lines.append(f"عينات الجلسة الأخيرة: {health['completed_session_forward_total']} | تنتظر أول تقييم {health['completed_session_forward_pending']} | دراسات تاريخية تنتظر {health['completed_session_historical_pending']}")
        lines.append('تقرير حالة العينات؛ نقص البيانات يبقى غير محسوم. قائمة الدراسات التاريخية لا تحجز إرسال تقرير المقارنة.')
    if jumps:
        jc = jumps.get('counts',{})
        lines.append(f"📚 مسارات القفزات — آخر {jumps['lookback_days']} يومًا: مرشحة {jumps['episodes']} | قفزات مؤكدة {jumps['confirmed']} | تنتظر {jc.get('PENDING',0)}")
        lines.append('مرشحو نطاق/فجوة يومية 8% أو أكثر بسيولة مرصودة قبل الحركة أو يومها؛ الأكبر له أولوية قراءة، والحد اليومي ليس مسارًا مثبتًا. لا يشترط شراء البوت أو قابلية التداول عند الوسيط.')
    benchmark = packet.get('factor_benchmark',{})
    if benchmark.get('protocol') == 'daily-factor-benchmark-v1':
        history = benchmark['retrospective']['all_days']
        forward = benchmark['forward']
        lines.append(f"⚖️ قياس العوامل اليومي — {history['days']} جلسات تاريخية؛ يشمل الأسهم التي لم تصعد.")
        for key,label in (('RANGE8','نطاق سابق ≥8%'),('ALL_THREE','اجتماع الشروط الثلاثة')):
            evidence=history['rules'][key]; match=evidence['matching']; pairs=evidence['pairs']
            lines.append(f"  {label}: بلغت +8% {match['reached']} | لم تبلغ {match['not_reached']} | نتيجة ناقصة {match['unknown']}")
            lines.append(f"  أزواج ضابطة مكتملة {pairs['complete']}/{pairs['selected']}؛ بلغت +8%: مطابقة {pairs['matching_reached']} مقابل ضابطة {pairs['control_reached']}")
        lines.append(f"  عينات مستقبلية مجمدة: {forward['cohort_days']} جلسات | تنتظر {forward['pending_symbol_days']} سهم/جلسة | قُيّمت {forward['evidence']['days']} جلسات.")
        lines.append('القياس: أعلى شمعة يومية لدى المزود ≥8% من افتتاحها؛ ليس مسار دقائق أو ربح صفقة. لا يستنتج ترتيب الوقف والهدف.')
        lines.append('الفترات التاريخية الأولى واللاحقة منفصلة، وتبقى بحثًا بأثر رجعي. العينات المستقبلية منفصلة؛ لا أفضلية مثبتة أو تطبيق تلقائي.')
    from market_factor_statistics import report_lines as statistics_report
    lines.extend(statistics_report(benchmark,packet.get('news_statistics',{})))
    link=packet.get('priority_link',{})
    if link:
        lines.append(f"🔗 ربط الباحث بـAPEX: {link.get('state')} | مرشحة {link.get('eligible',link.get('signals',0))} | جلسة {link.get('session_day','غير متاحة')}")
        lines.append('مقاعد محدودة داخل الرادار والتحليل العميق؛ لا تتجاوز فحوص الجودة أو تسمح بشراء مباشر.')
    entry=packet.get('entry_study',{})
    if entry:
        states=entry['states']; no_entry=sum(v for k,v in states.items() if k.startswith('NO_') or k=='INVALIDATED_BEFORE_ENTRY')
        unknown=sum(v for k,v in states.items() if k not in {'PENDING','SIMULATED_EXIT','INVALIDATED_BEFORE_ENTRY'} and not k.startswith('NO_'))
        lines.append(f"🧪 اختبار دخول نطاق أول 15 دقيقة: {entry['total']} عينة | تنتظر {states.get('PENDING',0)} | مخارج محاكاة {states.get('SIMULATED_EXIT',0)} | بلا دخول {no_entry} | ناقصة/ملتبسة {unknown}")
        for bps,cost in entry.get('costs',{}).items():
            mean=cost.get('mean_net_r'); text=f'{mean:+.2f}R' if mean is not None else 'N/A'
            lines.append(f"  تكلفة مفترضة {int(bps)/100:.2f}% لكل جانب: مخارج محاكاة {cost['simulated_exits']} | موجب {cost['positive']} | سالب {cost['negative']} | متوسط {text}")
        lines.append('تأكيد اختراق نطاق أول 15 دقيقة بحجم 1.5×، دخول افتتاح الدقيقة التالية ضمن حد المطاردة، وقف قاع النطاق وهدف 2R. ليس تنفيذًا أو ربحًا فعليًا؛ التكاليف مفترضة.')
    trace=packet.get('priority_trace',{})
    if trace:
        lines.append(f"🧾 تتبع الإشارات: {trace.get('state')} | المراحل {trace.get('stages',{})}")
    native=packet.get('priority_native_outcomes',{})
    if native:
        lines.append(f"  نتائج PAPER المرتبطة: {native.get('state')}")
        if native.get('state')=='NATIVE_RECONCILED_EXITS':
            lines.append(f"  صفقات لها خروج مؤكد {native['unique_trades']} | محقق قبل رسوم الوسيط ${native['realized_pnl_before_broker_fees']:+.2f}")
    requested = packet.get('requested_jumps',{})
    if requested:
        rc = requested.get('counts',{})
        lines.append(f"📌 طلب دراسة {requested['start']} إلى {requested['end']}: {requested['queued']} سهم/جلسة | مكتملة {rc.get('COMPLETE',0)} | تنتظر {rc.get('PENDING',0)} | ناقصة {rc.get('PARTIAL_DATA',0)} | تعذر الجلب {rc.get('DATA_UNAVAILABLE',0)}")
        for item in requested.get('symbols_status',[]):
            c = item['counts']
            lines.append(f"  {item['symbol']}: مكتملة {c.get('COMPLETE',0)} | تنتظر {c.get('PENDING',0)} | ناقصة/غير متاحة {c.get('PARTIAL_DATA',0)+c.get('DATA_UNAVAILABLE',0)}")
    strongest = packet.get('strongest_completed_jumps',[])
    profiles = packet.get('historical_jump_profiles',{})
    if profiles:
        from market_jump_profiles import FACTORS
        lines.append(f"🧠 ذاكرة عوامل القفزات: {profiles['completed_profiles']} مكتملة | {profiles['partial_profiles']} جزئية منفصلة")
        for factor in profiles.get('factors',[]):
            lines.append(f"  {FACTORS[factor['code']]}: ظهر في {factor['present']} من {factor['known']} بسياق معروف | مجهول {factor['unknown']}")
        common = profiles.get('common_factors',{})
        if common:
            from market_jump_common import FACTORS as DAILY_FACTORS
            labels = dict(FACTORS,**DAILY_FACTORS)
            for f in common['all_jumps']['factors']:
                if f['codes'][0] in DAILY_FACTORS:
                    lines.append(f"  {labels[f['codes'][0]]}: {f['present']}/{f['known']} معروف | مجهول {f['unknown']}")
            groups = common['large_jump_groups']
            lines.append('قفزات مكتملة ≥25/50/100%: '+'/'.join(str(g['cases']) for g in groups)+'؛ مجموعات متداخلة، وعتبات بحثية.')
            large = groups[0]
            shared = large['prominent_cooccurrences']
            for f in shared:
                lines.append('  اجتماع '+ ' + '.join(labels[c] for c in f['codes'])+
                    f": {f['present']}/{f['known']} معروف في قفزات ≥25% | مجهول {f['unknown']}")
            if not shared:
                lines.append('العوامل المشتركة للقفزات ≥25%: لم تتوفر حالات معلومة كافية بعد.')
        lines.append('وصف لعينات مختارة بعد الحركة؛ لا توجد مجموعة ضابطة أو أفضلية تنبؤية مثبتة، ولا تطبيق تلقائي على التداول.')
    if strongest:
        lines.append('🏔 أكبر المسارات المثبتة بالدقيقة في الدراسات المكتملة فقط — ليس ترتيب السوق كله:')
        for c in strongest[:6]:
            p = c['path']
            lines.append(f"  {c['symbol']} {c['session_day']}: {p['observed_rise_pct']:+.2f}% | ${p['start_price']:.4f} → ${p['peak_price']:.4f} خلال {p['duration_minutes']} دقيقة")
            profile = p.get('profile',{})
            if profile.get('status')=='AVAILABLE':
                phases = profile.get('volume_phases',[])
                shares = '/'.join(f"{phase['volume_share_pct']:.0f}%" for phase in phases) or 'غير كافٍ'
                lines.append(f"    توزيع حجم مبكر/وسط/متأخر: {shares} | تراجع بين دقائق المسار {profile['max_prior_minute_peak_to_later_low_decline_pct']:.2f}%")
                first = next((m for m in profile.get('milestones',[]) if m['threshold_pct']==8),{})
                after = next((w for w in profile.get('after_peak',[]) if w['minutes']==15),{})
                speed = str(first['elapsed_minutes'])+' دقيقة تقديرية' if first.get('elapsed_minutes') is not None else 'غير متاح'
                follow = (f"{after['close_vs_peak_pct']:+.2f}%" if after.get('status')=='COMPLETE_WINDOW'
                          and isinstance(after.get('close_vs_peak_pct'),(float,int)) else 'غير متاح')
                lines.append(f"    وصول +8% داخل المسار: {speed} | إغلاق بعد القمة بـ15 دقيقة: {follow}")
    diagnostics = packet.get('diagnostics', {})
    if diagnostics:
        from market.research_diagnostics import diagnostic_text
        lines.append('أسباب نقص سجلات التقييم (الأعداد سجلات، وليست دقائق): '+diagnostic_text(diagnostics.get('reasons', {})))
        if 'unique_incomplete_symbol_days' in diagnostics:
            lines.append(f"السجلات الناقصة تخص {diagnostics['unique_incomplete_symbol_days']} سهم/جلسة مختلفة.")
        coverage = diagnostics.get('coverage_totals', {})
        if coverage.get('measured_symbol_days'):
            lines.append(f"الدقائق غير المرجعة: {coverage['absent_provider_minutes']} | مرجعة ومرفوضة: {coverage['rejected_minutes']} | صفوف مكررة: {coverage['duplicate_rows']}؛ عبر {coverage['measured_symbol_days']} سهم/جلسة، مع حذف تكرار السجلات.")
        if coverage.get('conflicting_symbol_days'):
            lines.append(f"تغطية متعارضة: {coverage['conflicting_symbol_days']} سهم/جلسة؛ مستبعدة من إجمالي الدقائق.")
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
    patterns = packet.get('patterns', {})
    if patterns.get('protocol') == 'prior-day-move-patterns-v1':
        from market_move_patterns import LABELS
        labels = {'COLLECTING_TRAINING':'يجمع بيانات تدريب مستقبلية',
            'WAITING_FRESH_VALIDATION':'اختار نمطًا على التدريب وينتظر 20 جلسة لاحقة مستقلة',
            'ACTIVE_PRIORITY':'أولوية فحص PAPER مفعلة بعد التحقق',
            'FAILED_VALIDATION':'لم يجتز التقييم؛ التطبيق متوقف',
            'SUSPENDED':'أوقفت الأولوية بعد ضعف النتائج اللاحقة', 'EXPIRED':'انتهت صلاحية النمط'}
        lines.append('🧠 تعلم الأنماط: '+labels.get(patterns.get('state'),'غير متاح'))
        if patterns.get('rule'):
            lines.append('النمط: '+LABELS.get(patterns['rule'],patterns['rule']))
        else:
            lines.append('قواعد مرشحة ثابتة: 3؛ نشاط الحجم، نطاق السعر والزخم من اليوم السابق.')
        lines.append('تعلم ارتباطات وصفية، وليس إثباتًا لأسباب الصعود. التطبيق يرتب الفحص فقط؛ الشراء يمر بالشروط الأصلية.')
    from core.config import settings
    from market_move_trial import status as trial_status
    trial = trial_status(datetime.now(timezone.utc),paper=settings.PAPER_TRADING,
                         allow_live=settings.ALLOW_LIVE_TRADING)
    trial_labels = {'ACTIVE_EXPERIMENT':'مفعلة لأولوية فحص PAPER فقط',
        'DISABLED':'متوقفة', 'PAPER_ONLY':'متوقفة خارج PAPER', 'NOT_STARTED':'لم تبدأ', 'EXPIRED':'انتهت'}
    lines.append('🧪 تجربة بدر للعوامل المشتركة: '+trial_labels[trial['state']])
    lines.append('الشروط مجتمعة: نطاق اليوم السابق ≥8%، الحجم ≥2× اليوم الذي قبله، والصعود ≥3%.')
    lines.append('تجربة غير مثبتة؛ لا تتجاوز شروط الشراء والمخاطر. تنتهي 2026-10-31 05:53 السعودية.')
    for c in packet.get('cases', [])[:5]:
        result = c['result']
        lines.append(f"• {c['symbol']} {c['session_day']} | {roles.get(c['role'], c['role'])} | {outcomes.get(result.get('outcome'), 'غير محسوم')}")
        path = result.get('jump_path',{})
        if path.get('status') in {'COMPLETE','OBSERVED_SEGMENT'}:
            zone = ZoneInfo('Asia/Riyadh')
            start = utc(path['start_at']).astimezone(zone).strftime('%H:%M')
            peak = utc(path['peak_at']).astimezone(zone).strftime('%H:%M')
            label = 'جلسة مكتملة' if path['status']=='COMPLETE' else 'جزء متصل فقط؛ القمة العامة غير محسومة'
            lines.append(f"  بداية مرصودة ${path['start_price']:.4f} عند {start} → قمة ${path['peak_price']:.4f} عند {peak} السعودية | {path['observed_rise_pct']:+.2f}% خلال {path['duration_minutes']} دقيقة | {label}")
            lines.append(f"  حجم المرحلة {path['phase_volume']:,.0f} سهم؛ مسار {len(path['trajectory'])} شمعة دقيقة محفوظ. البداية تقديرية وترتيب الحركة داخل الدقيقة غير معلوم.")
        if result.get('status') == 'COMPLETE':
            lines.append(f"  أعلى حركة من الافتتاح {result['observed_mfe_pct']:+.2f}% | أدنى حركة {result['observed_mae_pct']:+.2f}%")
        historical_jump = c['role'] == 'JUMP_CASE'
        # The opening +8% crossing can occur after a retrospective jump began.
        # Never present that later context as evidence before the jump's onset.
        context = path.get('pre_jump_context', {}) if historical_jump else result.get('pre_move_context', {})
        if context.get('status') == 'COMPLETE':
            context_label = 'قبل بداية القفزة المرصودة 20 دقيقة' if historical_jump else 'قبل الحركة 20 دقيقة'
            lines.append(f"  {context_label}: تغير {context['return_pct']:+.2f}% | تسارع حجم {context['volume_acceleration']:.2f}x | فوق VWAP: {'نعم' if context['close_above_vwap'] else 'لا'}")
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
