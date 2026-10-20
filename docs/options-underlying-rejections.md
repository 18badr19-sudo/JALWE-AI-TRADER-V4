# Unsupported option underlyings

Alpaca may reject an option-chain request for an equity-catalog symbol, such as
`SKYD.WS`, with HTTP 400 and `invalid underlying symbol: SKYD.WS`. The stock
analysis previously continued safely with unavailable options, but each new
analysis repeated the rejected request and its traceback.

The options engine now retains only that exact, symbol-matched provider rejection
in an in-memory cache for one hour. The cache holds at most 256 symbols and expires
on process restart. It retries after expiry or eviction and does not infer option
eligibility from symbol punctuation. Cache hits make no price, chain, or contract
metadata request. The first rejection emits `OPTIONS_UNDERLYING_UNSUPPORTED`;
repeated hits remain quiet.

Both the initial rejection and cache hits return `available=False`, `score=None`,
and `bias=UNAVAILABLE`. Authentication failures, timeouts, other HTTP 400 errors,
and server errors retain the existing error reporting and retry behavior.
This repair does not add option availability as an entry gate or change native
entry, risk, target, or stop policies. A successful deployment does not establish
that a later real-market rejected-symbol request has exercised this branch.
