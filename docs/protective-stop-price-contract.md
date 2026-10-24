# Protective-stop price evidence

Stop preparation, watcher synchronization, execution and the Alpaca adapter share `broker.stop_pricing.normalize_stop_price`. It preserves existing rounding (two decimals at or above $1; four below), rejects boolean/nonfinite/nonpositive/unparseable values and rejects positive inputs rounded to zero. Validation precedes broker reads, cancellation, persistence of a replacement intent and submission callbacks. Internal strategy stop/target/risk calculations are unchanged.

An active stop must provide a finite, positive, tick-valid `stop_price` from the broker snapshot. The watcher cannot substitute a metadata price for this evidence. Missing or malformed active price evidence raises without canceling the existing order. Filled/terminal snapshots continue through cumulative fill reconciliation without requiring an active stop price.

A weaker actual stop is replaced after confirmed cancellation, using remaining quantity after any cancellation-time fills. A matching rounded stop is retained without cancellation churn. A stronger observed SELL stop is retained and reported at its actual price; quantity-only replacement preserves that stronger observed floor. This does not change the internal strategy floor.

Existing identity, cumulative fill, ledger, uncertain-submission and cancellation-terminal checks remain in force. Cancellation followed by submission is not an atomic broker replace and may still encounter price movement or broker failure. These tests do not prove a new replacement occurred on a real broker account; no trade is forced for verification.

Regression tests reproduce the original $8 broker/$9 metadata false match and invalid replacement cancel-before-validation errors, cover below-$1 one-tick differences, rounding boundaries, unknown broker prices, stronger protection, quantity changes and all submission boundaries. Existing legacy fixtures now supply the broker stop price when they assert that active protection was confirmed.
