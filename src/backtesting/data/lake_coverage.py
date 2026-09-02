"""Does the chain lake already hold every session a spec needs? (FC-096 B3)

The sim service replays behind ``ChainFetchRefusingProvider``: a lake miss is a
hard failure, by design. That makes the failure honest and makes it *late* — the
operator learns about it after the request was accepted, from a `failed` row.
This module is the other half: one ``list_blobs`` per symbol (~0.55 s measured),
up front, so an uncoverable spec is a 409 that names the missing symbol-days and
the command that fixes them.

**The session calendar is derived from the lake, not from a hardcoded holiday
table.** The plan left the source to the builder; this is the choice and its
reasoning.

A US market calendar is a second calendar to maintain, and it is wrong the first
time the NYSE closes for a state funeral or a hurricane — at which point the
guard refuses every spec spanning that day, for ever, with a message that names
days no chain can exist for. What the lake CAN answer without inventing
anything is: which sessions did the backfill actually produce objects for,
across the symbols in this spec. So:

    sessions  = union over the spec's symbols of that symbol's lake dates,
                clipped to the window
    missing   = sessions - this symbol's lake dates,   per symbol

* Weekends and market holidays are absent from every symbol, so they are absent
  from the union and are never demanded — the plan's "boundary weekends
  handled", without a calendar.
* A mid-window hole in ONE symbol is exactly what the union exposes: the other
  symbols traded that day, so the gap is that symbol's.
* A day the symbol did not trade at all (a halt, a listing that starts
  mid-window) reads as missing. That is the honest answer here: the replay's
  own decision calendar comes from bars, and the guard cannot see bars without
  fetching them. It over-refuses on a rare shape rather than under-refusing on
  a common one, and the message names the days so the operator can see what it
  is complaining about.

**The union has a floor.** If nothing has been backfilled at all, the union is
empty and every symbol is trivially "complete" — the guard's one catastrophic
failure mode, since it would wave through exactly the spec that is guaranteed to
refuse-and-fail mid-replay. So a union that covers less than
``MIN_SESSION_RATIO`` of the window's weekdays is itself the finding.

Nothing here writes, and a lake failure PROPAGATES: "we could not tell" must
never be rendered as "there is nothing there".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Sequence

import structlog

logger = structlog.get_logger(__name__)

#: A window whose observed sessions cover less than this fraction of its
#: weekdays is treated as "the lake does not really hold this window", whichever
#: symbol you ask. 0.8 leaves room for the ~9 US market holidays a year (≈3.5%
#: of weekdays) plus a genuinely thin stretch, and still catches an empty or
#: barely-seeded prefix, which is the case that matters.
MIN_SESSION_RATIO = 0.8

#: How many symbol-days a 409 body names before it says "and N more". A guard
#: that pastes six hundred dates into an HTTP response is not a message.
MAX_REPORTED_DAYS = 12


@dataclass
class CoverageReport:
    """What the pre-flight found. ``complete`` is the only gate."""

    complete: bool
    sessions: int = 0
    weekdays: int = 0
    #: symbol -> the sessions other symbols have and this one does not, sorted.
    missing: Dict[str, List[date]] = field(default_factory=dict)
    #: Set when the union itself is too thin to be a calendar.
    thin_window: bool = False

    @property
    def missing_symbol_days(self) -> int:
        return sum(len(days) for days in self.missing.values())

    def describe(self) -> str:
        """The 409 body: what is missing, and the command that fixes it."""
        if self.thin_window:
            return (
                f"the chain lake holds only {self.sessions} session(s) inside "
                f"this window, against {self.weekdays} weekday(s) — it has not "
                f"been backfilled for this range. The sim service may not fetch "
                f"chains from the vendor, so this spec cannot be answered here. "
                f"Backfill it first (`gcloud run jobs execute data-backfill "
                f"--update-env-vars=BACKFILL_SYMBOLS=<SYMS>,"
                f"BACKFILL_START=<start>,BACKFILL_END=<end>`) or submit it to "
                f"the `backtest-sweep` Job, which is allowed to fetch."
            )
        parts: List[str] = []
        shown = 0
        for symbol in sorted(self.missing):
            days = self.missing[symbol]
            room = max(MAX_REPORTED_DAYS - shown, 0)
            listed = [d.isoformat() for d in days[:room]]
            shown += len(listed)
            tail = "" if len(listed) == len(days) else f" (+{len(days) - len(listed)} more)"
            parts.append(f"{symbol}: {', '.join(listed) or '...'}{tail}")
        return (
            f"the chain lake is missing {self.missing_symbol_days} symbol-day(s) "
            f"inside this window — {'; '.join(parts)}. The sim service may not "
            f"fetch chains from the vendor, so those sessions would fail the "
            f"run. Backfill them (`gcloud run jobs execute data-backfill "
            f"--update-env-vars=BACKFILL_SYMBOLS="
            f"{','.join(sorted(self.missing))},BACKFILL_START=<start>,"
            f"BACKFILL_END=<end>`), narrow the window, or submit this spec to "
            f"the `backtest-sweep` Job, which is allowed to fetch."
        )


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in the inclusive window. Used only for the thinness floor."""
    if end < start:
        return 0
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def check_coverage(lake, symbols: Sequence[str], start: date,
                   end: date) -> CoverageReport:
    """One ``list_days`` per symbol; report what the window cannot answer.

    Args:
        lake: a ``ChainLake`` (or anything with ``list_days(symbol) -> set``).
            ``None`` means no lake is configured, which is reported as
            incomplete rather than as complete — a service with no lake can
            answer nothing, and saying "fine" would be the guard's worst lie.
        symbols: the spec's universe, already validated and upper-cased.
        start / end: the spec's window, inclusive.

    Raises whatever the lake raises. A 403 or a timeout is not evidence of
    absence.
    """
    weekdays = weekdays_between(start, end)
    if lake is None:
        return CoverageReport(complete=False, sessions=0, weekdays=weekdays,
                              thin_window=True)

    per_symbol: Dict[str, set] = {}
    for symbol in symbols:
        days = {d for d in lake.list_days(symbol) if start <= d <= end}
        per_symbol[symbol.upper()] = days

    sessions: set = set()
    for days in per_symbol.values():
        sessions |= days

    if not sessions or len(sessions) < MIN_SESSION_RATIO * weekdays:
        return CoverageReport(complete=False, sessions=len(sessions),
                              weekdays=weekdays, thin_window=True)

    missing = {
        symbol: sorted(sessions - days)
        for symbol, days in per_symbol.items()
        if sessions - days
    }
    report = CoverageReport(
        complete=not missing, sessions=len(sessions), weekdays=weekdays,
        missing=missing)
    logger.info(
        "Chain-lake coverage pre-flight",
        event_category="backtest", event_type="sim_coverage_checked",
        symbols=list(per_symbol), sessions=report.sessions,
        weekdays=report.weekdays, complete=report.complete,
        missing_symbol_days=report.missing_symbol_days,
    )
    return report
