"""Evaluate mode — run one symbol end to end and produce its fitness report.

Ties the pieces together: data provider → chain builder → simulator (which drives
the live strategy) → cycle analytics → fitness scorecard → markdown/JSON.

Also runs the **fill sensitivity** pass the plan requires. The headline result
uses mid-minus-haircut fills; the same window is replayed at the bid, and if the
verdict flips between the two the report says so outright. A fitness call that
survives only under optimistic fills is not a fitness call.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Optional, Sequence

import structlog

from ..utils.config import Config
from .data.alpaca_provider import AlpacaDataProvider
from .data.bar_store import BarStore, CachedBarProvider
from .data.chain_builder import ChainBuilder
from .data.chain_store import ChainStore
from .data.dividends import (
    DividendSchedule,
    describe_coverage,
    load_default_schedule,
)
from .data.provider import StockBar
from .engine.simulator import Materialised, SimulationResult, Simulator
from .metrics.cycles import build_cycles, count_rolls
from .metrics.fitness import FitnessReport, compute_fitness

logger = structlog.get_logger(__name__)

# Fill haircut of 1.0 = sell at the bid: the worst case a marketable order sees.
BID_FILL_HAIRCUT = 1.0

# Headline fill assumption. Named rather than inlined so config_hash() can
# include it: changing it changes verdicts, and a verdict must not be
# indistinguishable from one produced under a different fill model.
DEFAULT_FILL_HAIRCUT = 0.25


def evaluate_symbol(
    symbol: str,
    start: date,
    end: date,
    *,
    config: Optional[Config] = None,
    starting_cash: float = 100_000.0,
    fill_haircut: float = DEFAULT_FILL_HAIRCUT,
    run_sensitivity: bool = True,
    chain_store: Optional[ChainStore] = None,
    bar_provider: Optional[object] = None,
    use_cache: bool = True,
) -> tuple:
    """Replay ``symbol`` and score it.

    Args:
        chain_store: parquet chain cache to use; defaults to one rooted at
            ``cache/backtest/`` (gitignored).
        bar_provider: the data provider to read from. Defaults to an
            ``AlpacaDataProvider`` wrapped in a ``CachedBarProvider`` when
            ``use_cache``. Injected by the scenario runner so one provider (and
            one bar cache) is shared across a whole sweep and its calls can be
            counted.
        use_cache: set False to force every chain to be rebuilt from the API and
            every bar to be re-fetched.

    Returns:
        ``(FitnessReport, sensitivity_dict_or_None)``.
    """
    config = config or Config()
    provider = bar_provider if bar_provider is not None else AlpacaDataProvider.from_config(config)
    # Cache on by default. Chains for settled sessions are immutable, and the
    # sensitivity pass below replays the identical window a second time under a
    # different fill assumption — without a cache that pays the full API bill
    # twice over for chains that cannot differ between the two passes.
    if use_cache and chain_store is None:
        # from_env, not ChainStore(): mirrors the local cache to the GCS chain
        # lake when CHAIN_LAKE_BUCKET is set (FC-060 Layer 1). Unset — every
        # local run without the env var — it is exactly ChainStore().
        chain_store = ChainStore.from_env()
    # Bars get the same treatment as chains (FC-060 Layer 2, D4). Without it a
    # "warm" run still made four network calls per symbol, so it was never
    # actually offline and a socket-blocked replay died on the first one.
    # Wrapped, never a change to AlpacaDataProvider: the cache belongs to the
    # run, not to the vendor client.
    if use_cache and bar_provider is None:
        provider = CachedBarProvider(provider, BarStore())
    builder = ChainBuilder(provider, store=chain_store if use_cache else None)
    max_dte = getattr(config, "put_target_dte", 7)
    # ONE schedule for both legs. The wheel's dividends come from this table via
    # the broker ledger and the benchmark's from the same table via
    # total_between; loading them separately would let the two drift apart and
    # reintroduce, silently, exactly the directional bias this removes.
    dividends = load_default_schedule()

    # Materialise ONCE, replay twice (FC-060 Layer 2, D6). The sensitivity pass
    # replays the identical window under a different FILL assumption; the bars,
    # trading days, strike anchors and chains cannot differ between the two, and
    # rebuilding them was the single largest cost in a warm run.
    materialised = _materialise(
        symbol, start, end, config, provider, builder,
        starting_cash=starting_cash, fill_haircut=fill_haircut, max_dte=max_dte,
        dividends=dividends,
    )
    result = _run(
        symbol, start, end, config, provider, builder,
        starting_cash=starting_cash, fill_haircut=fill_haircut, max_dte=max_dte,
        dividends=dividends, materialised=materialised,
    )
    report = _score(symbol, result, materialised.stock_bars.get(symbol, []),
                    starting_cash, dividends)

    sensitivity = None
    if run_sensitivity:
        bid_result = _run(
            symbol, start, end, config, provider, builder,
            starting_cash=starting_cash, fill_haircut=BID_FILL_HAIRCUT, max_dte=max_dte,
            dividends=dividends, materialised=materialised,
        )
        bid_report = _score(symbol, bid_result, materialised.stock_bars.get(symbol, []),
                            starting_cash, dividends)
        sensitivity = {
            "mid_haircut": fill_haircut,
            "mid_return": report.total_return,
            "mid_verdict": report.verdict(),
            "bid_return": bid_report.total_return,
            "bid_verdict": bid_report.verdict(),
            "verdict_flips": report.verdict() != bid_report.verdict(),
            "return_delta": bid_report.total_return - report.total_return,
        }
        if sensitivity["verdict_flips"]:
            logger.warning(
                "Fitness verdict depends on the fill assumption",
                event_category="backtest", event_type="fill_sensitivity_flip",
                symbol=symbol, mid_verdict=report.verdict(),
                bid_verdict=bid_report.verdict(),
            )
    # One line per symbol, only when a lake is configured — with no lake the
    # logs must stay byte-identical to the pre-FC-060 engine. This is how the
    # first warm monthly screen is measurable: hits >> puts means the lake is
    # carrying the run, and lake_errors > 0 means it silently degraded.
    if chain_store is not None and chain_store.lake is not None:
        logger.info(
            "Chain lake usage for this symbol",
            event_category="backtest_data", event_type="chain_lake_summary",
            symbol=symbol, **chain_store.summary(),
        )
    return report, sensitivity


def _simulator(
    symbol: str, start: date, end: date, config, provider, builder, *,
    starting_cash: float, fill_haircut: float, max_dte: int,
    dividends: Optional[DividendSchedule] = None,
) -> Simulator:
    return Simulator(
        config, provider, builder, [symbol], start, end,
        starting_cash=starting_cash, max_dte=max_dte, fill_haircut=fill_haircut,
        dividend_schedule=dividends,
    )


def _materialise(
    symbol: str, start: date, end: date, config, provider, builder, *,
    starting_cash: float, fill_haircut: float, max_dte: int,
    dividends: Optional[DividendSchedule] = None,
) -> Materialised:
    return _simulator(
        symbol, start, end, config, provider, builder,
        starting_cash=starting_cash, fill_haircut=fill_haircut, max_dte=max_dte,
        dividends=dividends,
    ).materialise()


def _run(
    symbol: str, start: date, end: date, config, provider, builder, *,
    starting_cash: float, fill_haircut: float, max_dte: int,
    dividends: Optional[DividendSchedule] = None,
    materialised: Optional[Materialised] = None,
) -> SimulationResult:
    """One replay. A FRESH ``Simulator`` per haircut, sharing ``materialised``.

    Reusing one simulator object for both passes would also work and is what the
    plan's D6 sketches, but it shares one ``HistoricalEarningsCalendar`` between
    them — and that object accumulates ``symbols_without_data`` /
    ``symbols_past_horizon`` across calls, which land in the report's
    data-quality block. A fresh simulator keeps the bid pass reporting exactly
    what a standalone bid run would report, which is what makes the split
    byte-identical rather than merely equivalent. The construction cost is a
    config deep-copy and a 3 KB JSON read; the materialisation, which is the
    expensive half, is done once and passed in.
    """
    simulator = _simulator(
        symbol, start, end, config, provider, builder,
        starting_cash=starting_cash, fill_haircut=fill_haircut, max_dte=max_dte,
        dividends=dividends,
    )
    if materialised is None:
        return simulator.run()
    return simulator.replay(materialised)


def _score(
    symbol: str,
    result: SimulationResult,
    bars: Sequence[StockBar],
    starting_cash: float,
    dividends: Optional[DividendSchedule] = None,
) -> FitnessReport:
    """Score a replay. ``bars`` are the materialised bars — never re-fetched.

    They cover the warm-up buffer as well as the decision window, so they are
    clipped to ``[result.start, result.end]`` here. That is exactly the set
    ``provider.get_stock_bars(symbol, result.start, result.end)`` returned
    before FC-060 Layer 2: the materialisation fetched with the same ``end``, so
    the same settlement clamp applies, and the warm-up bars sit below
    ``result.start``.
    """
    cycles = build_cycles(result.broker.ledger)
    prices = _closes(bars, result.start, result.end)
    quality = _data_quality(result, cycles)
    # Buy-and-hold enters at the close of result.start, so a dividend going ex
    # ON that day belongs to the seller, not this holder — total_between's
    # half-open lower bound encodes that.
    schedule = dividends or load_default_schedule()
    bench_divs = schedule.total_between(symbol, result.start, result.end)
    quality["dividend_coverage"] = describe_coverage(
        schedule, symbol, result.start, result.end
    )
    return compute_fitness(
        symbol, result.daily, cycles, starting_cash,
        benchmark_prices=prices, benchmark_dividends_per_share=bench_divs,
        data_quality=quality, rolls=count_rolls(cycles),
    )


def _closes(bars: Sequence[StockBar], start: date, end: date) -> Dict[date, float]:
    return {b.bar_date: b.close for b in bars if start <= b.bar_date <= end}


def _data_quality(result: SimulationResult, cycles: Sequence) -> Dict:
    """Facts a reader needs to judge whether the result rests on real data."""
    return {
        "decision_days": len(result.daily),
        "days_with_a_qualifying_candidate": result.candidate_days,
        "blocked_days_by_reason": result.rejections,
        "ledger_events": len(result.broker.ledger),
        "cycles_still_open_at_end": sum(1 for c in cycles if c.is_open),
        "option_marks": "daily bar closes (trade prints); bid/ask modeled",
        "greeks": "Black-Scholes inversions, not published values",
        "dividends_credited": round(result.dividends_credited, 2),
        "ex_dividend_early_assignments": result.early_assignments,
        # The residual of C2: ITM short calls on an ex-date eve that had no mark
        # to price extrinsic from, so the early-assignment test could not run.
        # Non-zero means some early assignments may be missing.
        "ex_div_calls_with_no_mark": result.unpriced_ex_div_calls,
        # FC-013. A replay whose window reaches past a traded symbol's last
        # table date silently stops gating that symbol — same class of defect
        # as a data gap, so it is reported the same way. Empty lists are the
        # healthy state.
        "earnings_symbols_missing_from_table": result.earnings_symbols_without_data,
        "earnings_symbols_past_table_horizon": result.earnings_symbols_past_horizon,
    }
