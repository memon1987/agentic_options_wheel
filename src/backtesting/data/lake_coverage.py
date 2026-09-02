"""Does the chain lake already hold every session a spec needs? (FC-096 B3)

The sim service replays behind ``ChainFetchRefusingProvider``: a lake miss is a
hard failure, by design. That makes the failure honest and makes it *late* — the
operator learns about it after the request was accepted, from a `failed` row.
This module is the other half: an up-front check, so an uncoverable spec is a
409 that names the missing symbol-days and the command that fixes them.

**The session calendar comes from BARS, per symbol — the engine's exact one.**
``Simulator._trading_days`` derives a window's decision days from the union of
the symbols' stock-bar dates, and ``_build_chains`` then builds a chain for every
(symbol, day) the symbol has a close for. So the set this check must compare the
lake against is precisely *that symbol's bar dates inside the window*. Bars are
cheap, cached on disk by ``BarStore`` between runs, and explicitly PERMITTED
through the fetch guard (see ``fetch_guard``), so asking for them costs a
cache read on the second request and is rate-benign next to a chain.

**The lake-union calendar this replaced was wrong by construction**, and the
review that caught it was right about all three shapes:

* a gap in ONE symbol passed, because the union is built from the same lake
  days being checked — a day only that symbol was missing simply left the union;
* a gap SHARED by every symbol passed for the same reason, and a shared gap is
  the likely one (the backfill runs per window, not per symbol);
* a TRAILING gap passed, which is the default interactive shape: ``end``
  defaults to today, the lake never holds today's session (it is not settled,
  ``chain_store._is_cacheable``), and the union simply ended earlier.

The union survives only as a **boundary belt**: if the window's sessions extend
past the newest or oldest day the lake holds for this universe at all, that is
reported in its own terms rather than as a list of dates. It is deliberately
redundant with the per-symbol check in the ordinary case; belts are.

**Residual, stated rather than left to be discovered.** Bars and chains are
different sources with different coverage (the FC-095 data-key item), so this
check can still be wrong in one direction: a symbol whose bars exist for a day
whose chain the vendor never had (a corporate-action skip day, which the
backfill records as ``days_skipped_corporate_action``) reads as a lake gap. Such
a window is refused by ``Simulator.materialise`` anyway — an unmodelled split in
the decision window raises ``UnadjustedCorporateAction`` — so the spec was never
runnable; only the message is less precise than it could be. The opposite
direction, a missing day reported as present, cannot happen: bars are the
superset.

Nothing here writes. A lake failure PROPAGATES — "we could not tell" must never
be rendered as "there is nothing there" — while a BAR failure is reported as its
own refusal, because the operator can act on it and a 500 tells them nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import structlog

logger = structlog.get_logger(__name__)

#: How many symbol-days a 409 body names before it says "and N more". A guard
#: that pastes six hundred dates into an HTTP response is not a message.
MAX_REPORTED_DAYS = 12


@dataclass
class CoverageReport:
    """What the pre-flight found. ``complete`` is the only gate."""

    complete: bool
    sessions: int = 0
    weekdays: int = 0
    #: symbol -> sessions the symbol TRADED that the lake has no chain for.
    missing: Dict[str, List[date]] = field(default_factory=dict)
    #: The lake holds nothing for this universe at all (or is not configured).
    thin_window: bool = False
    #: Symbols with no bar in the window: nothing to replay, and nothing this
    #: check can compare against.
    no_sessions: List[str] = field(default_factory=list)
    #: The bar provider failed. Not evidence of absence, and not a 500 either.
    bars_error: Optional[str] = None
    #: The window's sessions run past the ends of what the lake holds.
    boundary: bool = False

    @property
    def missing_symbol_days(self) -> int:
        return sum(len(days) for days in self.missing.values())

    def _backfill(self, symbols: Sequence[str]) -> str:
        return (f"`gcloud run jobs execute data-backfill --update-env-vars="
                f"BACKFILL_SYMBOLS={','.join(sorted(symbols))},"
                f"BACKFILL_START=<start>,BACKFILL_END=<end>`")

    def describe(self) -> str:
        """The 409 body: what is missing, and the command that fixes it."""
        tail = ("The sim service may not fetch chains from the vendor, so those "
                "sessions would fail the run. Narrow the window, backfill the "
                "gap, or submit this spec to the `backtest-sweep` Job, which is "
                "allowed to fetch.")
        if self.bars_error:
            return (
                f"the coverage pre-flight could not read stock bars for this "
                f"window ({self.bars_error}), so it cannot tell which sessions "
                f"the chain lake would need. Refusing rather than guessing: the "
                f"replay would fail minutes from now with the same cause. Retry, "
                f"or submit this spec to the `backtest-sweep` Job."
            )
        if self.no_sessions:
            return (
                f"no stock bars at all in this window for "
                f"{', '.join(sorted(self.no_sessions))}. The replay derives its "
                f"decision days from bars, so it would refuse the window as "
                f"having no trading days. Check the ticker and the dates."
            )
        if self.thin_window:
            return (
                f"the chain lake holds no chains for this universe inside the "
                f"window ({self.sessions} session(s) needed, "
                f"{self.weekdays} weekday(s) spanned) — it has not been "
                f"backfilled for this range. {tail} "
                f"{self._backfill(list(self.missing) or ['<SYMS>'])}"
            )
        if not self.missing and self.boundary:
            return (
                "the window's sessions run past the ends of what the chain lake "
                "holds for this universe. TODAY's session is never in the lake "
                "(it is not settled yet, so it is deliberately not cached), so "
                "an `end` of today always needs narrowing by at least one "
                f"session. {tail}"
            )
        parts: List[str] = []
        shown = 0
        for symbol in sorted(self.missing):
            days = self.missing[symbol]
            room = max(MAX_REPORTED_DAYS - shown, 0)
            listed = [d.isoformat() for d in days[:room]]
            shown += len(listed)
            more = "" if len(listed) == len(days) else \
                f" (+{len(days) - len(listed)} more)"
            parts.append(f"{symbol}: {', '.join(listed) or '...'}{more}")
        boundary = ""
        if self.boundary:
            boundary = (" The window also runs past the ends of what the lake "
                        "holds for this universe — note that TODAY's session is "
                        "never in the lake (it is not settled yet), so an `end` "
                        "of today always needs narrowing.")
        return (
            f"the chain lake is missing {self.missing_symbol_days} symbol-day(s) "
            f"this window would replay — {'; '.join(parts)}.{boundary} {tail} "
            f"{self._backfill(list(self.missing))}"
        )


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in the inclusive window. Reported as context, never a gate.

    The gate is the per-symbol bar calendar; this number exists so a 409 about
    an empty lake can say how big the window was without implying that every
    weekday is a session.
    """
    if end < start:
        return 0
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def check_coverage(lake, symbols: Sequence[str], start: date, end: date, *,
                   bar_provider) -> CoverageReport:
    """Compare each symbol's TRADED sessions against the chains the lake holds.

    Args:
        lake: a ``ChainLake`` (or anything with ``list_days(symbol) -> set``).
            ``None`` means no lake is configured, which is reported as
            incomplete rather than complete — a service with no lake can answer
            nothing, and saying "fine" would be the guard's worst lie.
        symbols: the spec's universe, already validated and upper-cased.
        start / end: the spec's window, inclusive.
        bar_provider: anything with ``get_stock_bars(symbol, start, end)``. The
            service passes a ``CachedBarProvider`` so the second request pays a
            disk read; the fetch guard permits this call by design.

    Raises whatever the LAKE raises: a 403 or a timeout is not evidence of
    absence. A BAR failure is caught and reported (``bars_error``), because it
    is actionable and a 500 is not.
    """
    weekdays = weekdays_between(start, end)

    sessions_by_symbol: Dict[str, set] = {}
    try:
        for symbol in symbols:
            bars = bar_provider.get_stock_bars(symbol.upper(), start, end)
            sessions_by_symbol[symbol.upper()] = {
                b.bar_date for b in (bars or [])
                if start <= b.bar_date <= end
            }
    except Exception as exc:  # noqa: BLE001 - reported, never rendered as "fine"
        logger.warning(
            "Coverage pre-flight could not read stock bars",
            event_category="backtest", event_type="sim_coverage_bars_failed",
            symbols=list(symbols), error=f"{type(exc).__name__}: {exc}"[:300])
        return CoverageReport(complete=False, weekdays=weekdays,
                              bars_error=f"{type(exc).__name__}: {exc}"[:200])

    empty = sorted(s for s, days in sessions_by_symbol.items() if not days)
    if empty:
        return CoverageReport(complete=False, weekdays=weekdays,
                              no_sessions=empty)

    all_sessions: set = set()
    for days in sessions_by_symbol.values():
        all_sessions |= days

    if lake is None:
        return CoverageReport(complete=False, sessions=len(all_sessions),
                              weekdays=weekdays, thin_window=True)

    lake_by_symbol = {
        symbol: {d for d in lake.list_days(symbol) if start <= d <= end}
        for symbol in sessions_by_symbol
    }
    union: set = set()
    for days in lake_by_symbol.values():
        union |= days
    if not union:
        return CoverageReport(complete=False, sessions=len(all_sessions),
                              weekdays=weekdays, thin_window=True)

    missing = {
        symbol: sorted(sessions_by_symbol[symbol] - held)
        for symbol, held in lake_by_symbol.items()
        if sessions_by_symbol[symbol] - held
    }
    # The BELT. Compared against the window's own sessions rather than its raw
    # bounds, so a window ending on a Saturday is not reported as running past
    # the lake. Deliberately redundant with `missing` in the ordinary case.
    boundary = bool(max(all_sessions) > max(union)
                    or min(all_sessions) < min(union))

    report = CoverageReport(
        complete=not missing and not boundary,
        sessions=len(all_sessions), weekdays=weekdays, missing=missing,
        boundary=boundary)
    logger.info(
        "Chain-lake coverage pre-flight",
        event_category="backtest", event_type="sim_coverage_checked",
        symbols=list(sessions_by_symbol), sessions=report.sessions,
        weekdays=report.weekdays, complete=report.complete,
        boundary=boundary, missing_symbol_days=report.missing_symbol_days,
    )
    return report
