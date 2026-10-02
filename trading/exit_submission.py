"""Persist client identity before sending an exit; recover uncertain submissions by lookup."""
from datetime import datetime, timezone
from uuid import uuid4

from core.models import BrokerOrder, OrderStatus, TradeSide
from trading.trade_manager import TradeAction, TradeManagementDecision


def prepare_exit(database, trade_id, trade, decision):
    if trade.has_pending_exit or trade.metadata.get('exit_submission'):
        raise RuntimeError('An exit is already pending or awaiting broker recovery.')
    intent = {
        'client_order_id': 'JALWE-EXIT-' + uuid4().hex,
        'symbol': trade.symbol,
        'quantity': int(decision.quantity),
        'action': decision.action.value,
        'requested_price': float(decision.current_price),
        'reason': str(decision.reason),
        'state': 'PREPARED',
    }
    trade.metadata['exit_submission'] = intent
    database.save_managed_trade(trade_id, trade)
    return intent


def submit_prepared_exit(database, trade_id, trade, decision, execution, manager):
    intent = trade.metadata['exit_submission']
    if intent['state'] != 'PREPARED':
        raise RuntimeError('Uncertain exits require recovery before submission.')
    def before_submit():
        intent['state'] = 'SUBMITTING'
        # Broker preflight has passed. COMMIT immediately before submission.
        database.save_managed_trade(trade_id, trade)
    order = execution.submit_exit(
        symbol=trade.symbol, quantity=int(intent['quantity']),
        requested_price=intent['requested_price'], reason=intent['reason'],
        client_order_id=intent['client_order_id'],
        before_submit=before_submit,
    )
    manager.register_exit_order(trade, decision, order)
    intent['state'] = 'REGISTERED'
    database.save_managed_trade(trade_id, trade)
    database.save_broker_order(order)
    return order


def recover_exit_submission(database, trade_id, trade, broker, manager):
    intent = trade.metadata.get('exit_submission')
    if not intent or trade.has_pending_exit:
        return
    if intent.get('state') == 'PREPARED':
        # Broker code was never entered. A later management pass can try again.
        trade.metadata.pop('exit_submission')
        database.save_managed_trade(trade_id, trade)
        return
    raw = broker.get_order_by_client_id(intent['client_order_id'])
    symbol = str(getattr(raw, 'symbol', '')).upper()
    side = str(getattr(getattr(raw, 'side', ''), 'value', getattr(raw, 'side', ''))).lower()
    qty = int(float(getattr(raw, 'qty', 0)))
    if symbol != trade.symbol or side != 'sell' or qty != int(intent['quantity']):
        raise RuntimeError('Recovered exit does not match the persisted intent.')
    if str(getattr(raw, 'client_order_id', '')) != intent['client_order_id']:
        raise RuntimeError('Recovered exit client identity mismatch.')
    order_id = str(getattr(raw, 'id', '') or '')
    if not order_id:
        raise RuntimeError('Recovered exit has no broker order ID.')
    order = BrokerOrder(
        symbol=symbol, side=TradeSide.SELL, quantity=qty, order_id=order_id,
        client_order_id=intent['client_order_id'], status=OrderStatus.SUBMITTED,
        requested_price=intent['requested_price'], filled_quantity=0,
        submitted_at=datetime.now(timezone.utc), metadata={'recovered_exit_submission': True},
    )
    decision = TradeManagementDecision(
        symbol=symbol, action=TradeAction(intent['action']), quantity=qty,
        current_price=intent['requested_price'], stage_before=trade.stage,
        stage_after=trade.stage, stop_before=trade.current_stop,
        stop_after=trade.current_stop, reason=intent['reason'],
    )
    manager.register_exit_order(trade, decision, order)
    intent['state'] = 'REGISTERED'
    database.save_managed_trade(trade_id, trade)
    database.save_broker_order(order)
