# Owner-requested prior-day PAPER priority trial

Authorized October 17, 2026. This is a manual unvalidated hypothesis, not a
promotion of `prior-day-move-patterns-v1` or a new order-execution strategy.
The learned policy continues its original prospective training/validation.

All three thresholds are inclusive and required together:

* Prior session range: `100 * (high-low) / close >= 8`.
* Prior session volume divided by the session before it: `>= 2`.
* Prior session close-to-close return: `>= 3%`.

Only the immutable, pre-open SIP daily feature snapshot for the current NY
session may match. No current-session bar, historical post-jump label, partial
intraday context, unknown input, boolean, or nonfinite number can match.

Matching symbols go first among the **same fresh APEX reports already received**.
Original relative order is preserved within each group. Native strategies,
freshness, breakout confirmation, liquidity, sizing, stops, and existing-position
management remain authoritative. APEX's radar/deep-analysis limits are unchanged;
this cannot recover reports APEX did not send, or authorize a purchase itself.

The fixed experiment window is October 17 02:53:52 UTC to October 31 02:53:52 UTC
(05:53:52 Saudi). Restarts never extend it. It is enabled by default for this
owner-approved release; `JALWE_PRIOR_DAY_TRIAL_ENABLED=false` disables it.
It cannot apply outside PAPER or with LIVE permission.

`MARKET_MOVE_TRIAL_STATUS` records configuration/expiry/waiting for evidence.
`MARKET_MOVE_TRIAL_APPLIED` records matched symbols with exact prior feature
values, snapshot times and source packet. Neither log proves an entry or profit;
link actual subsequent decision/order logs before assessing trading outcomes.
The research status report distinguishes the manual trial from learned patterns.

Assess future matched/nonmatched outcomes and coverage separately, including
losers and missing data. Selected historical winners and synthetic tests do not
establish predictive advantage. This trial does not alter the original cohorts.
