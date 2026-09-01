"""FC-096 Phase A — incremental backfill of the bars cache and the chain lake.

The lake grows by accident today: a chain-day exists because some screen or
sweep happened to run cold over it. That makes freshness a side effect of the
monthly screen, caps the stored reach at ``universe_dte = 8`` (the live
``put_target_dte`` plus the universe buffer), and leaves candidate symbols with
no data path at all. This module is the deliberate version: given a symbol set
and a window, make sure every settled session in it is stored at
``universe_dte = 22`` over a strike window at least as wide as whatever is
already there.

**It builds chains; it does not decide anything.** No strategy code runs here,
nothing is replayed, nothing is scored. The only outputs are parquet objects in
the shared cache/lake and a summary of what changed.

Four rules carry the whole design; each one is a defect this would otherwise
have shipped:

1. **Union with the existing window, per day.** ``ChainStore``'s replacement
   rule is coverage-monotone: an object may only be replaced by a file whose
   request is a superset (same model, at least the DTE reach, at least the
   strike range). A rebuild at a spot-centred window is *narrower on one bound*
   for any symbol whose stored window was pushed out by a position anchor or an
   FC-091 merge — so the upload is refused, the day stays at reach 8, and the
   symbol is re-fetched cold on every run for ever. That is exactly the live
   SPY/IWM/PFE ``rejected=231 skipped=231`` pattern. So each day is built at
   ``[min(fresh_gte, stored_gte), max(fresh_lte, stored_lte)]`` and the written
   file is a strict superset on both axes by construction.

2. **A wider fresh window than the 7-DTE one.** ``STRIKE_WINDOW_PCT`` is 0.25,
   measured on the 0.10-0.20 delta band at **7** DTE. At 21 DTE and 90% IV the
   0.10-delta put sits near K/S ~ 0.78, outside that window — and the failure
   mode is not a slow build, it is a *silently different* chain: the strategy
   selects a nearer strike it would never have selected, baked into a file
   everything else then reads. ``BACKFILL_STRIKE_WINDOW_PCT = 0.40`` restores
   roughly the 3x band-edge margin the 0.25 constant was designed to have.
   ``STRIKE_WINDOW_PCT`` itself is NOT edited (it is the live 7-DTE reach's
   window, shared with every other caller); the wider window is expressed
   through the builder's existing anchor parameters — see ``window_anchors``.

3. **One model, frozen equal to the sweep path's.** ``chain_builder`` is
   constructed here byte-identically to ``scenarios/runner.py``'s — same
   ``risk_free_rate``, no ``dividend_yields``, default ``SpreadModel`` — because
   the model fingerprint is part of the cache key. Wiring dividend yields in
   would look like an improvement and would in fact fork the lake into a second,
   invisible model: every backfilled day would stop covering every sweep
   request. If the model changes it changes for all writers in one PR, with a
   ``CHAIN_LAKE_PREFIX`` bump. ``tests/test_backfill.py`` pins the two
   constructions equal.

4. **Per-day windows, never a whole-window anchor span.** The simulator takes
   its strike anchors from the *window's* highest and lowest closes, which is
   correct for a replay that may hold a position across it — and wrong here: a
   split inside the window would set the ceiling ~7x too high and turn the
   strike filter into a no-op. Each day is built from its own close, and a day
   whose bars carry a split-sized move is skipped and reported — reported, but
   not counted as a failure: a split is market data, and paging weekly for the
   month it takes to leave the trailing window is how an alert gets muted.

Everything degrades rather than dies: one symbol's failure does not stop the
others, one day's failure does not stop the symbol, and a half-finished run is
reconstructable from the per-symbol progress logs alone.

One interaction worth naming: the FC-091 heal (a rejected lake frame being
unioned with the rebuild that follows it) does **not** fire here, and should
not. It triggers only when the rejected file arrived from the lake *inside the
same* ``get`` that rejected it; the backfill reads the day's provenance first,
which leaves the object in the local cache, so the later ``get`` is a local
read. That is the right outcome twice over: this module has already computed the
union explicitly and across BOTH axes, whereas the merge refuses a DTE change
outright (``dte_mismatch``) — which is exactly what every widening is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import structlog

from ..scenarios.overrides import MAX_SWEEPABLE_DTE
from .alpaca_provider import (
    SPLIT_RATIO_HIGH,
    SPLIT_RATIO_LOW,
    AlpacaDataProvider,
)
from .bar_store import BarStore, CachedBarProvider, last_settled_day
from .chain_builder import STRIKE_WINDOW_PCT, UNIVERSE_DTE_BUFFER, ChainBuilder, strike_window
from .chain_store import _BOUND_TOL, _close, ChainStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...utils.config import Config

logger = structlog.get_logger(__name__)

# The DTE target every backfilled day is built to. Imported from `overrides.py`,
# never restated: that module is the stdlib-only copy the dashboard image also
# carries, so a number defined there is the one number both halves of the system
# see. The import direction is fixed — see MAX_SWEEPABLE_DTE's comment.
BACKFILL_MAX_DTE = MAX_SWEEPABLE_DTE

# What lands in the file's provenance: `ChainBuilder.build` fetches one calendar
# day wider than the strategy's DTE window (UNIVERSE_DTE_BUFFER), so a target of
# 21 stores `universe_dte = 22`. Stated here because the rollout verifies this
# exact number on written objects.
BACKFILL_UNIVERSE_DTE = BACKFILL_MAX_DTE + UNIVERSE_DTE_BUFFER

# Half-width of a FRESH day's strike ladder, as a fraction of that day's close.
# See rule 2 in the module docstring. Widen this (and re-run the widening) if
# the band-edge probe at DTE 21 ever shows a symbol's 0.10-delta strike outside
# it; never narrow it below `STRIKE_WINDOW_PCT`, which would make a backfilled
# day narrower than a live-reach build of the same session.
BACKFILL_STRIKE_WINDOW_PCT = 0.40

# The weekly Job's window. Its job is FRESHNESS, not re-verifying four hundred
# already-covered days through thousands of lake round-trips; historical
# widening passes an explicit start/end instead.
DEFAULT_HISTORY_DAYS = 30

# Bound and price tolerances are IMPORTED from `chain_store`, not restated: this
# module predicts what the store's coverage check will decide (see `covers`),
# and a second copy of the slack would drift into predicting it wrongly.

# Extra history fetched per symbol purely so a split on the window's first day
# has a predecessor bar to be compared against. See `_backfill_symbol`.
#
# Fourteen calendar days, not seven, and the reason is a guarantee rather than a
# margin: seven days spans at most five sessions and can span as few as two
# either side of a holiday-lengthened weekend, so "there will be a bar in there"
# was a probabilistic claim about the calendar. Fourteen days contains a full
# week of sessions even around Thanksgiving, Christmas/New Year and Independence
# Day — the three stretches where the US market closes on a weekday adjacent to
# a weekend. What is actually used is the LAST session before the window (see
# `_backfill_symbol`); the extra span exists only so that session is reliably
# inside the fetch.
_SPLIT_CONTEXT = timedelta(days=14)


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
@dataclass
class SymbolBackfill:
    """What one symbol's pass did.

    The write counters are derived from ``ChainStore``'s own public counters,
    sampled before and after each day — not from anything this module believes
    should have happened. The distinction is the point: the store is allowed to
    decline a write (a wider object already there, a lost generation race, an
    unreadable remote), and a summary that reported intent rather than outcome
    would call those days written.

    **Two different kinds of "this day has no chain", counted apart.** A
    corporate action is expected market data — the engine does not model an
    adjusted deliverable, so the session is skipped by design. A fetch failure
    is not. Merging them would make a stock split indistinguishable from a
    broken vendor response in the one number the exit code reads.
    """

    symbol: str
    days_checked: int = 0        # days attempted (settled sessions with a bar)
    days_written: int = 0        # an object was written for the day
    days_replaced_wider: int = 0  # ...over an existing object, as a superset
    days_skipped: int = 0        # the store DECLINED the write (any reason)
    days_covered: int = 0        # already stored at or above this window
    # Days deliberately not built because the session carries an unmodelled
    # corporate action. Reported everywhere, and NOT a failure — see ``failed``.
    days_skipped_corporate_action: int = 0
    errors: int = 0              # days that produced no chain BY FAILURE
    error: Optional[str] = None  # symbol-level failure; the pass stopped here
    seconds: float = 0.0

    #: The buckets every checked day lands in, exactly one each.
    DAY_BUCKETS = (
        "days_written", "days_skipped", "days_covered",
        "days_skipped_corporate_action", "errors",
    )

    @property
    def reconciles(self) -> bool:
        """Whether every checked day is accounted for in exactly one bucket.

        This is a real invariant, not a formality. Before it existed a lake
        operation that failed *before* the circuit breaker tripped moved no
        counter this module sampled, so the day fell into no bucket at all and
        a five-day run reported "4 written" with the fifth day mentioned
        nowhere — the run looked complete and was not. A hole in the lake that
        nothing reports is precisely the failure this whole phase exists to
        remove, so the arithmetic is asserted rather than assumed.

        A symbol that failed at the SYMBOL level is exempt: its pass stopped
        mid-window, so the days after it were never checked.
        """
        if self.error is not None:
            return True
        return self.days_checked == sum(
            getattr(self, name) for name in self.DAY_BUCKETS
        )

    @property
    def failed(self) -> bool:
        """Whether this symbol's pass is trustworthy as "done".

        **A corporate-action skip is NOT a failure.** A split is ordinary market
        data that recurs across the universe, and the trailing window slides —
        so counting it here would page the Job-failure alert every week for the
        month it takes to age out, on a condition nobody can act on. An alarm
        layer that cries wolf gets muted, and then it is not watching the
        failures that matter either (the FC-069 principle). The skip is loud in
        the summary, the progress log and the `backfill_day_skipped` event
        instead, where it informs without paging.

        Genuine failures — a vendor error, an exception mid-build, a symbol
        whose bars never arrived, a lake operation that did not land — keep the
        full fail posture. So does a pass whose day counters do not reconcile:
        if this module cannot say what happened to every checked day, it cannot
        claim the window is covered.
        """
        return (self.error is not None or self.errors > 0
                or not self.reconciles)

    def as_log(self) -> dict:
        return {
            "symbol": self.symbol,
            "days_checked": self.days_checked,
            "days_written": self.days_written,
            "days_replaced_wider": self.days_replaced_wider,
            "days_skipped": self.days_skipped,
            "days_covered": self.days_covered,
            "days_skipped_corporate_action": self.days_skipped_corporate_action,
            "errors": self.errors,
            "error": self.error,
            "reconciles": self.reconciles,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class BackfillSummary:
    """The whole run. ``failed()`` decides the process exit code."""

    start: date
    end: date
    universe_dte: int
    symbols: List[SymbolBackfill] = field(default_factory=list)
    seconds: float = 0.0
    lake: Dict[str, object] = field(default_factory=dict)

    def failed_symbols(self) -> List[str]:
        return [s.symbol for s in self.symbols if s.failed]

    @property
    def lake_configured(self) -> bool:
        """Whether this run was asked to write to a lake at all.

        ``CHAIN_LAKE_BUCKET`` unset is a legitimate local run — a developer
        building chains into their own cache — and must not fail. A lake that
        was configured and then died is the opposite case; see ``lake_failed``.
        """
        return bool(self.lake.get("lake_enabled"))

    @property
    def lake_failed(self) -> bool:
        """A configured lake that did not take everything it was given.

        **This fails the run**, and the reason is worth stating plainly: the
        entire purpose of an execution is to put objects in the lake. When a
        lake operation fails, ``ChainStore`` degrades to local-only — correct
        behaviour for a backtest, which still needs its chains, and exactly
        wrong for a backfill, whose filesystem is destroyed when the task
        exits. A green exit-0 run reporting "widened the lake" with
        ``lake_puts = 0`` is the silent failure this phase exists to kill, and
        it is worse than a loud one because the operator moves on to the next
        chunk believing this one landed.

        **Why this is not simply ``lake_disabled``.** ``ChainLake``'s circuit
        breaker counts *consecutive* failures and is reset by
        ``note_success()`` — and a successful read is a success, including a
        miss, which is the RPC reaching GCS. The backfill interleaves a
        provenance read and a write on every single day, so each day's read
        resets the counter the write just incremented and the breaker **never
        trips** for this workload. Measured: 12 consecutive failed uploads left
        ``_consecutive_errors`` at 1 and ``disabled`` False. Keying this on
        ``lake_disabled`` alone would therefore have reported "lake: ok" on a
        run where 100% of the writes failed, which is the exact sentence this
        property exists to prevent anyone from reading.

        (That interleaving is not a bug to fix here: the breaker's job is to
        stop a *backtest* paying for a dead lake 5,400 times, and the backfill
        does want to keep trying — every day it does not upload is a day it
        will have to redo. What it must not do is call the result a success.)
        """
        if not self.lake_configured:
            return False
        return (bool(self.lake.get("lake_disabled"))
                or int(self.lake.get("lake_errors") or 0) > 0)

    def failed(self) -> bool:
        """The process exit code, as a predicate."""
        return bool(self.failed_symbols()) or self.lake_failed

    def total(self, field_name: str) -> int:
        return sum(getattr(s, field_name) for s in self.symbols)

    def lake_health(self) -> dict:
        """The lake facts a reader needs to trust the write counts."""
        return {
            "lake_enabled": bool(self.lake.get("lake_enabled")),
            "lake_disabled": bool(self.lake.get("lake_disabled")),
            "lake_disabled_reason": self.lake.get("lake_disabled_reason"),
            "lake_bucket": self.lake.get("lake_bucket"),
            "lake_puts": int(self.lake.get("lake_puts") or 0),
            "lake_errors": int(self.lake.get("lake_errors") or 0),
        }

    def as_log(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "universe_dte": self.universe_dte,
            "symbols": len(self.symbols),
            "days_checked": self.total("days_checked"),
            "days_written": self.total("days_written"),
            "days_replaced_wider": self.total("days_replaced_wider"),
            "days_skipped": self.total("days_skipped"),
            "days_covered": self.total("days_covered"),
            "days_skipped_corporate_action": self.total(
                "days_skipped_corporate_action"),
            "errors": self.total("errors"),
            "failed_symbols": self.failed_symbols(),
            "lake_failed": self.lake_failed,
            "seconds": round(self.seconds, 2),
            **self.lake_health(),
        }

    def render(self) -> str:
        """A human summary for the Job log and the CLI."""
        lines = [
            f"Backfill {self.start} -> {self.end} at universe_dte="
            f"{self.universe_dte} ({len(self.symbols)} symbols, "
            f"{self.seconds:.1f}s)",
            "",
            f"{'symbol':<8} {'checked':>8} {'written':>8} {'wider':>7} "
            f"{'skipped':>8} {'covered':>8} {'corp_act':>9} {'errors':>7}  note",
        ]
        for s in self.symbols:
            lines.append(
                f"{s.symbol:<8} {s.days_checked:>8} {s.days_written:>8} "
                f"{s.days_replaced_wider:>7} {s.days_skipped:>8} "
                f"{s.days_covered:>8} {s.days_skipped_corporate_action:>9} "
                f"{s.errors:>7}  {s.error or ''}"
            )
        # Lake health sits beside the write counts, never below the fold: a
        # "written" column is only meaningful if the lake was actually up.
        health = self.lake_health()
        if not health["lake_enabled"]:
            lines += ["", "lake: NOT CONFIGURED (local-only run; "
                          "no object left this machine)"]
        elif health["lake_disabled"]:
            lines += ["", f"lake: DISABLED mid-run "
                          f"(reason={health['lake_disabled_reason']}, "
                          f"bucket={health['lake_bucket']}, "
                          f"lake_puts={health['lake_puts']}, "
                          f"lake_errors={health['lake_errors']}) — every day "
                          f"after that point was written to a container "
                          f"filesystem that is about to be destroyed"]
        elif health["lake_errors"]:
            lines += ["", f"lake: ERRORS (bucket={health['lake_bucket']}, "
                          f"lake_puts={health['lake_puts']}, "
                          f"lake_errors={health['lake_errors']}) — operations "
                          f"failed and the run continued local-only. The "
                          f"breaker did NOT trip: the per-day provenance read "
                          f"resets its consecutive-failure count, so a lake "
                          f"refusing every write still reads as 'enabled'"]
        else:
            lines += ["", f"lake: ok (bucket={health['lake_bucket']}, "
                          f"lake_puts={health['lake_puts']}, "
                          f"lake_errors={health['lake_errors']})"]
        failed = self.failed_symbols()
        if failed:
            lines += ["", f"FAILED: {', '.join(failed)}"]
        if self.lake_failed:
            lines += ["FAILED: the chain lake was configured and did not take "
                      "everything it was given; this execution did not do its "
                      "job. Re-run this window."]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The window rule
# --------------------------------------------------------------------------- #
def fresh_window(underlying_price: float) -> Tuple[float, float]:
    """The strike bounds a day with nothing stored is built at."""
    return (
        underlying_price * (1.0 - BACKFILL_STRIKE_WINDOW_PCT),
        underlying_price * (1.0 + BACKFILL_STRIKE_WINDOW_PCT),
    )


def union_window(
    underlying_price: float, stored: Optional[dict]
) -> Tuple[float, float]:
    """The bounds to build at: the fresh window, widened to cover ``stored``.

    ``stored`` is a ``ChainStore.stored_window`` mapping or ``None``. A bound the
    stored file cannot prove (missing or NaN provenance) contributes nothing —
    it cannot be covered, and claiming to cover it would be the exact lie the
    monotone guard exists to prevent. Such a file is instead *replaced* only if
    the guard itself accepts it, and skipped-and-reported if not.
    """
    gte, lte = fresh_window(underlying_price)
    if stored:
        stored_gte = stored.get("strike_gte")
        stored_lte = stored.get("strike_lte")
        if stored_gte is not None:
            gte = min(gte, float(stored_gte))
        if stored_lte is not None:
            lte = max(lte, float(stored_lte))
    return gte, lte


def window_anchors(
    underlying_price: float, strike_gte: float, strike_lte: float
) -> Tuple[float, float]:
    """``(cost_basis, low_anchor)`` that make the builder fetch exactly this window.

    ``ChainBuilder`` takes no window argument — it derives one from the day's
    close and two *anchor* prices (``chain_builder.strike_window``), which exist
    so a position's strikes stay reachable. They are the documented extension
    points for a wider window, so the union is expressed through them rather
    than by adding a parameter to a function every other caller shares:

        upper bound = max(price, cost_basis) * (1 + STRIKE_WINDOW_PCT)
        lower bound = min(price, low_anchor) * (1 - STRIKE_WINDOW_PCT)

    which inverts to the two lines below. Both anchors always bind here: the
    fresh window alone is +/-40%, so ``cost_basis >= 1.12 * price`` and
    ``low_anchor <= 0.8 * price`` before any stored window widens them further.
    ``effective_window`` re-derives the result through the real function so the
    inversion cannot silently drift from it.
    """
    return (
        strike_lte / (1.0 + STRIKE_WINDOW_PCT),
        strike_gte / (1.0 - STRIKE_WINDOW_PCT),
    )


def effective_window(
    underlying_price: float, cost_basis: float, low_anchor: float
) -> Tuple[float, float]:
    """What the builder will ACTUALLY fetch for these anchors (the real function)."""
    return strike_window(underlying_price, cost_basis, low_anchor)


def covers(stored: Optional[dict], *, strike_gte: float, strike_lte: float,
           model: str, underlying_price: float) -> bool:
    """Whether the stored object already answers this build — i.e. a cache hit.

    Mirrors ``ChainStore._covers`` plus ``get``'s close-price check, because the
    write accounting needs to know, *before* the build, whether a write was even
    going to be attempted. Kept deliberately conservative: anything unprovable
    reads as "not covered", which costs a rebuild and never a wrong count.
    """
    if not stored:
        return False
    if stored.get("model") != model:
        return False
    dte = stored.get("universe_dte")
    if dte is None or int(dte) < BACKFILL_UNIVERSE_DTE:
        return False
    lo, hi = stored.get("strike_gte"), stored.get("strike_lte")
    if lo is None or hi is None:
        return False
    if float(lo) > strike_gte + _BOUND_TOL or float(hi) < strike_lte - _BOUND_TOL:
        return False
    price = stored.get("underlying_price")
    # `_close`, not an absolute epsilon: the store's tolerance is RELATIVE
    # (`_PRICE_TOL * max(1, |a|, |b|)`), so an absolute 1e-9 would predict "not
    # covered" on a $900 underlying where the store says covered — and the
    # accounting would report a rebuild that never happened.
    if price is None or not _close(float(price), underlying_price):
        # A file built against a different close for the same session prices
        # every delta in it against that close; `get` refuses it and rebuilds.
        return False
    return True


def split_days(bars: Sequence[object]) -> Dict[date, float]:
    """Every split-sized move in ``bars``, as ``{date: ratio}``.

    ``alpaca_provider.detect_split`` deliberately returns only the FIRST such
    move and stops, which is right for "refuse this window" and wrong for
    "skip these days" — a two-year widening window can span two corporate
    actions, and reporting only the earlier one would let the later one through
    into the lake. Same thresholds, same raw-bar premise; only the arity
    differs.
    """
    out: Dict[date, float] = {}
    for prev, curr in zip(bars, bars[1:]):
        if prev.close <= 0:
            continue
        ratio = curr.close / prev.close
        if ratio < SPLIT_RATIO_LOW or ratio > SPLIT_RATIO_HIGH:
            out[curr.bar_date] = ratio
    return out


def build_chain_builder(provider, chain_store: Optional[ChainStore]) -> ChainBuilder:
    """The ONE construction, byte-identical to ``scenarios/runner.py``'s.

    Every default is load-bearing (module docstring, rule 3): the risk-free
    rate, the absent dividend map and the default spread model are all hashed
    into ``_model_fingerprint``, which is part of the cache key. A backfill that
    constructed this differently would write files no sweep can ever read.
    """
    return ChainBuilder(provider, store=chain_store)


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def run_backfill(
    config: "Config",
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    chain_store: Optional[ChainStore] = None,
    bar_provider: Optional[object] = None,
) -> BackfillSummary:
    """Bring bars + chains current for ``symbols`` over ``start..end``.

    Args:
        config: the profile whose Alpaca credentials build the provider. No
            strategy key is read — the chain is config-independent by design
            (``Simulator.materialise``'s contract), which is what lets one
            stored file serve every arm of every sweep.
        symbols: what to backfill. Order is preserved; duplicates collapse.
        start/end: inclusive decision window. ``end`` is clamped to the last
            settled session — today's chain is still forming and
            ``chain_builder._is_cacheable`` refuses to persist it anyway.
        chain_store: injected by the CLI (``ChainStore.from_env()``, so the lake
            is wired) and by tests. ``None`` builds a local-only store, which
            writes nothing to GCS.
        bar_provider: injected by tests. When ``None`` an ``AlpacaDataProvider``
            is built from the config and wrapped in ``CachedBarProvider`` so the
            bars cache fills as a side effect — the "bars" half of the Job's
            job. An injected provider is used exactly as given (the runner's
            convention): a test double must never be silently wrapped in a real
            disk cache.

    Returns:
        A ``BackfillSummary``. It never raises for a symbol- or day-level
        failure — those are recorded — so a caller decides the exit code from
        ``failed_symbols()``.
    """
    began = time.perf_counter()
    seen: Dict[str, None] = {}
    for raw in symbols:
        cleaned = str(raw).strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    universe = list(seen)

    end = min(end, last_settled_day())
    summary = BackfillSummary(
        start=start, end=end, universe_dte=BACKFILL_UNIVERSE_DTE
    )
    if end < start:
        logger.warning(
            "Backfill window is empty after clamping to the last settled "
            "session; nothing to do",
            event_category="backtest_data", event_type="backfill_empty_window",
            start=start.isoformat(), end=end.isoformat(),
        )
        return summary

    if chain_store is None:
        chain_store = ChainStore()
    if bar_provider is not None:
        provider = bar_provider
    else:
        provider = CachedBarProvider(
            AlpacaDataProvider.from_config(config), BarStore()
        )
    builder = build_chain_builder(provider, chain_store)

    logger.info(
        "Backfill starting",
        event_category="backtest_data", event_type="backfill_started",
        symbols=universe, start=start.isoformat(), end=end.isoformat(),
        universe_dte=BACKFILL_UNIVERSE_DTE,
        strike_window_pct=BACKFILL_STRIKE_WINDOW_PCT,
    )

    for symbol in universe:
        summary.symbols.append(
            _backfill_symbol(symbol, start, end, provider, builder, chain_store)
        )

    summary.seconds = time.perf_counter() - began
    summary.lake = chain_store.summary()
    return summary


def _backfill_symbol(
    symbol: str, start: date, end: date, provider, builder: ChainBuilder,
    chain_store: ChainStore,
) -> SymbolBackfill:
    """One symbol's pass. Never raises: a failure is recorded and returned."""
    row = SymbolBackfill(symbol=symbol)
    t0 = time.perf_counter()
    try:
        # Extra history fetched ONLY for split detection: a split-sized move is
        # a comparison between two adjacent sessions, so a split landing on the
        # window's first day is invisible without a predecessor bar. The extra
        # bars are never built into chains — only `in_window` is walked — and
        # they cost nothing: `CachedBarProvider` fetches the union of the
        # request and what it already holds in one call either way.
        bars = sorted(
            provider.get_stock_bars(symbol, start - _SPLIT_CONTEXT, end),
            key=lambda b: b.bar_date,
        )
        in_window = [b for b in bars if start <= b.bar_date <= end]
        preceding = [b for b in bars if b.bar_date < start]
        # Exactly ONE predecessor: the last settled session before the window.
        # Taking "whatever bars happen to fall in the lookback" made the
        # comparison depend on how many sessions the preceding fortnight
        # contained, which is a property of the calendar rather than of the
        # data. It also computed ratios BETWEEN pre-window bars, whose split
        # days are outside the window and can never be acted on.
        split_series = (preceding[-1:] + in_window)
        splits = split_days(split_series)
        if not preceding and in_window:
            # The residual blind spot, said out loud rather than left implicit:
            # with no earlier session in the fetch there is nothing to compare
            # the first day against, so a split exactly there would be missed.
            # Reachable only at the very start of the vendor's history for a
            # symbol; a widening chunk that begins mid-history always has one.
            logger.warning(
                "Backfill has no session before the window to detect a split "
                "on its first day",
                event_category="backtest_data",
                event_type="backfill_no_split_predecessor",
                symbol=symbol, as_of=in_window[0].bar_date.isoformat(),
                lookback_days=_SPLIT_CONTEXT.days,
            )
        model = builder._model_fingerprint(symbol)
        if not in_window:
            # A symbol with no settled session in a non-empty window did not
            # "complete with nothing to do" — something is wrong with the
            # symbol. The case this is written for is the one the onboarding
            # checklist creates: a typo'd ticker in `stocks.candidates`, which
            # Alpaca answers with an empty bar list rather than an error. As a
            # silent zero-day pass it would report success for ever, and the
            # first symptom would be a sweep on a symbol with no data at all.
            # Delisting and a window of pure market holidays land here too, and
            # both deserve the same "look at this" as the typo.
            raise ValueError(
                f"no settled sessions for {symbol} in {start}..{end} — the "
                f"provider returned no bars in the window. A typo'd ticker "
                f"answers exactly like this."
            )
        for bar in in_window:
            day = bar.bar_date
            row.days_checked += 1
            try:
                _backfill_day(
                    row, symbol, day, bar.close, splits, model, builder,
                    chain_store,
                )
            except Exception as exc:  # noqa: BLE001 - one day must not kill a symbol
                row.errors += 1
                logger.warning(
                    "Backfill day failed; continuing with the next session",
                    event_category="backtest_data",
                    event_type="backfill_day_failed",
                    symbol=symbol, as_of=day.isoformat(),
                    error=str(exc)[:300], error_class=type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
        row.error = f"{type(exc).__name__}: {exc}"[:300]
        logger.error(
            "Backfill symbol FAILED",
            event_category="backtest_data", event_type="backfill_symbol_failed",
            symbol=symbol, error=row.error,
        )
    row.seconds = time.perf_counter() - t0
    # Emitted per symbol, as it completes. A run killed at the task timeout, or
    # a SIGTERM'd container, leaves no terminal summary — these lines are what
    # make the half of the work that DID happen reconstructable.
    logger.info(
        "Backfill symbol complete",
        event_category="backtest_data", event_type="backfill_symbol_complete",
        **row.as_log(),
    )
    return row


def _backfill_day(
    row: SymbolBackfill, symbol: str, day: date, close: float,
    splits: Dict[date, float], model: str, builder: ChainBuilder,
    chain_store: ChainStore,
) -> None:
    """Ensure one (symbol, day) is stored at the backfill window."""
    if day in splits:
        # Skip-and-report, NOT skip-and-fail. Raw bars are the correct input for
        # point-in-time chain work (strikes are as-listed and never
        # retroactively adjusted), but the session a split lands on carries
        # adjusted contracts whose deliverable this engine does not model. That
        # is a property of the market, not a fault in the run — see
        # ``SymbolBackfill.failed`` for why it must not reach the exit code.
        row.days_skipped_corporate_action += 1
        logger.warning(
            "Backfill skipped a day: unmodelled corporate action",
            event_category="backtest_data", event_type="backfill_day_skipped",
            reason="corporate_action", symbol=symbol, as_of=day.isoformat(),
            ratio=round(splits[day], 4),
        )
        return
    if close is None or close <= 0:
        # An error, NOT a corporate-action skip: a settled session whose close
        # is missing or non-positive is broken vendor data, and the whole point
        # of separating the two counters is that this one still fails the run.
        row.errors += 1
        logger.warning(
            "Backfill FAILED a day: no usable underlying close",
            event_category="backtest_data", event_type="backfill_day_failed",
            reason="no_underlying_price", symbol=symbol, as_of=day.isoformat(),
            close=None if close is None else float(close),
        )
        return

    stored = chain_store.stored_window(symbol, day)
    gte, lte = union_window(close, stored)
    cost_basis, low_anchor = window_anchors(close, gte, lte)
    got_gte, got_lte = effective_window(close, cost_basis, low_anchor)
    if got_gte > gte + _BOUND_TOL or got_lte < lte - _BOUND_TOL:
        # Unreachable by construction; if the builder's window maths ever moves
        # under us, fail this day loudly rather than write a file that claims a
        # window it does not hold.
        raise ValueError(
            f"anchor back-solve does not reach the requested window for "
            f"{symbol} {day}: wanted [{gte:.6f}, {lte:.6f}], "
            f"anchors give [{got_gte:.6f}, {got_lte:.6f}]"
        )

    already = covers(stored, strike_gte=gte, strike_lte=lte, model=model,
                     underlying_price=close)
    before = _write_counters(chain_store)
    builder.build(
        symbol, day, BACKFILL_MAX_DTE,
        underlying_price=close, cost_basis=cost_basis, low_anchor=low_anchor,
    )
    after = _write_counters(chain_store)

    bucket = _classify_day(chain_store, before, after, already=already)
    if bucket == "errors":
        logger.warning(
            "Backfill FAILED a day: the chain lake did not take it",
            event_category="backtest_data", event_type="backfill_day_failed",
            reason="lake_write_failed", symbol=symbol, as_of=day.isoformat(),
            lake_errors=after["lake_errors"] - before["lake_errors"],
            lake_merge_gaps=after["lake_merge_gaps"] - before["lake_merge_gaps"],
            lake_live=after["lake_live"],
        )
    setattr(row, bucket, getattr(row, bucket) + 1)
    if bucket == "days_written" and stored is not None:
        row.days_replaced_wider += 1


def _classify_day(store: ChainStore, before: Dict[str, int],
                  after: Dict[str, int], *, already: bool) -> str:
    """Which single bucket this day belongs in. Exactly one, always.

    Reads the store's public counter deltas — ``summary()`` is its own
    aggregate view, so nothing here reaches into store internals. The order is
    a priority, not a preference:

    1. **A failed lake operation wins.** ``lake_errors`` moves when an RPC
       raised and the run continued local-only; ``lake_merge_gaps`` moves when
       two windows did not overlap so the merge published *nothing at all*.
       Either way the day is not in the lake. Before these two were sampled the
       day landed in no bucket whatsoever — a five-day run reporting four
       written and saying nothing about the fifth.
    2. **A declined upload** (``lake_skipped``) is the guard working: a wider
       object is already there, or a generation race was lost. Not a failure.
    3. **A write**: ``lake_puts`` for the ordinary path, ``lake_merged`` for an
       FC-091 union.
    4. **A dead lake makes everything after it an error, not a write.** When a
       CONFIGURED lake is switched off, ``_mirror_to_lake`` returns before
       touching a counter, so a local write moves nothing — and counting it as
       ``days_written`` would report a widened lake to an operator whose
       objects went to a container filesystem that is about to be destroyed.
    5. **No lake configured at all** is a legitimate local run: fall back to
       what the coverage check said, because ``lake_puts`` cannot move and the
       local file genuinely is the deliverable.
    6. Otherwise the stored object already covered the request — a hit.
    """
    if (after["lake_errors"] > before["lake_errors"]
            or after["lake_merge_gaps"] > before["lake_merge_gaps"]):
        return "errors"
    if after["lake_skipped"] > before["lake_skipped"]:
        return "days_skipped"
    if (after["lake_puts"] > before["lake_puts"]
            or after["lake_merged"] > before["lake_merged"]):
        return "days_written"
    if after["lake_configured"]:
        # Configured. If it is still live, no counter moving means the stored
        # object covered the request; if it is dead, this day never left the
        # machine.
        return "days_covered" if after["lake_live"] else "errors"
    return "days_written" if not already else "days_covered"


def _write_counters(store: ChainStore) -> Dict[str, int]:
    """The store's public write counters, plus its health. Sampled, not probed.

    ``summary()`` is the store's own aggregate view; taking deltas of it keeps
    this module out of ``ChainStore``'s internals entirely, which is what lets
    the store change how a write is decided without silently changing what this
    summary reports.
    """
    snapshot = store.summary()
    return {
        "lake_puts": int(snapshot.get("lake_puts") or 0),
        "lake_skipped": int(snapshot.get("lake_skipped") or 0),
        "lake_merged": int(snapshot.get("lake_merged") or 0),
        "lake_errors": int(snapshot.get("lake_errors") or 0),
        "lake_merge_gaps": int(snapshot.get("lake_merge_gaps") or 0),
        "lake_configured": bool(snapshot.get("lake_enabled")),
        "lake_live": bool(snapshot.get("lake_enabled"))
        and not snapshot.get("lake_disabled"),
    }


def resolve_window(
    *, history_days: int = DEFAULT_HISTORY_DAYS,
    start: Optional[date] = None, end: Optional[date] = None,
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """``(start, end)`` for a run, from any combination of the three inputs.

    ``end`` defaults to the last settled session (never today: today's chain is
    still forming). ``start`` defaults to ``history_days`` calendar days back
    from ``end`` — calendar, not trading, days, because the window is a
    freshness guarantee ("the last month is current"), not a session count.
    """
    resolved_end = end or last_settled_day(today)
    resolved_start = start or (resolved_end - timedelta(days=max(int(history_days), 0)))
    return resolved_start, resolved_end
