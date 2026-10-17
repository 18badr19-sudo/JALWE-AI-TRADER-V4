"""Descriptive, market-relative factor evidence; never a trading policy.

SPY open-to-close subtraction assumes beta=1. It is neither estimated alpha,
an executable return, nor a causal explanation. Intervals resample blocks of
whole days rather than treating correlated stocks as independent observations.
"""
import hashlib
import json
import math
import random
from collections import defaultdict

from market_move_research import utc

PROTOCOL = 'market-relative-factor-evidence-v1'
BENCHMARK = 'SPY'
MIN_DAYS, MIN_PAIRS, BLOCK_DAYS, RESAMPLES = 20, 60, 5, 2000
SPEC = hashlib.sha256(json.dumps(dict(protocol=PROTOCOL, benchmark=BENCHMARK,
    min_days=MIN_DAYS, min_pairs=MIN_PAIRS, block_days=BLOCK_DAYS,
    resamples=RESAMPLES, endpoint='DAILY_CLOSE_MINUS_OPEN_BETA_ONE'), sort_keys=True).encode()).hexdigest()
KEYWORDS = ('earnings', 'fda', 'merger', 'acquisition', 'offering', 'contract', 'guidance')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def mean(values):
    return math.fsum(v / len(values) for v in values) if values else None


def close_return(bar):
    if not isinstance(bar, dict) or not all(number(bar.get(k)) and bar[k] > 0 for k in ('o', 'c')):
        return None
    value = 100 * (bar['c'] / bar['o'] - 1)
    return value if number(value) else None


def observe_returns(records, day, indexed):
    return {r['symbol']: close_return(indexed.get(r['symbol'], {}).get(day)) for r in records}


def interval(daily_values, observations):
    """Circular moving-block bootstrap of an equal-day-weighted mean.

    Missing days are not invented. Blocks cover consecutive *observed* days;
    temporal gaps, short history and multiple exploratory comparisons remain
    limitations. No confidence interval authorizes promotion of a strategy.
    """
    values = sorted((day, value) for day, value in daily_values if number(value))
    if len({day for day, _ in values}) != len(values):
        raise ValueError('DUPLICATE_STATISTICAL_DAY')
    data = [value for _, value in values]
    result = dict(days=len(data), observations=observations, mean=mean(data),
        lower=None, upper=None, confidence=.95, method='CIRCULAR_MOVING_BLOCK_BOOTSTRAP_DAYS',
        block_days=BLOCK_DAYS, resamples=RESAMPLES, day_weighting='EQUAL_OBSERVED_DAY',
        status='INSUFFICIENT_DAYS_OR_PAIRS')
    if len(data) < MIN_DAYS or observations < MIN_PAIRS:
        return result
    rng = random.Random(hashlib.sha256(json.dumps([SPEC, values], sort_keys=True).encode()).hexdigest())
    estimates = []
    for _ in range(RESAMPLES):
        sampled = []
        while len(sampled) < len(data):
            start = rng.randrange(len(data))
            sampled.extend(data[(start + j) % len(data)] for j in range(BLOCK_DAYS))
        estimates.append(mean(sampled[:len(data)]))
    estimates.sort()
    def quantile(p):
        x = (len(estimates) - 1) * p
        lo = int(x); hi = min(lo + 1, len(estimates) - 1)
        fraction = x - lo
        return estimates[lo] * (1-fraction) + estimates[hi] * fraction
    result.update(status='DESCRIPTIVE_INTERVAL_ONLY', lower=quantile(.025), upper=quantile(.975))
    return result


def measure(records, day, outcomes, returns, market_return, pairs_by_rule):
    rules = {}
    records = [r for r in records if r['symbol'] != BENCHMARK]
    allowed = {r['symbol'] for r in records}
    for rule, pairs in pairs_by_rule.items():
        pairs = [(a, b) for a, b in pairs if a in allowed and b in allowed]
        known = [(a, b) for a, b in pairs if type(outcomes.get(a)) is bool and type(outcomes.get(b)) is bool]
        covered = [(a, b) for a, b in pairs if number(returns.get(a)) and number(returns.get(b))]
        complete = covered if number(market_return) else []
        # These are descriptive factor prevalences, including non-winners.
        table = {}
        for label, hit in (('reached_daily_high_8', True), ('did_not_reach_daily_high_8', False)):
            members = [r for r in records if outcomes.get(r['symbol']) is hit]
            yes = sum(r['signals'][rule] is True for r in members)
            table[label] = dict(known=len(members), factor_present=yes, factor_absent=len(members)-yes,
                prevalence=yes/len(members) if members else None)
        rules[rule] = dict(pairs_selected=len(pairs), barrier_pairs=len(known),
            barrier_difference_pp=mean([100*(int(outcomes[a])-int(outcomes[b])) for a, b in known]),
            return_pairs=len(complete), missing_stock_pairs=len(pairs)-len(covered),
            missing_benchmark_pairs=len(covered) if not number(market_return) else 0,
            matching_excess_pct=mean([returns[a]-market_return for a, b in complete]),
            control_excess_pct=mean([returns[b]-market_return for a, b in complete]),
            paired_excess_difference_pp=mean([returns[a]-returns[b] for a, b in complete]),
            outcome_groups=table)
    return dict(day=day, benchmark_return_pct=market_return, rules=rules)


