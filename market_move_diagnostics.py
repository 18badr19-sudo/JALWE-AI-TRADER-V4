"""Bounded retrieval diagnostics and retries, with no price or outcome inference."""
from collections import Counter
from datetime import timedelta
import hashlib
import json

from market_move_research import utc, observe_session

VERSION = 'market-move-coverage-v2'
MAX_ATTEMPTS = 3
RETRY_INTERVAL = timedelta(minutes=15)
RETRY_HORIZON = timedelta(hours=48)


def observation_due(row, now):
    close = utc(row['session']['close'])
    if now < close+timedelta(minutes=16):
        return False
    if row['status'] == 'PENDING':
        return True
    anchor = utc(row['frozen_at']) if row['role'] == 'JUMP_CASE' else close
    if (row['status'] not in {'PARTIAL_DATA', 'DATA_UNAVAILABLE'}
            or row['attempts'] >= MAX_ATTEMPTS or now > anchor+RETRY_HORIZON):
        return False
    # Upgrade previously unclassified observations once, without changing cohorts.
    if row['result'].get('diagnostic_version') != VERSION:
        return True
    return row['checked_at'] is None or now >= utc(row['checked_at'])+RETRY_INTERVAL


def diagnose_gaps(provider, symbol, result, now, *, cached=None):
    diagnostics = result.get('data_diagnostics', {})
    samples = []
    # Only genuinely unreturned minutes; invalid returned bars are a different issue.
    for stamp in diagnostics.get('first_absent_minutes', [])[:2]:
        if cached and stamp in cached:
            samples.append(cached[stamp])
            continue
        minute = utc(stamp)
        try:
            sample = provider.gap_trades(symbol, minute, now)
        except Exception as exc:
            sample = dict(minute=stamp, feed='sip', complete=False,
                          classification='TRADE_READ_UNAVAILABLE', error_type=type(exc).__name__)
        samples.append(sample)
    diagnostics['gap_trade_evidence'] = samples
    diagnostics['sample_scope'] = 'SAMPLED_MINUTES_ONLY_NOT_PRICE_COVERAGE'
    return samples


def recheck_absent_bars(provider, symbol, frame, session, result, now):
    """Recover at most two real SIP minute bars, never estimate a missing price."""
    import pandas as pd
    checked, recovered, failures = [], [], []
    pieces = [frame]
    for stamp in result.get('data_diagnostics', {}).get('first_absent_minutes', [])[:2]:
        minute = utc(stamp)
        window = dict(date=session['date'],open=minute.isoformat(),
                      close=(minute+timedelta(minutes=1)).isoformat())
        checked.append(stamp)
        try:
            raw = provider.intraday(symbol,window,now)
            quality = observe_session(raw,window)
            if quality['status'] != 'COMPLETE':
                continue
            # Discard the inclusive end row and any other evidence outside this minute.
            bar = raw.loc[raw.index == minute].copy()
            pieces.append(bar)
            recovered.append(stamp)
        except Exception as exc:
            from market.research_diagnostics import error_diagnostics
            failures.append(dict(minute=stamp,**error_diagnostics(exc)))
    merged = pd.concat(pieces) if recovered else frame.copy()
    merged.attrs = dict(frame.attrs)
    merged.attrs['targeted_bar_recheck'] = dict(attempted_minutes=checked,recovered_minutes=recovered,
        failures=failures,source_feed='sip',scope='REAL_PROVIDER_BARS_ONLY_NO_PRICE_INFERENCE')
    return merged


def aggregate(rows, now):
    incomplete = [r for r in rows if r['status'] in {'PARTIAL_DATA', 'DATA_UNAVAILABLE'}]
    reasons = Counter(r['result'].get('data_diagnostics', {}).get('reason', 'LEGACY_UNCLASSIFIED') for r in incomplete)
    sampled = {}
    for row in incomplete:
        for sample in row['result'].get('data_diagnostics', {}).get('gap_trade_evidence', []):
            key = (row['symbol'],row['session_day'],sample.get('minute'))
            sampled.setdefault(key,set()).add(sample.get('classification','UNKNOWN'))
    samples = Counter(next(iter(labels)) if len(labels)==1 else 'CONFLICTING_TRADE_SAMPLES'
                      for labels in sampled.values())
    windows = {}
    fields = ('expected_minutes','absent_provider_minutes','rejected_minutes','duplicate_rows')
    for row in incomplete:
        diagnostics = row['result'].get('data_diagnostics', {})
        if all(type(diagnostics.get(k)) is int and diagnostics[k] >= 0 for k in fields):
            key = (row['symbol'],row['session_day'])
            windows.setdefault(key,set()).add(tuple(diagnostics[k] for k in fields))
    consistent = [next(iter(values)) for values in windows.values() if len(values)==1]
    coverage_totals = dict(measured_symbol_days=len(consistent),
        absent_provider_minutes=sum(v[1] for v in consistent),
        rejected_minutes=sum(v[2] for v in consistent),duplicate_rows=sum(v[3] for v in consistent),
        conflicting_symbol_days=sum(len(v)>1 for v in windows.values()))
    return dict(version=VERSION, incomplete_episodes=len(incomplete), reasons=dict(reasons),
                unique_incomplete_symbol_days=len({(r['symbol'],r['session_day']) for r in incomplete}),
                unique_sampled_minutes=len(sampled),
                coverage_totals=coverage_totals,
                sampled_minutes=dict(samples), retry_eligible=sum(observation_due(r, now) for r in incomplete),
                max_attempts=MAX_ATTEMPTS, retry_horizon_hours=48,
                scope='SAMPLES_DO_NOT_EXPLAIN_ALL_GAPS_OR_INFER_PRICES')


def emit_evidence(store, rows, logger):
    """One bounded manifest per evidence revision; never export credentials or news text."""
    records = []
    for r in rows[-64:]:
        if r['status'] == 'PENDING':
            records.append(dict(kind='COHORT', symbol=r['symbol'], role=r['role'],
                prospective=bool(r['prospective']), session_day=r['session_day'], frozen_at=r['frozen_at'],
                evidence_day=r['features'].get('evidence_day'),
                available_after=r['features'].get('available_after'),
                evaluate_after=(utc(r['session']['close'])+timedelta(minutes=16)).isoformat()))
        else:
            records.append(dict(kind='OBSERVATION', symbol=r['symbol'], role=r['role'],
                prospective=bool(r['prospective']), session_day=r['session_day'], status=r['status'],
                attempts=r['attempts'], outcome=r['result'].get('outcome'),
                missing_minutes=r['result'].get('missing_minutes'),
                observed_minutes=r['result'].get('observed_minutes'),
                duplicates=r['result'].get('duplicates'),
                prefix_context=r['result'].get('prefix_context', {}),
                data_diagnostics=r['result'].get('data_diagnostics', {})))
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    if store.state('diagnostic_manifest') == digest:
        return
    for record in records:
        logger.info('MARKET_MOVE_EVIDENCE %s', json.dumps(record, sort_keys=True))
    store.state('diagnostic_manifest', digest)
