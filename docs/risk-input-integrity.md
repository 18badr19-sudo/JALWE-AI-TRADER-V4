# Finite risk evidence and whole-share entry

Risk approval rejects nonfinite or boolean numeric evidence rather than treating
it as a score, balance, ATR, price, or risk percentage. Invalid broker account
payloads produce a rejected risk decision. Finite numeric strings remain
supported. Existing risk limits, allocation grades, ATR clamping, price range,
entry ceiling and daily-loss threshold are unchanged.

Reproductions on the previous release:
- A NaN daily starting balance or AI score could pass its comparison guard.
- NaN broker equity could be hidden by `min(strategy_equity, broker_equity)`.
- Infinite ATR could be clamped to an apparently valid stop. Other malformed
  inputs raised exceptions instead of returning a rejected decision.
- The final execution guard truncated a fractional quantity and accepted an
  infinite last target; invalid evidence could reach broker preflight.

The final guard now validates whole shares, finite ordered prices and numeric
risk before broker reads or the submission callback. The submission path uses
the same whole-share parser. This is validation, not a change to trading policy.

`tests/test_risk_input_integrity.py` covers these regressions, valid numeric-string
compatibility, the 3% daily-loss boundary, finite ATR clamping, and an independent
Decimal sizing reference across 60 input combinations. Tests use mock broker
objects and temporary local storage; they do not establish that malformed input
occurred in production or explain any historical loss.
