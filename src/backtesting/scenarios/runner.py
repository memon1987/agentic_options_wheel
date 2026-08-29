"""Materialise once, replay many — the FC-060 Layer 2 scenario sweep.

    for each symbol:
        for each window (fit / holdout, or just one):
            materialise ONCE           <- every provider call the sweep makes
            for each scenario:
                apply_overrides -> Simulator(config_s).replay(materialised)
                score -> one row

The whole bet is in the indentation: data assembly is config-independent (see
``Simulator.materialise``), so a 10-scenario sweep costs one materialisation per
(symbol, window) plus 10 in-memory replays, instead of 10 full runs. Measured
before the split, 10 scenarios x 6 symbols was 4-6 minutes; after, tens of
seconds.

**Zero provider calls during replays is asserted, not hoped for.** A regression
that reintroduced a per-replay fetch would still produce correct numbers — just
slowly, and only on a machine with network — so nothing about the results would
reveal it. ``_CountingProvider`` wraps whatever provider is supplied and the
sweep raises if the count moves inside the scenario loop.

SEQUENTIAL, DELIBERATELY (D10). Do NOT add a ``ThreadPoolExecutor`` here. Two
process-global hazards make this code thread-UNSAFE, and neither is local to
anything a worker would own:

  1. ``ExecutionEngine._failed_symbols`` is a module-global set that the day loop
     clears every simulated day and the simulator snapshots/restores around a
     run. Two replays in one process would clear each other's.
  2. ``RejectionTally.__enter__`` calls ``structlog.configure()``, which is
     process-global. Two concurrent tallies would install each other's
     processors and count each other's events.

Multiprocessing would sidestep both, and is out of scope precisely because the
measured cost does not justify the work: the sweep already fits inside a coffee
break.

**No BigQuery.** A sweep never writes to ``backtest_runs``. That table's
documented "current demotion candidates" query takes the latest ``run_kind='full'``
row, so a persisted full-universe sweep would displace the production screen with
a hypothetical. Layer 3 owns a store that cannot do that.

**No ``binding_constraint`` column, and it is not an oversight.** A sweep runs
many replays in one process, and only the FIRST of them gets a working
``RejectionTally``. ``setup_logging`` configures structlog with
``cache_logger_on_first_use=True``; a ``BoundLoggerLazyProxy`` caches its whole
processor chain on first use, and ``structlog.configure()`` — which is how the
tally installs itself — does not invalidate that cache. So every strategy logger
binds during replay #1 and keeps delivering to replay #1's tally for the life of
the process. Replays 2..N report an empty ``blocked_days_by_reason``, which reads
as "the strategy was never blocked" for a strategy that was blocked constantly.

Measured on ``origin/main`` at 7087007, two ``evaluate_symbol`` calls in one
process::

    call #1: blocked_days_by_reason={'already holds this underlying (scan, put)': 16,
                                     'selection: duplicate underlying': 4}
    call #2: blocked_days_by_reason={}

This is a PRE-EXISTING defect, not one Layer 2 introduces, and it is not Layer
2's to fix: the monthly screen runs 14 symbols in one process, so 13 of every 14
``backtest_runs`` rows already carry an empty tally and a NULL
``binding_constraint``, and correcting that changes what that table means. It
needs its own FC. What Layer 2 will not do is *launder* it — reporting a column
that is NULL by artifact as though it were a finding is the FC-057
dishonest-metric class, so the sweep does not carry one at all.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from ...utils.config import Config
from ..data.alpaca_provider import AlpacaDataProvider, UnadjustedCorporateAction
from ..data.bar_store import BarStore, CachedBarProvider
from ..data.chain_builder import ChainBuilder
from ..data.chain_store import ChainStore
from ..data.dividends import load_default_schedule
from ..engine.simulator import Materialised, Simulator
from ..evaluate import BID_FILL_HAIRCUT, DEFAULT_FILL_HAIRCUT, _score
from ..reporting.bq_writer import config_hash
from .overrides import apply_overrides, validate_overrides

logger = structlog.get_logger(__name__)

# The comparator every sweep runs first: the config exactly as configured, no
# overrides. Implicit — a scenario file never has to declare it, and a file that
# declares a scenario by this name is using the same thing.
BASE_SCENARIO_NAME = "base"

# Strategy loggers whose INFO stream is silenced during replays (D11). ~1.8 MB
# of `logs/options_wheel.log` per pass; 60 replays would be 100+ MB of noise that
# nobody reads and that slows the sweep measurably. WARNING and above still land,
# and the RejectionTally still counts everything: it installs its processor at
# the FRONT of the structlog chain, ahead of `structlog.stdlib.filter_by_level`,
# so it sees an event the stdlib level then drops.
_QUIET_LOGGERS = ("src", "deploy")


@contextmanager
def quiet_strategy_logs(enabled: bool = True):
    """Raise the strategy loggers to WARNING for the duration of a replay.

    The runner's own logger is exempted explicitly — it is a child of ``src`` and
    would otherwise silence its own phase and timing lines, which are the only
    output an operator watches during a sweep.
    """
    if not enabled:
        yield
        return
    saved = {}
    for name in _QUIET_LOGGERS + (__name__,):
        lg = logging.getLogger(name)
        saved[name] = lg.level
    try:
        for name in _QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        logging.getLogger(__name__).setLevel(logging.INFO)
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


@dataclass(frozen=True)
class Scenario:
    """One arm of a sweep: a name, some overrides, optionally a fill assumption.

    ``fill_haircut`` is a scenario field rather than a config key on purpose:
    the headline value lives on ``evaluate.DEFAULT_FILL_HAIRCUT`` and
    ``config_hash`` hashes the *module* value, so two scenarios differing only in
    haircut would carry the same hash and be indistinguishable in any record.
    """

    name: str
    overrides: Dict[str, Any] = field(default_factory=dict)
    fill_haircut: Optional[float] = None


@dataclass
class ScenarioResult:
    """One (scenario, symbol, window) cell of the sweep."""

    scenario: str
    symbol: str
    start: date
    end: date
    split: str  # 'fit' | 'holdout' | 'all'
    config_hash: str
    verdict: Optional[str] = None
    demote: Optional[bool] = None
    total_return: Optional[float] = None
    annualized_return: Optional[float] = None
    annualized_return_on_collateral: Optional[float] = None
    benchmark_return: Optional[float] = None
    excess_return: Optional[float] = None
    option_pnl: Optional[float] = None
    stock_pnl_realized: Optional[float] = None
    stock_pnl_unrealized: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    assignment_rate: Optional[float] = None
    puts_sold: Optional[int] = None
    calls_sold: Optional[int] = None
    cycles_completed: Optional[int] = None
    cycles_open: Optional[int] = None
    decision_days: Optional[int] = None
    days_in_position_fraction: Optional[float] = None
    bid_fill_return: Optional[float] = None
    verdict_flips_on_fill: Optional[bool] = None
    replay_seconds: Optional[float] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.verdict is not None

    @property
    def insufficient(self) -> bool:
        """The window contained no completed cycle.

        Never averaged away and never silently rendered as a return: an
        ``insufficient`` cell is a statement about the WINDOW, not a measurement
        of the scenario, and treating it as a small number is how a sweep
        manufactures a ranking out of nothing.
        """
        return self.verdict == "insufficient"

    def as_dict(self) -> Dict[str, Any]:
        out = {
            k: v for k, v in self.__dict__.items()
        }
        out["start"] = self.start.isoformat()
        out["end"] = self.end.isoformat()
        out["insufficient"] = self.insufficient
        return out


@dataclass
class SweepResult:
    """Everything the report needs, plus the provenance to trust it."""

    rows: List[ScenarioResult] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    windows: List[Tuple[str, date, date]] = field(default_factory=list)
    base_config_hash: str = ""
    scenario_config_hashes: Dict[str, str] = field(default_factory=dict)
    scenario_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    materialise_seconds: Dict[str, float] = field(default_factory=dict)
    replay_seconds: Dict[str, float] = field(default_factory=dict)
    wall_seconds: float = 0.0
    provider_calls_total: int = 0
    provider_calls_during_replays: int = 0
    starting_cash: float = 0.0
    run_sensitivity: bool = False

    @property
    def errors(self) -> List[ScenarioResult]:
        return [r for r in self.rows if r.error is not None]

    @property
    def has_holdout(self) -> bool:
        return any(split == "holdout" for split, _s, _e in self.windows)

    def cell(self, scenario: str, symbol: str, split: str = "all") -> Optional[ScenarioResult]:
        for row in self.rows:
            if row.scenario == scenario and row.symbol == symbol and row.split == split:
                return row
        return None


class _CountingProvider:
    """Counts every call that reaches the wrapped data provider.

    The "zero provider calls during replays" contract is the whole reason the
    sweep is affordable, and it is invisible in the results: a regression that
    re-fetched per replay would still produce correct numbers. So it is asserted
    on a counter rather than inferred from wall-clock or read off a log line.
    """

    def __init__(self, provider) -> None:
        self._provider = provider
        self.calls = 0
        self.by_method: Dict[str, int] = {}

    def _count(self, method: str) -> None:
        self.calls += 1
        self.by_method[method] = self.by_method.get(method, 0) + 1

    def get_contract_universe(self, *args, **kwargs):
        self._count("get_contract_universe")
        return self._provider.get_contract_universe(*args, **kwargs)

    def get_option_bars(self, *args, **kwargs):
        self._count("get_option_bars")
        return self._provider.get_option_bars(*args, **kwargs)

    def get_stock_bars(self, *args, **kwargs):
        self._count("get_stock_bars")
        return self._provider.get_stock_bars(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._provider, name)


def _windows(
    start: date, end: date, holdout_start: Optional[date]
) -> List[Tuple[str, date, date]]:
    """``[('all', start, end)]``, or a fit/holdout pair when a split is asked for.

    The fit window ends the day BEFORE ``holdout_start``, so the two are disjoint
    — an overlapping split would let a scenario be chosen on data it is then
    'validated' against, which is the failure the split exists to prevent.
    """
    if holdout_start is None:
        return [("all", start, end)]
    if not (start < holdout_start <= end):
        raise ValueError(
            f"--holdout-start {holdout_start} must fall inside "
            f"({start}, {end}]; otherwise one of the two windows is empty."
        )
    return [
        ("fit", start, holdout_start - timedelta(days=1)),
        ("holdout", holdout_start, end),
    ]


def _row_from_report(
    *, scenario: str, symbol: str, window: Tuple[str, date, date],
    cfg_hash: str, report, sensitivity: Optional[dict], seconds: float,
) -> ScenarioResult:
    split, start, end = window
    verdict = report.verdict()
    return ScenarioResult(
        scenario=scenario, symbol=symbol, start=start, end=end, split=split,
        config_hash=cfg_hash,
        verdict=verdict,
        # Same rule as `bq_writer.build_row`: 'insufficient' is a statement about
        # the window and a verdict that flips on the fill assumption is not a
        # verdict, so neither sets the flag.
        demote=(verdict == "unfit"
                and not (sensitivity or {}).get("verdict_flips", False)),
        total_return=report.total_return,
        annualized_return=report.annualized_return,
        annualized_return_on_collateral=report.annualized_return_on_collateral,
        benchmark_return=(report.benchmark.total_return if report.benchmark else None),
        excess_return=report.excess_return,
        option_pnl=report.option_pnl,
        stock_pnl_realized=report.stock_pnl,
        stock_pnl_unrealized=report.unrealized_stock_pnl,
        max_drawdown=report.max_drawdown,
        win_rate=report.win_rate,
        assignment_rate=report.assignment_rate,
        puts_sold=report.puts_sold,
        calls_sold=report.calls_sold,
        cycles_completed=len(report.closed_cycles),
        cycles_open=len(report.cycles) - len(report.closed_cycles),
        decision_days=report.decision_days,
        days_in_position_fraction=report.days_in_position_fraction,
        bid_fill_return=(sensitivity or {}).get("bid_return"),
        verdict_flips_on_fill=(sensitivity or {}).get("verdict_flips"),
        replay_seconds=round(seconds, 3),
    )


def run_sweep(
    base_config: Config,
    scenarios: Sequence[Scenario],
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    holdout_start: Optional[date] = None,
    starting_cash: float = 100_000.0,
    run_sensitivity: bool = False,
    chain_store: Optional[ChainStore] = None,
    bar_provider: Optional[object] = None,
    quiet_logs: bool = True,
) -> SweepResult:
    """Replay every scenario over every symbol, materialising each window once.

    Args:
        base_config: the config every scenario is derived from. Never mutated.
        scenarios: the arms. A ``base`` scenario (no overrides) is prepended
            unless the caller already supplied one, and always runs first — every
            other row is only interesting relative to it.
        symbols: run SCOPE, not a scenario dimension (see ``overrides``).
        start/end: the decision window.
        holdout_start: split the window into a fit half and a holdout half.
        starting_cash: per-symbol capital; identical across scenarios so the
            rows are comparable.
        run_sensitivity: also replay each scenario at the bid. Doubles the
            replay count; off by default because a sweep is a ranking exercise
            and the flip flag matters most on the arm you finally choose.
        chain_store / bar_provider: injected by tests and by the CLI.
        quiet_logs: silence the strategy loggers below WARNING during replays.

    Raises:
        OverrideError: from the up-front validation pass, before any replay
            starts. A typo in scenario 9 must not be discovered after scenario 8
            has already burned two minutes.
        RuntimeError: if any provider call happens during the replay loop.

    A scenario that RAISES mid-replay records its error on that row and the sweep
    continues: one bad arm must not cost the other fifty-nine their results.
    """
    began = time.perf_counter()
    scenarios = _with_base_first(scenarios)

    # Validate everything up front (behavior contract). apply_overrides also
    # validates, but doing it here means a bad key fails in milliseconds rather
    # than after the first materialisation.
    for scenario in scenarios:
        validate_overrides(
            scenario.overrides,
            chain_reach_dte=int(getattr(base_config, "put_target_dte", 7)),
        )
    _check_unique_names(scenarios)

    symbols = [s.upper() for s in symbols]
    windows = _windows(start, end, holdout_start)

    provider = _CountingProvider(
        bar_provider if bar_provider is not None
        else CachedBarProvider(AlpacaDataProvider.from_config(base_config), BarStore())
    )
    if chain_store is None:
        chain_store = ChainStore.from_env()
    builder = ChainBuilder(provider, store=chain_store)
    max_dte = int(getattr(base_config, "put_target_dte", 7))
    dividends = load_default_schedule()

    scenario_configs: Dict[str, Config] = {
        s.name: apply_overrides(base_config, s.overrides) for s in scenarios
    }
    result = SweepResult(
        scenarios=[s.name for s in scenarios],
        symbols=list(symbols),
        windows=windows,
        base_config_hash=config_hash(base_config),
        scenario_config_hashes={
            name: config_hash(cfg) for name, cfg in scenario_configs.items()
        },
        scenario_overrides={s.name: dict(s.overrides) for s in scenarios},
        starting_cash=starting_cash,
        run_sensitivity=run_sensitivity,
    )
    logger.info(
        "Scenario sweep starting",
        event_category="backtest", event_type="sweep_started",
        scenarios=len(scenarios), symbols=len(symbols),
        windows=[f"{sp}:{s}..{e}" for sp, s, e in windows],
        base_config_hash=result.base_config_hash,
    )

    for symbol in symbols:
        for window in windows:
            split, w_start, w_end = window
            key = f"{symbol}:{split}"
            calls_before_materialise = provider.calls
            t0 = time.perf_counter()
            try:
                materialised = _materialise_window(
                    base_config, provider, builder, symbol, w_start, w_end,
                    starting_cash=starting_cash, max_dte=max_dte,
                    dividends=dividends,
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the sweep
                elapsed = time.perf_counter() - t0
                result.materialise_seconds[key] = round(elapsed, 3)
                message = _describe(exc)
                logger.error(
                    "Materialisation FAILED — every scenario for this window is "
                    "recorded as an error rather than silently missing",
                    event_category="backtest",
                    event_type="sweep_materialise_failed",
                    symbol=symbol, split=split, error=message[:300],
                )
                for scenario in scenarios:
                    result.rows.append(ScenarioResult(
                        scenario=scenario.name, symbol=symbol,
                        start=w_start, end=w_end, split=split,
                        config_hash=result.scenario_config_hashes[scenario.name],
                        error=message,
                    ))
                continue
            result.materialise_seconds[key] = round(time.perf_counter() - t0, 3)
            logger.info(
                "Window materialised",
                event_category="backtest", event_type="sweep_materialised",
                symbol=symbol, split=split,
                window=f"{w_start}..{w_end}",
                decision_days=len(materialised.days),
                seconds=result.materialise_seconds[key],
                provider_calls=provider.calls - calls_before_materialise,
            )

            calls_before_replays = provider.calls
            by_method_before = dict(provider.by_method)
            with quiet_strategy_logs(quiet_logs):
                for scenario in scenarios:
                    row = _replay_one(
                        scenario=scenario,
                        config=scenario_configs[scenario.name],
                        cfg_hash=result.scenario_config_hashes[scenario.name],
                        provider=provider, builder=builder,
                        symbol=symbol, window=window,
                        materialised=materialised,
                        starting_cash=starting_cash, max_dte=max_dte,
                        dividends=dividends,
                        run_sensitivity=run_sensitivity,
                    )
                    result.rows.append(row)
                    rkey = f"{scenario.name}:{symbol}:{split}"
                    result.replay_seconds[rkey] = row.replay_seconds or 0.0
            leaked = provider.calls - calls_before_replays
            result.provider_calls_during_replays += leaked
            if leaked:
                # Hard failure, not a warning. A replay that reaches the network
                # invalidates the entire premise of the sweep — and it does so
                # invisibly, because the numbers still come out right.
                escaped = {
                    method: count - by_method_before.get(method, 0)
                    for method, count in provider.by_method.items()
                    if count - by_method_before.get(method, 0) > 0
                }
                raise RuntimeError(
                    f"{leaked} provider call(s) escaped during the replay loop "
                    f"for {symbol} ({split}): {escaped}. A replay must read only "
                    "the Materialised it was handed; something is re-fetching per "
                    "scenario and the sweep's cost model is void."
                )

    result.wall_seconds = round(time.perf_counter() - began, 3)
    result.provider_calls_total = provider.calls
    logger.info(
        "Scenario sweep complete",
        event_category="backtest", event_type="sweep_completed",
        rows=len(result.rows), errors=len(result.errors),
        wall_seconds=result.wall_seconds,
        materialise_seconds=round(sum(result.materialise_seconds.values()), 3),
        replay_seconds=round(sum(result.replay_seconds.values()), 3),
        provider_calls_total=result.provider_calls_total,
        provider_calls_during_replays=result.provider_calls_during_replays,
    )
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _with_base_first(scenarios: Sequence[Scenario]) -> List[Scenario]:
    """``base`` first, always. Every other row is read relative to it."""
    ordered = list(scenarios)
    existing = next((s for s in ordered if s.name == BASE_SCENARIO_NAME), None)
    if existing is None:
        return [Scenario(BASE_SCENARIO_NAME, {})] + ordered
    ordered.remove(existing)
    return [existing] + ordered


def _check_unique_names(scenarios: Sequence[Scenario]) -> None:
    seen = set()
    for scenario in scenarios:
        if scenario.name in seen:
            raise ValueError(
                f"duplicate scenario name {scenario.name!r}: rows are keyed by "
                "(scenario, symbol, split), so two arms sharing a name would "
                "overwrite each other in the grid."
            )
        seen.add(scenario.name)


def _simulator(
    config, provider, builder, symbol: str, start: date, end: date,
    *, starting_cash: float, max_dte: int, fill_haircut: float, dividends,
) -> Simulator:
    return Simulator(
        config, provider, builder, [symbol], start, end,
        starting_cash=starting_cash, max_dte=max_dte,
        fill_haircut=fill_haircut, dividend_schedule=dividends,
    )


def _materialise_window(
    base_config, provider, builder, symbol: str, start: date, end: date,
    *, starting_cash: float, max_dte: int, dividends,
) -> Materialised:
    """Build the window's data ONCE, under the BASE config.

    Using the base config here is safe *because* the allowlist says so: no
    allowed override touches chain reach or the strike window, so any scenario's
    materialisation would be byte-identical to this one. ``Simulator.replay``
    re-checks the universe/window/DTE agreement per scenario, so if that ever
    stops being true the sweep fails loudly instead of replaying against chains
    that do not cover it.
    """
    return _simulator(
        base_config, provider, builder, symbol, start, end,
        starting_cash=starting_cash, max_dte=max_dte,
        fill_haircut=DEFAULT_FILL_HAIRCUT, dividends=dividends,
    ).materialise()


def _replay_one(
    *, scenario: Scenario, config, cfg_hash: str, provider, builder,
    symbol: str, window: Tuple[str, date, date], materialised: Materialised,
    starting_cash: float, max_dte: int, dividends, run_sensitivity: bool,
) -> ScenarioResult:
    split, w_start, w_end = window
    haircut = (
        DEFAULT_FILL_HAIRCUT if scenario.fill_haircut is None
        else scenario.fill_haircut
    )
    t0 = time.perf_counter()
    try:
        bars = materialised.stock_bars.get(symbol, [])
        result = _simulator(
            config, provider, builder, symbol, w_start, w_end,
            starting_cash=starting_cash, max_dte=max_dte,
            fill_haircut=haircut, dividends=dividends,
        ).replay(materialised)
        report = _score(symbol, result, bars, starting_cash, dividends)

        sensitivity = None
        if run_sensitivity:
            bid_result = _simulator(
                config, provider, builder, symbol, w_start, w_end,
                starting_cash=starting_cash, max_dte=max_dte,
                fill_haircut=BID_FILL_HAIRCUT, dividends=dividends,
            ).replay(materialised)
            bid_report = _score(symbol, bid_result, bars, starting_cash, dividends)
            sensitivity = {
                "mid_haircut": haircut,
                "mid_return": report.total_return,
                "mid_verdict": report.verdict(),
                "bid_return": bid_report.total_return,
                "bid_verdict": bid_report.verdict(),
                "verdict_flips": report.verdict() != bid_report.verdict(),
                "return_delta": bid_report.total_return - report.total_return,
            }
        return _row_from_report(
            scenario=scenario.name, symbol=symbol, window=window,
            cfg_hash=cfg_hash, report=report, sensitivity=sensitivity,
            seconds=time.perf_counter() - t0,
        )
    except Exception as exc:  # noqa: BLE001 - one arm must not lose the others
        message = _describe(exc)
        logger.error(
            "Scenario replay FAILED",
            event_category="backtest", event_type="sweep_scenario_failed",
            scenario=scenario.name, symbol=symbol, split=split,
            error=message[:300],
        )
        return ScenarioResult(
            scenario=scenario.name, symbol=symbol, start=w_start, end=w_end,
            split=split, config_hash=cfg_hash, error=message,
            replay_seconds=round(time.perf_counter() - t0, 3),
        )


def _describe(exc: BaseException) -> str:
    """A message that says what KIND of failure this was, not just its text.

    A corporate action is a data-scope limit, not a verdict on the scenario, and
    the two must not read the same in the report.
    """
    if isinstance(exc, UnadjustedCorporateAction):
        return f"corporate_action: {exc}"
    return f"{type(exc).__name__}: {exc}"
