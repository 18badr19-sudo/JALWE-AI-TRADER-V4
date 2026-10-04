# JALWE AI TRADER V4 — PAPER

JALWE alone owns trading decisions and broker execution. APEX supplies research.
Live trading remains disabled by the existing safety gates.

## Decision outcome memory

The watcher saves every JALWE decision on an APEX report, including rejected
decisions, in SQLite `decision_outcome_memory`. The original reason, gates,
scores, strategy, report version, feature snapshot and execution audit remain
immutable. Identical rechecks of one report are deduplicated; changed evidence
creates a new observation. Collection starts when this version is deployed.

A single background worker checks up to four due observations per minute. It
requests the original 60-minute window of completed 1-minute bars, so restart
cannot substitute today's prices for an older decision. Missing, invalid or
stale reference prices are `UNOBSERVABLE`. Missing minutes and provider errors
are recorded separately from complete paths, with a ten-minute data retry grace.
Closed-market windows can therefore have no usable outcome. Measurements use
the configured feed (IEX is limited coverage); they are not market-wide cash flow.

When native trigger/stop levels exist, research activation requires the first
completed 1-minute close at or above the trigger. Level touches before that close
are excluded. The original T1 is used, or an explicitly theoretical 2R level if
T1 was absent. Same-bar stop/target touches are ambiguous; incomplete paths
cannot receive a first-hit label. This is an observation convention, not a
simulation of JALWE's entry, fills, costs, scale-outs or realized profit.

Counts are included in the existing daily opportunity report and learning status.
The structured results are grouped by source decision and strategy for future
evaluation. They do not train models, change learning weights, relax trading
gates or submit orders. Existing learning still requires eligible closed PAPER
trades. Later training needs chronological holdouts and execution-cost modeling.

## Session strategy experiments and conditional PAPER preference

Each session analysis freezes all six candidates and the original highest-score
winner. Native invalid or below-threshold candidates remain ineligible. Eligible
candidates share the decision's original 60-minute completed-bar observation.
`strategy_trials` deduplicates repeated decisions of one report/bar and retains
the original levels, eligibility and baseline for paired comparison.

The versioned simulator uses a completed 1m close at/above the trigger, entry at
the next 1m open, an assumed 0.10% spread/slippage cost on each side, a fixed 2R
target, and a 60m time exit. Stop gaps use the worse open. Invalidated setups and
entries outside the plan are not filled; ambiguous paths and incomplete data
cannot receive usable return labels. This is a research proxy, not a replay of
JALWE's execution, scale-outs, trailing stops, actual fills or portfolio PnL.

Once per New York day, the background worker evaluates fully matured earlier
days within a 90-day history. Context is market regime, feed and session bucket
(opening hour, middle session, late session, extended hours). Each comparison
uses at most one symbol/day/strategy/context observation, paired with the native
score baseline from the same episode. Whole days separate training (first 2/3)
from validation (last 1/3), including purging overlap at the data cutoff.

Promotion requires at least 40 training pairs on 10 days, 20 validation pairs on
5 days, 90% usable coverage in each split, and at least 20/10 simulated activations
in training/validation for both candidates and baseline. The training winner
must improve by at least 0.10R, retain a positive conservative daily edge on
validation, have positive validation mean, satisfy drawdown/worst-loss checks,
and include observations within the past seven days. Validation failure keeps
the baseline; it does not search the holdout for a different winner. Preferences
expire after 24 hours and require the same protocol and context.

The preference can select only an already-valid, above-threshold native session
candidate while PAPER is enabled and LIVE is disabled. Trigger, breakout and risk
gates still run afterward with the original routed risk limit. A selection or
database error retains the native score winner. No result changes learning
weights or enables LIVE. Counts appear in learning status and the daily report.

## Forward performance guard and preference version history

Validated preferences now receive a durable `strategy_policy_versions` ID.
Daily revalidation of the same strategy/context/protocol retains its original
activation time and immutable activation evidence. Switching strategies or
reintroducing a retired preference creates a new version. Activation,
revalidation, monitoring, retirement and rollback events are retained separately
in `strategy_policy_events`. Pre-upgrade preferences without a valid active
version fall back to the score winner until the next daily revalidation.

Only frozen episodes that actually selected the preference after its activation
are linked to that version in `strategy_policy_observations`. They must pass the
native pre-session gates, have eligible selected and baseline candidates, and
change the native score winner. Rechecks cannot retroactively assign an older
episode to a new policy. Monitoring reuses the original paired simulator and
bars; it is **simulated research, not realized trade/portfolio performance**.

