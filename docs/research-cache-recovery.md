# Refresh daily research comparisons when evidence changes

The daily-factor cache previously keyed on the source session, mature session and
cohort keys. A missing daily candle could arrive later within the same source
session without invalidating that cache, leaving recoverable results unknown.

The key now includes a deterministic fingerprint of normalized OHLCV evidence,
the universe, session calendar, rolling-window boundary and definition timestamps.
Transport order and unrelated provider metadata do not invalidate price evidence.
Invalid and duplicate symbol-days remain unknown. Existing forward cohort identities
and preserved observed outcomes are unchanged; retrospective corrections do not
rewrite sealed membership or become prospective validation.

No broker, risk, entry, stop or priority policy changes. No new provider requests.
Regression coverage reproduces missing-data recovery, same-session corrections,
transport-order cache hits and universe changes.
