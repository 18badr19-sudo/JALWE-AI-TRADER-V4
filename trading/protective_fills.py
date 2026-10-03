"""Apply cumulative protective-stop fills before another SELL can use local quantity."""
import math
from datetime import datetime, timezone

from core.models import OrderStatus
from trading.trade_manager import TradeStage

CANCELED_STATUSES = {'canceled', 'cancelled', 'expired'}
REJECTED_STATUSES = {'rejected'}
TERMINAL_STATUSES = CANCELED_STATUSES | REJECTED_STATUSES | {'filled'}


def _quantity(value):
    number = float(value or 0)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError('Protective stop fill quantity must be finite, nonnegative whole shares.')
    return int(number)


def apply_protective_stop_snapshot(database, trade_id, trade, raw):
    metadata = dict(trade.metadata or {})
    order_id = str(metadata.get('protective_stop_order_id') or '').strip()
    if not order_id:
        raise ValueError('Protective stop has no persisted broker identity.')
    raw_id = str(getattr(raw, 'id', '') or '')
    raw_symbol = str(getattr(raw, 'symbol', '') or '').strip().upper()
    raw_side = getattr(raw, 'side', '')
    raw_side = str(getattr(raw_side, 'value', raw_side) or '').lower()
    if ((raw_id and raw_id != order_id) or
            (raw_symbol and raw_symbol != trade.symbol) or
            (raw_side and raw_side != 'sell')):
        raise ValueError('Protective stop snapshot identity does not match the managed trade.')
    raw_status = getattr(raw, 'status', '')
    raw_status = str(getattr(raw_status, 'value', raw_status) or '').lower()
    filled = _quantity(getattr(raw, 'filled_qty', 0))
    previous = _quantity(metadata.get('protective_stop_applied_qty', filled if metadata.get('protective_stop_fill_applied') else 0))
    if filled < previous:
        raise ValueError('Protective stop cumulative fills moved backwards.')
    requested = metadata.get('protective_stop_quantity')
    if requested is not None and filled > _quantity(requested):
        raise ValueError('Protective stop cumulative fill exceeds requested quantity.')
    new_qty = filled - previous
    status = (OrderStatus.FILLED if raw_status == 'filled' else
              OrderStatus.CANCELED if raw_status in CANCELED_STATUSES else
              OrderStatus.REJECTED if raw_status in REJECTED_STATUSES else
              OrderStatus.PARTIALLY_FILLED if raw_status == 'partially_filled' else
              OrderStatus.ACCEPTED)
    active = raw_status not in TERMINAL_STATUSES
    average = getattr(raw, 'filled_avg_price', None)
    average = float(average) if average is not None else None
    realized = 0.0
    incremental_price = None
    if new_qty:
        if average is None or not math.isfinite(average) or average <= 0:
            raise ValueError('Protective stop fill price is unavailable.')
        if new_qty > int(trade.remaining_quantity):
            raise ValueError('Protective stop fill exceeds local remaining quantity.')
        cumulative_notional = filled * average
        previous_notional = float(metadata.get('protective_stop_applied_notional', 0.0))
        if previous and previous_notional <= 0:
            raise ValueError('Previous protective stop proceeds unavailable.')
        proceeds = cumulative_notional - previous_notional
        if not math.isfinite(proceeds) or proceeds <= 0:
            raise ValueError('Invalid protective stop cumulative proceeds.')
        incremental_price = proceeds / new_qty
        realized = proceeds - new_qty * trade.entry_price
        trade.remaining_quantity -= new_qty
        trade.realized_quantity += new_qty
        metadata['realized_pnl'] = float(metadata.get('realized_pnl') or 0.0) + realized
        metadata['protective_stop_applied_qty'] = filled
        metadata['protective_stop_applied_notional'] = cumulative_notional
    if int(trade.remaining_quantity) == 0:
        trade.trailing_active = False
        trade.stage = TradeStage.CLOSED
    metadata.update(protective_stop_status=status.value, protective_stop_active=active,
                    protective_stop_fill_applied=raw_status == 'filled')
    trade.metadata = metadata
    if new_qty:
        filled_at = getattr(raw, 'filled_at', None) or datetime.now(timezone.utc)
        event_time = filled_at.isoformat() if hasattr(filled_at, 'isoformat') else str(filled_at)
        database.record_strategy_pnl_event(
            trade_state=trade, event_key=f'{order_id}:{filled}', trade_id=trade_id,
            order_id=order_id, symbol=trade.symbol, action='BROKER_PROTECTIVE_STOP',
            quantity=new_qty, fill_price=incremental_price, entry_price=trade.entry_price,
            realized_pnl=realized, event_time=event_time,
            metadata={'source': 'PROTECTIVE_STOP_RECONCILIATION', 'protective_stop': True},
        )
    else:
        database.save_managed_trade(trade_id, trade)
    return dict(checked=True, active=active, filled=raw_status == 'filled',
                status=status.value, raw_status=raw_status, order_id=order_id,
                filled_quantity=filled, new_fill_quantity=new_qty, fill_price=average)
