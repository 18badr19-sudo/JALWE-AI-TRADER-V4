# Native historical execution laboratory

The existing decision replay replays **recorded component answers**. This lab
instead recalculates the native feature, AI scoring, opportunity, regime,
strategy, trigger, breakout and risk gates from timestamped inputs. It then
uses `TradeManager` and `RecoveryEngine._targets_from_actual_fill` to simulate
the lifecycle, including whole-share target allocations, confirmed partial
fills, T1 → entry, T2 → T1, T3 → T2 and the native runner ratchet.

This is an **offline, hypothetical execution model**, not a reproduction of
Alpaca/exchange matching or an automated strategy promotion. Neither Telegram
nor the live worker imports this package. No provider/LLM request is made.

## Run

From the repository with its normal dependencies installed:

```bash
python -m research_lab historical-evidence.json --output historical-comparison.json
```

The launcher strips inherited environment settings. A child process clears the
environment again, disables dotenv, installs network/process and external
SQLite access guards **before importing native modules**, and uses a temporary
database. It has no broker credentials, configured broker submission, Telegram
client, production database or authority to promote a policy. The temporary
schema is necessary because the existing native modules initialize a database
at import. The output path must be new, and a run has a 10-minute timeout.

`RiskEngine.evaluate` accepts optional account/position readers. Existing callers
use exactly the original Alpaca/database readers; the lab supplies a virtual
wallet. No risk constants or production gate behavior are changed.

## Required JSON evidence

Use protocol `native-event-execution-lab-v1`. The synthetic builders in
`tests/lab_fixture.py` document the exact schema and exercise a full native
WATCHING → READY → virtual fill → targets → exit lifecycle. **They are generated
engineering fixtures, not real historical prices or investment results.**

| Field | Required evidence / interpretation |
| --- | --- |
| `start`, `end` | Explicit timezone-aware horizon, at most 366 days |
| `feed` | One `sip` or `iex` source; do not merge feeds silently |
| `provenance` | `kind`, `source`, `universe`, `coverage`, `unadjusted_prices` |
| `configuration`, `configuration_frozen_at` | Exact `research_lab.native.configuration()` profile, frozen before the horizon; mismatch aborts |
| `strategy_selection` | `NATIVE_SCORE_BASELINE`; learned strategy preferences are unsupported |
| `learning` | Timestamped, complete frozen factor multipliers; no current/future learned model is read |
| `capital` | Starting virtual wallet, normally $100; no brokerage $100,000 substitution |
| `sessions` | Provider exchange calendar with date, open and close in UTC; supplied boundaries handle DST and early close |
| `bars` | Symbol, `5m`/`1d`, start `at`, completion `end`, `available_at`, OHLCV; at least 51 symbol 5-minute bars and 52 completed SPY daily bars at a decision |
| `observations` | Symbol, timestamp `at`, contemporaneous eligible trade `price`, quote `bid`/`ask`, displayed `bid_size`/`ask_size` **in shares** |
| `enrichments` | Archived `news`/`options` scores, original availability flags, timestamp and bounded expiry; absent ≠ score zero |
| `batches` | Exact original APEX report membership/order, publication timestamps, batch identity, immutable prior-day snapshots |
| `costs` | Explicit adverse slippage bps, fee bps and per-share fee assumptions, applied to both sides |
| `scan_seconds`, `management_seconds` | Declared simulation timing, **not measured historical processing latency** |
| `holdout_windows` (optional) | Predeclared, disjoint evaluation windows with training cutoff and declaration time |

Bars are exposed only after **both** completion and recorded availability.
Duplicate timestamps, nonfinite prices, fractional/boolean sizes, crossed quotes,
invalid OHLC geometry, future policy snapshots and stale initial report
membership are rejected. No absent minute, quote, trade, optional source or
historical model weight is fabricated. Missing required inputs are
`DATA_UNAVAILABLE`, separately from native REJECTED/WATCHING decisions.

