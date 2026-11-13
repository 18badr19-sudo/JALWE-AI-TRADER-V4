# PAPER bearish opportunity policy

PAPER_BEAR_95_V1 is a deliberate experimental policy change, not a proven
profitability improvement or a fix for broker submission. Previously momentum
and pullback candidates required a perfect native 100 to meet the bearish
routed threshold of 90. In explicitly PAPER-only configuration (PAPER_TRADING
true and ALLOW_LIVE_TRADING false), their penalty becomes -5 instead of -10:
native 95 passes, 94.99 does not. The routed threshold remains 90.

All bearish PAPER routes are capped at 0.50% strategy-capital risk, and cannot
exceed the original opportunity allocation. Other regimes retain their policy.
Default router construction, LIVE-enabled and ambiguous configurations retain
the original policy. PANIC and UNKNOWN remain blocked. Opportunity quality,
freshness, liquidity, spread, session, trigger, breakout, broker eligibility and
execution risk checks remain required. Scores around 58-63 are not rescued.

Decision and native research constructors receive the same explicit mode flags.
Candidate evidence records the policy, native score and applied adjustment;
historical records are not recomputed. Existing bearish reachability notes
describe the standard policy; this is its PAPER-only exception.

No historical replay establishes that this change would have bought a winner.
No order is forced for validation. Natural PAPER entries and subsequent fills
are still required to establish runtime entry and exit evidence.
