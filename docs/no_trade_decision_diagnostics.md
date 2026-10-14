# No-trade decision diagnostics

Store the strategy router's evaluated score, threshold, market regime, confidence,
approval and warnings as decision evidence without changing routing behavior.
The diagnostic report renders the saved score/threshold for router rejection and
source bar age/limit when available. Older decisions may show their recorded
router threshold from their exact existing warning; current settings never supply
a guessed historical threshold.

Decision event timestamps now display explicit Saudi time. SQLite's naive
CURRENT_TIMESTAMP is interpreted as UTC; already aware timestamps retain their
actual instant. Unknown timestamps/evidence remain unavailable. This changes
reporting only, not entry thresholds, risk, execution or stops.
