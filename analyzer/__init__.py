"""Meridian package — implicit state machines in your codebase.

The AI (whatever model drives this Hermes) *proposes*: it calls the
meridian tools, reads machine candidates, drafts the .meridian.yaml spec.
Deterministic code *decides*: every state, transition and verdict below
is derived from source by ast/token analysis, never from vibes. The
ledger (`analyzer.ledger`) keeps the audit trail.
"""

__version__ = "1.0.2"
