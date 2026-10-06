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

Diagnostics distinguish empty windows, missing minutes, rejected bars, HTTP
403 subscription denial, HTTP 429 rate limits, timeouts, server failures and
observation-feed mismatch. Raw exception strings, request URLs and credentials
are excluded from diagnostic payloads. Old terminal records with no details
are labelled `LEGACY_UNCLASSIFIED` rather than assigned a guessed cause.

The daily report separates original-feed research from delayed SIP research,
and explicitly identifies originating decision feeds. Historical SIP access
does not establish permission for current SIP quotes or after-hours execution.
