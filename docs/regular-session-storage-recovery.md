# Regular-session exits and bounded position persistence

The 2026-10-21 22:14:44 UTC INVE failure occurred while saving an unchanged
extended-session subscription diagnostic, before broker-stop synchronization.
It demonstrates SQLite write contention; it does not identify the competing
writer, establish a lost broker stop, or prove an incorrect entry or stop price.

Managed-position upserts now retry only SQLite BUSY/LOCKED failures on fresh,
rolled-back connections, at most three attempts with ten-second busy waits.
The total wait remains approximately the previous thirty-second budget. The
retry does not encompass broker requests or caller-owned fill/ledger
transactions. Persistent locks and other storage errors still fail closed.

Unchanged zero-fill stop snapshots no longer rewrite managed positions on every
poll. Status normalization is consistent between reconciliation and the watcher.
A bounded broker observation is logged on the first successful check and at most
once per fifteen minutes thereafter, without a heartbeat database write.
Every broker response is still validated; new fills retain atomic broker
snapshot, position and PnL ledger persistence. Subscription diagnostics are saved
only when changed, after broker-stop synchronization. An existing GTC stop is not
canceled or widened by these changes.

Extended exits default to disabled in configuration, status and the example
environment. Production must also explicitly set JALWE_EXTENDED_EXIT_ENABLED=false
so an older true override cannot keep the path enabled. Closed-session polling
still reconciles broker protection without reading an execution quote or marking
targets. Standard GTC stops are not a promise of execution outside regular hours.

Shadow model responses request exactly three scenarios and roles. Failed reviews
retain a fixed, sanitized validation code and log the failure stage. Unsupported
references, incomplete responses and invalid JSON remain failures; no retry,
quota increase, paid fallback or execution authority is added. The historical BB
ValueError lacks a recorded specific code in available logs; its cause remains
unconfirmed and is not rewritten as success. A new response is required to
verify provider behavior.

Provider-missing minute bars remain incomplete. This repair does not interpolate
prices, relax research coverage, activate a learned preference, or change entry,
risk, target or stop policies.
