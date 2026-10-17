# Market-relative factor research

`market-relative-factor-evidence-v1` extends the existing daily factor benchmark
without modifying its rules, cohorts, pair membership, or any trading policy.
No foreign repository code is imported. No additional API or LLM calls are made.

The separate endpoints are:

- Provider daily high >= opening price +8%, including non-winners and missing
  observations. This is not the minute UP8_FIRST endpoint or a trade return.
- Provider daily close/open movement minus same-day SPY close/open movement,
  in percentage points. This assumes beta=1, not fitted alpha. Stock and SPY
  bars must both be exact-day, finite, valid and unique. Missing SPY does not
  invalidate an otherwise known high endpoint, and never becomes zero return.
- Paired differences using the original deterministic prior-price/liquidity
  buckets. SPY cancels in the within-day pair difference; reporting the two
  sides' market-relative means separately shows this explicitly. SPY is never
  its own research stock/control.

Means weight observed days equally. Descriptive 95% percentile intervals use
2,000 deterministic circular moving-block bootstrap resamples with five-day
blocks, requiring at least 20 observed days and 60 complete pairs. Blocks cover
observed dates; gaps and longer dependence are limitations. Intervals are not
adjusted for multiple exploratory rules. No p-value, causal effect, calibrated
win probability, promotion decision or validated edge is claimed. The all-history,
earlier and later retrospective periods and sealed forward evidence stay separate.

A durable statistical definition timestamp excludes sessions that opened before
this endpoint was defined from its forward statistics. Existing original cohort
IDs, definitions and barrier results remain intact. Observed forward close returns
and SPY movement are stored with the frozen membership and statistical spec;
daily-cache expiry cannot erase previously observed values. Unavailable returns
remain unknown. The existing provider daily availability delay is unchanged.

News analysis is separate and describes only original ACTIVITY/CONTROL prospective
cohorts, deduplicated by symbol/session. It excludes post-move historical cases,
entry simulations, partial prefixes, ambiguous paths and conflicting duplicates.
Only complete original lookup evidence with created/updated times within its
24-hour window and cutoff <= frozen_at < open is usable. A zero-result lookup
means no news in that saved provider window, not no news anywhere. The provider
retains five articles; absent keywords are unknown if the full list was larger.
Keyword presence is descriptive and cannot establish the cause of a jump.

The existing asset-catalog survivorship bias, coarse matching, provider daily
session scope, missing-minute coverage and unmeasured execution costs still apply.
Nothing in this module submits an order, changes risk, or changes priority policy.
