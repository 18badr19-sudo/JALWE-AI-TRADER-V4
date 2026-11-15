"""End-of-session ORB research; no execution or automatic promotion authority."""
from collections import Counter
from datetime import timedelta
import math
from market_move_research import utc

PROTOCOL = 'end-of-day-entry-review-v1'
ROLES = {'JUMP_CASE', 'ACTIVITY', 'CONTROL'}


def evaluate(data, session, now):
    base = dict(protocol=PROTOCOL, execution_authority=False,
                automatic_application=False, evaluated_at=now.isoformat())
    if now < utc(session['close']) + timedelta(minutes=16):
        return dict(base, state='WAITING_SESSION_DATA', scenarios={})
    from prior_day_entry_study import simulate
    result = simulate(data, session)
    return dict(base, simulation=result, state=result['state'])


def summary(rows, day):
    """Hindsight-selected jumps and frozen cohorts must never be pooled."""
    groups = {key: [] for key in ('RETROSPECTIVE_JUMPS', 'FROZEN_ACTIVITY', 'FROZEN_CONTROL')}
    identities = {}
    for row in rows:
        if row['session_day'] != day or row['role'] not in ROLES:
            continue
        key = (row['symbol'], row['session_day'], row['role'])
        identities.setdefault(key, []).append(row)
    duplicates = 0
    for candidates in identities.values():
        if len(candidates) != 1:
            duplicates += 1
            continue
        row = candidates[0]
        if row['role'] == 'JUMP_CASE':
            group = 'RETROSPECTIVE_JUMPS'
        else:
            try:
                valid = bool(row['prospective']) and utc(row['frozen_at']) < utc(row['session']['open'])
            except (KeyError, ValueError, TypeError):
                valid = False
            if not valid:
                continue
            group = 'FROZEN_' + row['role']
        groups[group].append(row)
    result = {}
    for group, selected in groups.items():
        states, samples = Counter(), []
        for row in selected:
            review = row.get('result', {}).get('end_of_day_entry_review', {})
            state = review.get('state', 'NOT_EVALUATED') if review.get('protocol') == PROTOCOL else 'NOT_EVALUATED'
            if row['status'] != 'COMPLETE':
                state = 'PENDING' if row['status'] == 'PENDING' else 'UNKNOWN_DATA'
            states[state] += 1
            if state == 'SIMULATED_EXIT':
                samples.append(review.get('simulation', {}))
        costs = {}
        for bps in ('10', '25'):
            values = [s.get('scenarios', {}).get(bps, {}).get('net_r') for s in samples]
            known = [v for v in values if type(v) in (float, int) and math.isfinite(v)]
            costs[bps] = dict(evaluated=len(known), missing=len(values)-len(known),
                positive=sum(v > 0 for v in known), negative=sum(v < 0 for v in known),
                mean_net_r=sum(known)/len(known) if known else None)
        result[group] = dict(total=len(selected), states=dict(states), costs=costs)
    return dict(protocol=PROTOCOL, session_day=day, groups=result,
                excluded_duplicate_identities=duplicates, automatic_application=False,
                execution_authority=False, scope='ORB_SIMULATION_NOT_NATIVE_JALWE_TRADES')


def report_lines(review):
    if not review:
        return []
    lines = [f"🔬 محاكاة ORB بعد الإغلاق — {review.get('session_day')}"]
    labels = {'RETROSPECTIVE_JUMPS': 'مرشحو القفزات',
              'FROZEN_ACTIVITY': 'نشاط مجمد', 'FROZEN_CONTROL': 'ضابطة مجمدة'}
    for key, group in review.get('groups', {}).items():
        if not group['total']:
            continue
        cost = group['costs']['25']
        mean = cost['mean_net_r']
        text = f'{mean:+.2f}R' if mean is not None else 'غير متاح'
        lines.append(f"{labels[key]}: {group['total']} | مخارج {cost['evaluated']} | متوسط {text}")
    lines.append('تكلفة 0.25% لكل جانب؛ عينات منفصلة، الناقص غير محسوم، ولا تطبيق تلقائي.')
    return lines
