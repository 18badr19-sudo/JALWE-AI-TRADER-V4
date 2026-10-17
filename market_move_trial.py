"""Owner-requested, bounded PAPER scan experiment; no order/entry authority.

This is an unvalidated manual hypothesis, separate from patterns-v1 training.
It only permutes the same fresh APEX reports using pre-open SIP snapshots.
"""
import json
import math
import os
import sqlite3
from pathlib import Path

from market_move_research import NY, default_path, read_packet, utc

PROTOCOL = 'owner-prior-day-confluence-paper-trial-v1'
START = '2026-10-17T02:53:52+00:00'
END = '2026-10-31T02:53:52+00:00'
RULE = {'range_pct': 8., 'volume_ratio_previous_day': 2., 'daily_return_pct': 3.}
MODE = 'OWNER_PAPER_PRIORITY_TRIAL'


def status(now, *, paper, allow_live):
    enabled = os.getenv('JALWE_PRIOR_DAY_TRIAL_ENABLED', 'true').strip().lower() in {'1','true','yes','on'}
    state = ('DISABLED' if not enabled else 'PAPER_ONLY' if not paper or allow_live
             else 'NOT_STARTED' if now < utc(START) else 'EXPIRED' if now >= utc(END)
             else 'ACTIVE_EXPERIMENT')
    return dict(protocol=PROTOCOL, state=state, mode=MODE, starts_at=START, expires_at=END,
                rule=dict(RULE), combination='ALL_THREE_REQUIRED', validation='UNPROVEN',
                scope='SAME_APEX_REPORTS_PRIORITY_ONLY', entry_authority=False)


def matches(features):
    values = [features.get(k) for k in RULE]
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
        return None
    return all(features[k] >= minimum for k,minimum in RULE.items())


def prioritize(items, *, now, symbol_of, paper, allow_live, path=None):
    baseline = list(items)
    audit = status(now, paper=paper, allow_live=allow_live)
    if audit['state'] != 'ACTIVE_EXPERIMENT' or not baseline:
        return baseline, audit
    try:
        packet = read_packet(now=now, path=path)
        if not packet or packet['health'].get('state') != 'READY':
            return baseline, dict(audit, reason='RESEARCH_UNAVAILABLE')
        symbols = sorted({str(symbol_of(item)).upper() for item in baseline})
        target = Path(path or default_path())
        conn = sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)
        try:
            records = []
            for start in range(0,len(symbols),200):
                batch = symbols[start:start+200]
                placeholders = ','.join('?' for _ in batch)
                records.extend(conn.execute(f'''SELECT symbol,payload_json FROM move_pattern_features
                    WHERE session_day=? AND symbol IN ({placeholders})''',
                    (now.astimezone(NY).date().isoformat(),*batch)).fetchall())
        finally:
            conn.close()
        preferred, unknown, nonmatching = {}, [], []
        for symbol,encoded in records:
            try:
                value = json.loads(encoded)
                f,s = value['features'],value['session']
                eligible = (s['date'] == now.astimezone(NY).date().isoformat()
                    and f['symbol'] == symbol and f['evidence_day'] < s['date']
                    and utc(f['available_after']) <= utc(value['prepared_at']) < utc(s['open']) <= now < utc(s['close'])
                    and f.get('source_feed') == 'sip')
                match = matches(f) if eligible else None
                if match is True:
                    preferred[symbol] = dict(symbol=symbol, evidence_day=f['evidence_day'],
                        available_after=f['available_after'], prepared_at=value['prepared_at'],
                        session_day=s['date'], **{key:f[key] for key in RULE})
                elif match is False:
                    nonmatching.append(symbol)
                else:
                    unknown.append(symbol)
            except (KeyError,TypeError,ValueError,OverflowError):
                unknown.append(symbol)
        unknown = sorted(set(unknown) | (set(symbols)-set(preferred)-set(nonmatching)))
        ordered = sorted(baseline, key=lambda item:str(symbol_of(item)).upper() not in preferred)
        return ordered, dict(audit, reason='PREOPEN_SNAPSHOT_EVALUATED',
            session_day=now.astimezone(NY).date().isoformat(), packet_id=packet['packet_id'],
            prioritized_symbols=sorted(preferred), matched_evidence=list(preferred.values()),
            nonmatching_symbols=sorted(nonmatching), unknown_symbols=unknown,
            learned_policy_state=(packet.get('patterns') or {}).get('state'))
    except Exception as exc:
        return baseline, dict(audit, reason='TRIAL_READ_UNAVAILABLE', error_type=type(exc).__name__)
