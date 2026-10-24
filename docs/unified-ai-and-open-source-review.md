# JALWE AI integration and comparable open-source systems

Review date: 2026-10-24. This is original local code informed by public designs;
no external bot, pretrained model, agent tool, provider credential or vendored
framework code was installed. No additional package or paid API is required.

| Project / primary source | Relevant design | Existing JALWE equivalent | Addition / limit |
| --- | --- | --- | --- |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | analyst roles, research debate, risk separation | one Gemini call with three perspectives, native risk gates | unify saved evidence; preserve one-model/three-view distinction and exact fact matching |
| [FreqAI](https://www.freqtrade.io/en/stable/freqai/) and [model parameters](https://www.freqtrade.io/en/stable/freqai-parameter-table/) | train/inference separation, normalization, historical evaluation, model IDs and expiry | frozen forward panels, delayed SIP, unknown-data policy | local classifier lab, fixed chronological split, train-only scaler, bounded registry and expiring JSON artifacts |
| [Qlib Recorder](https://qlib.readthedocs.io/en/latest/component/recorder.html) | run IDs, recorded inputs, metrics and artifacts | native replay IDs and strategy research trials | dataset/protocol/source fingerprints and reproducible lab run/model cards |

These systems are design references, not evidence their models are profitable
on JALWE's US-stock/IEX/Alpaca setup. FreqAI's published examples use crypto data;
TradingAgents' provider calls and simulated decisions do not reproduce native
broker fills. No default settings or claimed returns are imported.

## One evidence interface

`/ai_system` reads saved native factor-learning runs, focused research collection,
pattern-learning state, classifier-lab status and shadow-review counts.
`/ai_case SYMBOL` searches the latest 500 native decision events and adds the latest matching saved native decision and an optional saved
Gemini interpretation. A symbol match alone cannot attach a review: provider
facts, native state, bar timestamp/timeframe, feed and supplied levels must match
exactly, and the current prompt/schema must validate. This identifies equivalent
facts sent to the model, **not** the entire decision, unique execution ID or fill.
No SDK constructor, provider call, new training or order is triggered by a read.
Missing sources stay unavailable. Old timestamps are displayed rather than
presenting a historical result as fresh. Old decision/profit evidence is not
synthesized from current data.

Native scoring, learned bounded factor multipliers, descriptive pattern rules,
Gemini language interpretation and the new offline classifier are different
components. The interface does not combine their scores into a buying vote.
Native gates and independent risk/stop/order management retain sole authority.

## Actual local classifier benchmark

The independent research child records an immutable lab definition before the
sessions studied by this new protocol. Data come only from the existing focused
panels: original ACTIVITY/CONTROL membership and pre-open features, plus actual
retained, split-adjusted SIP full-session bars. The lab revalidates raw paths;
postselected jump cases, same-day features, missing/ambiguous paths, duplicate
stock/day evidence and future-dated capture evidence cannot become labels.

Five fixed prior-day features: range, return, relative volume, close and dollar
volume; the last three use log1p where specified. One fixed, strongly regularized
LogisticRegression(C=.1) is fitted using the already installed scikit-learn.
The first ten panel sessions are training, the next is an embargo, and the next
five are test. No random split or hyperparameter search. At least 30 usable
training pairs, 15 test pairs and 90% usable coverage in each partition are
required, with both label classes represented. Unknown registered pairs stay
in the denominator; later convenient sessions cannot replace the first windows.
These are **exploratory lab thresholds**, not changes to the existing 40-pair /
10-day pattern policy, its 20-session future validation, or native trade learning.

Target: an observed full-session +8% barrier before -3% from the session open.
This is neither trade PnL nor native strategy entry quality. Brier error is
compared with a constant training-prevalence baseline; lower error is descriptive,
not proof of tradable edge, calibrated probability or superiority to native JALWE.
The scaler sees training data only. Test predictions, labels, metrics, model
coefficients, transform, source/library versions, dataset hash and split days are
recorded. Model storage is credential-free JSON; no pickle/dynamic code loading.
Artifacts expire after seven days and checksums detect accidental alteration.
There is **no model-serving or automatic-promotion path** into native trading.

Runs occur at most hourly in the separate research child and reuse a run when
its dataset fingerprint is unchanged. At most thirty run records are retained.
A missing dataset yields WAITING/BLOCKED, never fabricated training success.
This is an out-of-time **backtest run after observing the data**, not a claim of
predictions actually recorded before test outcomes. Nonconverged fitting is blocked; invalid retained evidence remains unknown in coverage. A fresh forward prediction
study remains required before any future use in PAPER selection.

## Gaps explicitly left visible

- Real full-quality data, class diversity and enough distinct sessions.
- Prospective prediction recording, calibration and comparison of executable
  returns/costs against native baseline before any promotion decision.
- Gemini currently receives no verified timestamped articles; its news role must
  remain UNKNOWN. Numerical news score alone does not establish a catalyst.
- Three perspectives of one language model are not three independent experts.
- Training and collection correctness do not establish trading profitability.

## Validation

Offline fixtures cover train/test causality, embargo, raw-evidence eligibility,
coverage/duplicate/class guards, independent Brier reconstruction, unchanged
fitted parameters when test data change, registry idempotence, read-only missing
sources, stale/mismatched model evidence, artifact expiry/checksum and command
routing. No external model call, order, Telegram message, risk change, LIVE
activation, paid subscription or extra Gemini daily quota is introduced.
