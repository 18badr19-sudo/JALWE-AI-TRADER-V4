# Retrospective prior-day factors and ORB entry models

The owner requested faster study using the available historical archive rather
than waiting for every new forward observation. `archival-prior-day-orb-v1`
reuses the existing SIP split-adjusted daily cache. It samples two pairs per
session across the latest ten completed sessions at initial preparation, then
seals each newly available session once. Cases pass the existing prior-day
8% range / 2x volume / 3% return conjunction, $0.50–$100 price and $100k dollar
volume floor; controls do not pass the conjunction and match the existing
prior-price/dollar-volume buckets. Pair selection and ordering use only the two
preceding daily bars and deterministic symbol/session hashes. No target-session
bar, jump ranking, daily high, return or minute outcome selects the pairs.
Invalid/duplicate or non-adjacent prior data are excluded and counted.

This is still retrospective reconstruction with the current active catalog,
including a survivorship limitation. It is not a newly recovered record of
what the bot actually knew, nominated or traded. Daily cache revisions cannot
replace sealed membership or episode inputs. Missing outcomes remain in the
denominator. Separate `ARCHIVE_STUDY` roles are never prospective and cannot
enter the forward learner, preference activation, nomination packet, native
decision path, broker ledger or strategy wallet. The owner-requested
SHFS / 2026-10-22 post-move focus is explicitly separate from cases/controls
and can be studied even when its prior price fails the original screen.

Full-session minutes use the existing reader, batch deduplication, calendar
coverage checks and retained evidence mechanism. A completed jump report does
not imply its entire raw session was retained: a low-to-peak trajectory alone
cannot reconstruct missing opening/closing minutes for ORB. Re-read full-session
minutes within the original total study budget when necessary; never invent
bars or infer prices from absent minutes. The same ORB15 next-minute-open entry,
range-low stop, 2R exit and assumed 10/25 bps cost stress are used. Actual spreads,
queue fills, broker fees and native partial exits are not measured by this model.

The scheduler keeps frozen forward cohorts, explicit historical path requests,
and up to four already sealed entry-study seats ahead of archives. At most
eight archive seats use the remaining existing 48-study production budget;
they displace historical discovery work rather than increase request budgets.
The explicit focus is first in that lane; retries otherwise precede untouched
work so the same three-attempt/48-hour retry limit remains usable for old dates.
The archive retry/cache deadline is anchored to actual after-session enqueue
time, not to a session that ended weeks ago. No changes to deadlines of other
roles, read pacing, targeted gap requests, trading gates, stops, capital, orders
or PAPER/LIVE settings are made.

Reports:

- `/research_archive`: case/control/focus states, missing paths, no entries,
  model exits and mean net-R under both assumed costs. A paired difference is
  stored only for pairs whose two models both entered and exited; its selected
  subset is explicit and is not a full-universe profitability claim.
- `/research_stock SHFS 2026-10-22`: appends the separate after-session inputs,
  current eligibility replay and model result without changing the original
  nomination membership or inventing a cause for its past absence.
- `/research_findings`: links the separate archival sample and progress.

Bounded preparation/result/summary logs permit verification without Telegram
messages or placing trades. Tests cover outcome-blind selection, deterministic
controls, immutable sampling, rejection of forward/unfinished archive episodes,
retry limits, scheduler budget and forward priority, cost/missing-data handling,
separation of focus/native/forward results, full-session reader integration and
read-only reporting. No return is claimed before complete observed coverage.
