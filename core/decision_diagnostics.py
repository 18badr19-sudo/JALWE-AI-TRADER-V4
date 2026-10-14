"""Display saved decision evidence without inferring additional rejection gates."""
from datetime import datetime, timezone
import math
import re
from zoneinfo import ZoneInfo


def saudi_time(value):
    try:
        at = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        # SQLite CURRENT_TIMESTAMP is UTC despite omitting the offset.
        at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
        return at.astimezone(ZoneInfo('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M السعودية')
    except (ValueError,TypeError):
        return 'وقت غير متاح'


def numeric(value):
    if type(value) not in (int,float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def decision_details(decision):
    metadata = decision.get('metadata',{})
    metadata = metadata if isinstance(metadata,dict) else {}
    lines = []
    feature = metadata.get('feature_diagnostics',{})
    if isinstance(feature,dict):
        age,limit = numeric(feature.get('latest_bar_age_minutes')),numeric(feature.get('max_bar_age_minutes'))
        if age is not None and limit is not None:
            lines.append(f'  عمر الشمعة: {age:.1f} دقيقة | الحد {limit:g}')
    router = metadata.get('strategy_router_evidence',{})
    if isinstance(router,dict) and router.get('approved') is False:
        score,limit = numeric(router.get('score')),numeric(router.get('minimum_score'))
        if score is not None and limit is not None:
            lines.append(f'  الموجّه: درجة {score:.2f} | المطلوب {limit:g}')
    if 'strategy router' in str(decision.get('reason','')).lower():
        regime = decision.get('market_regime')
        if isinstance(regime,str) and regime:
            lines.append('  حالة السوق: '+regime[:40])
        if not any('الموجّه:' in s for s in lines):
            # Legacy decisions already saved their router warning. Never
            # invent a threshold from today's config for an old decision.
            warnings = decision.get('warnings',[])
            for warning in warnings if isinstance(warnings,list) else []:
                match = re.fullmatch(r'Best strategy did not meet regime minimum score ([0-9]+(?:\.[0-9]+)?)\.',str(warning))
                if match:
                    lines.append('  الموجّه: لم يجتز الحد المسجل '+match.group(1))
                    break
    return lines
