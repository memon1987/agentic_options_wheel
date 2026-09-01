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

**Zero data-layer reads during replays is asserted, not hoped for.** A
regression that reintroduced a per-replay fetch would still produce correct
numbers — just slowly, and only on a machine with network — so nothing about the
results would reveal it. Both halves are counted: network round-trips on a
``_CountingProvider`` wrapped directly around the vendor client, and bar-cache
reads on ``CachedBarProvider.hits``. The sweep raises if either moves inside the
scenario loop; counting only the network would let a replay that re-read bars
off disk pass unnoticed.

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

import json
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
from ..engine.simulator import Materialised, Simulator, narrow_to_dte
from ..evaluate import BID_FILL_HAIRCUT, DEFAULT_FILL_HAIRCUT, _score
from ..metrics.fitness import MIN_DAYS_IN_POSITION
from ..reporting.bq_writer import config_hash
from .identity import scenario_arm_hash
from .overrides import (
    DTE_OVERRIDE_KEYS, apply_overrides, validate_overrides,
)

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

    def scenario_hash(self) -> str:
        """Identity of this ARM: its effective overrides plus its fill haircut.

        ``config_hash`` cannot do this job. It hashes nine strategy parameters
        plus the module-level scoring constants — 12 of the 19 allowlisted
        override keys are outside it entirely (every ``rolling.*`` and
        ``earnings.*`` key, ``universe.*``, ``min_avg_volume``), and the fill
        haircut it hashes is the MODULE default, not this scenario's. So two
        arms that differ in a roller knob, or only in ``fill_haircut``, carry the
        SAME ``config_hash`` and are indistinguishable in any stored record.

        Both hashes are reported. ``config_hash`` keeps a sweep row comparable
        with a ``backtest_runs`` row; ``scenario_hash`` is what makes two rows of
        this sweep distinguishable from each other.

        **The bytes are produced by ``identity.scenario_arm_hash``**, which is
        stdlib-only and copied into the dashboard image (FC-060 Layer 3): the
        API computes this exact hash to key its dedup lookup before it launches
        anything, and a second implementation would be a second definition of
        "the same arm".
        """
        return scenario_arm_hash(self.overrides, self.fill_haircut)


