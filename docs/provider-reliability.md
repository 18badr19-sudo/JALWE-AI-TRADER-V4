# Provider throttling and research coverage

Telegram's controller and execution watcher retry a request once only after an
explicit HTTP 429 rejection. They honor a valid `parameters.retry_after` of up to
ten seconds. Longer waits are returned as a typed rate limit; getUpdates is
deferred without stopping child monitoring or sending a recursive error alert.
Timeouts and HTTP 5xx sends are not retried automatically because delivery may
already have occurred. This is bounded recovery, not guaranteed message delivery.

Owner review polling honors GitHub's reset and Retry-After headers, defaults to
an hour after quota exhaustion without timing headers, and polls unauthenticated
reads at most every fifteen minutes. It does not create a credential or bypass
owner approval. Direct authenticated review still requires the owner's
`JALWE_CODE_REVIEW_GITHUB_TOKEN`. Failed notification delivery is not marked sent.

Historical SIP bars are already fully paginated or fail closed. Missing minutes
are not repaired by interpolating prices, carrying a close forward, or treating
sampled trades as complete minute bars. Provider pagination, shared-batch reads,
delay checks and missing coverage stay subject to the existing research tests.
Consequently these transport fixes cannot make unavailable source data complete
or activate a learned preference. No trading gates, risk, stops or targets change.
