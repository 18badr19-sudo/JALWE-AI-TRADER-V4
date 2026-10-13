# Independent market-move research

`market_move_research_loop.py` is a separate controller-managed subprocess in the
existing JALWE Railway service. It uses its own persistent SQLite file,
`/app/data/market_move_research.db`, and polls every 15 minutes. It reads Alpaca
assets, calendar, historical SIP bars and news through a paced GET-only allowlist.
It cannot submit, cancel or replace orders. The existing PAPER-only guard applies.

This first version studies completed regular sessions; it does not promise live
all-market breakout detection. The current active tradable US-equity catalog can
include ETFs and is not a historical point-in-time or delisted-symbol universe.
Eligibility uses preceding completed daily-bar price $0.50–$100 and dollar volume
at least $100,000. Daily-bar features describe the provider's daily aggregation;
they are not represented as exclusively regular-session statistics.

## Two separate study populations

1. Retrospective cases: up to five largest eligible daily close-to-prior-close
   gains of at least 8% in the latest completed session, including symbols JALWE
   never analyzed. A control is selected from the prior-day price/liquidity bin
   without filtering its subsequent outcome. These examples and controls never
   count toward the forward comparison.
2. Prospective cohorts: up to eight activity/control pairs frozen before the next
   session opens, only after its immediately preceding session has completed.
   Activity requires prior-day volume at least 1.5 times the previous session and
   daily range at least 3%. This is a daily volume ratio, not intraday RVOL.
   Selection uses a deterministic symbol/date hash and matches log2 price and
   log10 dollar-volume bins. Controls do not satisfy the activity rule. Later
   outcomes cannot change these immutable cohorts. Missing matches are omitted.
  Both members of every pair and the session freeze marker are committed in one
  transaction after news retrieval. The live clock is checked again after the
  SQLite writer lock and before commit; a crossed opening deadline or storage
  failure rolls the new cohort back rather than leaving an orphan or partial batch.
  Existing frozen cohorts and legacy evidence are never rewritten.

Historical bars are queried only after a 16-minute delay beyond the session close.
Every regular-session minute must be present, valid, timezone-aware and unique
for a conclusive path (390 normally; early-close sessions follow the calendar).
Duplicate detection applies only inside the actual session's half-open window.
Extra rows at the inclusive provider end boundary or before session open are
excluded, including their duplicates; they cannot invalidate otherwise complete
regular-session coverage or a valid opening-prefix description.
Missing bars are never filled with a previous price. Provider and pagination
failures remain unavailable. News is retained only when both creation and update
timestamps precede the study cutoff; incomplete news stays explicitly incomplete.
Keyword tags describe content and do not prove the cause of a price move.

Coverage diagnostics distinguish genuinely absent provider minutes, returned but
invalid bars, duplicate timestamps, schema failures and request status codes.
For at most two absent minutes per observation, a GET-only historical SIP trade
probe checks up to two pages. It distinguishes no returned trades, recognized odd
lots, regular trades without a bar, unknown conditions and incomplete requests.
These are samples: they do not explain every gap or synthesize an OHLCV bar.
Successful samples are reused during retries; unavailable samples may be retried.
Repeated case/control episodes for the same symbol reuse intraday and completed
trade-sample reads within a cycle. Diagnostic counts report episodes separately
from distinct symbol-days and sampled symbol-minutes; conflicting samples remain
explicitly conflicting.

For an incomplete session, a separate `prefix_context` can describe consecutive
valid minute bars from the actual session open to the first gap. It reports a
barrier observed inside that interval and up to twenty complete pre-move minutes.
Missing opening bars or duplicate evidence make the prefix unavailable. Later
bars cannot influence it, and a same-bar crossing stays ambiguous. This is a
partial case-study description: the session status remains PARTIAL_DATA, its
outcome stays null, and this prefix never enters the forward preference comparison.
No barrier inside the prefix does not mean no barrier in the rest of the day.

Incomplete/unavailable observations can be rechecked after 15 minutes, at most
three total observation attempts, within 48 hours of session close. A legacy
unclassified result gets one diagnostic upgrade within that same cap/horizon.
Completed paths are immutable. Original features, frozen timestamps and cohort
membership never change on a retry. First observations take priority over retries;
each worker cycle processes at most twelve observations. HTTP-level retries are
separately bounded by the transport. Reports include diagnostics and the next
forward-cohort availability time in Saudi time. Bounded, deduplicated
`MARKET_MOVE_EVIDENCE` manifests allow operators to review stored coverage and
cohort timing without exporting credentials, headlines, full price series or raw trades.
During an existing incomplete-observation retry, at most two genuinely absent
minutes are also requested individually from the same delayed historical SIP
endpoint. Only complete, valid, unique returned minute bars are merged; empty,
invalid, duplicate, or unavailable replies cannot synthesize or replace a price.
The full session is then validated again. Inclusive end rows are excluded and
shared symbol/session reads are reused within the cycle. These bounded requests
do not reset the three-observation attempt limit or the 48-hour retry horizon.
Report reason counts are labeled as episode counts, separate from missing-minute
totals across distinct symbol/session windows. Conflicting window counts are
excluded from those minute totals and reported explicitly.

