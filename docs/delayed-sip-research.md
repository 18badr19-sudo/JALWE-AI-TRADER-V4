# Independent delayed SIP research

The configured decision feed, trading gates, risk limits and strategy preference
evaluation continue to use their existing inputs. A separate retrospective
measurement reads `feed=sip` for the original 60-minute research window only
after its end is at least 16 minutes old. Alpaca documents free historical SIP
access when `end` is at least 15 minutes old:
https://docs.alpaca.markets/us/docs/market-data-faq

Measurements are stored in `delayed_sip_research`, separately from original
decision evidence, IEX outcome metrics and strategy experiments. They never
call `StrategyExperiments.observe` or submit orders. The background worker
reads at most four windows per run, prioritizes eligible opportunities and
recent unmeasured windows, and incrementally backfills sources from seven days.
Sources without a trustworthy original reference stay unobservable.

A result is complete only with 60 valid, unique minute bars. No gap is filled
and no later price substitutes for the fixed window. Incomplete and failed
requests retry for ten minutes after the access delay, then remain explicitly
classified. Failed or incomplete measurements never become successful or
losing trade labels. Original evidence is not rewritten on restart.

Terminal PARTIAL_DATA and DATA_UNAVAILABLE observations now have a separate,
bounded recovery queue: at most one additional window per worker cycle, three
rechecks per observation within 48 hours of the original window end, and at
least 60 minutes since its previous check. The four-window fresh budget is
unchanged. COMPLETE and UNOBSERVABLE observations are excluded. Each recovery
atomically stores the prior status/result and the replacement result in
delayed_sip_recheck_history; it never changes the fixed window, decision,
reference price, trading settings or strategy preferences. Failed recoveries
remain explicitly incomplete/unavailable; no missing candle is synthesized.

The report includes NO_LEVEL, ACTIVATION_PAST_TARGET, NO_VALID_SETUP_LEVELS and
unknown/unrecorded outcomes, so all complete observations have a visible bucket.
DELAYED_SIP_RESEARCH_OUTCOME records newly completed results with their source
key, original decision state/reason, timestamps, levels and theoretical-target
flag. completed_audit_for_ny_date is read-only and fetches no market data.
Startup emits at most 1000 saved current-NY-day results under
DELAYED_SIP_RESEARCH_SAVED_OUTCOME with an explicit truncation summary. These
saved records are not new evaluations and must be deduplicated by source key.
None of these research paths proves that an actual trade could have filled.

Diagnostics distinguish empty windows, missing minutes, rejected bars, HTTP
403 subscription denial, HTTP 429 rate limits, timeouts, server failures and
observation-feed mismatch. Raw exception strings, request URLs and credentials
are excluded from diagnostic payloads. Old terminal records with no details
are labelled `LEGACY_UNCLASSIFIED` rather than assigned a guessed cause.

The daily report separates original-feed research from delayed SIP research,
and explicitly identifies originating decision feeds. Historical SIP access
does not establish permission for current SIP quotes or after-hours execution.

