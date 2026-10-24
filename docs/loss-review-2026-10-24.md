# PAPER loss review — 2026-10-24

Reviewed JALWE main 3628577104bd6c29a20c9bda0d5236b3c9883fa8,
tree 684b8823b94cfc82a9def738035f878e4a964601, matching Railway deployment
717aec9f-cfc4-4e1e-9c02-838e34ad5c47. Saved evidence, not direct broker/SQL access.

## Evidence and limits

The saved native audit at 2026-10-24 10:03:29 Saudi lists eight closed PAPER trades:
two positive, six negative, gross positive $4.26, gross negative $5.039989,
net -$0.779989 before unavailable broker fees. Strategy wallet approximately
$99.22 from $100; Alpaca's approximately $100k is a separate account.
Eight outcomes are insufficient to establish a stable success rate or superiority.
Seven historical trades lack detailed exit execution snapshots; do not infer their
loss causes, actual slippage or stop/target ordering from rounded notifications.

Read bounded Railway INVE logs 2026-10-21 22:40–23:10 Saudi (51 filtered rows)
and 2026-10-22 16:25–16:40 (3 lifecycle rows), neither hitting the 300-row limit.
INVE entered at 22:46:28 Saudi, 13m32s before regular close, under GAP_AND_GO.
Entry 33 then cumulative 34 shares at $2.80, broker stop $2.77, internal stop
$2.7661. Extended exit was deferred at 23:00:09 with SUBSCRIPTION_REQUIRED.
The closed audit records 34 total exits and -$1.29999 on the next regular session;
no T1 or T2 completion. The buy execution was better than its $2.81 reference.
This does not establish a profitable counterfactual earlier entry or earlier exit.
A GTC stop is not an assurance of extended-hours execution or an exact fill price:
https://docs.alpaca.markets/us/docs/orders-at-alpaca

## Confirmed research defect and correction

runner_stops raised the research stop to T1 immediately at T1, contrary to the
native T1-to-entry policy, and could lower a previously raised floor on a later
candle. Reproduction: entry10/stop9, T1=12, bars with high12.2 and low11.6 produce
AMBIGUOUS_RAISED_STOP even though the true entry stop10 cannot be touched.
Correct to T1->entry, T2->T1 or the compared T2 floor, and monotonic floors.
Preserve ambiguity where the new actual floor and target share a candle.
Version only the runner sidecar. Freeze its protocol on new candidates and exclude
old saved runner comparisons; do not reinterpret old rows or change native PnL,
strategy selection, risk, order/stop settings or provider subscriptions.
This defect affects research comparisons; it is not evidence of INVE's loss cause.

## Priority comparisons for the coming sessions — proposed, not enabled

1. Calendar-aware near-close entry restriction against the original entry policy,
   including early-close days; proposed 30-minute exclusion is an experimental
   threshold, not an optimized or proven profitable rule. Existing positions and
   broker stops must continue management regardless of entry restriction.
2. Current stop versus the existing structure/ATR challenger, on the same complete
   windows and same dollar risk with quantity reduced for wider stops. Include
   spreads, gaps and costs; missing data stays unknown. Never widen a live open stop
   just because the trade is losing.
3. Confirmation/hold or retest versus direct entry on frozen complete windows;
   count missed winners and avoided losses, distinct days and symbols, not just
   postselected winning stocks. No preference promotion from a handful of outcomes.

A lower risk budget reduces dollar exposure but does not make an unprofitable
entry rule profitable. Goal this week is correct evidence and lower avoidable risk;
no positive weekly return or zero-loss guarantee can be made.
