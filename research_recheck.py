"""Bounded scheduling only; every retry still runs the full decision engine."""
import math

INTERVAL_SECONDS = 60
WINDOW_SECONDS = 600
RETRY_REASONS = {
    "OpportunityEngine rejected the setup.",
    "Feature freshness check failed.",
    "Breakout crossed the trigger but confirmation quality is not strong enough.",
    "AI signal is not BUY.",
}


def valid_entry(entry, version):
    return (
        isinstance(entry, dict) and entry.get("version") == version
        and all(isinstance(entry.get(k), (int, float))
                and math.isfinite(entry[k]) for k in ("expires", "next"))
    )


def update_recheck(state, symbol, version, decision_state, reason, now):
    entries = state.setdefault("rechecks", {})
    entry = entries.get(symbol)
    if decision_state == "WATCHING":
        # Do not extend the same report's window on every watcher poll.
        # Once a scheduled recheck actually runs, advance the next due
        # time so WATCHING remains on the intended 60-second cadence.
        if not valid_entry(entry, version):
            entry = {"version": version, "expires": now + WINDOW_SECONDS,
                     "next": now + INTERVAL_SECONDS}
        elif now >= entry["next"] and now < entry["expires"]:
            entry["next"] = min(
                now + INTERVAL_SECONDS,
                entry["expires"],
            )
        entries[symbol] = entry
    elif (decision_state == "REJECTED" and reason in RETRY_REASONS
          and valid_entry(entry, version) and now < entry["expires"]):
        entry["next"] = now + INTERVAL_SECONDS
    else:
        entries.pop(symbol, None)
        return False
    return now < entry["expires"]


def recheck_due(state, symbol, version, now):
    entry = state.get("rechecks", {}).get(symbol)
    return (valid_entry(entry, version)
            and entry["next"] <= now < entry["expires"])
