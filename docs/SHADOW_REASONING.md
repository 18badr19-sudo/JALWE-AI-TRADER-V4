# Evidence-bound shadow reasoning

`intelligence/shadow_advisor.py` adds an independent reviewer with explicitly
selected OpenAI Responses or Gemini Interactions REST transports.
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
atomic reservation, 10 OpenAI or 3 Gemini reviews per New York day by default (hard ceiling 30),
2,000 output tokens per request and 90-day retention bound volume and storage.
The quota bounds requests, not a guaranteed dollar spend or complete coverage.
The small initial quota may sample only early qualifying reports.

Default is DISABLED: no paid request is made by installing/deploying this code.
To opt into PAPER shadow reviews, configure these Railway variables securely:

* `JALWE_SHADOW_AI_PROVIDER=openai` (compatibility default).
* `OPENAI_API_KEY`: an API project key; never send it in chat or commit it.
* `JALWE_SHADOW_AI_ENABLED=true`.
* Optional `JALWE_SHADOW_AI_DAILY_LIMIT` (1–30, default 10).
* Optional `JALWE_SHADOW_AI_MODEL` (default `gpt-6-astra`; must support Responses,
  strict structured outputs and reasoning effort `low`).

For the temporary Gemini Free Tier option:

* Create a Gemini API key in Google AI Studio for a project shown as **Free Tier**.
  Do not link/upgrade billing for that project. Add only the key to Railway as
  `GEMINI_API_KEY`, never in chat or Git.
* Set `JALWE_SHADOW_AI_PROVIDER=gemini` and `JALWE_SHADOW_AI_DAILY_LIMIT=3`.
* After verifying that project's Free Tier in AI Studio, set
  `JALWE_GEMINI_FREE_TIER_CONFIRMED=true`; otherwise the enabled advisor remains
  WAITING_FREE_TIER_CONFIRMATION and sends no request.
* Enable `JALWE_SHADOW_AI_ENABLED=true` only after the above setup.
* Gemini currently permits only `gemini-3.5-flash-lite`, a model listed with free
  input/output tokens as of 2026-10-14. Other model names block execution.

The local confirmation is a setup attestation, not an API-side billing audit.
The API key does not give this code access to verify or control Google billing.
If its project is later upgraded to Paid Tier, the same model may be billed;
disable the advisor before changing billing. Google may use unpaid-service
content to improve its products; `store=false` controls interaction retention,
not Google's training terms. Only the whitelisted market evidence is sent.

HTTP 429 durably pauses Gemini requests for 24 hours (GEMINI_QUOTA_PAUSED),
without immediate retry, model substitution or paid-provider fallback. Pending
reviews expire; the native PAPER loop continues independently. Other failures
remain failed and consume the local request quota. Responses must be completed,
contain exactly one model-output text and pass the same local evidence validator.
No tool, agent, conversation history or background execution is requested.

Both HTTP transports independently enforce explicit enablement, PAPER-only mode,
the selected provider and a present key before opening a connection; the Gemini
transport additionally enforces its free-project attestation and model allowlist.
HTTP failures retain only a validated provider/status code such as GEMINI_HTTP_403
in storage and logs. `/ai_review SYMBOL` displays this safe code for failed calls.
Response bodies, URLs and exception messages remain excluded; generic errors
still retain only their exception type. This does not retry failed calls or
verify a key's validity without an actual successful provider response.

Provider/model identity is stored with every new review. Legacy rows retain
OpenAI attribution. Changing provider/model cancels old pending work instead of
replaying it through the newly selected provider. To move later to OpenAI,
explicitly select `openai`, configure `OPENAI_API_KEY`, select the OpenAI model
and redeploy; this never happens automatically. Gemini's secret is not reused.

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
[Gemini Interactions API](https://ai.google.dev/api/interactions-api),
[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing),
[Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key),
[Alpaca market data coverage](https://docs.alpaca.markets/us/docs/about-market-data-api).
