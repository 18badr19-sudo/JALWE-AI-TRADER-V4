"""Persist explicit stop-price rejection while retaining ambiguous submissions."""
import json


def stop_price_rejection(error):
    try:
        payload = json.loads(str(error))
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict) or payload.get('code') != 42210000
            or payload.get('message') != 'stop price must be less than current price'):
        return None
    return payload


def persist_stop_rejection(database, trade_id, trade, payload, *, source):
    metadata = trade.metadata
    metadata.update(protective_stop_submission_uncertain=False,
                    protective_stop_submission_state='REJECTED',
                    protective_stop_order_id=None, protective_stop_active=False,
                    protective_stop_status='REJECTED', protective_stop_rejection=payload)
    database.save_managed_trade(trade_id, trade)
    database.log_event(event_type='BROKER_PROTECTIVE_STOP_REJECTED', severity='ERROR',
                       message=f'{trade.symbol}: protective stop explicitly rejected',
                       metadata={'trade_id': trade_id, 'symbol': trade.symbol,
                                 'client_order_id': metadata.get('protective_stop_client_order_id'),
                                 'source': source, 'rejection': payload})


def record_stop_submission_error(database, trade_id, trade, error):
    if getattr(error, 'status_code', None) != 422:
        return False
    payload = stop_price_rejection(error)
    if payload is None:
        return False
    persist_stop_rejection(database, trade_id, trade, payload, source='BROKER_RESPONSE')
    return True