def aggregate(days, rule_names):
    result = dict(days=len(days), benchmark_available_days=sum(number(d['benchmark_return_pct']) for d in days), rules={})
    for rule in rule_names:
        rows = [(d['day'], d['rules'][rule]) for d in days]
        count = lambda key: sum(r[key] for _, r in rows)
        groups = {}
        for group in ('reached_daily_high_8', 'did_not_reach_daily_high_8'):
            totals = {k: sum(r['outcome_groups'][group][k] for _, r in rows)
                      for k in ('known', 'factor_present', 'factor_absent')}
            totals['prevalence'] = totals['factor_present']/totals['known'] if totals['known'] else None
            groups[group] = totals
        result['rules'][rule] = dict(pairs_selected=count('pairs_selected'),
            return_pairs=count('return_pairs'), missing_stock_pairs=count('missing_stock_pairs'),
            missing_benchmark_pairs=count('missing_benchmark_pairs'),
            matching_excess_pct=mean([r['matching_excess_pct'] for _, r in rows if number(r['matching_excess_pct'])]),
            control_excess_pct=mean([r['control_excess_pct'] for _, r in rows if number(r['control_excess_pct'])]),
            barrier_difference_pp=interval([(d, r['barrier_difference_pp']) for d, r in rows], count('barrier_pairs')),
            paired_excess_difference_pp=interval([(d, r['paired_excess_difference_pp']) for d, r in rows], count('return_pairs')),
            outcome_groups=groups)
    return result


def report_lines(benchmark, news):
    lines = []
    if benchmark.get('statistics_method', {}).get('protocol') == PROTOCOL:
        def fmt(value, suffix='%'):
            return f'{value:+.2f}{suffix}' if number(value) else 'N/A'
        def bounds(value):
            if value.get('status') != 'DESCRIPTIVE_INTERVAL_ONLY':
                return 'العينة غير كافية لفاصل ثقة'
            return f"فاصل 95% [{value['lower']:+.2f}، {value['upper']:+.2f}] نقطة مئوية"
        lines.append('📐 إحصاء العوامل مقابل السوق — وصف بحثي')
        for name, part in (('تاريخي', benchmark['retrospective']['all_days']),
                           ('مستقبلي بعد تعريف القياس', benchmark['forward']['evidence'])):
            data = part.get('statistics', {}); r = data.get('rules', {}).get('ALL_THREE', {})
            if not r:
                continue
            edge = r['paired_excess_difference_pp']; hit = r['barrier_difference_pp']
            lines.append(f"  {name}: أيام {data['days']} | أيام SPY معروفة {data['benchmark_available_days']} | أزواج عائد مكتملة {r['return_pairs']}/{r['pairs_selected']}")
            lines.append(f"  متوسط الحركة ناقص SPY: مطابقة {fmt(r['matching_excess_pct'])} | ضابطة {fmt(r['control_excess_pct'])}؛ فرق {fmt(edge['mean'],' نقطة مئوية')}؛ {bounds(edge)}")
            lines.append(f"  فرق تكرار بلوغ أعلى يومي +8%: {fmt(hit['mean'],' نقطة مئوية')}؛ {bounds(hit)}")
        lines.append('طرح SPY يفترض بيتا=1؛ حركة يومية بلا تكاليف وليست ربح صفقة. الفواصل تتطلب 20 يومًا و60 زوجًا، بكتل من 5 أيام مرصودة؛ لا تثبت سببية أو أفضلية تداول.')
    if news.get('protocol') == PROTOCOL:
        g = news['groups']; known = sum(x['known_paths'] for x in g.values())
        lines.append(f"📰 أخبار العينات الأصلية المجمدة: {news['symbol_days']} سهم/جلسة | مسارات مكتملة غير ملتبسة {known} | تنتظر {news['pending']} | ناقصة {news['incomplete']} | ملتبسة {news['ambiguous']} | غير صالحة/متعارضة {news['invalid']+news['conflicts']}")
        lines.append(f"  أخبار محفوظة: {g['HAS_NEWS']['known_paths']} (صعود 8% أولًا {g['HAS_NEWS']['up8_first']}) | بلا خبر في نافذة المصدر المحفوظة: {g['NO_NEWS_IN_FROZEN_LOOKUP']['known_paths']} (صعود أولًا {g['NO_NEWS_IN_FROZEN_LOOKUP']['up8_first']}) | أخبار مجهولة {g['UNKNOWN_NEWS']['known_paths']}")
        lines.append('سياق قبل الافتتاح فقط؛ غياب خبر في المصدر لا ينفي وجود أخبار. الكلمات الغائبة من قائمة مختصرة تبقى مجهولة؛ المقارنة وصفية ولا تثبت سبب القفزة.')
    return lines


