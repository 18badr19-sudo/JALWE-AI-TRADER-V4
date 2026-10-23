# Saved nomination evidence and prospective entry results

`/research_stock SYMBOL YYYY-MM-DD` now reads the actual insert-only PostgreSQL
nomination packet for that exact session, validates its digest/protocol/timing,
and reports membership and the original pre-open freeze time. It does not infer
membership from a retrospective jump profile or from absence of downstream
events. A missing/invalid packet remains unknown.

The immutable `move_pattern_features` inputs are retained for 30 calendar days
instead of deleting the previous session when a new snapshot is prepared.
The same write-once and pre-open transaction checks apply. Current/future
nomination preparation and daily benchmark sealing select current/future dates;
priority consumers continue to query the exact current New York date. Archived
rows cannot create late prospective samples or change entry gates. Snapshots
already deleted by older deployments cannot be recovered by this change.

For a non-member, the report can describe failed eligibility gates only from
the exact saved session snapshot whose input availability/preparation precedes
the packet's freeze. This is explicitly labelled a replay of retained eligibility
inputs, **not** a logged APEX rejection, a market-indicator recomputation, or
causal evidence. The producer emits a bounded `RESEARCH_NOMINATION_AUDIT` summary
only declaring a match when rebuilding the entire packet at the actual original
freeze reproduces the published packet digest. Price ($0.50–$100), prior dollar
volume ($100k), all three prior-day factor conditions, SIP identity, and timing
are unchanged. Missing inputs remain unknown; an eligible replay absent from
the actual list is reported as a conflict rather than assigned a reason.

The stock trace aggregates counts and first/last timestamps across **all available
events** for the stable stock/session signal ID. Latest payloads per stage show
the saved reason and any recorded score/threshold. A separate bounded query
reads up to 1,000 order mappings, so an old submission is not silently lost
behind hundreds of newer radar events. Limits and retention gaps are explicit.
Stage events do not prove fills, and the first radar audit event can describe
an ineligible candidate rather than a successful deep scan. All report database
connections are read-only, with existing connect/statement timeouts; they never
write audit events, request fresh provider data, or call a broker.

The report also reads a separately sealed `ENTRY_STUDY` for that symbol/day.
Only a unique pre-open sample with matching protocol, signal ID and source times
may display the existing ORB15 simulation. Incomplete paths cannot expose carried
profit metrics. Complete models show next-minute entry, range-low stop, 2R target,
and assumed 10/25 bps per-side net-R separately from native broker-reconciled
ledger exits. The findings summary includes this same cost-stressed simulation
sample size/mean. The ORB rules, provider/retry budgets, forward/control cohorts,
learner validation gates, native trading capital/risk/stop and PAPER/LIVE locks
are unchanged. This release does not assert a profitable or causal advantage.

Validation covers preserved/expired snapshots, immutable preparation, exact
packet replay and conflicts, late/unknown inputs, price exclusions versus APEX
rejection, independent simulation costs and missing paths, full-history stage
times versus separate mappings, and real read-only PostgreSQL query round trips
in CI. No native order or Telegram message is sent during verification.
