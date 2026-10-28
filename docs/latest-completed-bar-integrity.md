# Preserve latest completed candle identity through normalization

The provider adapter coerced OHLCV and dropped NaN rows before excluding open
intervals. If the newest *closed* candle was corrupt, a valid older candle was
returned as the newest. It could still pass a 15-minute freshness check. The
downstream breakout validation cannot detect a row already deleted upstream.

The adapter now identifies the latest completed provider row before numeric
cleaning and blocks analysis if cleaning removes it. It does not substitute or
fill a missing candle. Corrupt current open intervals remain excluded, and the
existing older historical-row drop policy is unchanged. No extra provider read,
subscription, threshold or order/risk/stop change is introduced.

Regression executes the actual production get_bars method with a mocked client
and clock. The corrupt closed-price case failed before the fix; coverage also
checks all five OHLCV fields, invalid numeric text, older corrupt rows and the
existing incomplete-row behavior. This proves an adapter bug in isolation,
not its occurrence in production or a cause of historical losses.
