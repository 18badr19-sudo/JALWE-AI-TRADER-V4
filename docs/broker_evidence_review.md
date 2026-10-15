# Broker evidence and research-clock review

This review checked syntax and undefined names across 132 production Python
files in JALWE and APEX, ran both test suites, and inspected execution,
reconciliation, recovery, protective-stop, market-data and research boundaries.
It is not a claim that every possible runtime path has been exercised.

## Confirmed defects

1. Three separate status mappings treated `done_for_day` and `replaced` as
   cancellations, and `stopped` and `suspended` as rejections. A partial exit
   reported as done for the day could clear its pending order identity while
   the order remained unresolved. This could permit another exit attempt.
2. Entry recovery checked some identity fields only when saving the database
   snapshot, after attempting cancellation. An inconsistent broker response
   could therefore cancel a different order before the database rejected it.
   Ordinary reconciliation also trusted contradictory symbol/side/quantity
   evidence without checking it against the owned order.
3. `int(float(...))` truncated fractional broker fills, and a `filled` status
   without the complete requested quantity could be treated as terminal.
   This included marking an incomplete protective stop inactive.
4. External research freshness clamped any future timestamp to zero age.
   A report dated a day ahead could remain apparently fresh until that date.

These defects were reproduced with mocked broker responses and controlled
clocks; production logs did not establish that these rare states occurred.

## Resulting behavior

One mapping now treats only filled, canceled/cancelled, expired and rejected
as confirmed terminal broker statuses. Paused, replaced, pending, calculated
and unknown states retain ownership and block competing execution. An
untracked JALWE order with an unknown status is reported as unresolved.
Replacement orders are not automatically adopted or canceled: resolving
their ownership still requires broker reconciliation.

Incoming order responses are checked before local mutation and before entry
recovery cancellation. Broker ID is mandatory; identity fields supplied by
older adapters must agree with the owned order. Whole-share quantities use
exact decimal parsing; filled quantity cannot exceed requested quantity.
Confirmed fills need a finite positive average price, and `filled` needs the
entire requested quantity. Unfilled responses may still have no average.
Protective-stop fills retain their existing cumulative, idempotent accounting.

Research dated more than five seconds ahead is unavailable for freshness
checks. Small clock skew, timezone conversion and legacy UTC timestamps
continue to work.

Eleven new regression tests cover identity mismatches, invalid quantities and
prices, paused entry/exit recovery, subsequent real cancellation/fill,
incomplete stop fills, unknown statuses and future-dated research. An older
stop-race fixture now includes actual quantity and price, and asserts that
all five shares close without a second sell. All broker operations in these
tests are mocks; no orders are submitted to Alpaca.

Runtime also showed repeated `CODE_OWNER_REVIEW_POLL_UNAVAILABLE HTTPError`
messages. Those logs hid the HTTP status, so their cause was not established.
The diagnostic now emits only a numeric status, a bounded error category and
whether a review credential is configured. It never emits exception text,
URLs, response bodies or credentials. Two additional tests check that boundary.
This does not repair authentication or claim that GitHub polling has recovered;
that requires reading the post-deploy diagnostic.

Entry thresholds, strategy selection, risk sizing and the T1/T2/T3 stop policy
are unchanged. Research missing-minute coverage and the historical queue
remain separate limitations.

Primary references:
- https://docs.alpaca.markets/us/docs/orders-at-alpaca#order-lifecycle
- https://alpaca.markets/sdks/python/api_reference/trading/models.html
