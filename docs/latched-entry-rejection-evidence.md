# Distinguish latched entry rejection causes

A confirmed latch can pass current AI/opportunity/router/session-quality gates but
still be below the refreshed candidate trigger. Previously the top-level reason
called this a quality-recheck failure, as it did for candidate invalidation and
engine exceptions. Historical logs can therefore not establish quality deterioration
from that reason alone.

Keep all existing rejection, latch clearing and risk-call behavior. Save a stable
failure code, current/original levels, final gate snapshot and available current
scores. Give trigger-not-reached, current invalidation and refresh exceptions their
own English/Arabic reasons. Gate failure keeps its existing reason. The tests verify
no risk evaluation or execution occurs in these rejection cases.

This is an evidence/classification correction, not an entry-policy relaxation or
proof of the cause of historical losses. No order, sizing, stop or target logic changes.
