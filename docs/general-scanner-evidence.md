# General scanner history and retained bar status

APEX forwards a whitelisted copy of its existing stage audit rows to a bounded
background queue (32 batches, at most 20,000 rows/batch). The worker writes only
advisory evidence to shared Postgres, independently of the frozen nomination
trial. Failed writes, queue overflow or process termination can leave gaps;
logs identify failed/queued/saved states. Queuing is not proof of persistence.
The scanner never waits for database I/O and no evidence failure can authorize
or reject an entry.

History retains first/last observation payloads and an observation count for
each NY day/symbol/stage/reason, for 14 days. Exact repeated batches are
transactionally deduplicated; this count is not trades or distinct stocks.
An aggregate is not a full event timeline or evidence of a reason precisely
at a retrospectively selected jump onset. The first/last payloads preserve
the corresponding scan identity, price/rank/bar time where present. Secrets,
provider bodies and arbitrary fields are excluded.

The byte-identical `scanner_evidence.py` module lives in both repositories.
The APEX producer creates its two dedicated tables; the JALWE reader only uses
a read-only session. Missing tables or history remain unknown. No old Railway
logs are backfilled automatically. `/research_stock SYMBOL YYYY-MM-DD` now
combines this general scan history, local native decisions, frozen nomination
trace, original research study and any archival study.

Existing strict retained-SIP merging already preserves actual returned minutes
between research reads. The daily SIP report now explicitly states this and
counts windows with retained raw evidence, including failed later reads. This
is not recovery of never-returned bars or permission to use delayed SIP in live
entry. Conflict rejection, retry budgets, fixed windows, feeds, completed-bar
requirements, original decision inputs and trading policies remain unchanged.

Validation: synthetic normalization/queue/read-only tests; isolated Postgres
transactional retry and out-of-order first/last integration in CI; controller
route tests; existing complementary-minute, conflict, restart and transport
failure suites. No real broker, Telegram or production SQL is used in tests.
