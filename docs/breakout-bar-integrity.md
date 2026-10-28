# Breakout confirmation evidence integrity

Reproduction on the previous version: append a newest candle with NaN close to
a frame whose prior candle confirms a breakout. `_prepare_bars` drops that row,
then confirms the older candle. An infinite newest volume or duplicate final
timestamp can also be approved by the isolated confirmation engine.

Preparation now blocks non-finite or boolean OHLCV, invalid price geometry,
negative volume, invalid timestamps and duplicate instants. Valid numeric text
and reversed chronological input remain normalized. It does not fill gaps or
silently replace a corrupt latest row with an older candle.

These are isolated engine reproductions, not evidence of an executed bad trade:
upstream feature checks and downstream risk/order gates remain separate. No
thresholds, sizing, stop policy or trading mode change. Timestamp validation
does not itself prove candles are closed; existing market-data completion and
freshness checks retain that responsibility.
