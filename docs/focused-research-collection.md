# Focused research collection: improve evidence, never invent missing prices

## Observed gap sample

Read-only deployment logs on 2026-10-24 from 05:54:00 to 05:59:49 UTC
(08:54–08:59 Saudi) contained six delayed-SIP research windows. They recorded
146 absent provider minutes and zero returned/rejected minutes. Six gap trade
samples classified three as unknown trade conditions, two as no trades in the
feed, and one as only non-price-updating trades. These overlapping windows and
minute samples are not a population estimate. No subscription rejection occurred
in this sample; absence does not establish the reason for every missing minute.
This is an audit of actual saved diagnostics, not a new provider request.

Existing delayed research already had persistent SIP evidence and bounded
retries. The new work focuses collection earlier and retains a small raw panel
rather than promising to recover minutes a provider never returns.

## Frozen selection

Production enables `focused_collection=True` in the independent research worker.
Before the exchange-calendar open it freezes up to four **existing prospective
ACTIVITY/CONTROL pairs**, ranked by the weaker member's prior-day dollar volume,
plus four existing pre-open ENTRY_STUDY nominees by prior-day dollar volume.
There are at most 12 distinct symbols; overlapping roles share one read. Original
cohorts, nomination lists, decisions, features and outcomes are not rewritten.
The collector cannot retrofit a panel after open. SQL lock acquisition and a
second deadline check protect against crossing open during the transaction.
The liquidity-focused subset has an explicit selection policy; it is not an
unbiased sample of the whole market or evidence of a predictive edge.

## Delayed collection and budgets

Each batch reads only full minute bars before floor(now - 16 minutes), capped at
calendar close. The provider's inclusive end bar is removed. This is delayed SIP
history, not real-time SIP and not native IEX decision data. Batches are paced at
15 minutes with durable reservations: at most 32 attempts per session, including
at most three after the full session becomes historically available. Post-close
collection ends within 48 hours. Empty successes and request failures never erase
previously returned bars. No fallback to paid APIs or another feed is added.

During a focused session the historical study budget is capped at 12 seats;
focused symbol seats are deducted from that budget. Outside the session the
normal production limit remains 48. Full-session frames just fetched by the
collector are reused rather than fetched twice. At most two trade-minute probes
per focused batch classify actual absent-minute samples. This is a symbol/read
seat budget, **not an absolute HTTP-call cap**: existing pagination, transport
retries, daily/calendar reads and separate historical diagnostics remain bounded
by their existing policies.

Raw captures retain feed, split adjustment, window, actual returned OHLCV,
first-seen time, read time and conflicts for 90 days, only for this small panel.
No interpolation, forward fill, zero-volume fabricated minutes, current prices,
trade-to-bar synthesis, alternate-feed substitution or larger retry allowance is
introduced for original historical evaluations. Original post-close episode
attempts remain separate and unchanged. Collection has no order endpoint.

## Evidence and training

As each original full-session episode becomes due, it can merge same-symbol,
same-session, split-adjusted SIP captures with its own retained bars. Conflicts
stay conflicts. Full-session completion still requires the existing strict
390/210-minute checks. A complete *available prefix* does not complete a session
or prove a target/stop order. Original learning validation and frozen forward
samples remain the only authorities for any later training; the collector never
activates training, preferences, or a trading model.

The research report and FOCUSED_COLLECTION_STATUS log show distinct symbols,
original pairs/nominees, stored coverage against currently available minutes,
complete whole-session paths/pairs, last read times, request errors, and gap
sample categories. Zero available minutes is unknown coverage, not 100%.
Existing decision-feature snapshots and saved final-entry quote assessment stay
separate. No quote is invented or retroactively fetched for a rejected decision;
coverage in this report is minute-bar coverage, not quote/spread completeness.

## Verification and limits

Tests use synthetic OHLCV and provider mocks, temporary SQLite, dummy credentials
and blocked sockets. They cover prior-only deterministic selection, freeze
rollback at open, delay/end boundaries, early close, restart cadence, empty and
complementary reads, persistent conflicts, bool rejection, failed provider reads,
original-episode settlement, read reuse, and report round-trip. Full-session
completion and improved model quality must be measured on subsequent real
sessions; a Saturday deployment cannot establish them.
