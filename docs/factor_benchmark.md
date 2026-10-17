# Daily factor benchmark

`daily-factor-benchmark-v1` adds a **separate** daily endpoint to the movement
research worker. It reuses existing cached SIP bars and makes no provider or
broker requests. Existing minute studies, frozen cohorts, learner states, the
owner PAPER priority trial, entry gates, sizing, and stops are unchanged.

The fixed rules measure prior-day range ≥8%, volume ≥2× the preceding session,
close-to-close momentum ≥3%, and the conjunction of all three. Inputs require
the exact two previous calendar sessions, finite valid bars, a prior price
between $0.50 and $100, and prior dollar volume ≥$100k. No target-day input
selects a signal, its control, or its price/liquidity bucket.

Historical sampling covers **all eligible cached current-catalog symbols**, not
only stocks selected after a jump. For each rule, matching and nonmatching
counts include outcomes that failed to reach the daily endpoint and unknown
outcomes. A deterministic hash pairs matching/nonmatching stocks without
replacement inside coarse prior-price/liquidity buckets **before** inspecting
outcomes. Missing outcomes do not cause replacement or become failures. The
summary includes pair coverage and an equal-day-weighted frequency difference.
Historical earlier/later periods are chronological and reported separately;
neither is presented as unseen validation of rules chosen after past research.

The endpoint is **provider daily high ≥1.08× provider daily open**. It is not the
minute path's +8%/−3% first-barrier label, not an executable entry/exit, and not
profit. Provider daily-session scope is not established as regular-session
minute coverage. Bars settle no earlier than regular close plus 24h16m to avoid
treating a still-changing daily bar as final. Duplicate, invalid, absent,
nonfinite, and boolean price/volume values remain unavailable.

After defining the rule hash, the worker seals independent future cohorts from
the original immutable pre-open SIP feature snapshots. Freeze time must follow
feature availability and precede session open, rechecked after processing under
the write lock. Existing members cannot be changed on restart or rerun. These
cohorts use independent `daily_factor_cohort_v1:` state keys, are retained for
60 days, and never relabel original samples or enter the learned-pattern policy.
Their daily endpoint waits for the same conservative publication delay.
Once a future member's valid daily endpoint has been observed, its outcome is
preserved separately for the cohort's retention window. Expiry from the rolling
provider cache cannot turn a previously known outcome back into unknown; missing
members remain eligible for later data without replacing cohort membership.

`MARKET_FACTOR_BENCHMARK` logs historical denominators, matched controls, unknown
outcomes, and separate prospective waiting/evaluated coverage. Full fixed-rule
statistics and chronology are in the research packet. Results cache once per
daily source/maturity/cohort change, and optional errors cannot stop minute
studies. There are no extra intraday queue seats or increased request rates.

Limitations remain explicit: active-catalog survivorship, coarse matching and
unmeasured confounders, repeated stock/day observations, retrospective rule
choice, and no proven predictive advantage. No benchmark result activates or
changes trading automatically.
