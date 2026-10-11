"""Offline entry and runner-stop comparisons. Never used to submit broker orders."""
from __future__ import annotations

import math

PROTOCOL = "breakout-retest-runner-stop-cost10bps-v1"
COST = .001


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
    """One unsold runner unit: T1=2R, T2=3R, T3=4R; no partial-sale PnL."""
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
                new_stop = t1
            if bar.high >= t2:
                reached_t2 = True
                new_stop = t2 if rule == "T2" else t1
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
    return {"protocol": PROTOCOL, "scope": "RUNNER_UNIT_SIMULATION_NOT_TRADE_PNL",
            "targets": [t1, t2, t3], "one_way_cost_pct": COST * 100,
            "rules": results, "auto_apply": False}
