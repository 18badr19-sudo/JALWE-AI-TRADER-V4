# Native trade factors and bounded focused recovery

`/trade_factors` compares the latest 20 native trades with a complete, arithmetically consistent closed ledger. It uses the saved entry decision linked by both managed trade ID and broker order ID. It never substitutes a newer same-symbol report, recomputes an old indicator or fills missing entry features from subsequent research.

WIN, LOSS and FLAT are separate. Each flag has present, known and unknown counts; unknown is not absence. Open, inconsistent and duplicate trade ledgers are excluded. Strategy names remain categorical descriptions. Initial stop distance uses the original internal stop, not the current trailing stop or the broker's rounded price. ATR ratios use only the original saved feature and retain its timeframe. Last-30-minute entry uses a valid saved calendar retrospectively, including early closes; absent calendar means unknown. Overnight holding is a post-entry description, not a predictive entry feature.

These are descriptive associations in executed native PAPER trades, separate from stocks selected after a jump. The small sample cannot establish a cause of loss, predictive edge or a new trading rule. Saved ledger PnL is before unavailable broker fees. Older missing execution snapshots remain explicitly tagged; this report does not claim independent broker reconciliation. No model, order, risk gate, stop policy or preference is changed.

The `NATIVE_TRADE_FACTOR_REVIEW` startup log exposes the same whitelisted facts and limitations for a read-only review. No unrelated decision metadata or credentials are exported.

## Focused minute retrieval

`focused-two-minute-repair-v1` retains the original pre-open focused panel and collection protocol. After close plus the existing 16-minute delay, at most two real one-minute SIP/split bar reads replace the two post-close trade diagnostic probes. Intraday trade diagnostics are unchanged. The existing three durable post-close reservations bound this to at most six logical targeted reads per panel; existing retry limits and overall worker budgets are unchanged.

Only genuinely absent timestamps are eligible. Returned invalid or conflicting bars are not silently overwritten. Preselected pairs closest to coverage completion are prioritized by missing coverage, never by price outcomes. Each targeted symbol/minute is attempted once across restarts. The exact requested minute must be an actual valid aware SIP/split OHLCV row; inclusive-end or unrelated rows cannot fill other gaps. Boolean, duplicate, nonfinite, wrong-feed and empty results remain unknown. Authentication, subscription, rate limit, server and timeout failures halt the remaining provider fanout.

Recovered actual bars join retained focused evidence; no interpolation or synthetic zero-volume bar is created. Original bulk response frames remain unchanged as the record of that read. The report shows recovered minutes and attempted reads. The collector does not rewrite expired historical cohorts or guarantee that missing provider minutes will become available. Logical read limits do not override the provider client's own existing transport policy.

Tests use temporary local databases, synthetic fixtures, fake keys and a socket/DNS block. They cover exact entry linkage, unknown denominators, early-close calendars, NY holding dates, numeric corruption, initial-stop thresholds, no-evidence reporting, restart-durable retrieval limits, inclusive endpoints, invalid source rows and provider failure fanout.
