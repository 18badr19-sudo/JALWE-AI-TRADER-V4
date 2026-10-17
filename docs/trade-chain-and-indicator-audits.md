# Native trade chain and indicator audits

This change follows the whole native stock PAPER path: source bars, indicators, saved entry decision, owned broker entry, realized exit ledger, and current saved stop/protection state. It adds no LLM/provider reads and never submits or changes an order.

## Fixed defect

FeatureEngine previously counted duplicate timestamps, inconsistent OHLC bounds and infinite volume as good input. Native diagnostics and feature generation now reject these structurally malformed frames. Nonfinite OHLCV, nonpositive prices, negative volume, duplicate columns and invalid timestamps also fail quality checks. Zero-volume supplied bars are permitted, not invented. Existing freshness, activity, strategy, risk and exit policies still apply. This reproduces a code defect, not proof that a historical loss was caused by it.

The native indicator calculation block is factored into `calculate_indicators` unchanged and shared by production `build` and the offline auditor. No alternate indicator implementation or replacement trading framework is installed.

## /trade_audit [SYMBOL]

Read-only SQLite snapshot; no schema initialization or broker calls. Join the trade to its entry order by ID and to its entry decision by BOTH managed trade ID and entry broker order ID. A later rejection for the same symbol cannot become its entry reason. Exit increments are joined by trade ID; order snapshots by exact ID.

Check position quantities, ledger coverage, order symbol/side, cumulative filled quantity, weighted exit average, per-event PnL arithmetic, and recorded entry basis. Report saved decision-reference price deviation (not complete execution cost), initial dollar risk, and available decision/submission/fill timings. Fees are unavailable and never treated as zero. Missing snapshots or entry links stay unknown; stale/inconsistent snapshots are review findings, not proof of a current runtime or broker defect.

For open positions only, compare saved stop with the floor implied by completed T1/T2/T3 flags, and compare saved protection quantity/price with remaining position/current stop. Do not infer target touches from later daily highs or demand that stop orders fill exactly at their trigger. Closed snapshots cannot prove historical protection. No causal conclusion about a market loss is generated.

The controller emits `NATIVE_TRADE_CHAIN_AUDIT` for up to 20 recent native trades at startup. The command shows up to 10 recent trades or 5 for a symbol. Up to 1,000 exit events per trade; larger evidence sets are reported truncated rather than partially totaled. Output and counts explicitly describe the selected range.

## /indicator_audit

Future real native decision transcripts are sampled in the background audit worker (up to four/day, retained 14 days), after a successful coordinator replay. Nothing is added to the order path. The auditor tests the native indicator series at several cutoffs: values on an observed historical prefix must agree when later bars are added and when only those later prices/volumes are perturbed. Unknown/NaN-only indicators are listed unmeasured.

Warmup sensitivity compares the same final observation using 51/100/200/all available bars. Differences are descriptive, not automatic bugs or reasons to change a gate. VWAP session context and exponentially weighted initialization can legitimately differ. This samples native indicators only; it does not certify every strategy, data timestamp, LLM training cutoff or end-to-end execution timing. Synthetic tests are not market evidence. Until a new native transcript is available the command says it is waiting.

Validation covers malformed evidence rejection, prefix invariance, intentionally injected future leakage, warmup sensitivity, read-only files, exact entry ownership, cumulative partial exits, weighted price/PnL math, incomplete evidence, protection snapshots and Telegram command routing. Existing full PAPER tests remain required.

## Follow-up from real saved evidence

The first production audit found complete exit-ledger quantities for seven closed trades, but older broker stop-order rows still held submission-time quantities/prices. Future validated protective-stop fill snapshots are now persisted with the realized ledger event and managed position in the same SQLite transaction. A persistence failure rolls all three back; repeated cumulative snapshots do not add realized PnL twice. Zero-new-fill terminal observations also refresh the saved order snapshot. Historical snapshots are not fabricated or rewritten by the auditor.

The saved top-level research report timestamp occurs after orchestration, so it is not used as decision start time. Timing uses the recorded native entry-quote check when available, plus broker submission/fill timestamps. Missing timing stays unavailable.

Entry-decision links use one bounded SQL scan for all selected trade identities, rather than rescanning the full JSON history for every trade. Reads have a ten-second progress budget, and startup audit runs on a daemon thread so it cannot hold up native worker startup. No index/schema mutation is needed. Saturating the entry-link result cap is explicitly recorded.
