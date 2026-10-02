# JALWE AI TRADER V4 — PAPER

JALWE alone owns trading decisions and broker execution. APEX supplies research.
Live trading remains disabled by the existing safety gates.

BUY entries use a fill-or-kill limit. The configured ceiling is rounded down to
the valid price increment. Risk, allocation and cash sizing use this same ceiling.
Existing pending entries are canceled and rechecked before their confirmed shares
are converted into a managed trade. Cancellation requests are not fill confirmation.

Exit and protective stop client IDs are saved before broker submission. Recovery
looks up uncertain submissions by the saved ID and never creates a replacement
SELL while that lookup is unresolved. A prolonged lookup failure requires investigation.

On Railway, mount persistent storage at `/app/data`. Controller state, runtime
controls, watcher state and the local APEX child share this location. Explicit
`JALWE_CONTROLLER_DATA_DIR` and `JALWE_DATABASE_PATH` overrides are supported.
The external APEX service and its PostgreSQL setup memory remain independent.

Research requires completed, timestamped bars and rejects stale or invalid data.
News keyword sentiment is a heuristic; it is not a calibrated probability.

Run `python -m unittest discover -s tests -v`. PostgreSQL bridge integration tests
require an isolated `TEST_POSTGRES_DSN` (configured in GitHub Actions).

Alpaca order semantics: https://docs.alpaca.markets/us/docs/orders-at-alpaca
