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

Historical bars are queried only after a 16-minute delay beyond the session close.
Every regular-session minute must be present, valid, timezone-aware and unique
for a conclusive path (390 normally; early-close sessions follow the calendar).
Missing bars are never filled with a previous price. Provider and pagination
failures remain unavailable. News is retained only when both creation and update
timestamps precede the study cutoff; incomplete news stays explicitly incomplete.
Keyword tags describe content and do not prove the cause of a price move.

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

The Telegram button **🔬 أبحاث الحركات**, `/move_research`, and `/learning` show the
latest research. With the existing `AUTO_DAILY_REPORT` switch enabled, the
controller sends one research report per completed session after all that
session's queued examples have been checked. Pending future cohorts do not block
this report. Restart markers suppress ordinary repeated reports; a crash between
Telegram delivery and persisting the marker may repeat a report.

Only complete, unambiguous prospective pairs enter the fixed comparison of
UP8_FIRST frequencies. It requires at least 40 pairs, 90% pair coverage and ten
distinct days before considering a chronological holdout. Whole days are split
2/3 training and 1/3 validation, with at least five days and twenty pairs in each
split. A positive training edge and a positive conservative 99% day-level
Hoeffding lower bound are required to label a research association. The bound
assumes independent days, which is not proven; overlapping market regimes and
repeated symbols limit interpretation. It never activates a trading preference.
Even constant success over a tiny holdout does not mean zero uncertainty.

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
