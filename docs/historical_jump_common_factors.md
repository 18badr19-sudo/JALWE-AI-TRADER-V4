# Historical jump factors

The independent PAPER research worker scans the active Alpaca US equity catalog
over its existing 30-calendar-day history, regardless of bot ownership. Historical
discovery seats now prioritize daily upward bounds of at least 25%, computed from
the session open or prior close to the daily high. These bounds select reads; they
do not establish intraday chronology. Existing daily range candidates remain
eligible. Prospective cohorts retain first priority, explicit requests retain up
to eight seats, and the total remains twelve studies per cycle.

`historical-jump-common-factors-v1` describes seven pre-onset factors:

* Twenty completed minutes before the retrospectively selected onset: volume
  acceleration >=2, return >=1%, close above context VWAP, range <=2%.
* The exact preceding calendar session, compared with its exact preceding session:
  volume ratio >=2, closing return >=3%, daily range >=8%. Both daily bars must
  be valid, unique, split-adjusted SIP evidence available before the target open.

The current day's high, close and volume are selection evidence only; they are
never explanatory pre-onset inputs. Prior-session factors can be measured for
opening jumps even when twenty pre-onset regular minutes are unavailable. Missing
or disputed inputs stay UNKNOWN. Saved raw minute profiles, frozen cohorts,
original outcomes and retry/audit fields are preserved during bounded enrichment
of up to 24 saved profiles per cycle. Enrichment adds no provider requests.
If prior-day evidence was unavailable, later cycles can fill that unknown context
once valid cached bars arrive. Still-unknown evidence produces no repeated writes,
and known context is never replaced. Daily cache decoding occurs once per batch;
an auxiliary enrichment failure preserves the measured minute path and outcome.

The packet counts individual factors and all 21 two-factor co-occurrences, using
only complete, conflict-free, deduplicated symbol/session profiles. Co-occurrences
require both inputs known; absent data never count as absent factors. Nested,
overlapping rise groups >=25%, >=50%, >=100% use the ordered minute path, not a
daily screening bound. The short report shows up to three co-occurrences having
at least three known cases and two appearances; this is a descriptive display
threshold, not significance or validation. Complete and partial profiles remain
separate; no partial prefix enters these aggregates or prospective comparisons.

All cases were selected after their outcomes. There is no matched control base
rate, causal conclusion, predictive edge or automated application from these
counts. Repeated stocks/days are disclosed in each group. Any trading use requires
a separately specified, frozen, chronological training and validation protocol
and owner approval. The existing prior-day-move-patterns-v1 learner, PAPER entry
gates, stops, targets and broker submission behavior are unchanged.
