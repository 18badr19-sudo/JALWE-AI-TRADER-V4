"""Read-only review of frozen rejected/watching decisions and recorded price paths.

No provider calls, fabricated fills, new evaluations, orders, or policy changes.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
STATES = {'REJECTED', 'WATCHING', 'READY_FOR_PAPER_EXECUTION'}


def obj(value):
    try:
        parsed = json.loads(value or '{}') if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def stamp(value):
    at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('Missing timezone')
    return at.astimezone(timezone.utc)


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def complete(row, result, *, sip=False):
    count = result.get('observed_minutes' if sip else 'observed_bars')
    return (row.get('status') == 'COMPLETE' and result.get('complete_path') is True
            and count == 60 and (not sip or result.get('observation_feed') == 'sip'))


def summarize(rows, sip_rows, entered, *, start_day, end_day, as_of):
    sip_by_key = {r['source_key']: r for r in sip_rows if r.get('source_kind') == 'DECISION'}
    groups, candidates, no_time = {}, [], 0
    for row in sorted(rows, key=lambda r: (r['decided_at'], r['decision_key'])):
        try:
            at = stamp(row['decided_at'])
            local = at.astimezone(NY)
        except (TypeError, ValueError):
            no_time += 1
            continue
        if at > as_of or not start_day <= local.date() <= end_day or row['source_state'] not in STATES:
            continue
        day = local.date().isoformat()
        group = groups.setdefault(day, dict(states=Counter(), sources=Counter(), statuses=Counter(),
                                            outcomes=Counter(), outcomes_by_state=defaultdict(Counter), reasons=defaultdict(Counter),
                                            symbols=set(), regular_clock=Counter()))
        state = row['source_state']
        group['states'][state] += 1
        group['symbols'].add(row['symbol'])
        group['reasons'][state][row['reason']] += 1
        regular = 570 <= local.hour * 60 + local.minute < 960
        group['regular_clock']['regular' if regular else 'outside_regular'] += 1
        original = obj(row.get('result_json'))
        srow = sip_by_key.get(row['decision_key'], {})
        sresult = obj(srow.get('result_json'))
        # Prefer a complete consolidated historical path. Never combine partial feeds.
        if complete(srow, sresult, sip=True):
            source, result = 'sip', sresult
        elif complete(row, original):
            source, result = row.get('data_feed') or 'unknown', original
        else:
            group['statuses'][row['status']] += 1
            group['outcomes']['INCONCLUSIVE'] += 1
            group['outcomes_by_state'][state]['INCONCLUSIVE'] += 1
            continue
        group['sources'][source] += 1
        levels = obj(result.get('levels'))
        outcome = levels.get('outcome') or 'NO_VALID_SETUP_LEVELS'
        group['outcomes'][outcome] += 1
        group['outcomes_by_state'][state][outcome] += 1
        if state not in {'REJECTED', 'WATCHING'} or outcome != 'TARGET_FIRST':
            continue
        trigger, stop, target, activation = (number(levels.get(k)) for k in
            ('trigger_price', 'stop_price', 'target_1', 'activation_close'))
        try:
            activated = stamp(levels['activated_at'])
            touched = stamp(levels['target_touch_at'])
            stopped = stamp(levels['stop_touch_at']) if levels.get('stop_touch_at') else None
            ended = stamp(row['window_end'])
            started = stamp(row['window_start'])
            valid = (None not in (trigger, stop, target, activation)
                     and target > trigger > stop > 0 and trigger <= activation < target
                     and at <= started < activated <= touched < ended
                     and ended <= as_of and (stopped is None or touched < stopped))
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            group['outcomes']['TARGET_FIRST'] -= 1
            group['outcomes_by_state'][state]['TARGET_FIRST'] -= 1
            group['outcomes']['INVALID_TARGET_FIRST_EVIDENCE'] += 1
            group['outcomes_by_state'][state]['INVALID_TARGET_FIRST_EVIDENCE'] += 1
            continue
        decision = obj(obj(row['payload_json']).get('jalwe'))
        metadata = obj(decision.get('metadata'))
        conflict = (complete(row, original) and complete(srow, sresult, sip=True)
                    and obj(original.get('levels')).get('outcome') != outcome)
        candidates.append(dict(symbol=row['symbol'], date=day, state=state, reason=row['reason'],
            strategy=row.get('strategy'), decision_key=row['decision_key'],
            research_version=row['research_version'], decided_at_ny=local.isoformat(),
            activated_at_ny=activated.astimezone(NY).isoformat(),
            target_touch_at_ny=touched.astimezone(NY).isoformat(),
            trigger=trigger, stop=stop, target_1=target, activation_close=activation,
            theoretical_target=bool(levels.get('theoretical_target')), observation_feed=source,
            decision_feed=row.get('data_feed'), regular_clock=regular,
            symbol_entered_same_day=(row['symbol'], day) in entered,
            cross_feed_outcome_disagreement=bool(conflict),
            feature_diagnostics=obj(metadata.get('feature_diagnostics')),
            opportunity_evidence=obj(metadata.get('opportunity_evidence')),
            execution_feasibility='NOT_ESTABLISHED_FROM_BARS',
            target_return_from_activation_pct=round((target / activation - 1) * 100, 4)))
    # Earliest qualifying decision per report episode, never sum repeated returns.
    unique = {}
    for item in candidates:
        key = (item['symbol'], item['date'], item['research_version'])
        unique.setdefault(key, item)
    return dict(protocol='read-only-missed-opportunity-review-v1', as_of=as_of.isoformat(),
        start_day=start_day.isoformat(), end_day=end_day.isoformat(),
        scope='RESEARCH_PATHS_NOT_EXECUTED_PROFIT', malformed_timestamps=no_time,
        days={day: {**g, 'symbols': len(g['symbols']),
                    'reasons': {state: dict(counts) for state, counts in g['reasons'].items()}}
              for day, g in sorted(groups.items())},
        target_first_rejected_watching_decisions=len(candidates),
        target_first_report_episodes=len(unique),
        target_first_symbol_days=len({(c['symbol'], c['date']) for c in candidates}),
        candidates=list(unique.values()),
        limits=['60 valid 1m bars required', 'activation is completed 1m close',
                'same-bar order unresolved', 'bar paths do not prove fill, spread, budget or entry eligibility',
                'regular_clock does not verify an exchange calendar',
                'same-day execution cannot be attributed to an exact research episode'])


def read_audit(database_path, *, now=None, days=7):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end_day = now.astimezone(NY).date()
    start_day = end_day - timedelta(days=max(1, min(int(days), 14)) - 1)
    start = datetime.combine(start_day, datetime.min.time(), NY).astimezone(timezone.utc).isoformat()
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), NY).astimezone(timezone.utc).isoformat()
    # Mode ro and query_only avoid migrations, PRAGMAs, schema changes, and broker access.
    with sqlite3.connect(Path(database_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'decision_outcome_memory' not in tables:
            return {'status': 'UNAVAILABLE', 'reason': 'NO_RECORDED_DECISIONS'}
        rows = [dict(r) for r in conn.execute('''SELECT * FROM decision_outcome_memory
            WHERE decided_at >= ? AND decided_at < ? ORDER BY decided_at, decision_key''', (start, end))]
        sips = [] if 'delayed_sip_research' not in tables else [dict(r) for r in conn.execute('''
            SELECT * FROM delayed_sip_research WHERE source_kind='DECISION'
            AND decided_at >= ? AND decided_at < ?''', (start, end))]
        entered = set()
        if 'managed_trades' in tables:
            for r in conn.execute('SELECT symbol, metadata_json FROM managed_trades'):
                try:
                    filled = stamp(obj(r['metadata_json'])['entry_filled_at'])
                    if filled <= now:
                        entered.add((r['symbol'], filled.astimezone(NY).date().isoformat()))
                except (KeyError, ValueError, TypeError):
                    pass
    return summarize(rows, sips, entered, start_day=start_day, end_day=end_day, as_of=now)


def emit_audit(database_path, *, now=None):
    """Bounded structured log snapshot for review, no Telegram message."""
    try:
        audit = read_audit(database_path, now=now)
        candidates = audit.pop('candidates', [])
        days = audit.pop('days', {})
        audit['day_dates'] = list(days)
        print('MISSED_OPPORTUNITY_AUDIT_SUMMARY ' + json.dumps(audit, ensure_ascii=True), flush=True)
        for day, group in days.items():
            for state, reasons in group['reasons'].items():
                top = sorted(reasons.items(), key=lambda r: (-r[1], r[0]))[:10]
                group['reasons'][state] = dict(top)
                group.setdefault('unlisted_reason_decisions', {})[state] = sum(reasons.values()) - sum(n for _, n in top)
            print('MISSED_OPPORTUNITY_AUDIT_DAY ' + json.dumps({'date': day, **group}, ensure_ascii=True), flush=True)
        for item in candidates[:100]:
            print('MISSED_OPPORTUNITY_AUDIT_CANDIDATE ' + json.dumps(item, ensure_ascii=True), flush=True)
        print('MISSED_OPPORTUNITY_AUDIT_END ' + json.dumps({'candidate_rows': len(candidates),
            'emitted_rows': min(len(candidates), 100), 'truncated': len(candidates) > 100}), flush=True)
    except Exception as exc:
        print('MISSED_OPPORTUNITY_AUDIT_ERROR ' + json.dumps({'error_type': type(exc).__name__}), flush=True)
