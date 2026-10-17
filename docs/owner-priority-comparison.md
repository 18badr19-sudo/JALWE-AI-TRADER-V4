# Owner PAPER priority experiment: observation plan

The existing `owner-prior-day-confluence-paper-trial-v1` experiment already reorders the same fresh APEX reports using pre-open SIP snapshots. Its original enable/expiry settings, three-factor conjunction, entry gates, risks, targets and stops are unchanged. This follow-up starts recording a reproducible same-batch rank comparison; it does not activate a second policy or place alternate orders.

## What is measured now

`MARKET_MOVE_TRIAL_COMPARISON` logs session day, source packet ID, observation time, report count, the original and resulting ordered symbol occurrences, matching-report count and their mean ranks. Repeated symbols remain distinct occurrences within that batch; they are not independent stocks or trades. The baseline is explicitly **after learned priority and before the owner trial**, not the raw APEX universe. Both orderings contain exactly the same objects. These observations exist only when the existing experiment can read an eligible current packet; missing evidence stays unknown.

The complete batch comparison is also in the existing trial audit. Its digest changes when either batch order changes, allowing the normal logger to record a changed cohort without repeating unchanged observations. Report ranks cannot establish hypothetical execution times, missed trades, profits or a causal advantage. Actual native decision and broker evidence must establish those separately. Synthetic tests are never market results.

## Prospective comparison protocol

Hypothesis: pre-session factors can prioritize otherwise eligible reports early enough to reduce expiry before analysis. Freeze the code, native gates and three-factor rule before collecting new sessions. Exclude historical winner-selected studies and incomplete/ambiguous paths from outcome comparisons; retain their counts and reasons in coverage denominators. Never use post-decision facts to choose a report.

The primary desired metric is the percentage of eligible reports analyzed before their data expires, compared on the same report cohorts. **Only the actually processed ordering has measured processing times**. The alternate order is currently ranks only; a timed replay or independent shadow scheduler is still required before claiming a paired timing improvement. Do not turn rank improvements into fabricated timing or PnL.

For eventual outcome comparison, join immutable source reports and native decision timestamps, keep market coverage and freshness explicit, and separately report T1/stop ordering, MFE/MAE, R, documented costs and drawdown. A target and stop in the same bar are ambiguous without finer evidence. No missing candle means an unchanged price.

Initial design target: at least ten future sessions and fifty valid paired observations; this is a collection target, not proof of statistical power. Proposed review thresholds are a ten-percentage-point improvement in timely coverage, no more than five points deterioration in stop-before-T1 frequency, and positive mean R difference with uncertainty reported. If paired timing/outcome data are unavailable, the result remains **RANK_OBSERVATIONS_ONLY** regardless of how many ranks were collected. One day cannot justify promotion. Do not extend the existing experiment expiry or activate learned preferences automatically to meet a sample target.

## Review output

Report sessions, unique stocks, reports and repeated observations separately; packet freshness; unknown features; baseline/experimental ranks; coverage of measured processing times and outcomes; and which comparisons remain unmeasured. Compare the current commit with Railway deployment and cite individual dated evidence. If no new real batch or trade is available, say waiting. All execution remains native PAPER; LIVE stays disabled.
