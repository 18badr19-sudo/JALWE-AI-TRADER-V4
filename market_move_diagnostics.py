"""Bounded retrieval diagnostics and retries, with no price or outcome inference."""
from collections import Counter
from datetime import timedelta
import hashlib
import json

from market_move_research import utc

VERSION = 'market-move-coverage-v1'
MAX_ATTEMPTS = 3
RETRY_INTERVAL = timedelta(minutes=15)
RETRY_HORIZON = timedelta(hours=48)


def observation_due(row, now):
    close = utc(row['session']['close'])
    if now < close+timedelta(minutes=16):
        return False
    if row['status'] == 'PENDING':
        return True
    if (row['status'] not in {'PARTIAL_DATA', 'DATA_UNAVAILABLE'}
            or row['attempts'] >= MAX_ATTEMPTS or now > close+RETRY_HORIZON):
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


def aggregate(rows, now):
    incomplete = [r for r in rows if r['status'] in {'PARTIAL_DATA', 'DATA_UNAVAILABLE'}]
    reasons = Counter(r['result'].get('data_diagnostics', {}).get('reason', 'LEGACY_UNCLASSIFIED') for r in incomplete)
    samples = Counter(sample.get('classification', 'UNKNOWN') for r in incomplete
                      for sample in r['result'].get('data_diagnostics', {}).get('gap_trade_evidence', []))
    return dict(version=VERSION, incomplete_episodes=len(incomplete), reasons=dict(reasons),
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
                data_diagnostics=r['result'].get('data_diagnostics', {})))
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    if store.state('diagnostic_manifest') == digest:
        return
    for record in records:
        logger.info('MARKET_MOVE_EVIDENCE %s', json.dumps(record, sort_keys=True))
    store.state('diagnostic_manifest', digest)
