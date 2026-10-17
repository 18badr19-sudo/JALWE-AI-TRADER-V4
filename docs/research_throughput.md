# Bounded historical research throughput

The deployed worker previously evaluated 12 episodes, then slept 900 seconds
**after** processing. At most 1,152 episodes/day were possible before any HTTP,
CPU or retry overhead. The observed queue was growing beyond 20,000 pending
episodes. This is a throughput limit, not evidence that absent prices exist.

Production now selects at most 48 episodes per cycle. Existing prospective
priority and up to eight explicit-request seats remain. The discovery lane
reserves up to eight eligible retry seats at this budget, earliest expiry first;
the 12-episode scheduler default still reserves two. Three attempts, 15-minute
retry spacing and the 48-hour retry horizon are unchanged.

The worker groups full minute-bar reads by exact session open/close and batches
at most 24 distinct symbols per request. Existing SIP, split adjustment, delayed
availability, OHLCV checks and full pagination remain. Missing symbols after
all pages yield empty frames; no minute or price is synthesized. Conflicting
duplicates remain visible. Symbol-specific or malformed batches fall back to
the previous isolated reads. Account/rate/server/transport failures stop batch
prefetch without fanning out or spending episode attempt counters.

The production loop targets 300 seconds **start to start**, with at least 60
seconds idle after slow cycles. The provider's existing two-second pacing and
bounded HTTP retries are unchanged; there are no concurrent requests. The
theoretical 13,824 studies/day ceiling is not a promised throughput: pagination,
gap samples, targeted retries, provider latency and preparation can take longer.
The packet/report includes selected/processed counts, prefetched symbol-sessions
and processing duration so actual progress can be checked.

No trading, entry, risk, stop or pattern activation rules change. The original
cohorts and their timestamps are preserved. Full-session requirements remain;
partial prefixes do not enter prospective comparisons. Context before an early
onset or across a gap remains unknown if 20 valid preceding minutes are absent.
This change does not claim to recover that evidence or clear the backlog.

The provider supports several symbols per bars request and sorts by symbol;
pagination must finish before concluding a later symbol is absent:
https://docs.alpaca.markets/us/reference/stockbars