def news_evidence(row):
    """Use only original pre-open lookup evidence, never post-move news.

    The provider stores at most five articles, so missing keywords are unknown
    whenever the lookup had more articles than were retained.
    """
    unknown = ('UNKNOWN_NEWS', {k: None for k in KEYWORDS})
    try:
        news = row['features'].get('pre_session_news', {})
        frozen, opening = utc(row['frozen_at']), utc(row['session']['open'])
        cutoff = utc(news['cutoff']); count = news['count']; articles = news['articles']
        if not (news.get('status') == 'AVAILABLE' and news.get('historical_updated_content_filtered') is True
                and type(count) is int and count >= 0 and isinstance(articles, list)
                and len(articles) <= count and cutoff <= frozen < opening):
            return unknown
        ids = set(); present = set()
        for article in articles:
            if article['id'] in ids or not cutoff.timestamp()-86400 <= utc(article['created_at']).timestamp() <= utc(article['updated_at']).timestamp() <= cutoff.timestamp():
                return unknown
            ids.add(article['id'])
            words = article['keywords']
            if not isinstance(words, list) or any(k not in KEYWORDS for k in words):
                return unknown
            present.update(words)
        if count and not articles:
            return unknown
        words = {k: True if k in present else False if len(articles) == count else None for k in KEYWORDS}
        return ('HAS_NEWS' if count else 'NO_NEWS_IN_FROZEN_LOOKUP', words)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return unknown


def news_summary(episodes):
    groups = defaultdict(list)
    for r in episodes:
        if r['prospective'] and r['role'] in {'ACTIVITY', 'CONTROL'}:
            groups[(r['symbol'], r['session_day'])].append(r)
    result = dict(protocol=PROTOCOL, scope='ORIGINAL_FROZEN_FORWARD_COHORT_DESCRIPTION_NOT_CAUSAL',
        symbol_days=len(groups), conflicts=0, pending=0, incomplete=0, ambiguous=0, invalid=0,
        groups={k:dict(known_paths=0,up8_first=0) for k in ('HAS_NEWS','NO_NEWS_IN_FROZEN_LOOKUP','UNKNOWN_NEWS')},
        keywords={k:dict(present=0,absent=0,unknown=0,present_up8_first=0,absent_up8_first=0) for k in KEYWORDS})
    day_groups = defaultdict(lambda: defaultdict(list))
    for rows in groups.values():
        row = rows[0]
        if any((r['features'],r['result'],r['frozen_at']) != (row['features'],row['result'],row['frozen_at']) for r in rows[1:]):
            result['conflicts'] += 1; continue
        if row['status'] == 'PENDING':
            result['pending'] += 1; continue
        path = row['result']; outcome = path.get('outcome')
        if row['status'] != 'COMPLETE' or path.get('status') != 'COMPLETE':
            result['incomplete'] += 1; continue
        try:
            expected = (utc(row['session']['close'])-utc(row['session']['open'])).total_seconds()/60
        except (KeyError, TypeError, ValueError, OverflowError):
            result['invalid'] += 1; continue
        if not (all(type(path.get(k)) is int for k in ('expected_minutes','observed_minutes','missing_minutes','duplicates'))
                and path['expected_minutes'] == expected and expected > 0
                and path.get('observed_minutes') == path['expected_minutes']
                and path.get('missing_minutes') == 0 and path.get('duplicates') == 0):
            result['invalid'] += 1; continue
        if outcome == 'AMBIGUOUS':
            result['ambiguous'] += 1; continue
        if outcome not in {'UP8_FIRST','DOWN3_FIRST','NO_BARRIER'}:
            result['invalid'] += 1; continue
        label, words = news_evidence(row); hit = int(outcome == 'UP8_FIRST')
        result['groups'][label]['known_paths'] += 1
        result['groups'][label]['up8_first'] += hit
        day_groups[row['session_day']][label].append(hit)
        for key, value in words.items():
            r = result['keywords'][key]
            r['unknown' if value is None else 'present' if value else 'absent'] += 1
            if value is not None:
                r['present_up8_first' if value else 'absent_up8_first'] += hit
    days, comparable = [], 0
    for day, values in day_groups.items():
        yes, no = values['HAS_NEWS'], values['NO_NEWS_IN_FROZEN_LOOKUP']
        if yes and no:
            days.append((day,100*(mean(yes)-mean(no))))
            comparable += len(yes)+len(no)
    result['news_frequency_difference_pp'] = interval(days, comparable)
    result['automatic_application'] = False
    return result
