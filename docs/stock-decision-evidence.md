# Native stock decision evidence

`/research_stock SYMBOL YYYY-MM-DD` now includes the saved native decisions for
that New York calendar day even when the symbol was not a frozen prior-day
nominee. The reader opens SQLite read-only, bounds the query at 2001 rows,
labels truncation, uses exact symbol and timestamp parameters, and never
creates a database or makes broker/provider requests.

The native decision now retains existing fetch/completed-bar diagnostics:
latest provider timestamp, latest completed timestamp, and excluded-row counts.
Old decisions without this evidence stay unknown. No later candle is substituted.
Reasons come from the saved decision, not default-false gates. Submission and
filled trades/PnL are not inferred from a decision.

This change does not import general APEX deployment logs into the shared
nomination database. General APEX exclusions still require those logs; existing
frozen-signal trace remains a separate part of the same command. Retaining that
general scanner history is separate future work. Missing provider bars are not
filled or declared recovered. Entry, risk, stop and model policies are unchanged.
