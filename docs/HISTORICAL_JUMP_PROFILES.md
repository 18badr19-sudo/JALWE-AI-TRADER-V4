# Evidence profiles for historical jumps

`historical-jump-profile-v1` adds descriptive analysis to actual saved chronological
minute paths. It has no broker API, model API, order authority, trading score or
automatic application. It does not change the forward protocol or its validation.

New observations use the same validated contiguous segment as the jump-path
calculation. The profile records exactly twenty pre-onset minute bars when
available: volume buildup, return, range and context VWAP. Fixed descriptions
are volume acceleration >=2x, return >=1%, close at/above context VWAP, and
range <=2%. Unknown inputs remain UNKNOWN rather than negative/neutral evidence.
Onset selection is retrospective; availability of earlier bars does not prove
the bot could have chosen this onset live or that these factors caused a jump.

The observed onset-to-peak minute bars are divided into three equal-count phases
for volume distribution. First threshold minutes at +8/+20/+50/+100% describe
the saved path, not trade targets or fills. A prior-minute-low onset excludes its
own minute high from these milestones. Drawdown compares a previous minute's
high with a later minute's low; same-minute high-to-low order is never assumed.
Whole onset-minute volume is not represented as exact tick volume after onset.

For new observations, post-peak 5/15/30/60-minute windows require every actual
minute in each requested window. Peak-minute lows are excluded. A gap or window
past the segment end yields UNKNOWN, with no inferred price or assumed stability.
Saved-only paths do not contain these later bars, so their post-peak fields stay
UNKNOWN_NOT_RECORDED. They can reuse only temporally marked derived pre-context
already saved in the original record; missing raw pre-range stays unknown.

At most 24 saved observed records gain profiles each cycle with zero provider
reads. This update adds only the nested profile, using compare-and-swap on the
old result JSON. It never changes features, outcome, status, attempts, checked
time or frozen cohort membership. Repeating the upgrade is idempotent.

The descriptive factor memory includes only completed-session strong-jump
profiles. Partial segment profiles stay separate; prospective/other roles are
excluded. Duplicate symbol/session profiles count once, and conflicting copies
are excluded. Presence counts use known-context denominators and show unknowns.
These are selected historical winners without a control base rate, not learned
probabilities, causal findings, predictive edges or validated strategy changes.
Future validation and user approval remain required for trading changes.

Packets/status reports expose factor memory and phase analysis. Logs show bounded
`MARKET_JUMP_PROFILE_UPGRADE` results and `historical_jump_profiles` in the normal
`MARKET_MOVE_PACKET`. Synthetic tests cannot establish actual market performance.
