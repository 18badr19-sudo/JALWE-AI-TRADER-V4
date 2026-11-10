# Exceptional bearish long routing

Momentum breakout and pullback continuation previously had maximum routed
scores of 82 and 85 against a minimum of 90 in BEAR_TREND. Their penalties are
now -10, so only a native score of 100 can pass. This is a deliberate policy
change, not evidence of trading profitability. The threshold remains 90 and
the regime risk multiplier remains 0.50. Other regimes, strategies, broker
eligibility, freshness checks, downstream gates and LIVE settings are unchanged.

Decision evidence retains each candidate's native score, market adjustment and
routed score. Reports display saved values, including historical penalties,
without recomputing them using today's policy. Scanner reports explain saved
broker-ineligible and stale-minute reasons without inventing a provider cause.

Boundary tests cover 99.99 rejection, 100 acceptance, risk caps, rejected inputs,
PANIC/UNKNOWN, invalid scores and immutability. These tests establish behavior,
not market execution. A naturally qualifying PAPER trade and broker fills are
still required to demonstrate entry and the complete exit lifecycle.
