# Prior-day researcher → APEX link and execution study

The owner-approved PAPER priority trial now nominates stocks **before APEX's
radar/deep truncation**, using a separately versioned shared advisory protocol:
`prior-day-apex-priority-v1`. Its window matches the existing trial, October 17
02:53:52 UTC through October 31 02:53:52 UTC. No new native buy strategy is added.

## Frozen transport

JALWE reads its immutable pre-open SIP feature snapshots. All three inclusive
conditions must hold: prior range ≥8%, prior volume ≥2× the preceding session,
and prior close-to-close return ≥3%. Prior price must be $0.50–$100 and prior
dollar-volume proxy ≥$100k. Availability/preparation/freeze precede the session
open. Publication and entry-study transactions recheck the open before commit.
Missing data remain unavailable; no same-session or historical post-peak input
can nominate a stock. Original prospective cohorts/learner definitions remain
untouched.

The producer creates `jalwe_prior_day_packets` and `jalwe_prior_day_events` in
the existing shared PostgreSQL database. Session packets are insert-only and
carry an integrity digest, authoritative session open/close, source times and
stable per-symbol/session signal IDs. Both repositories use the identical
`prior_day_priority_bus.py`. The digest detects corruption; it is not a digital
signature. Existing database access controls remain the trust boundary.

APEX reads only the current session's valid packet and requires PAPER, complete
full-market scanning, existing native eligibility, tradability and a live trade
no older than 150 seconds. At most 4 of the usual 40 radar seats and 1 of 8 deep
seats are reserved. Five-minute deterministic rotation shares seats among
nominees. Existing early-surge seats/native order are retained behind the new
seats. Pre-confidence and all later research-quality/risk filters still apply;
scores, verdicts and trading gates are never upgraded by the nomination.

Unavailable/expired/malformed transport preserves the native selection. The
`JALWE_PRIOR_DAY_TRIAL_ENABLED=false` override disables both research
priority consumers and publication. Existing PAPER/LIVE locks still govern
actual trading. Database connects/timeouts, packet size, and writes are bounded.

## Independent execution model

`prior-day-orb-execution-study-v1` seals separate `ENTRY_STUDY` episodes before
open. They are excluded from the original prospective learner/comparison. Four
study seats per existing 48-study budget are reserved **after** original forward
cohorts and explicit requests; no extra provider request budget is introduced.

The research-only model requires every valid regular-session minute. It fixes
the first 15 minutes' high/low, then waits for a closed-minute breakout with
≥1.5× the preceding five-minute mean volume and ≤35% upper wick. The confirming
close and next opening price must stay within 1.5% above the trigger. The next
minute's open models entry; a lost trigger or a gap through the stop or chase limit models no
fill. The range low models stop and one full exit at 2R models target. This is a
distinct benchmark, not JALWE's multi-target/ratcheting trade manager.

Entry/exit each incur an **assumed all-in** 10 bps, with a separate 25 bps stress
case. These are not observed historical spreads, fees or queue fills. Stop gaps
use the worse opening price; a minute touching stop and target stays ambiguous.
No-entry, no-next-bar, missing data and ambiguous paths stay separate. Results
are per-share/net-R simulations, never realized strategy-wallet/account PnL.

## Correlated evidence

Stable signal IDs join APEX radar/pre/deep/research/bridge events, JALWE native
decisions and PAPER order-submission IDs. Submission is explicitly not a fill.
The research worker separately reads the native broker-reconciled exit ledger
in read-only mode, correlates managed-trade IDs, and records realized exits
**before broker fees**, separately from simulation costs. It does not contact
the broker or change a managed position.

Research packets/reports show transport status, entry-study outcomes/cost cases,
distinct signal/event counts by stage, and native reconciled exits only when
evidence exists. Missing native evidence is waiting/unavailable, not zero profit.
No Telegram message or order is manually sent by the deployment workflow.
