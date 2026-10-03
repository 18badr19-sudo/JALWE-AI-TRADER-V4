"""Pre/post-market eligibility and fresh executable SELL limits."""
import math
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_CEILING
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')


def utc_now():
    return datetime.now(timezone.utc)


def exit_session(broker, *, enabled=True):
    if broker.market_is_open():
        return 'REGULAR'
    if not enabled:
        return 'CLOSED'
    clock = broker.get_clock()
    timestamp = getattr(clock, 'timestamp', None)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        return 'CLOSED'
    age = (utc_now() - timestamp.astimezone(timezone.utc)).total_seconds()
    if not -5 <= age <= 60:
        raise ValueError('Broker clock is stale.')
    local = timestamp.astimezone(NY)
    calendar = broker.get_calendar_day(local.date())
    if calendar is None:
        return 'CLOSED'
    opening, closing = calendar.open, calendar.close
    if not isinstance(opening, datetime) or not isinstance(closing, datetime):
        return 'CLOSED'
    opening = opening.replace(tzinfo=NY) if opening.tzinfo is None else opening.astimezone(NY)
    closing = closing.replace(tzinfo=NY) if closing.tzinfo is None else closing.astimezone(NY)
    if opening.date() != local.date() or closing.date() != local.date():
        raise ValueError('Calendar does not match the broker date.')
    pre_open = datetime.combine(local.date(), time(4), NY)
    post_close = datetime.combine(local.date(), time(20), NY)
    if pre_open <= local < opening or closing <= local < post_close:
        return 'EXTENDED'
    return 'CLOSED'


def sell_limit(quote, *, floor=None, slippage_pct=0.35):
    stamp = quote.get('timestamp')
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError('Extended-session quote has no trustworthy timestamp.')
    age = (utc_now() - stamp.astimezone(timezone.utc)).total_seconds()
    if not -5 <= age <= 30:
        raise ValueError('Extended-session quote is stale.')
    bid, ask = float(quote.get('bid') or 0), float(quote.get('ask') or 0)
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
        raise ValueError('Extended-session bid/ask is invalid.')
    tolerance = float(slippage_pct)
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 5:
        raise ValueError('Exit slippage cap is invalid.')
    price = Decimal(str(bid)) * (Decimal('1') - Decimal(str(tolerance))/100)
    if floor is not None:
        floor = float(floor)
        if not math.isfinite(floor) or floor <= 0:
            raise ValueError('Target exit floor is invalid.')
        price = max(price, Decimal(str(floor)))
    tick = Decimal('0.01') if price >= 1 else Decimal('0.0001')
    return float(price.quantize(tick, rounding=ROUND_CEILING))


def prepare_exit_context(execution, market_data, trade, decision):
    from core.config import settings
    session = exit_session(execution.broker, enabled=settings.EXTENDED_EXIT_ENABLED)
    if session != 'EXTENDED':
        return {'session': session, 'extended_hours': False}
    floors = {'TAKE_PROFIT_1': trade.target_1, 'TAKE_PROFIT_2': trade.target_2,
              'TAKE_PROFIT_3': trade.target_3}
    quote = market_data.get_execution_quote(trade.symbol)
    return {'session': session, 'extended_hours': True,
            'limit_price': sell_limit(quote, floor=floors.get(decision.action.value),
                                     slippage_pct=settings.MAX_EXIT_SLIPPAGE_PCT),
            'quote_at': quote['timestamp'].isoformat()}


def settle_exit(order, execution, reconciliation, *, extended_hours=False):
    from core.models import OrderStatus
    settled = reconciliation.wait_for_terminal_state(order, timeout_seconds=30.0, poll_interval_seconds=1.0)
    if extended_hours and settled.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
        execution.broker.cancel_order(settled.order_id)
        # Cancellation may race another fill; always fetch and apply its cumulative result.
        settled = reconciliation.wait_for_terminal_state(settled, timeout_seconds=5.0, poll_interval_seconds=0.5)
    return settled
