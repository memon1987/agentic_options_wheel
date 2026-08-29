"""Screen mode — evaluate the whole universe on a cadence.

Where `evaluate` answers "is this candidate symbol a fit?", screen answers
"which symbols we currently trade have stopped being one?" It runs every
configured symbol through the same engine, writes one row per symbol to
`options_wheel.backtest_runs`, and flags demotion candidates.

**Demotion stays a human decision.** The row carries `demote=true` as a
recommendation and the reasons behind it; nothing here changes the trading
universe. The plan is explicit that two cycles should be observed before any
automation is wired, and the fidelity caveats (unmodeled dividends, unmodeled
early assignment, a single vol regime) are exactly why.

A symbol that fails still gets a row, carrying its error. Dropping it would make
the run look like it screened fewer symbols than it attempted — the difference
between "this symbol is fine" and "this symbol was never checked".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import structlog

from ..utils.config import Config
from .data.alpaca_provider import ALPACA_OPTIONS_HISTORY_START, UnadjustedCorporateAction
from .data.chain_store import ChainStore
from .evaluate import evaluate_symbol
from .reporting.bq_writer import BacktestRunWriter, build_row, config_hash

logger = structlog.get_logger(__name__)

# Stamped on every `backtest_runs` row so a verdict can be traced to the engine
# that produced it. FC-068 repointed the replay from WheelEngine's dead
# orchestration path onto the production scan -> select -> execute pipeline and
# premium-netted the assignment basis: rows either side of this string are NOT
# comparable. (FC-048 changed the measurement just as much — every backtest
# before it ran a put-only wheel — and did NOT bump this, so its boundary is
# timestamp-only, 2026-07-29.)
#
# FC-069 item 12 bumped it again. Two things moved under the replay: the
# scanner's put-side existing-position check stopped substring-matching OCC
# symbols (a symbol the replay used to skip on a spelling coincidence is now
# scanned), and the rejection vocabulary gained a bucket that is deliberately
# excluded from `binding_constraint` selection. So rows either side differ in
# both `blocked_days_by_reason` and `binding_constraint` semantics, on top of
# any verdict the un-over-blocking moved. Not bumping would have reproduced the
# FC-048 timestamp-only wart this very comment criticizes.
ENGINE_VERSION = "fc-069-scanner-rewire"

# Default lookback for a screening run. Long enough for a meaningful number of
# cycles, short enough that a symbol's *recent* behavior dominates — a demotion
# decision should not be anchored to how a name traded two years ago.
DEFAULT_LOOKBACK_DAYS = 365


@dataclass
class SymbolResult:
    symbol: str
    report: object = None
    sensitivity: Optional[dict] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.report is not None

    @property
    def verdict(self) -> str:
        return self.report.verdict() if self.ok else "error"


@dataclass
class ScreenResult:
    run_id: str
    start: date
    end: date
    results: List[SymbolResult] = field(default_factory=list)
    persisted: bool = False
    run_kind: str = "full"
    universe_size: int = 0

    @property
    def demote_candidates(self) -> List[str]:
        return [r.symbol for r in self.results if r.ok and r.verdict == "unfit"]

    @property
    def failures(self) -> List[str]:
        return [r.symbol for r in self.results if not r.ok]

    def tally(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.results:
            out[r.verdict] = out.get(r.verdict, 0) + 1
        return out


def run_screen(
    *,
    config: Optional[Config] = None,
    symbols: Optional[Sequence[str]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    starting_cash: float = 100_000.0,
    persist: bool = True,
    run_sensitivity: bool = True,
) -> ScreenResult:
    """Evaluate every symbol and (optionally) persist the results.

    Args:
        config: strategy config; the universe comes from ``config.stock_symbols``.
        symbols: override the universe.
        start/end: evaluation window; defaults to the last year, clamped to
            Alpaca's options-history floor.
        starting_cash: per-symbol capital. Each symbol is evaluated
            independently on the same notional, so verdicts are comparable.
        persist: write to BigQuery.
        run_sensitivity: also replay at the bid. Doubles runtime; worth it,
            because a verdict that flips on the fill assumption is not a verdict.
    """
    config = config or Config()
    universe = list(symbols or config.stock_symbols)
    end = end or date.today()
    start = start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    if start < ALPACA_OPTIONS_HISTORY_START:
        logger.info("Clamping screen window to Alpaca's options-history floor",
                    requested=start.isoformat(),
                    floor=ALPACA_OPTIONS_HISTORY_START.isoformat())
        start = ALPACA_OPTIONS_HISTORY_START

    run_id = uuid.uuid4().hex[:16]
    cfg_hash = config_hash(config)
    # A run covering the whole configured universe is 'full'; anything narrower
    # is 'adhoc' and must never be mistaken for the current state of the
    # universe by a "latest run" query.
    run_kind = "full" if set(universe) == set(config.stock_symbols) else "adhoc"
    logger.info("Screen run starting",
                event_category="backtest", event_type="screen_started",
                run_id=run_id, symbols=len(universe),
                window=f"{start}..{end}", config_hash=cfg_hash)

    # Construct the writer BEFORE the loop: a missing credential or wrong
    # project discovered after ~a minute per symbol wastes the entire run and
    # leaves the results only in memory.
    writer = BacktestRunWriter() if persist else None
    if persist and not writer.enabled:
        logger.error(
            "BigQuery writer unavailable — screening anyway, but results will "
            "NOT be persisted. Fix credentials/GCP_PROJECT to record this run.",
            event_category="backtest", event_type="screen_writer_unavailable",
        )

    result = ScreenResult(run_id=run_id, start=start, end=end,
                          run_kind=run_kind, universe_size=len(universe))
    # ONE lake for the run, shared by the per-symbol stores (FC-060). The GCS
    # client, the bucket probe and the circuit breaker are then run-scoped: a
    # dead bucket is discovered once, not once per symbol, and five consecutive
    # failures switch the lake off for the whole screen rather than for one
    # symbol at a time.
    lake = ChainStore.lake_from_env()
    lake_totals: Dict[str, int] = {}
    for symbol in universe:
        chain_store = ChainStore(lake=lake) if lake is not None else None
        try:
            report, sensitivity = evaluate_symbol(
                symbol, start, end, config=config,
                starting_cash=starting_cash, run_sensitivity=run_sensitivity,
                chain_store=chain_store,
            )
            result.results.append(SymbolResult(symbol, report, sensitivity))
            logger.info("Screened symbol",
                        event_category="backtest", event_type="screen_symbol_done",
                        symbol=symbol, verdict=report.verdict(),
                        total_return=round(report.total_return, 4))
        except UnadjustedCorporateAction as exc:
            # A split in the window is a data-scope limit, not a symbol verdict.
            # Recording it as an error keeps it out of the demote list, where it
            # would read as a judgement about the symbol.
            result.results.append(SymbolResult(symbol, error=f"corporate_action: {exc}"))
            logger.warning("Symbol skipped: unmodelled corporate action",
                           event_category="backtest",
                           event_type="screen_symbol_skipped",
                           symbol=symbol, reason=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the run
            result.results.append(SymbolResult(symbol, error=str(exc)))
            logger.error("Symbol FAILED during screen",
                         event_category="backtest",
                         event_type="screen_symbol_failed",
                         symbol=symbol, error=str(exc)[:300])
        finally:
            # Accumulated in `finally` so a symbol that raised still contributes
            # its lake usage — a run whose lake died mid-way is exactly the case
            # the run-level summary exists to make visible.
            if chain_store is not None:
                accumulate_lake_summary(lake_totals, chain_store.summary())

    _log_lake_run_summary(lake, lake_totals, run_id)

    if persist:
        rows = [
            build_row(run_id=run_id, symbol=r.symbol, report=r.report,
                      sensitivity=r.sensitivity, cfg_hash=cfg_hash,
                      engine_version=ENGINE_VERSION, error=r.error,
                      run_kind=run_kind, universe_size=len(universe),
                      window_start=start, window_end=end)
            for r in result.results
        ]
        result.persisted = writer.write(rows)

    logger.info("Screen run complete",
                event_category="backtest", event_type="screen_completed",
                run_id=run_id, tally=result.tally(),
                demote_candidates=result.demote_candidates,
                failures=result.failures, persisted=result.persisted)
    return result


# PUBLIC (FC-060 Layer 3): the sweep Job aggregates the same per-symbol counters
# into the same run-level shape, so `scenario_sweeps.lake_summary_json` and the
# screen's `chain_lake_run_summary` log line describe the lake the same way. A
# second copy of this tuple in `scenarios/persist.py` would drift the day a
# counter is added and nothing would notice — the sweep would just stop
# reporting it.
LAKE_COUNTERS = (
    "lake_hits", "lake_misses", "lake_rejected",
    "lake_puts", "lake_skipped", "lake_skipped_unreadable_remote",
    "lake_merged", "lake_merge_gaps", "lake_merge_refused",
    "lake_errors",
)


def accumulate_lake_summary(totals: Dict[str, int], summary: Dict) -> None:
    """Add one ``ChainStore.summary()`` into a run-level total, in place."""
    for key in LAKE_COUNTERS:
        totals[key] = totals.get(key, 0) + int(summary.get(key, 0))


# Back-compat aliases. `tests/test_chain_store_lake.py` imports the private
# names, and renaming a helper is not what this FC is for.
_LAKE_COUNTERS = LAKE_COUNTERS
_accumulate_lake = accumulate_lake_summary


def _log_lake_run_summary(lake, totals: Dict[str, int], run_id: str) -> None:
    """One run-level line, plus a warning when the lake did not do its job.

    The per-symbol `chain_lake_summary` lines are the detail; this is the line
    an operator reads after a monthly screen. `chain_lake_degraded` exists
    because the failure mode of this feature is *silence*: a lake that errored
    on every call still produces a green run that took the full cold 1h47m, and
    without a warning nobody would look.

    A refused merge (FC-091) degrades the run for the same reason an error
    does: the day did not heal, so it will be re-fetched cold next month and
    every month after. `lake_merge_gaps` and `lake_merge_refused` therefore
    trip the warning too — a screen whose thrashing symbols are silently
    failing to heal must not read as clean.
    """
    if lake is None:
        return
    payload = dict(totals)
    payload.update(
        lake_bucket=lake.bucket_name,
        lake_prefix=lake.prefix,
        lake_disabled=lake.disabled,
        lake_disabled_reason=lake.disabled_reason,
    )
    logger.info("Chain lake usage for this run",
                event_category="backtest_data",
                event_type="chain_lake_run_summary",
                run_id=run_id, **payload)
    if (
        lake.disabled
        or totals.get("lake_errors", 0) > 0
        or totals.get("lake_merge_gaps", 0) > 0
        or totals.get("lake_merge_refused", 0) > 0
    ):
        logger.warning(
            "Chain lake degraded during this run — the screen ran at least "
            "partly cold, or a thrashing symbol failed to heal, and the lake "
            "may not have been updated",
            event_category="backtest_data",
            event_type="chain_lake_degraded",
            run_id=run_id, **payload,
        )


def render_screen_summary(result: ScreenResult) -> str:
    """Operator-facing summary. Demotions are recommendations, not actions."""
    out: List[str] = []
    a = out.append
    a(f"# Universe screen — {result.start} → {result.end}")
    a("")
    scope = ("full universe" if result.run_kind == "full"
             else "**ad-hoc subset — not a picture of the whole universe**")
    a(f"Run `{result.run_id}` · {len(result.results)} symbols · {scope} · "
      f"persisted: {'yes' if result.persisted else '**NO**'}")
    a("")

    a("| symbol | verdict | return | vs B&H | ann. on collateral | days in position |")
    a("|---|---|---:|---:|---:|---:|")
    for r in sorted(result.results, key=lambda x: (x.verdict, x.symbol)):
        if not r.ok:
            a(f"| {r.symbol} | **error** | — | — | — | — |")
            continue
        rep = r.report
        aroc = rep.annualized_return_on_collateral
        aroc_cell = f"{aroc:+.2%}" if aroc is not None else "—"
        # An absent benchmark must render as "—", never "+0.00%": the latter
        # reads as "exactly matched buy-and-hold", i.e. fabricated evidence in
        # the table that justifies a demotion.
        excess = rep.excess_return
        excess_cell = f"{excess:+.2%}" if excess is not None else "—"
        a(f"| {rep.symbol} | {rep.verdict()} | {rep.total_return:+.2%} "
          f"| {excess_cell} | {aroc_cell} "
          f"| {rep.days_in_position_fraction:.0%} |")
    a("")

    if result.demote_candidates:
        a("## Demotion candidates")
        a("")
        a("These scored **unfit**. A recommendation for a human to weigh, not "
          "an action.")
        a("")
        a("> **Weigh it against the engine's biases, which do not all point the "
          "same way.** Unmodeled dividends *flatter* the wheel on the "
          "buy-and-hold comparison, but *penalise* it on the absolute gates "
          "(`lost money`, `below the risk-free rate`) that actually produce a "
          "demotion — the wheel forgoes real dividends whenever it holds "
          "assigned shares. On a 5–7% yielder that alone can push it under the "
          "floor, so **on income names this list is biased toward demoting**. "
          "Unmodeled early assignment is optimistic in the other direction. "
          "Do not demote a dividend payer on this engine until dividends are "
          "modeled.")
        a("")
        for symbol in result.demote_candidates:
            r = next(x for x in result.results if x.symbol == symbol)
            a(f"- **{symbol}** — " + "; ".join(r.report.verdict_reasons()))
        a("")
    else:
        a("No demotion candidates in this run.")
        a("")

    if result.failures:
        a("## Failed to screen")
        a("")
        a("These produced no verdict at all. They are **not** implicitly fine — "
          "they were never checked.")
        a("")
        for symbol in result.failures:
            r = next(x for x in result.results if x.symbol == symbol)
            a(f"- **{symbol}** — {(r.error or '')[:160]}")
        a("")

    if not result.persisted:
        a("> **Results were not persisted to BigQuery.** This summary is the only "
          "record of the run.")
        a("")
    return "\n".join(out)
