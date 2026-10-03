# JALWE AI TRADER V4 — PAPER

JALWE alone owns trading decisions and broker execution. APEX supplies research.
Live trading remains disabled by the existing safety gates.

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
