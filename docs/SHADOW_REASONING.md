# Evidence-bound shadow reasoning

`intelligence/shadow_advisor.py` adds an independent OpenAI Responses reviewer.
It observes a frozen native WATCHING or READY_FOR_PAPER_EXECUTION decision **after
that decision has been recorded**, possibly after its PAPER execution. It is not
a pre-entry veto or authorization. Native execution never reads the model output.
No broker, stop, order, runtime-control or source-write capability is exposed.

Reviews describe BREAKOUT_CONTINUES, FALSE_BREAKOUT and MOVE_EXHAUSTED, their
supporting fact IDs, missing evidence and a next observation. A fixed structured
schema and local validation reject extra fields, invented/null references,
duplicate scenarios, incomplete output and refusals. This validates structure,
not factual correctness or predictive performance. The native AI score remains
unchanged; metadata explicitly identifies its probability as an uncalibrated
score proxy and confidence as feature coverage.

Only finite numeric/boolean whitelisted market features, known feed, feature
timestamp, symbol and existing native price levels are forwarded. Free-form
news, prompts, errors, credentials, account balances and metadata are excluded.
Bad/stale evidence and missing/naive/future timestamps are ineligible. Timestamp
semantics explicitly account for completed interval starts: 1m bars must be no
older than five minutes; 5m bars use the native fifteen-minute ceiling. Unknown
timeframes are ineligible. The native stale flag always blocks a review. A news
score is not evidence of a specific catalyst.

Submission writes a small durable SQLite queue in `/app/data`; a separate daemon
thread performs model HTTP requests. There is no network call in submission.
SQLite acquisition times out after 0.2s, transport after 15s; failures never
block native decisions. Pending work expires after two minutes. In-flight work
interrupted by restart is recorded as INTERRUPTED and never automatically
replayed. Failed calls count against the budget. Input hash deduplication,
atomic reservation, 10 reviews per New York day by default (hard ceiling 30),
2,000 output tokens per request and 90-day retention bound volume and storage.
The quota bounds requests, not a guaranteed dollar spend or complete coverage.
The small initial quota may sample only early qualifying reports.

Default is DISABLED: no paid request is made by installing/deploying this code.
To opt into PAPER shadow reviews, configure these Railway variables securely:

* `OPENAI_API_KEY`: an API project key; never send it in chat or commit it.
* `JALWE_SHADOW_AI_ENABLED=true`.
* Optional `JALWE_SHADOW_AI_DAILY_LIMIT` (1–30, default 10).
* Optional `JALWE_SHADOW_AI_MODEL` (default `gpt-6-astra`; must support Responses,
  strict structured outputs and reasoning effort `low`).

All trading remains PAPER; a non-PAPER configuration blocks model analysis too.
The code does not switch subscriptions, enable LIVE, or activate itself.
`/ai_status` shows configuration and aggregate observation states;
`/ai_review SNAP` reads the latest saved review. SHADOW_AI_STATUS and
SHADOW_AI_REVIEW record status, hashes and exception type, without secrets or
provider bodies. ANALYZED means the explanation passed structural validation,
not that the market prediction was correct or a trade was executed.

Frozen evidence and native states are retained alongside model reviews for
future outcome evaluation. The model does not train its own weights from these
rows and no accuracy/edge claim is made. Promotion into any entry decision
requires a separately designed, chronologically held-out comparison with
adequate data, costs and owner approval. Historical winners and partial paths
cannot substitute for prospective full-session outcomes.

The independent movement report now waits for the current forward cohort's
first observation pass, rather than the large retrospective JUMP_CASE backlog.
Pending historical work is displayed separately. Partial/failed forward records
are still partial/failed, excluded from comparison and pattern activation.
Legacy packets retain their old report gate. This fixes delayed reporting,
not missing provider data.

References: [Responses structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses),
[reasoning](https://developers.openai.com/api/docs/guides/reasoning),
[Alpaca market data coverage](https://docs.alpaca.markets/us/docs/about-market-data-api).