Once per New York day, the guard compares matured prior-day observations from
the last 30 calendar days, with one symbol/day per policy, at least 20 pairs on
five days, 90% complete paired coverage, ten simulated activations on each side,
and data within seven days. Historical training/validation outcomes do not count
as forward evidence. Missing, ambiguous, stale or insufficient observations are
`WAITING_FORWARD_DATA`, not evidence of degradation.

Rollback requires either a conservative daily mean-edge upper bound below
-0.10R (mean + 2.58 daily standard errors), or daily mean edge below -0.10R
together with more than 3R of excess simulated drawdown against the baseline.
These fixed monitoring thresholds are research guardrails, not calibrated
guarantees. Rollback forces the native score selection for that day's evaluation
and blocks the same strategy/context/protocol for seven days. A different
strategy still needs a later complete daily validation. Cooldown expiry alone
does not activate anything: a fresh successful evaluation and new version are
required. Even a restored stale profile cannot select a retired/rolled-back ID.

Versions, rollback, profiles and the daily evaluation marker commit atomically
and survive restarts. A failure rolls back the entire publication. The guard
affects new strategy selection; existing positions and pending breakout setups
remain under their original manager/deadline. No code rewriting, dependency
updates, risk-limit changes, additional market requests or LIVE activation are
performed. Guard state is included in the existing learning/daily report.

Breakout latching now starts at the initial WAITING result only after all
pre-entry gates passed. Pending confirmation retains that strategy and the
original seven-minute deadline; rechecks cannot extend or recreate its approval.
BUY entries use a DAY limit. The configured ceiling is rounded down to
the valid price increment. Risk, allocation and cash sizing use this same ceiling.
After the reconciliation timeout (30 seconds by default), unfilled remainders
are canceled and rechecked before their confirmed shares
are converted into a managed trade. Startup recovery does the same for older
pending entries. Cancellation requests are not fill confirmation. FOK/IOC
eligibility is not assumed; Alpaca marks those time-in-force options as requiring
sales confirmation in its order documentation.

Exit and protective stop client IDs are saved before broker submission. Recovery
looks up uncertain submissions by the saved ID and never creates a replacement
SELL while that lookup is unresolved. A prolonged lookup failure requires investigation.

Protective stop fills are also reconciled during cancellation. A partial fill
updates quantity and PnL atomically before a replacement stop or another exit.
An exit decision based on the old quantity is discarded and re-evaluated.
The SUBMITTING state is committed after broker preflight passes, so a failure
before the broker submission does not create an orphan submission state.

On Railway, mount persistent storage at `/app/data`. Controller state, runtime
controls, watcher state and the local APEX child share this location. Explicit
`JALWE_CONTROLLER_DATA_DIR` and `JALWE_DATABASE_PATH` overrides are supported.
The external APEX service and its PostgreSQL setup memory remain independent.

Research requires completed, timestamped bars and rejects stale or invalid data.
News keyword sentiment is a heuristic; it is not a calibrated probability.

Run `python -m unittest discover -s tests -v`. PostgreSQL bridge integration tests
require an isolated `TEST_POSTGRES_DSN` (configured in GitHub Actions).

Alpaca order semantics: https://docs.alpaca.markets/us/docs/orders-at-alpaca


## Manual PAPER close and extended sessions

The Telegram menu includes a sell button for each managed position. A five-minute
confirmation binds the trade, share quantity and requesting user. Its durable
request is idempotent across duplicate taps and controller restarts. JALWE's
single watcher owns execution, cancellation, fill reconciliation and protection
for the remaining shares; Telegram never submits a broker order directly.

Eligible pre/post-market exits use extended-hours DAY SELL limits based on fresh
consolidated quotes, rounded up to preserve the configured slippage floor. Profit
targets keep their target price as a minimum. Missing/old quotes defer execution
before canceling protection. Regular-session exits continue using market orders.
Calendar checks include holidays and early closes; weekends and the overnight
window remain closed for this feature. A limit is not a guarantee of a fill.

Any remainder after the 30-second wait is canceled and reconciled again; restart
recovery also cancels unresolved extended limits. While cancellation is uncertain,
there is no competing stop or repeated sell. Manual requests continue for the
confirmed remainder until the managed position closes. Changing quantity before
confirmation requires a refreshed confirmation.

Live SIP execution-quote access depends on the Alpaca account's data entitlement.
The startup EXTENDED_EXIT_DATA_ACCESS diagnostic checks access read-only; failure
does not silently substitute delayed/IEX data. No subscription is purchased by
this change. PAPER_LIFECYCLE_CHECKPOINT records actual stage, fills, remaining
shares, runner and protective-stop state for observation after market reopening.
