# Research-window follow-up review (2026-10-16)

The shared 60-minute research filter discarded duplicate timestamps by keeping
the first valid row. Contradictory OHLCV rows for the same minute could therefore
produce a COMPLETE window whose target/stop outcome depended on input row order.
An invalid duplicate could also disappear before this conflict was considered.

The filter now compares numeric OHLCV copies before validity filtering, excluding
every copy of a conflicting timestamp. Identical duplicates remain deduplicated.
The rejected minute is recorded as returned-but-invalid, rather than absent from
the provider. Decision memory, opportunity tracking and delayed SIP observation
share this filter; entry gates and order management do not use it.

Four regression tests cover both input orders, an invalid conflicting copy,
identical repeats, and persisted decision/delayed-SIP incompleteness. Before the
repair three tests failed (four subcases); identical-repeat behavior already
passed. The tests use synthetic bars and never submit orders.

Recent runtime diagnostics did not establish conflicting duplicates in the
reported windows. This repair is not an explanation or a fix for the repeated
74-minute provider gaps. Re-reading sample minutes returned no recovered bar;
trade samples cannot explain unsampled gaps or supply missing prices.

Existing saved observations are not relabeled or backfilled by this patch.
No trading thresholds, data-freshness limits, PAPER locks or T1/T2/T3 stop
movement change. GitHub review credentials and extended-session quote entitlement
remain separate configuration limitations.
