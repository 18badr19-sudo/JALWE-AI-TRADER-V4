# Prospective paired loss-reduction study

Protocol `paired-cutoff30-common-entry-structure-stop-v1` is a research sidecar.
It cannot submit orders, change native entry/stop/risk settings, select a strategy
preference or promote itself. `/loss_study` reads saved results only.

## Four frozen arms on the same opportunity

- CURRENT: common baseline entry and current frozen stop.
- CUTOFF30: same plan, but no entry at or after actual regular close minus 30 minutes.
- STRUCTURE: same common entry, stop=min(current stop, low of the five preceding
  completed feature bars minus 0.25 ATR from that timeframe).
- CUTOFF30_STRUCTURE: both changes together.

The 30-minute threshold, five bars, 0.25 ATR and cost assumptions are predeclared
experimental choices, not optimized profitable rules. Only new, native-eligible,
selected baseline candidates from the existing unlatched strategy-trial path are
registered. This is not a replay of every native entry or broker fill. Duplicate
research episodes keep their first frozen plan; old trials are not backfilled.

Capture the five bar-start timestamps/lows, ATR, timeframe and decision feed at
the original decision. Reject invalid/nonfinite inputs, incomplete or repeated
context, bars that have not closed yet, stale context or feed mismatch. Never
replace missing original inputs with later features.

Reuse the independent research worker's immutable pre-open Alpaca calendar panel
through read-only SQLite. Freeze its session and manifest hash in each new plan.
Missing/late/mismatched calendar evidence stays unknown; there is no hardcoded
16:00 fallback. Tests cover early closes and New York daylight-saving offsets.

## Entry, exit, cash, risk and evidence

Use the first full minute after the decision, with at most 60 minutes ending no
later than the verified regular close. All used minutes must be present, ordered,
unique and valid. Data gaps do not become zero-return labels. Extra rows after
close are ignored. First require a closed baseline trigger confirmation and then
use the next full minute open as the common entry. Baseline pre-entry invalidation
or a gap outside the original entry plan applies to all four arms; a wider stop
cannot retroactively create an alternative earlier entry. The cutoff is checked
at that modeled entry time, including exact boundary cases.

For each arm and each cost assumption independently, quantity is the floor of the
smaller of $1 planned risk / (cost-inclusive entry minus stop) and $100 cash /
cost-inclusive entry. Wider stops cannot increase quantity. Gap losses can exceed
planned risk. Each arm's full-exit target is 2R from its common cost-inclusive
entry and frozen stop; otherwise exit at the last verified bar close. This is a
full-exit research proxy, not native partial T1/T2/T3 management or realized PnL.

Costs are assumed 0.10% and 0.25% per side, not measured broker fees/spreads. Record
both entry/exit costs, integer quantity, stop, target, PnL and the actual retained
input bars/hash. Same-minute stop/target ambiguity leaves the entire matched
comparison unknown. A complete path with no activation/allocatable shares is a
known no-trade; an unavailable path or original input remains unknown.

## Existing observations only

The existing decision-outcome worker provides its IEX window. The existing
16-minute delayed SIP worker supplies its separately retained/revalidated window,
including already-budgeted recoveries. No additional market-data request, paid
provider, Telegram send or broker endpoint is introduced. Near-close regular
prefixes may be measurable even when the legacy full 60-minute window is partial;
no missing after-hours minute is needed for this regular-session experiment.
The existing workers' schedules still determine when the saved window is read.

Store separate results by (trial,feed); do not pool or overwrite IEX with SIP.
A complete saved observation is immutable; incomplete evidence can recover through
existing reads. Missing calendar/context remains frozen unknown. The report shows
registered windows, distinct days/symbols, complete/unknown/pending counts and
unknown reasons, plus baseline totals, paired dollar differences, skipped losses
and missed wins for each cost/feed. Symbols/episodes can repeat; these are research
windows, not independent real trades. The bounded reader declines partial totals
when its display limit is exceeded.

Results are descriptive. There is no automatic adoption or fixed profitability
claim, and a handful of windows or one profitable week cannot establish an edge.
Any later adoption requires a separately reviewed sample-quality and out-of-time
validation decision, with costs and missed winners included.
