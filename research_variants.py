"""Offline entry and runner-stop comparisons. Never used to submit broker orders."""
from __future__ import annotations

import math

PROTOCOL = "breakout-retest-runner-stop-cost10bps-v1"
RUNNER_STOP_PROTOCOL = "runner-t1-entry-t2-floor-comparison-v2"
COST = .001


def entry_stop_matrix(bars, trigger, stop, context):
    """Paired 2x2 research: same future window, frozen structure, $1 risk/$100 cash."""
    from strategy_experiments import simulate, number
    base = {"protocol": "paired-entry-structure-atr-fixed-risk-v1",
            "scope": "SIMULATED_NOT_REALIZED_PNL", "auto_apply": False,
            "risk_budget": 1.0, "cash_limit": 100.0, "atr_buffer_multiple": .25,
            "target_rule": "2R_FROM_TRIGGER", "one_way_cost_pct": COST * 100,
            "rules": {}}
    trigger, stop = number(trigger), number(stop)
    context = context if isinstance(context, dict) else {}
    atr, low = number(context.get("atr")), number(context.get("model_low"))
    if (not context.get("available_at_decision") or atr is None or low is None
            or atr <= 0 or low <= 0 or trigger is None or stop is None
            or not trigger > stop > 0):
        return {**base, "outcome": "MISSING_FROZEN_STOP_CONTEXT"}
    structure_stop = min(stop, low - .25 * atr)
    if structure_stop <= 0:
        return {**base, "outcome": "INVALID_STRUCTURE_STOP"}
    base.update(atr=atr, atr_timeframe=context.get("atr_timeframe"), model_low=low)
    for stop_name, stop_price in (("CURRENT", stop), ("STRUCTURE_ATR", structure_stop)):
        for entry_name, entry_fn in (("DIRECT", simulate), ("RETEST", retest)):
            result = entry_fn(bars, trigger, stop_price)
            quantity = 0
            fill = number(result.get("entry_fill"))
            if result.get("traded") and fill is not None and fill > stop_price:
                quantity = max(0, math.floor(min(1.0 / (fill - stop_price), 100.0 / fill)))
            net_r = number(result.get("net_r"))
            # Ambiguous paths remain unknown even when shares cannot be allocated.
            pnl = (None if net_r is None else 0.0 if quantity == 0 else
                   net_r * (trigger - stop_price) * quantity)
            base["rules"][entry_name + "_" + stop_name] = {
                **result, "stop": stop_price, "quantity": quantity,
                "planned_risk_dollars": quantity * (fill - stop_price) if fill is not None else 0,
                "net_pnl_dollars": pnl, "traded": bool(result.get("traded") and quantity > 0)}
    return {**base, "outcome": "PAIRED_RESEARCH"}


def retest(bars, trigger, stop):
    """Wait for a closed breakout, a later retest, then enter at next open."""
    from strategy_experiments import simulate, number
    base = {"protocol": PROTOCOL, "scope": "SIMULATED_NOT_REALIZED_PNL", "net_r": None,
            "entry_rule": "BREAKOUT_RETEST", "retest_tolerance_pct": .1}
    trigger, stop = number(trigger), number(stop)
    if trigger is None or stop is None or not trigger > stop > 0 or bars.empty:
        return {**base, "outcome": "INVALID_PLAN"}
    breakout = False
    for i, (_, bar) in enumerate(bars.iterrows()):
        if bar.low <= stop:
            return {**base, "outcome": "AMBIGUOUS_ACTIVATION" if bar.close >= trigger else "INVALIDATED_BEFORE_ENTRY",
                    "net_r": None if bar.close >= trigger else 0.0}
        if not breakout:
            breakout = bar.close >= trigger
            continue
        if bar.low <= trigger * 1.001 and bar.close >= trigger:
            result = simulate(bars.iloc[i:], trigger, stop)
            return {**result, **base, "net_r": result.get("net_r"),
                    "retest_at": bars.index[i].isoformat()}
    return {**base, "outcome": "NO_RETEST_ENTRY", "net_r": 0.0, "traded": False}


def runner_stops(bars, entry, stop, *, entry_fill=None):
    """One unsold unit: T1 raises to entry; T2 compares T1/T2 floors.

    Subsequent candles can only raise the floor. This is a research comparison,
    not a replay of broker orders, native quantities or realized trade PnL.
    """
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (entry, stop)) or not entry > stop > 0:
        return {"protocol": PROTOCOL, "outcome": "INVALID_PLAN"}
    fill = entry * (1 + COST) if entry_fill is None else entry_fill
    risk = entry - stop
    t1, t2, t3 = (entry + n * risk for n in (2, 3, 4))
    results = {}
    for rule in ("T1", "T2"):
        current = stop
        reached_t2 = False
        outcome, exit_raw, exit_at = "TIME_EXIT", float(bars.close.iloc[-1]) if len(bars) else entry, None
        for stamp, bar in bars.iterrows():
            # Intrabar stop/target ordering cannot be reconstructed from OHLC.
            next_target = t1 if current == stop else t2 if not reached_t2 else t3
            if bar.low <= current and bar.high >= next_target:
                outcome, exit_raw = "AMBIGUOUS_STOP_TARGET", None
                break
            if bar.low <= current:
                outcome, exit_raw, exit_at = "STOP", min(current, float(bar.open)), stamp.isoformat()
                break
            new_stop = current
            if bar.high >= t1:
                new_stop = max(new_stop, entry)
            if bar.high >= t2:
                reached_t2 = True
                new_stop = max(new_stop, t2 if rule == "T2" else t1)
            if bar.high >= t3:
                # Comparison ends at T3; subsequent trailing rules are out of scope.
                if bar.low <= new_stop:
                    outcome, exit_raw = "AMBIGUOUS_STOP_TARGET", None
                else:
                    outcome, exit_raw, exit_at = "T3", t3, stamp.isoformat()
                break
            if new_stop > current and bar.low <= new_stop:
                outcome, exit_raw = "AMBIGUOUS_RAISED_STOP", None
                break
            current = new_stop
        results[rule] = {"outcome": outcome, "reached_t2": reached_t2, "exit_at": exit_at,
                         "net_r": None if exit_raw is None else
                         (exit_raw * (1 - COST) - fill) / risk}
    return {"protocol": RUNNER_STOP_PROTOCOL, "scope": "RUNNER_UNIT_SIMULATION_NOT_TRADE_PNL",
            "targets": [t1, t2, t3], "one_way_cost_pct": COST * 100,
            "rules": results, "auto_apply": False}
