# Native AI validation, score evidence, and bounded shadow sampling

The native AI remains deterministic scoring. This change does not enable LIVE,
change scoring thresholds/valid-input weights, train a new model, or let Gemini
approve, delay, submit, resize or manage an order.

## Component input contract

AIEngine independently requires a finite positive numeric price even if an
upstream quality flag is true. NaN, infinity, bool, numeric strings and invalid
numeric domains are excluded. Price failure rejects the analysis; an invalid
optional indicator is missing evidence, not bullish or bearish evidence. Score
indicators must be in [0,100]; RVOL and compression nonnegative; VWAP and EMAs
positive; distances may be signed. The caller's snapshot is never mutated.
VWAP's direction must be a real bool; unknown is no longer interpreted as below
VWAP. Invalid learned contributions fail closed through the existing decision
exception handling. Stale/bad-quality rejection reports zero coverage.

Confidence is coverage of ten scoring factor groups, not a forecast confidence
or a calibrated win probability. A missing optional subfield may leave the
rest of its factor group usable. Probability remains an explicitly labelled
score proxy. No historical score or input is reconstructed with today's values.

## Reproducible explanation

AI metadata records factor, side, original points and applied learned points,
raw side totals, the capped formula, thresholds and excluded input names.
DecisionEngine preserves `ai_score_evidence` for both ordinary decisions and
latched quality refreshes. `/explain SYMBOL` presents the saved contributions;
old decisions without that evidence explicitly report it unavailable.

The formula remains clamp(clamp(bullish,0,100) - .70*clamp(bearish,0,100),0,100).
This does not establish why a stock subsequently moved or prove a rejected
candidate would have been a profitable execution.

## Gemini selection without extra calls

With the production default, the same daily limit is partitioned across three
fixed New York sampling windows: 09:30–11:40, 11:40–13:50, 13:50–16:00. For the
current limit of three this permits one distinct symbol per window, with no
catch-up burst or carry-over. Pending, failed, expired and interrupted attempts
continue to consume both daily and window budgets, including existing records.
Reservation and duplicate checks use one SQLite immediate transaction. Identical
validated cache hits require no call or new budget. Limits below/above three are
divided evenly with remainders allocated to earlier windows.

These clock windows are sampling policy, **not a trading calendar**. The native
freshness/quality/state restrictions still apply. Weekends and outside windows
cannot enqueue new Gemini reviews. The windows are fixed even on early-close days; a late review is not proof
that the market is open. The third window may be unused if no fresh eligible
decision arrives. This change makes no new broker/calendar requests and does
not certify holidays or market status.
Three views remain one model, not three independent experts. The existing free
confirmation, model allowlist, durable quota pause, daily cap and no-paid-fallback
restrictions remain. Programmatic fixtures can disable spreading to isolate
transport/cache tests; the environment default always enables it for Gemini.

## Validation scope

Offline fixtures exercise numeric domains, missing coverage, no mutation,
score reconstruction, learned points, decision/replay evidence, read-only
explanation, window/daily boundaries, symbol diversity, restarts and expiration.
The repository PAPER suite runs with dummy keys, temporary databases, and socket
connections blocked. Local PostgreSQL integration cases require a separate
isolated PostgreSQL service; report them as skipped when unavailable.
No model or broker request is needed for these tests. Runtime deployment and
actual model quality remain separate from unit/integration correctness.
