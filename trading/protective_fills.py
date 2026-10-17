"""Apply cumulative protective-stop fills before another SELL can use local quantity."""
import math
from datetime import datetime, timezone

from core.models import OrderStatus, BrokerOrder, TradeSide
from broker.order_evidence import whole_shares, validate_order_snapshot
from trading.trade_manager import TradeStage

CANCELED_STATUSES = {'canceled', 'cancelled', 'expired'}
REJECTED_STATUSES = {'rejected'}
TERMINAL_STATUSES = CANCELED_STATUSES | REJECTED_STATUSES | {'filled'}


def _quantity(value):
    return whole_shares(0 if value is None else value, allow_zero=True)


def apply_protective_stop_snapshot(database, trade_id, trade, raw):
    metadata = dict(trade.metadata or {})
    order_id = str(metadata.get('protective_stop_order_id') or '').strip()
    if not order_id:
        raise ValueError('Protective stop has no persisted broker identity.')
    raw_status = getattr(raw, 'status', '')
    raw_status = str(getattr(raw_status, 'value', raw_status) or '').lower()
    filled = _quantity(getattr(raw, 'filled_qty', 0))
    previous = _quantity(metadata.get('protective_stop_applied_qty', filled if metadata.get('protective_stop_fill_applied') else 0))
    if filled < previous:
        raise ValueError('Protective stop cumulative fills moved backwards.')
    requested = metadata.get('protective_stop_quantity')
    if requested is not None and filled > _quantity(requested):
        raise ValueError('Protective stop cumulative fill exceeds requested quantity.')
    expected = _quantity(requested) if requested is not None else previous + int(trade.remaining_quantity)
    # Validate all supplied identity and fill fields before changing protection,
    # quantities or persisted proceeds, including terminal zero-fill responses.
    evidence = validate_order_snapshot(
        raw, symbol=trade.symbol, side='sell', quantity=expected, order_id=order_id,
        client_order_id=metadata.get('protective_stop_client_order_id'),
    )
    new_qty = filled - previous
    status = (OrderStatus.FILLED if raw_status == 'filled' else
              OrderStatus.CANCELED if raw_status in CANCELED_STATUSES else
              OrderStatus.REJECTED if raw_status in REJECTED_STATUSES else
              OrderStatus.PARTIALLY_FILLED if raw_status == 'partially_filled' else
              OrderStatus.ACCEPTED)
    active = raw_status not in TERMINAL_STATUSES
    def optional_time(value):
        if value is None: return None
        try:
            parsed=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return parsed if parsed.tzinfo is not None else None
        except (ValueError,TypeError): return None
    snapshot=BrokerOrder(symbol=trade.symbol,side=TradeSide.SELL,quantity=expected,
        order_id=order_id,client_order_id=getattr(raw,'client_order_id',None) or metadata.get('protective_stop_client_order_id'),status=status,
        filled_quantity=filled,filled_price=evidence['filled_price'],
        submitted_at=optional_time(getattr(raw,'submitted_at',None)),
        filled_at=optional_time(getattr(raw,'filled_at',None)),
        metadata={'source':'PROTECTIVE_STOP_RECONCILIATION','observed_at':datetime.now(timezone.utc).isoformat()})
    average = evidence['filled_price']
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
            trade_state=trade, broker_order_snapshot=snapshot, event_key=f'{order_id}:{filled}', trade_id=trade_id,
            order_id=order_id, symbol=trade.symbol, action='BROKER_PROTECTIVE_STOP',
            quantity=new_qty, fill_price=incremental_price, entry_price=trade.entry_price,
            realized_pnl=realized, event_time=event_time,
            metadata={'source': 'PROTECTIVE_STOP_RECONCILIATION', 'protective_stop': True},
        )
    else:
        database.save_managed_trade(trade_id, trade)
        if filled>0 or raw_status in TERMINAL_STATUSES:
            database.save_broker_order(snapshot)
    return dict(checked=True, active=active, filled=raw_status == 'filled',
                status=status.value, raw_status=raw_status, order_id=order_id,
                filled_quantity=filled, new_fill_quantity=new_qty, fill_price=average)