@dataclass
class ScenarioResult:
    """One (scenario, symbol, window) cell of the sweep."""

    scenario: str
    symbol: str
    start: date
    end: date
    split: str  # 'fit' | 'holdout' | 'all'
    config_hash: str
    # Identity of the ARM (overrides + fill haircut). See Scenario.scenario_hash:
    # config_hash does not separate arms that differ outside its nine keys.
    scenario_hash: str = ""
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
    # FC-078 roller activity for THIS cell, off the replay's own
    # `SimulationResult`. `evaluated` counts ITM-trigger hits the roller looked
    # at; `executed` counts rolls it actually placed, and the two are far apart
    # in practice (a credit-only roller declines most of what it evaluates), so
    # reporting only the second reads as "the roller never ran".
    #
    # Deliberately NOT persisted: `rows_from_sweep` writes an explicit column
    # list, so these stay in-process and add no `scenario_runs` column. Phase E
    # wants roll counts on the console — that is when the column gets added,
    # with the schema change argued on its own.
    #
    # `None`, never 0, on an errored cell: "the roller evaluated nothing" and
    # "this cell was never measured" are different statements.
    rolls_evaluated: Optional[int] = None
    rolls_executed: Optional[int] = None
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

    @property
    def low_activity(self) -> bool:
        """The wheel held a position on too few days to mean anything.

        ``MIN_DAYS_IN_POSITION`` (0.25) is the same floor ``FitnessReport``
        already uses to refuse to call a symbol fit, and it is the difference
        between "this arm earned 4% on capital that was working" and "this arm
        earned 4% on capital that sat idle 95% of the time and got lucky twice".
        Annualising the second is how a sweep manufactures a winner: the fewer
        days deployed, the more a single good trade is multiplied by 365/days.

        Counted and shown, never averaged — same treatment as ``insufficient``,
        for the same reason.

        **``insufficient`` wins.** A window with no completed cycle also has a
        tiny days-in-position fraction, so without the guard below one cell was
        both, and ``_scenario_summary`` counted it twice: ``position_size_20pct``
        reported ``measured 1 | insuf 2 | low-act 5`` over six symbols, which
        sums to eight. The columns are a partition of the cells or they are
        decoration, and a reader who cannot add up the row stops trusting the
        table. They also answer different questions — "the window contained no
        cycle" is not "the wheel was barely deployed" — so the more specific
        verdict is the one that should be shown.
        """
        return (
            self.ok
            and not self.insufficient
            and self.days_in_position_fraction is not None
            and self.days_in_position_fraction < MIN_DAYS_IN_POSITION
        )

    @property
    def measured(self) -> bool:
        """Whether this cell carries a number worth ranking.

        ``measured``, ``insufficient``, ``low_activity`` and "errored" partition
        every cell — exactly one is true. Pinned by
        ``tests/test_scenarios.py::TestTheCellStatesPartition``.
        """
        return self.ok and not self.insufficient and not self.low_activity

    def as_dict(self) -> Dict[str, Any]:
        out = {
            k: v for k, v in self.__dict__.items()
        }
        out["start"] = self.start.isoformat()
        out["end"] = self.end.isoformat()
        out["insufficient"] = self.insufficient
        out["low_activity"] = self.low_activity
        out["measured"] = self.measured
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
    scenario_hashes: Dict[str, str] = field(default_factory=dict)
    scenario_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    scenario_fill_haircuts: Dict[str, Optional[float]] = field(default_factory=dict)
    materialise_seconds: Dict[str, float] = field(default_factory=dict)
    replay_seconds: Dict[str, float] = field(default_factory=dict)
    wall_seconds: float = 0.0
    # Round-trips that reached the RAW vendor client. A bar served from the
    # cache is NOT one of these — it is a `bar_cache_hits`. Conflating the two
    # (which the first cut did) reports a fully-offline sweep as having made six
    # provider calls, which is the opposite of the claim being made.
    provider_fetches_total: int = 0
    bar_cache_hits: int = 0
    provider_calls_during_replays: int = 0
    starting_cash: float = 0.0
    run_sensitivity: bool = False
    # The reach every window in this run was materialised to — the max over the
    # base config and every arm's DTE overrides (see `effective_max_dte`). The
    # report reads it to decide whether the DTE_REACH_BIAS caveat applies: a
    # 7-reach run carries the engine's ordinary biases and nothing more, and
    # appending a fidelity warning it does not earn is how a footer stops being
    # read.
    effective_max_dte: int = 0
    # FC-013 coverage, unioned across every replay in this run: symbols absent
    # from the committed earnings table entirely. Non-empty means the gate was
    # silently pass-through for those symbols — which matters most for exactly
    # the runs this field was added for (FC-096 A4: a newly-onboarded candidate
    # is absent from the table by default). Surfaced in the report header, never
    # inferred from an empty list.
    earnings_symbols_without_data: List[str] = field(default_factory=list)

    @property
    def errors(self) -> List[ScenarioResult]:
        return [r for r in self.rows if r.error is not None]

    @property
    def has_holdout(self) -> bool:
        return any(split == "holdout" for split, _s, _e in self.windows)

    @property
    def in_sample_only(self) -> bool:
        """No holdout was asked for, so nothing here has been validated.

        Not a footnote: it is the default, and the default is the dangerous
        case. A ranking chosen on the same data it was measured on, over a
        single vol regime (Alpaca's option history starts 2024-02), is a
        hypothesis — the report says so at the top rather than at the bottom.
        """
        return not self.has_holdout

    def cell(self, scenario: str, symbol: str, split: str = "all") -> Optional[ScenarioResult]:
        for row in self.rows:
            if row.scenario == scenario and row.symbol == symbol and row.split == split:
                return row
        return None


