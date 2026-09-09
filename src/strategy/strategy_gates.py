"""Strategy-profile gates shared by the live services and the replay (FC-096 C4).

These are policy predicates about WHICH opportunities a profile may act on. They
lived in ``deploy/cloud_run_server.py``, which the backtesting engine cannot
import: that module builds a Flask app, reads credentials and constructs an
``AlpacaClient`` at import time. A covered-call replay has to apply the SAME
gate the covered-call service applies, and the only way to do that without
forking the rule was to move it somewhere both can reach.

``deploy/cloud_run_server.py`` keeps a re-export, so its public shape and every
existing patch target are unchanged.

**The emitted event keeps ``component="cloud_run_server"`` VERBATIM.** BigQuery
views group error events by that column, and "correcting" it to
``strategy_gates`` would silently split one series into two — the new half of
which no dashboard queries. It is pinned by a test against exactly that
well-meaning correction. The component names WHERE THE RULE IS APPLIED (the
request path), not which file the function happens to live in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from src.utils.logging_events import log_error_event
from src.utils.option_symbols import strict_option_type

#: The component string on this gate's error events. A CONSTANT so the value
#: exists in exactly one place and the test that pins it has something to point
#: at. See the module docstring for why it is not "strategy_gates".
GATE_EVENT_COMPONENT = "cloud_run_server"


def call_only_opportunities(
    opportunities: Sequence[Dict[str, Any]], strategy_id: str, log: Any
) -> List[Dict[str, Any]]:
    """Drop non-call opportunities (FC-075 Phase 2 DD-2, defense in depth).

    A covered-call (non-wheel) service must NEVER execute a put. The put scan is
    gated and the blob is strategy-keyed, so this only fires on a hand-written or
    corrupted blob (expected live count: 0). Returns the call-only subset and
    logs each refusal. The caller applies the result to BOTH the working list and
    the blob snapshot, so a refused put is not then mislabeled `previously_failed`
    by the ``_underlyings_removed`` diff.

    Classification is by the OCC contract (``strict_option_type``), never by a
    substring of the symbol — the FC-045/FC-048 family of defects.
    """
    kept: List[Dict[str, Any]] = []
    for opp in opportunities:
        if strict_option_type(opp.get('option_symbol') or '') == 'call':
            kept.append(opp)
        else:
            log_error_event(
                log,
                error_type="non_call_opportunity_refused",
                error_message=f"Non-call opportunity refused on {strategy_id} profile",
                component=GATE_EVENT_COMPONENT,
                recoverable=True,
                option_symbol=opp.get('option_symbol'),
                symbol=opp.get('symbol'),
            )
    return kept
