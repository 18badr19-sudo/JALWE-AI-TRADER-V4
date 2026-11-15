# End-of-day entry review

The existing research worker evaluates new JUMP_CASE, ACTIVITY and CONTROL
episodes using the existing fixed ORB15 simulator, after the calendar close
plus 16 minutes. It reuses the bars already fetched for that episode; no extra
provider requests, broker calls or scheduler are introduced.

Results are saved in the existing episode result JSON and included in the
latest completed session report. They include missing/ambiguous/no-entry states,
entry/exit evidence and assumed 10/25 bps per-side cost scenarios. Old completed
records without this field are NOT_EVALUATED. They are not silently rewritten.
Backlog and unavailable provider minutes can delay or prevent completion.

Retrospectively selected jump candidates are descriptive only. Frozen activity
and control cohorts are shown separately, including symbols that did not rise.
These groups are not pooled and do not establish out-of-sample strategy profit.
Duplicate identities are excluded. Missing returns are not zero returns.

This is one fixed ORB simulation, not a replay of every native JALWE strategy
or broker fills, and it does not infer which factors caused a jump. Existing
factor research remains separate. No automatic strategy promotion, order,
risk-setting change or APEX execution authority is added.

Validation: tests/test_end_of_day_entry_review.py runs with synthetic bars;
production acceptance additionally requires observing the deployed worker
produce end_of_day_entry_review evidence after an eligible session.
