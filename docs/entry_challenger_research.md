# Volume-confirmed entry challengers

`volume-confirmed-entry-challengers-v1` adds two **research-only** entry rules:

* **BREAKOUT_HOLD_VOLUME:** two consecutive closes at/above the frozen trigger;
  the second candle stays at/above it and has volume at least 1.5 times the
  mean of the preceding five candles (the signal candle is excluded).
* **SUPPORT_RECLAIM_VOLUME:** an earlier candle closes below the frozen trigger
  while staying above the frozen stop; a later candle closes back at/above the
  trigger with the same volume confirmation.

Both enter at the next candle's open, retain the original stop and target
of trigger + 2R, assume 0.10% cost each side, and exit remaining simulation
units at the end of the shared window if no barrier is hit. This is a full
exit proxy, **not** the bot's actual partial-exit PnL or an execution claim.
Insufficient next bars, gaps outside the plan, stop-before-signal and ambiguous
OHLC paths stay separate from actual simulated entries. Only complete,
consecutive, timezone-aware 60-minute OHLCV windows are accepted.

The protocol tag is frozen with new session candidates. Existing pending or
completed trials without this tag are not relabeled or backfilled. Experiments
run only for the four existing breakout candidate families, use their original
frozen price levels, and preserve the native result and preference pipeline.
The labels describe a later signal, not an assertion that the setup was
eligible to buy when the original report arrived. Every input uses the existing
decision feed; volume is feed-specific, not consolidated market liquidity.

The status report counts measured entries, no-entry paths and unknown paths
separately. Counts are trial windows, not unique stocks or independent samples.
No winner is selected and neither rule is added to the native execution router.
No capital, risk, stop, live-trading or automatic preference settings change.
These fixed thresholds are hypotheses, not optimized or demonstrated edges.
Any future activation requires separate validation across later NY days with
adequate coverage, transaction costs, and owner review. Synthetic tests verify
the implementation only; they do not demonstrate market performance.
