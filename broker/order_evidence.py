"""Interpret broker evidence before it can change an owned whole-share order."""
from decimal import Decimal, InvalidOperation
import math

from core.models import OrderStatus


TERMINAL_RAW_STATUSES = frozenset({'filled', 'canceled', 'cancelled', 'expired', 'rejected'})


def text(value):
    return str(getattr(value, 'value', value) or '').strip()


def map_order_status(value):
    status = text(value).lower()
    if status == 'filled':
        return OrderStatus.FILLED
    if status == 'partially_filled':
        return OrderStatus.PARTIALLY_FILLED
    if status in {'canceled', 'cancelled', 'expired'}:
        return OrderStatus.CANCELED
    if status == 'rejected':
        return OrderStatus.REJECTED
    if status in {'accepted', 'pending_new', 'accepted_for_bidding', 'new', 'held'}:
        return OrderStatus.ACCEPTED
    # Paused, replaced, pending and unknown states do not establish cancellation.
    # Keep ownership unresolved until the original order is safely reconciled.
    return OrderStatus.SUBMITTED


def whole_shares(value, *, allow_zero=False):
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or number != number.to_integral_value()
                or (not allow_zero and number == 0)):
            raise ValueError('Broker quantity must be finite nonnegative whole shares.')
        return int(number)
    except (InvalidOperation, TypeError, OverflowError) as exc:
        raise ValueError('Invalid broker share quantity.') from exc


def validate_order_snapshot(raw, *, symbol, side, quantity, order_id=None, client_order_id=None):
    """Reject contradictory identity or fills before mutation or recovery cancellation.

    Older response adapters may omit redundant symbol/side/qty/client fields;
    when present they must match the persisted intent. Broker ID is mandatory.
    The owned order's requested quantity always bounds confirmed fills.
    """
    broker_id = text(getattr(raw, 'id', None))
    if not broker_id or (order_id and broker_id != text(order_id)):
        raise ValueError('Broker order ID does not match the owned order.')
    for name, expected, normalize in (
        ('symbol', symbol, str.upper), ('side', side, str.lower),
        ('client_order_id', client_order_id, lambda s: s),
    ):
        received = text(getattr(raw, name, None))
        if received and expected and normalize(received) != normalize(text(expected)):
            raise ValueError('Broker order '+name+' does not match the owned order.')
    requested = whole_shares(quantity)
    received_qty = getattr(raw, 'qty', None)
    if received_qty is not None and whole_shares(received_qty) != requested:
        raise ValueError('Broker requested quantity does not match the owned order.')
    raw_filled = getattr(raw, 'filled_qty', None)
    filled = whole_shares(0 if raw_filled is None else raw_filled, allow_zero=True)
    if filled > requested:
        raise ValueError('Broker fill exceeds the owned requested quantity.')
    if text(getattr(raw, 'status', None)).lower() == 'filled' and filled != requested:
        raise ValueError('FILLED status lacks the full confirmed quantity.')
    price = None
    if filled:
        raw_price = getattr(raw, 'filled_avg_price', None)
        if isinstance(raw_price, bool):
            raise ValueError('Broker fill average must be a finite positive price.')
        try:
            price = float(raw_price)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('Broker fill average is unavailable.') from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError('Broker fill average must be a finite positive price.')
    return dict(order_id=broker_id, quantity=requested, filled_quantity=filled, filled_price=price)