class _CountingProvider:
    """Counts every call that reaches the wrapped data provider.

    Sits INNERMOST, directly around the vendor client, so what it counts is
    network round-trips — a bar served from ``BarStore`` never reaches it. The
    outer layer's cache hits are counted separately (``CachedBarProvider.hits``),
    because reporting the two as one number describes a fully-offline sweep as
    having made six provider calls, which is the opposite of the claim.

    The "zero I/O during replays" contract is the whole reason the sweep is
    affordable, and it is invisible in the results: a regression that re-fetched
    per replay would still produce correct numbers. So it is asserted on these
    counters rather than inferred from wall-clock or read off a log line — and
    the assertion covers cache hits too, since a replay must read neither.
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


#: The reach assumed for a DTE target a profile does not declare. The wheel
#: profile ships 7 on both legs; the covered-call profile declares only
#: `call_target_dte`, so reading its `put_target_dte` raises.
DEFAULT_TARGET_DTE = 7


def config_target_dte(config, leg: str) -> int:
    """``config.{leg}_target_dte``, or ``DEFAULT_TARGET_DTE`` when absent.

    NOT ``getattr(config, key, default)``. ``Config``'s DTE accessors are
    *properties* that index ``_config["strategy"]`` directly, so a profile
    missing the key raises ``KeyError`` from inside the property — and
    ``getattr``'s default only swallows ``AttributeError``. The covered-call
    profile is exactly that case: it declares ``call_target_dte: 14`` and no
    ``put_target_dte`` at all, so the three-argument ``getattr`` this replaced
    would have raised on every covered-call sweep rather than falling back.
    """
    try:
        value = getattr(config, f"{leg}_target_dte")
    except (AttributeError, KeyError, TypeError):
        return DEFAULT_TARGET_DTE
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_DTE


def arm_max_dte(config) -> int:
    """The reach ONE arm's own config needs: the max over its two DTE targets.

    Both legs, because either one can be the longer: an arm overriding only
    ``call_target_dte`` still needs calls that far out, and the covered-call
    profile's base is already 14 on the call leg with no put leg at all.
    """
    return max(config_target_dte(config, "put"), config_target_dte(config, "call"))


def effective_max_dte(base_config, scenarios: Sequence[Scenario]) -> int:
    """The DTE reach every window in this sweep is MATERIALISED to.

    The MAXIMUM over the base config's ``put_target_dte`` AND ``call_target_dte``
    and every arm's overrides of either. The base config's call leg counts: the
    covered-call profile ships ``call_target_dte: 14``, so a sweep of that
    profile with no DTE arm at all still needs a 14-reach materialisation.

    **This is the materialisation reach, not what any one arm sees.** Each arm
    replays against a view of the shared window masked back to its OWN reach
    (``arm_max_dte`` -> ``simulator.narrow_to_dte``), because not every consumer
    of a chain is capped by the arm's DTE target — the roller's replacement
    search is bounded by ``old_expiry + rolling.max_extension_days`` and by
    nothing else, so an unmasked arm would see roll candidates its own config
    could never have produced, and its numbers would depend on which OTHER arms
    shared the spec. Materialise wide, replay narrow.

    A call-DTE arm has to be counted here or it is not materialised at all and
    reads as "no call ever qualified" — the silent-fiction failure the allowlist
    exists to prevent, arriving through the door PR-2 opens.
    """
    reach = max(
        config_target_dte(base_config, "put"),
        config_target_dte(base_config, "call"),
    )
    for scenario in scenarios:
        for key in DTE_OVERRIDE_KEYS:
            value = scenario.overrides.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                reach = max(reach, value)
    return reach


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
    cfg_hash: str, scenario_hash: str, report, sensitivity: Optional[dict],
    seconds: float, rolls_evaluated: Optional[int] = None,
    rolls_executed: Optional[int] = None,
) -> ScenarioResult:
    split, start, end = window
    verdict = report.verdict()
    return ScenarioResult(
        scenario=scenario, symbol=symbol, start=start, end=end, split=split,
        config_hash=cfg_hash, scenario_hash=scenario_hash,
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
        # From the MID replay, not the bid-sensitivity one: the row's other
        # numbers all describe that replay, and mixing the two would make the
        # roll counts describe a run nothing else on the row came from.
        rolls_evaluated=rolls_evaluated,
        rolls_executed=rolls_executed,
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
        validate_overrides(scenario.overrides)
    _check_unique_names(scenarios)

    symbols = [s.upper() for s in symbols]
    windows = _windows(start, end, holdout_start)

    # The counter sits INNERMOST, around the vendor client, so `fetches` means
    # network round-trips and a bar served off disk is not one. The bar cache
    # then wraps it, and its `hits` are reported separately. Counting at the
    # outer edge (the first cut) described a fully-offline sweep as having made
    # six provider calls.
    fetch_counter = _CountingProvider(
        bar_provider if bar_provider is not None
        else AlpacaDataProvider.from_config(base_config)
    )
    # An injected provider is used as given — the caller owns its caching, and
    # the sweep must not silently wrap a test double in a real disk cache.
    provider = (
        fetch_counter if bar_provider is not None
        else CachedBarProvider(fetch_counter, BarStore())
    )
    if chain_store is None:
        chain_store = ChainStore.from_env()
    builder = ChainBuilder(provider, store=chain_store)
    max_dte = effective_max_dte(base_config, scenarios)
    dividends = load_default_schedule()
    earnings_gaps: set = set()

    scenario_configs: Dict[str, Config] = {
        s.name: apply_overrides(base_config, s.overrides) for s in scenarios
    }
    # Each arm's OWN reach, read off its resolved config rather than off its
    # overrides: an arm that overrides neither leg still inherits the base
    # profile's targets, and the covered-call profile's base call leg is 14.
    scenario_reaches: Dict[str, int] = {
        name: min(arm_max_dte(cfg), max_dte)
        for name, cfg in scenario_configs.items()
    }
    result = SweepResult(
        scenarios=[s.name for s in scenarios],
        symbols=list(symbols),
        windows=windows,
        base_config_hash=config_hash(base_config),
        scenario_config_hashes={
            name: config_hash(cfg) for name, cfg in scenario_configs.items()
        },
        scenario_hashes={s.name: s.scenario_hash() for s in scenarios},
        scenario_overrides={s.name: dict(s.overrides) for s in scenarios},
        scenario_fill_haircuts={s.name: s.fill_haircut for s in scenarios},
        starting_cash=starting_cash,
        run_sensitivity=run_sensitivity,
        effective_max_dte=max_dte,
    )
    logger.info(
        "Scenario sweep starting",
        event_category="backtest", event_type="sweep_started",
        scenarios=len(scenarios), symbols=len(symbols),
        windows=[f"{sp}:{s}..{e}" for sp, s, e in windows],
        base_config_hash=result.base_config_hash,
        in_sample_only=result.in_sample_only,
        effective_max_dte=max_dte,
    )

    for symbol in symbols:
        for window in windows:
            split, w_start, w_end = window
            key = f"{symbol}:{split}"
            io_before_materialise = _io_signature(fetch_counter, provider)
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
                        scenario_hash=result.scenario_hashes[scenario.name],
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
                provider_fetches=fetch_counter.calls - io_before_materialise[0],
                bar_cache_hits=_cache_hits(provider) - io_before_materialise[1],
            )

            io_before_replays = _io_signature(fetch_counter, provider)
            by_method_before = dict(fetch_counter.by_method)
            # One masked view per distinct reach, not per arm: ten arms at the
            # sweep-wide reach share one object (which `narrow_to_dte` returns
            # as the input itself), and a mixed spec pays for the mask once.
            views: Dict[int, Materialised] = {}
            with quiet_strategy_logs(quiet_logs):
                for scenario in scenarios:
                    row = _replay_one(
                        scenario=scenario,
                        config=scenario_configs[scenario.name],
                        cfg_hash=result.scenario_config_hashes[scenario.name],
                        scenario_hash=result.scenario_hashes[scenario.name],
                        provider=provider, builder=builder,
                        symbol=symbol, window=window,
                        materialised=materialised,
                        starting_cash=starting_cash,
                        max_dte=scenario_reaches[scenario.name],
                        dividends=dividends,
                        run_sensitivity=run_sensitivity,
                        earnings_gaps=earnings_gaps,
                        views=views,
                    )
                    result.rows.append(row)
                    rkey = f"{scenario.name}:{symbol}:{split}"
                    result.replay_seconds[rkey] = row.replay_seconds or 0.0
            io_after = _io_signature(fetch_counter, provider)
            leaked = sum(a - b for a, b in zip(io_after, io_before_replays))
            result.provider_calls_during_replays += leaked
            if leaked:
                # Hard failure, not a warning. A replay that reads ANYTHING —
                # the network or even the bar cache — invalidates the premise of
                # the sweep, and does so invisibly, because the numbers still
                # come out right. Cache hits count here for exactly that reason:
                # a replay that re-read bars off disk would be caught by no other
                # signal.
                escaped = {
                    method: count - by_method_before.get(method, 0)
                    for method, count in fetch_counter.by_method.items()
                    if count - by_method_before.get(method, 0) > 0
                }
                hits = io_after[1] - io_before_replays[1]
                if hits:
                    escaped["bar_cache_read"] = hits
                raise RuntimeError(
                    f"{leaked} data-layer read(s) escaped during the replay loop "
                    f"for {symbol} ({split}): {escaped}. A replay must read only "
                    "the Materialised it was handed; something is re-fetching per "
                    "scenario and the sweep's cost model is void."
                )

    result.wall_seconds = round(time.perf_counter() - began, 3)
    result.provider_fetches_total = fetch_counter.calls
    result.bar_cache_hits = _cache_hits(provider)
    result.earnings_symbols_without_data = sorted(earnings_gaps)
    logger.info(
        "Scenario sweep complete",
        event_category="backtest", event_type="sweep_completed",
        rows=len(result.rows), errors=len(result.errors),
        wall_seconds=result.wall_seconds,
        materialise_seconds=round(sum(result.materialise_seconds.values()), 3),
        replay_seconds=round(sum(result.replay_seconds.values()), 3),
        provider_fetches=result.provider_fetches_total,
        bar_cache_hits=result.bar_cache_hits,
        provider_calls_during_replays=result.provider_calls_during_replays,
        in_sample_only=result.in_sample_only,
    )
    return result


def _cache_hits(provider) -> int:
    """Bar-cache hits, or 0 for a provider that has no cache in front of it."""
    return int(getattr(provider, "hits", 0) or 0)


def _io_signature(fetch_counter: "_CountingProvider", provider) -> Tuple[int, int]:
    """``(network fetches, bar-cache reads)`` so far.

    Both halves are guarded during replays. Counting only the network would let
    a replay that re-read bars off disk pass — slower, still correct, and
    completely silent.
    """
    return fetch_counter.calls, _cache_hits(provider)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _with_base_first(scenarios: Sequence[Scenario]) -> List[Scenario]:
    """``base`` first, always. Every other row is read relative to it.

    A scenario NAMED ``base`` that carries overrides or a fill haircut is
    refused outright. ``base`` is the comparator: every delta in the report,
    and the whole sign-agreement column, is measured against it. An arm that
    quietly redefined it would move every other number in the table while
    looking like an ordinary row — the reader would be comparing nine arms
    against a tenth arm and calling it the status quo.
    """
    ordered = list(scenarios)
    existing = next((s for s in ordered if s.name == BASE_SCENARIO_NAME), None)
    if existing is None:
        return [Scenario(BASE_SCENARIO_NAME, {})] + ordered
    if existing.overrides or existing.fill_haircut is not None:
        raise ValueError(
            f"the scenario named {BASE_SCENARIO_NAME!r} must carry no overrides "
            f"and no fill_haircut — it is the comparator every other row is read "
            f"against. Got overrides={existing.overrides!r}, "
            f"fill_haircut={existing.fill_haircut!r}. Rename it, and the implicit "
            f"{BASE_SCENARIO_NAME!r} arm will be added back."
        )
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
    """Build the window's data ONCE, under the BASE config at ``max_dte``.

    ``max_dte`` is NOT the base config's ``put_target_dte``: since FC-096 Phase A
    both DTE targets are allowlisted, so the caller passes
    ``effective_max_dte(base_config, scenarios)`` — the max over the base and
    every arm. No other allowed override touches chain reach or the strike
    window, so with the reach settled this way any scenario's materialisation
    would be byte-identical to this one.

    **Materialising wider than an arm asks for DOES change that arm's inputs, so
    the widening is undone at replay.** What is capped by the arm's own config is
    entry selection (``_check_call_criteria_detailed`` treats ``call_target_dte``
    as a hard ceiling). What is NOT capped by it is the roller, whose replacement
    search is bounded by ``old_expiry + rolling.max_extension_days`` alone — so a
    wider chain hands it candidates the arm could never have produced. Each arm
    therefore replays against ``narrow_to_dte(materialised, arm_reach)`` and its
    simulator is built at that same reach; ``Simulator.replay`` re-checks the
    universe/window/DTE agreement, so a mismatch fails loudly rather than
    replaying against chains that do not match the request.
    """
    return _simulator(
        base_config, provider, builder, symbol, start, end,
        starting_cash=starting_cash, max_dte=max_dte,
        fill_haircut=DEFAULT_FILL_HAIRCUT, dividends=dividends,
    ).materialise()


def _replay_one(
    *, scenario: Scenario, config, cfg_hash: str, scenario_hash: str, provider,
    builder, symbol: str, window: Tuple[str, date, date],
    materialised: Materialised, starting_cash: float, max_dte: int, dividends,
    run_sensitivity: bool, earnings_gaps: Optional[set] = None,
    views: Optional[Dict[int, Materialised]] = None,
) -> ScenarioResult:
    """Replay ONE arm against a view of the shared window masked to its reach.

    ``max_dte`` here is the ARM's reach, not the sweep's. The window was
    materialised at the widest reach any arm asked for; handing that object to a
    shorter arm would show it contracts its own config could never have selected
    — harmless for the entry paths, which cap at ``*_target_dte``, and NOT
    harmless for the roller, whose replacement search is bounded only by
    ``rolling.max_extension_days``. See ``simulator.narrow_to_dte``.

    ``views`` is an optional per-window cache keyed by reach, so N arms at one
    reach mask once rather than N times. An arm at the sweep-wide reach gets the
    shared object back unchanged.
    """
    split, w_start, w_end = window
    haircut = (
        DEFAULT_FILL_HAIRCUT if scenario.fill_haircut is None
        else scenario.fill_haircut
    )
    t0 = time.perf_counter()
    try:
        bars = materialised.stock_bars.get(symbol, [])
        if views is None:
            view = narrow_to_dte(materialised, max_dte)
        else:
            if max_dte not in views:
                views[max_dte] = narrow_to_dte(materialised, max_dte)
            view = views[max_dte]
        result = _simulator(
            config, provider, builder, symbol, w_start, w_end,
            starting_cash=starting_cash, max_dte=max_dte,
            fill_haircut=haircut, dividends=dividends,
        ).replay(view)
        # FC-096 A4. The single-symbol report surfaces this; a sweep did not, so
        # a candidate sweep said nothing at all about a symbol its earnings gate
        # never gated. Unioned across arms because the gap is a property of the
        # TABLE, not of the arm — every arm on that symbol has it.
        if earnings_gaps is not None:
            earnings_gaps.update(result.earnings_symbols_without_data)
        report = _score(symbol, result, bars, starting_cash, dividends)

        sensitivity = None
        if run_sensitivity:
            bid_result = _simulator(
                config, provider, builder, symbol, w_start, w_end,
                starting_cash=starting_cash, max_dte=max_dte,
                fill_haircut=BID_FILL_HAIRCUT, dividends=dividends,
            ).replay(view)
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
            cfg_hash=cfg_hash, scenario_hash=scenario_hash,
            report=report, sensitivity=sensitivity,
            seconds=time.perf_counter() - t0,
            rolls_evaluated=result.rolls_evaluated,
            rolls_executed=result.rolls_executed,
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
            split=split, config_hash=cfg_hash, scenario_hash=scenario_hash,
            error=message,
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
