# Isolated ten-day PAPER trial

Status: PREPARING; branch created, no trial service deployed yet.
Base main: 2387c5f5c3827479fd045c4549afb3b5896856c3.

Use a separate Railway service/environment and volume/database. Do not reuse the production controller data directory or Telegram bot token. A separate branch alone does not isolate execution. Never run two trading writers against the same Alpaca PAPER account. Require independently verified separate PAPER account identity before enabling submissions; otherwise use observation/replay only. Do not copy secrets into this repository.

Initial environment: ALPACA_BASE_URL=https://paper-api.alpaca.markets, JALWE_PAPER_TRADING=true, JALWE_AUTO_PAPER_EXECUTION=false, JALWE_BROKER_SUBMISSION=false, JALWE_EXTENDED_EXIT_ENABLED=false. These are preparation defaults, not proof of isolation. Verify actual runtime settings and service start behavior before launch. No LIVE, forced trades, cancellation tests, subscription purchases or Telegram messages.

Start the ten calendar days only after deployed commit, isolated storage, worker health and PAPER account boundaries are verified. Persist started_at_utc and ends_at_utc (start + 240 hours). Record market-session count and outages separately; ten days do not imply ten trading sessions or sufficient evidence. Do not restart the clock silently after fixes.

Each hourly review: inspect runtime errors, stale/missing prices, scanner-to-decision flow and gate reasons, broker submission/acceptance/fills, partial fills, quantity reconciliation and remaining protective stop. Preserve event timestamps, order IDs, trade IDs, commit and deployment IDs. Fix reproducible defects on this trial branch; add regression tests, require passing CI before trial deployment, verify deployed SHA and runtime behavior. Preserve risk gates; do not lower thresholds just to create trades. Missing evidence remains UNKNOWN.

Carry forward work/paper-lifecycle-monitor-state.json and outputs/JALWE-PAPER-lifecycle-evidence.md without overwriting historical evidence. Keep trial results separately under work/paper-10-day-trial-state.json and outputs/JALWE-PAPER-10-day-trial.md. Compare production read-only; all fixes stay on the trial branch.

Acceptance: no unresolved critical defects; no duplicate orders; quantities and protective stops reconciled after every observed fill; fresh-price provenance; real market PAPER T1 -> T2 -> T3 -> runner evidence for one trade. Tests and simulated profits are not execution proof. SIP subscription denial means extended exit remains unverified. A stop/manual close before targets is its own outcome, not a completed ladder.

At expiry, produce a review with failures, fixes, CI, execution evidence, missing evidence and rollback. Never auto-merge or promote to main: user must review and explicitly approve promotion. Do not abruptly stop protection of open trial positions or force-close them at expiry; agree a wind-down plan. Keep the independent October 4 review unchanged.
