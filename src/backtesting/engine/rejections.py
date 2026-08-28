"""Tally *why* the strategy declined to trade on each decision day.

A fitness verdict that does not name its own binding constraint is close to
useless for the decision it feeds. An adversarial review produced an AMD run
reading "unfit, held a position on 5 of 60 decision days" — while the real story
was that candidates existed on 41 of those 60 days and the gap-risk filter
excluded the symbol from 2025-01-13 onward. A reader concludes "AMD is a bad
wheel symbol" when the finding is "our gap filter excludes AMD in elevated-vol
regimes." Those imply completely different actions.

Rather than reimplement the filter logic (which would immediately drift from the
live code this FC exists to replay), this listens to the structured events the
live stages *already* emit and counts them.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Optional

import structlog

from ...utils import clock

# FC-069 item 12, review fix 1. This bucket is NOT a stand-down: it fires on
# every day the wheel is doing exactly what it exists to do — holding a position
# on the underlying it already sold a put against. On a healthy deployment it is
# the *most common* bucket by a wide margin (a golden-path fixture puts it at
# 40 of 45 days), so ranking it with the real blockers would stamp "the strategy
# was working" as `backtest_runs.binding_constraint` and lead the "why the
# strategy stood down" table with it. That is the FC-057 dishonest-metric class
# the tally exists to end, reintroduced from the other side.
#
# It stays counted and stays visible — deployment density is real information —
# but it is reported as deployment, never ranked as a constraint. Consumers:
# `reporting/bq_writer._binding` and `reporting/report.render_markdown`.
HOLDS_UNDERLYING_REASON = "already holds this underlying (scan, put)"

# Reasons that must be excluded from binding-constraint selection and from the
# stood-down ordering. A set, not a single constant, because the next reason of
# this species (a "capital fully deployed" bucket, say) belongs here too rather
# than in another one-off `!=` test.
NOT_A_STAND_DOWN = frozenset({HOLDS_UNDERLYING_REASON})


def stand_down_reasons(blocked: Optional[Dict[str, int]]) -> Dict[str, int]:
    """``blocked_days_by_reason`` minus the reasons that are not stand-downs.

    Order-preserving (the tally emits most-common-first, and both consumers
    take the first key as the binding constraint).
    """
    return {reason: count for reason, count in (blocked or {}).items()
            if reason not in NOT_A_STAND_DOWN}


# Live event_type -> the human-facing reason a day produced no trade. Ordered
# roughly by how early the stage sits in the pipeline.
#
# FC-068 rewrote this table. The replay used to drive WheelEngine's dead
# orchestration path, which emitted a gap/wheel-state/per-cycle-cap vocabulary
# production has not spoken since 2025-10-03. Those entries are gone with their
# emitters; what is here is what OptionsScanner -> ExecutionEngine actually
# emits. An entry with no live emitter is worse than no entry: it reads as
# coverage while counting nothing.
_REASONS = {
    # FC-057: stage 1 was invisible. A symbol blocked on the price band or
    # volume floor produced NO tally entry at all, so a screen reported it as
    # simply having done nothing -- "insufficient", "0% days in position", no
    # reason given. That is how a $400 max_stock_price silently excluded SPY,
    # QQQ and AMD for months while their verdicts were read as strategy
    # results. The event was always emitted with full detail; nothing counted it.
    "stock_rejected_filter": "price/volume band (stage 1)",
    # The two chain-empty events, both from market_data, both on the path the
    # scanner drives. `stage_7_complete_not_found` is the PUT leg's binding
    # constraint -- the one that makes F/PFE/KMI/VZ untradeable at the $0.50
    # premium floor -- and it was never mapped: pre-FC-068 the tally counted
    # `no_suitable_puts`, whose only emitter lived inside the deleted
    # `put_seller.find_put_opportunity`. Leaving it unmapped would have
    # produced zero tally entries for the single most common no-trade outcome,
    # which is the exact FC-057 failure mode this module exists to end.
    "stage_7_complete_not_found": "no put cleared delta/DTE/premium (stage 7)",
    # And the CALL leg's, mapped for the same reason: it is the dominant
    # call-side no-trade in production today (every NVDA scan since 07-30 ends
    # here). Fixing one leg's blindness and not the other is the same defect
    # twice.
    "stage_8_complete_not_found": "no call cleared floor/delta/DTE/premium (stage 8, scan)",
    "stage_8_blocked": "position sizing (stage 8)",
    # Scan-stage skips (FC-065). A held symbol whose floor will not resolve, or
    # whose cross-check disagrees with the broker, writes no calls at all --
    # a decision, not an absence.
    "call_scan_skipped_cost_basis_unresolved": "cost-basis floor unresolved (scan)",
    "call_scan_skipped_cost_basis_divergent": "cost-basis floor divergent (scan)",
    "call_scan_skipped_quote_unavailable": "stock quote unusable (scan)",
    # FC-013's two gate events. The `*_earnings_unknown` pair is deliberately
    # NOT mapped: in a replay the historical calendar never returns unknown
    # (table-backed, fail-open on gaps and reported instead), so a mapping
    # would be an entry with no replay emitter — the FC-068 rule this table
    # was rewritten to enforce. Stated here so a reviewer does not "fix" it.
    # Partial span rejections do not get a tally entry either; they surface
    # through the `expires_into_earnings` key in `reason_counts` on
    # `no_candidates` rows. Only a symbol the gate *terminated* is a no-trade
    # day with a reason.
    "put_scan_skipped_earnings_blackout": "earnings blackout (scan, put)",
    "call_scan_skipped_earnings_blackout": "earnings span emptied the chain (scan, call)",
    # FC-069 item 12 closes BACKTEST_ENGINE.md's accepted gap #7: the put-side
    # "we already hold this underlying" skip was silent, so a replay day it
    # blocked showed as a symbol that simply did nothing. It now emits, on the
    # live path and therefore in the replay (same scanner since FC-068). The
    # skip's fail-closed API-error sibling, `position_check_failed`, is
    # deliberately NOT mapped here: it means the positions call failed, not
    # that a position exists, and the two must not share a bucket. This bucket
    # is counted but never ranked — see HOLDS_UNDERLYING_REASON above.
    "put_scan_skipped_existing_position": HOLDS_UNDERLYING_REASON,
    # Execution-stage failures (FC-048). Without these the tally cannot see an
    # opportunity that was found and ranked but died at the router or in the
    # wrong seller -- which is exactly how the covered-call misroute stayed
    # invisible: every call was rejected here and nothing counted it.
    "naked_call_blocked": "insufficient available shares (execution)",
    "unroutable_opportunity": "unroutable opportunity (execution)",
    "call_rejected_by_put_seller": "wrong_seller: call sent to put_seller (execution)",
    "put_rejected_by_call_seller": "wrong_seller: put sent to call_seller (execution)",
}

# `selection_dropped` is ONE event_type carrying a `reason` field (the closed
# DROP_REASONS enum), so it cannot be a flat _REASONS row -- every drop would
# collapse into one bucket. FC-068 made batch selection the stage that decides
# what trades, so a tally blind to *why* selection dropped something is blind to
# the deciding stage. Bucketed per reason instead.
SELECTION_DROPPED_EVENT = "selection_dropped"

_SELECTION_DROP_REASONS = {
    "insufficient_available_shares": "selection: insufficient available shares",
    "insufficient_buying_power": "selection: insufficient buying power",
    "duplicate_underlying": "selection: duplicate underlying",
    "sizing_failed": "selection: sizing failed",
    "positions_unavailable": "selection: positions snapshot unavailable",
    # FC-041: raised at the execution stage, not by select_batch, but it rides
    # the same `selection_dropped` event and the same closed enum -- and
    # test_every_closed_enum_value_has_a_bucket requires every member to have a
    # bucket, precisely so a new reason cannot go silently uncounted.
    "naked_call_invariant": "execution: naked-call invariant violated",
}


class RejectionTally:
    """Counts blocking reasons emitted during a replay.

    Installed as a structlog processor for the duration of the run and removed
    afterwards. Never raises into the run: a diagnostic that can break the thing
    it is diagnosing is not worth having.
    """

    def __init__(self) -> None:
        # Keyed by (day, reason) so a reason counts ONCE per day. Live emits two
        # events for one logical gap rejection — analyze_gap_risk's inner
        # _is_suitable_for_trading and the outer filter_symbols_by_gap_risk —
        # and both map to the same bucket. Counting events produced "335 days"
        # against 206 decision days: impossible on its face, and exactly the
        # class of dishonest metric this review round existed to remove.
        self._seen: set = set()
        self._candidate_days: set = set()
        self._prev_config: Optional[dict] = None

    # ------------------------------------------------------------------ #
    def processor(self, logger, name, event_dict):
        try:
            event_type = event_dict.get("event_type", "")
            # structlog.configure() is PROCESS-global, so this processor sees
            # events from every thread. clock.is_frozen() is thread-local and is
            # the only reliable "this event came from the replay" signal.
            #
            # Tagging outside this guard mislabelled LIVE events as backtest=true
            # — the mirror of the analytics-singleton bug, and worse for its own
            # purpose: FC-039 filters `backtest != true`, so a mislabelled live
            # event is silently DROPPED from real analysis. A false negative is
            # worse than the untagged-replay false positive this fills.
            if not clock.is_frozen():
                return event_dict
            day = clock.now().date()

            # Synthetic cycles must be distinguishable from live ones in Cloud
            # Logging. Promised by the plan (§5).
            event_dict.setdefault("backtest", True)
            if event_type == SELECTION_DROPPED_EVENT:
                reason = _SELECTION_DROP_REASONS.get(event_dict.get("reason"))
            else:
                reason = _REASONS.get(event_type)
            if reason and day is not None:
                self._seen.add((day, reason))
            # Days on which the chain *was examined* and offered a qualifying
            # candidate. Not a count of days a tradeable option existed: when an
            # earlier stage blocks, stage 7 never runs.
            if event_type == "stage_7_complete_found" and day is not None:
                self._candidate_days.add(day)
        except Exception:  # noqa: BLE001 - diagnostics must never break a run
            pass
        return event_dict

    def __enter__(self) -> "RejectionTally":
        try:
            self._prev_config = structlog.get_config()
            processors = list(self._prev_config.get("processors", []))
            structlog.configure(processors=[self.processor] + processors)
        except Exception:  # noqa: BLE001
            self._prev_config = None
        return self

    def __exit__(self, *exc) -> None:
        if self._prev_config is not None:
            try:
                structlog.configure(**self._prev_config)
            except Exception:  # noqa: BLE001
                pass
        return None

    # ------------------------------------------------------------------ #
    @property
    def candidate_days(self) -> int:
        return len(self._candidate_days)

    def summary(self) -> Dict[str, int]:
        """Distinct DAYS blocked, per reason, descending."""
        per_reason: Counter = Counter(reason for _day, reason in self._seen)
        return dict(per_reason.most_common())

    def binding_constraint(self) -> Optional[str]:
        """The reason that blocked the most days, if any."""
        top = Counter(r for _d, r in self._seen).most_common(1)
        return top[0][0] if top else None
