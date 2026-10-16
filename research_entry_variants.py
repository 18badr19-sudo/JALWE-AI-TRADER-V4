"""Fixed entry challengers, evaluated offline on complete future windows only."""
from __future__ import annotations

import math
from numbers import Real

import numpy as np
import pandas as pd

PROTOCOL = "volume-confirmed-entry-challengers-v1"
RULES = ("BREAKOUT_HOLD_VOLUME", "SUPPORT_RECLAIM_VOLUME")
VOLUME_LOOKBACK = 5
MIN_VOLUME_MULTIPLE = 1.5


def _valid_window(bars):
    if not isinstance(bars, pd.DataFrame) or len(bars) != 60:
        return False
    index = bars.index
    if (not isinstance(index, pd.DatetimeIndex) or index.tz is None
            or index.hasnans or not index.is_unique or not index.is_monotonic_increasing
            or any((later - earlier).total_seconds() != 60
                   for earlier, later in zip(index, index[1:]))):
        return False
    if (not bars.columns.is_unique
            or not {"open", "high", "low", "close", "volume"}.issubset(bars.columns)):
        return False
    for row in bars[["open", "high", "low", "close", "volume"]].itertuples(index=False, name=None):
        if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, Real)
               or not math.isfinite(v) for v in row):
            return False
        opening, high, low, close, volume = row
        if not (0 < low <= min(opening, close) <= max(opening, close) <= high
                and volume >= 0):
            return False
    return True


def entry_challengers(bars, trigger, stop):
    """Signal on closed bars, enter at next open, frozen stop and 2R target.

    Volume uses only the five preceding rows, excluding the signal row.
    A support sweep must close below the frozen trigger without touching the
    frozen stop; a later high-volume reclaim can then signal. Neither rule
    accesses a broker or participates in native strategy preferences.
    """
    from strategy_experiments import number, simulate

    base = dict(protocol=PROTOCOL, scope="SIMULATED_NOT_REALIZED_PNL",
                auto_apply=False, target_rule="2R_FROM_FROZEN_TRIGGER",
                volume_lookback=VOLUME_LOOKBACK, min_volume_multiple=MIN_VOLUME_MULTIPLE,
                one_way_cost_pct=.10)
    boolean_plan = isinstance(trigger, (bool, np.bool_)) or isinstance(stop, (bool, np.bool_))
    trigger, stop = number(trigger), number(stop)
    if (boolean_plan or trigger is None or stop is None
            or not trigger > stop > 0):
        return {**base, "outcome": "INVALID_PLAN", "rules": {}}
    if not _valid_window(bars):
        return {**base, "outcome": "INVALID_WINDOW", "rules": {}}

    results = {}
    for rule in RULES:
        result = dict(entry_rule=rule, traded=False, net_r=0.0, outcome="NO_ENTRY")
        swept = False
        for i, (stamp, bar) in enumerate(bars.iterrows()):
            # Once invalidated, a later recovery cannot resurrect this plan.
            if bar.low <= stop:
                ambiguous = bar.close >= trigger
                result.update(outcome="AMBIGUOUS_ACTIVATION" if ambiguous else "INVALIDATED_BEFORE_ENTRY",
                              net_r=None if ambiguous else 0.0)
                break
            previous = bars.iloc[max(0, i - VOLUME_LOOKBACK):i]
            mean_volume = float(previous.volume.mean()) if len(previous) == VOLUME_LOOKBACK else 0.0
            volume_ok = mean_volume > 0 and bar.volume >= MIN_VOLUME_MULTIPLE * mean_volume
            if rule == "BREAKOUT_HOLD_VOLUME":
                signal = (i > 0 and bars.close.iloc[i - 1] >= trigger
                          and bar.low >= trigger and bar.close >= trigger and volume_ok)
            else:
                signal = swept and bar.close >= trigger and volume_ok
                swept = swept or (bar.low < trigger and bar.close < trigger)
            if not signal:
                continue
            if i + 1 == len(bars):
                result.update(outcome="NO_NEXT_BAR")
                break
            # simulate starts at the confirmed signal and cannot enter earlier.
            result = {**simulate(bars.iloc[i:], trigger, stop), "entry_rule": rule,
                      "signal_at": stamp.isoformat(), "signal_volume": float(bar.volume),
                      "prior_mean_volume": mean_volume}
            break
        results[rule] = result
    return {**base, "outcome": "RESEARCH_ONLY", "rules": results}


def count_results(comparisons):
    """Report entries separately from no-entry and unknown paths; no ranking."""
    from strategy_experiments import number

    counts = {r: dict(entries=0, no_entry=0, unknown=0) for r in RULES}
    for comparison in comparisons:
        if comparison.get("protocol") != PROTOCOL:
            continue
        rules = comparison.get("rules") or {}
        for rule in RULES:
            result = rules.get(rule) or {}
            key = ("unknown" if number(result.get("net_r")) is None else
                   "entries" if result.get("traded") else "no_entry")
            counts[rule][key] += 1
    return counts
