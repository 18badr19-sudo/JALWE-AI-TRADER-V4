# Historical jump study requests

The user-requested `badr-week-2026-10-05-09-v1` studies MI, WFF, SAIQ,
APUS, PFAI and VEEA in each completed exchange session of October 5–9, 2026.
The full five-session calendar yields 30 symbol/session reads. No prices or
claimed returns from a website are injected into the study. Existing identical
historical episode keys and results are reused, never overwritten. These are
`JUMP_CASE` retrospective studies, never forward cohorts or pattern training.

Explicit requests do not require portfolio ownership, current broker
tradability, a prior daily volume threshold, or a successful daily universe
read. They use actual delayed SIP intraday data after session close plus
16 minutes. The active US equity research catalog also includes broker
nontradable assets. Trading catalogs and broker permission checks are unchanged;
historically delisted coverage is not exhaustive.

Automatic historical screening keeps all valid daily range/gap bounds of at
least 8% with observed dollar volume of at least $100,000 on either the prior
or event day. Research has no trading price-range filter. This includes quiet
stocks that become active on the event day. A daily bound screens a read and
never establishes the time-ordered jump or its cause.

Explicit episodes are initially seeded without daily prices. After a successful
daily read, a separate requested-symbol/day bound overlay supplies their current
retrospective read priority. It does not rewrite stored episode features/results
or apply post-session data to forward cohorts. Invalid or overflowing priority
values cannot interrupt scheduling. The overlay is audited in
`MARKET_JUMP_REQUEST_PRIORITY`.

The worker retains its 12 observations per cycle and 15-minute interval. Due
forward observations come first. Up to eight remaining seats serve explicit
requests, with the rest available for general discovery; unused discovery seats
can serve more requests. Within each lane, first observations precede eligible
retries, then larger finite daily bounds precede smaller ones. Existing retry
limits and 48-hour discovery horizons remain in force.

The packet and status report include per-symbol requested counts and the six
largest **completed** historical minute paths. Partial paths are excluded from
that ranking and retain their existing missing-data descriptions. Results track
the chronological onset, subsequent peak, duration, phase volume and real minute
trajectory; same-minute low/high order and paths across gaps are not invented.
Pre-onset context and temporally admissible news describe associations rather
than proving the cause. Intraday paths exclude the overnight gap and are not
the previous-close-to-weekly-high percentages shown by a web price table.

These changes do not train Gemini, change entry/risk/stop rules, activate
pattern preferences, or submit broker orders. Synthetic integration tests do
not establish real-market coverage or profitability. Actual requested progress
is exposed in `MARKET_JUMP_REQUEST_QUEUED`, `MARKET_JUMP_STUDY_BATCH`,
`MARKET_JUMP_STUDY_RESULT`, `MARKET_MOVE_EVIDENCE` and `MARKET_MOVE_PACKET` logs.
