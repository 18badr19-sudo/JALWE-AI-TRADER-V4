# ORB candle evaluation review

Review scope: the independent first-15-minute ORB research model used by the
pre-open entry study and retrospective archive. This is not an audit of every
native JALWE strategy or evidence that a strategy is profitable.

## Reproduced input handling defects and correction

`observe_session` validated a sorted, numeric copy of the data. `simulate` then
used the original frame, so transport order and numeric representation could
change the model after validation had passed:

- A complete chronological synthetic path produced a target exit at entry
  10.11; reversing exactly the same rows produced INVALIDATED_BEFORE_ENTRY.
- The same valid OHLCV encoded as numeric strings passed coverage validation
  but raised TypeError during entry comparisons.
- An extra off-grid record could be ignored by the minute coverage validator
  and then incorrectly consumed by the model.

The simulator now selects exactly the validated calendar's expected minute
timestamps, converts OHLCV to numeric, normalizes UTC and sorts before computing
the opening range and signal. Boolean prices/volumes remain unknown rather
than becoming 1/0. The input frame is not mutated, missing bars are never filled,
and duplicate/corrupt expected minutes still fail the original validator.
Valid chronological production inputs retain identical results. The existing
provider/retained-evidence reader normally already normalizes its data, so this
reproduction is not proof that these defects caused any deployed rejection.

## Confirmed conservative policies, not automatically changed

1. The model demands a complete regular session before evaluation. A missing
   late minute can therefore hide a result that a sufficiently complete earlier
   interval might support. The review reproduced this with a target already
   reached before a deliberately removed final minute. This does not establish
   where gaps lie in the actual 24 incomplete archival samples.
2. Touching the opening range low before confirmation cancels the opportunity
   for the remaining session. A later valid high reclaim is still ignored. This
   is an explicit strategy policy, not a candle-format error. Its superiority
   or inferiority to a reclaim variant remains untested on adequate matched data.
3. The model's next-open price and assumed costs do not measure bid/ask spread,
   queue position, liquidity at execution, native multi-target management or
   actual broker fills. SHFS's positive post-selected simulation is not an edge
   estimate for the case/control sample.

A future causal-interval comparison must prove contiguous, valid minutes from
the session opening through range formation, confirmation, next-open entry and
the first exit. A gap before exit cannot be skipped; a missing interval with an
open position remains unknown. A session-close exit requires a complete path
to the close. Stop/target touched within one minute stays ambiguous unless the
minute opening resolves the ordering. A high-reclaim alternative requires its
own fixed version and matched comparisons, with no automatic trading changes.

Validation includes order invariance, numeric/timezone invariance, off-grid
records, boolean rejection, non-mutation, the two unchanged conservative policies,
and the existing gap, stop/target ambiguity, cost and future-data tests. No trading
gate, capital, stop, provider budget, stored cohort/result, PAPER/LIVE setting or
native ledger is modified by the input normalization fix.