The path starts at the first regular-session bar open, with research barriers
of +8% and −3%. A same-minute crossing remains ambiguous. These are movement
labels, not strategy targets, executable fills, commissions or realized profit.
Twenty complete minutes strictly before an upward crossing supply retrospective
return, volume acceleration and VWAP context. They are not asserted to have been
available as live SIP features to the trading account at that instant.

## Receiving results in JALWE

The worker publishes a versioned research-only outbox. JALWE reads packets less
than one hour old with the exact protocol/feed/scope and logs
`MARKET_MOVE_BOT_RECEIVED`. A symbol-specific excerpt is attached to decision
metadata only; no score, risk, stop or entry gate uses it.
Module import, filesystem permission, and unexpected packet-reader exceptions
are isolated at the native decision boundary and scheduled-report poll. JALWE continues its own gates and
records only the unavailable exception type; private exception text is not logged.

The Telegram button **🔬 أبحاث الحركات**, `/move_research`, and `/learning` show the
latest research. With the existing `AUTO_DAILY_REPORT` switch enabled, the
controller sends one research report per completed session after all that
session's queued examples have been checked. Pending future cohorts do not block
this report. Restart markers suppress ordinary repeated reports; a crash between
Telegram delivery and persisting the marker may repeat a report.
Examples prioritize the most recent completed session, including incomplete
observations. Older complete examples cannot crowd it out; future pending cohorts
are shown separately from those completed-session examples.

Only complete, unambiguous prospective pairs enter the fixed comparison of
UP8_FIRST frequencies. It requires at least 40 pairs, 90% pair coverage and ten
distinct days before considering a chronological holdout. Whole days are split
2/3 training and 1/3 validation, with at least five days and twenty pairs in each
split. A positive training edge and a positive conservative 99% day-level
Hoeffding lower bound are required to label a research association. The bound
assumes independent days, which is not proven; overlapping market regimes and
repeated symbols limit interpretation. It never activates a trading preference.
Even constant success over a tiny holdout does not mean zero uncertainty.

## Pattern learning and guarded PAPER application

`market_move_patterns.py` defines three fixed prior-day hypotheses: volume ratio
at least 2 and range at least 5%; range at least 8% and daily gain at least 3%;
volume ratio at least 3 and daily gain at least 2%. These use completed prior-day
SIP inputs, not intraday RVOL. Completed retrospective UP8_FIRST examples supply
descriptive hypothesis support; news and pre-move context remain descriptions,
not proven causes or automatic entry rules.

The definition and thresholds are versioned and stored before collecting learning
cohorts. The original sixteen cohorts retain their original protocol and cannot
be relabeled to train this new model. Training requires at least forty matched
prospective pairs across ten days, 90% complete pair and known-feature coverage,
and support from three completed retrospective examples on three days. The best
training daily association is selected once and sealed, with a minimum edge .10.
Exactly the first twenty later frozen cohort session days test that fixed winner;
they cannot alter it or select another candidate after failure. Pending/retryable
observations delay assessment. Missing and ambiguous outcomes count against
coverage. Validation requires forty matched pairs on ten matched days, recent
evidence, and a positive day-level Hoeffding lower bound. Generation k spends
alpha .01/(k*(k+1)); these bounds assume independent days, which is not proven.
This measures UP8_FIRST frequency, not simulated or realized profitability.

A passing model can automatically give matching fresh APEX reports earlier
analysis priority in PAPER during the regular session. Prior-day inputs for the
whole active catalog are stored once before open; only the training price and
liquidity domain is eligible. The priority adapter checks a fresh research packet,
current active policy version, frozen input timing, source, session and expiry.
It makes a stable permutation of the same reports, never adds symbols, changes
native gates/scores, modifies sizing/stops, submits orders or changes exit priority.
Optional failures preserve the original analysis order. Native JALWE can execute
only after all its existing gates pass. LIVE cannot use this adapter.

The model retires after fourteen calendar days or suspends after at least twenty
matched pairs over five later days show a nonpositive forward association with
adequate coverage. After seven days a new generation begins using only newly
frozen evidence, with a fresh holdout and smaller alpha. Definitions, sealed
training, validation, generation and applied priority audit are durable. Bot
reports show whether learning is collecting, validating, active or suspended.

Storage retains 90 days of episodes and 30 packet versions. Daily and universe
caches prevent repeated full-catalog reads in the same session. Individual bad
symbols are excluded; request failures cannot interrupt trading. A current
15-minute cycle may take additional time to fetch data. This path adds no new
Railway service, but uses resources in the existing service and volume.

Official provider references:
- https://docs.alpaca.markets/us/docs/market-data-faq
- https://docs.alpaca.markets/us/reference/stockbars
- https://docs.alpaca.markets/us/reference/news-3

Validation: `python -m unittest discover -s tests -q` covers chronological
availability, immutable cohorts, controls, gaps/early close, ambiguous paths,
holdout isolation, pagination, news revisions, provider outages, packet freshness,
native gate invariance, controller lifecycle and report deduplication. No broker
orders or live credentials are used by these tests.
