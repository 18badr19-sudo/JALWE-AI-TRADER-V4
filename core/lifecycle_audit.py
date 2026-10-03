"""Persist observable PAPER lifecycle evidence without placing test orders."""
import json


def checkpoint(database, trade_id, trade):
    metadata = trade.metadata
    evidence = {'trade_id': trade_id, 'symbol': trade.symbol, 'stage': trade.stage.value,
        'remaining': int(trade.remaining_quantity), 't1_filled': int(trade.t1_realized_quantity),
        't2_filled': int(trade.t2_realized_quantity), 't3_filled': int(trade.t3_realized_quantity),
        't1_complete': bool(trade.t1_completed), 't2_complete': bool(trade.t2_completed),
        't3_complete': bool(trade.t3_completed), 'runner': bool(trade.trailing_active),
        'stop': float(trade.current_stop), 'stop_order_id': metadata.get('protective_stop_order_id'),
        'stop_active': bool(metadata.get('protective_stop_active')),
        'stop_quantity': metadata.get('protective_stop_quantity'),
        'pending_order_id': trade.pending_order_id}
    key = json.dumps(evidence, sort_keys=True)
    if metadata.get('lifecycle_checkpoint') == key:
        return
    database.log_event(event_type='PAPER_LIFECYCLE_CHECKPOINT', severity='INFO',
        message=f"{trade.symbol}: PAPER stage={evidence['stage']} remaining={evidence['remaining']}", metadata=evidence)
    metadata['lifecycle_checkpoint'] = key
    database.save_managed_trade(trade_id, trade)
    print('PAPER_LIFECYCLE_CHECKPOINT ' + json.dumps(evidence, sort_keys=True), flush=True)
