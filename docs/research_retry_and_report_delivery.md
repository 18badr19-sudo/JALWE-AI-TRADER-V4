# Research retry scheduling and complete report delivery

The research report can exceed Telegram's message limit after adding jump
profiles, requested studies and pattern state. The controller used to send only
the first 4,000 Python characters and then mark the daily report as sent.
Both controller and watcher now use the same lossless splitter, with at most
3,800 UTF-16 units per part. The daily sent marker is saved only after every
part succeeds. An unconfigured chat does not count as delivery. If a later part
fails, the next attempt resends the report; earlier parts may be repeated.
Existing daily sent markers are preserved, so this does not resend reports
already acknowledged before the fix. The research command returns all parts.

With more than 21,000 pending historical cases, always placing pending cases
ahead of retries could consume an incomplete study's 48-hour retry window.
The scheduler now reserves up to two remaining discovery seats for eligible
retries when that lane is oversubscribed, earliest expiry first. Prospective
cohorts remain first; explicit requests keep up to eight seats. The total stays
at 12 studies per cycle. Unused seats are filled by the existing ordering.
Eligibility, three-attempt limit, 15-minute interval and 48-hour horizon remain
enforced by `observation_due` and rechecked by durable settlement. Batch logs
include selected statuses, attempts and eligible/selected retry counts.

This fixes starvation, not overall throughput or missing source coverage.
The worker waits 15 minutes after each cycle, so 12 studies per cycle is less
than 1,152 studies per day in practice, before reserving forward observations
and requested studies. It cannot guarantee retrying all 914 eligible records
or clearing the historical backlog within 48 hours. Missing minute bars remain
unknown; no price is inferred and no incomplete path becomes a complete pair.

Regression checks cover a large new-case backlog, earliest-expiring retries,
unchanged forward/request priority, exhausted and premature retries, lossless
Arabic/emoji delivery, continuation failure and an unconfigured chat. All
transport calls in tests are mocked. Trading entry, risk, stops and learning
activation criteria are unchanged.