The input's provenance/coverage declarations must come from a trustworthy
collector. The lab validates structure and availability constraints; it cannot
prove a collector delivered all market events or all APEX reports. Splits,
dividends and symbol changes require prepared, consistent unadjusted evidence;
this version does not process corporate actions.

## Two independent wallets on the same tape

1. Baseline follows the **supplied APEX batch order before the owner trial**.
   This is not automatically the raw scanner order: include any earlier learned
   priority already applied when exporting the batch.
2. Researcher reorders the **same members**, using the existing owner trial's
   exact pure `snapshot_match` function: prior-day range ≥8%, volume ratio ≥2,
   return ≥3%, all required, with pre-open SIP evidence.

The lab evaluates the fixed hypothesis retrospectively even outside the live
trial's dates. It does **not** activate or extend that trial. It does not replay
`patterns-v1` learned policy activation/training, add historical winners to the
candidate list, or change native entry gates. Missing snapshot evidence is
reported and blocks a complete comparative result.

Scan latency is an explicit constant assumption. Changing rank can change the
simulated analysis time, current quotes, open positions and available wallet.
Pending entries reserve both cash and a position slot. Both arms see only the
data available by their own simulated clock, use the same initial wallet and
cost model, and cannot spend reserved cash twice.

## Fill and management model

- Entry submits the native protected limit. Only a **later** observation may
  fill it, at ask plus slippage, capped by displayed size and the limit. There is
  one fill attempt; an unfilled remainder is canceled. No quote within 30 seconds
  expires the request. This is a conservative approximation, **not the native
  live DAY-limit queue/retry behavior**.
- Targets are rebased from the actual simulated entry fill using native
  recovery logic. The native manager changes realized quantity and target
  completion only after a cumulative simulated broker fill is reconciled.
- An observed stop crossing triggers an exit. Execution occurs on a later bid
  minus slippage; a gap may execute far below the trigger. No target/stop order
  is inferred from a daily or minute bar high/low.
- Exit quantities are capped by displayed bid size. Partial fills reconcile
  cumulative quantity and weighted average price without double counting.
- If a target remainder is pending when a stop is observed, the model cancels
  that remainder before stopping the remaining position. Cancellation latency
  is assumed zero; no exchange race or queue position is modeled.
- Native management is polled at the declared interval; observed highs are
  accumulated only from observations after entry. Stop detection is checked on
  each supplied observation. Sparse observations can miss crossings.
- Only the supplied regular session is eligible. The simulator does not model
  extended-hours routing, broker stop-election sale conditions, halts, network
  failures, emergency/manual exits or restart recovery.
- Horizon-end positions stay open. Bid marks require a quote within 30 seconds;
  otherwise marked equity is unknown. No artificial final sell inflates realized
  PnL. Entry fees are charged immediately; `net_realized` therefore includes all
  charged fees, including entry fees on positions still open.

## Interpretation and next data requirement

Output includes source hashes, data hash, exact configuration, assumptions,
per-decision gate/call traces, simulated fills, gross/net realized amounts,
open positions, observed drawdown, daily last-observed equity and paired ending
marked equity difference. Marked equity is not liquidation proceeds and does
not deduct a hypothetical future exit fee. Drawdown is sampled, not the exact
continuous-market maximum.

Optional holdouts reuse the same frozen policy, reset capital per window and
report **every** predeclared window. There is no parameter search or retraining;
these are sequential holdouts, not an adaptive walk-forward learning system.

`SYNTHETIC_ENGINEERING_RUN` is only a software check.
`INCOMPLETE_EVIDENCE` suppresses a complete paired profit comparison.
`HISTORICAL_SIMULATION_ONLY` still does not establish prospective profitability.
`policy_promotion_allowed` is always false. Selected historical jump winners
cannot become a valid whole-bot comparison simply by producing a positive PnL.

**Current blocker for a real-market comparison:** existing movement summaries,
fills and coordinator replay capsules do not contain a complete ordered tape of
historical trade/quote observations, all original APEX batches, frozen learning
profiles and optional-source inputs. Supply a validated archival evidence bundle
or collect it prospectively. This PR does not claim those missing market data
were recovered, and it does not change the running collector.
