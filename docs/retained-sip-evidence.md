# Retaining observed SIP minutes during research recovery

A delayed SIP recheck previously replaced its whole result with the latest
response. If one read returned 59 minutes and a later read returned another 59,
with a different minute absent, the research stayed partial even though all 60
actual bars had been retrieved. A sparser response also discarded earlier
coverage. A regression reproduces this before the repair.

Delayed SIP research now stores bounded minute evidence in its existing atomic
result JSON. It combines only provider-returned OHLCV for the same observation,
symbol, SIP reader, and exact fixed window. One-minute boundaries and the
existing strict validation remain required. No interpolation, forward fill,
trade-to-bar reconstruction, current quote, or alternate feed is used.

Two distinct copies of a minute are retained at most; a conflict remains
rejected across restart and subsequent reads. Invalid values are represented
as JSON null and remain invalid. A successful empty read cannot erase previous
bars. Request failures preserve the evidence for a future bounded retry but
remain unavailable, never successful results from old outcome metrics.

The daily report and audit identify coverage using saved bars. Current-read
diagnostics are saved separately from combined evidence. This may recover
coverage when separate responses contain complementary real bars; it cannot
recover a minute that never returns, and does not resolve why it is absent.

No original decision, IEX evaluation, strategy preference, risk threshold,
entry gate, order, or PnL ledger changes. Existing completed observations are
not rechecked or relabelled. Existing old partial results contain no raw bars,
so accumulation begins with their next eligible read. Fresh and retry budgets,
the 16-minute SIP delay, three-retry/48-hour limit and database schema stay the
same. No additional market-data requests are made.

Synthetic stop tests also verify that T1 moves the stop to entry and T2 moves
it to T1, surviving restart and replacing broker protection for only the
remaining shares. Mocks do not establish actual broker execution or LIVE
readiness. The first INVE trade did not exercise both target transitions.
