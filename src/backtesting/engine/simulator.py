"""The day loop — drives the live strategy over history.

Each simulated trading day:

    freeze the clock  ->  wheel_engine.reconcile_positions()  (housekeeping)
                      ->  OptionsScanner.scan_for_put_opportunities()
                          + scan_for_call_opportunities()      (find candidates)
                      ->  ExecutionEngine: filter -> rank -> select -> execute
                      ->  wheel_engine.run_rolling_cycle()   (daily, FC-078)
                      ->  settle expirations against today's close
                      ->  record equity

That is the production pipeline verbatim: ``/scan`` builds candidates with
``OptionsScanner`` and ``/run`` filters, ranks, batch-selects and executes them
with ``ExecutionEngine``. FC-068 repointed the generation half here. Before it,
the replay called ``WheelEngine.run_strategy_cycle()`` — a code path production
abandoned on 2025-10-03, three days before the live account's first fill — so
every backtest measured a strategy with a drawdown pause, gap filtering,
wheel-state phase gating, per-cycle caps and single-candidate selection that
production does not have, and *without* the two-pool batch selection and
committed-share ledger it does.

Three orderings here were learned the hard way, and all are load-bearing.

**Execution is a second phase.** The scan only *finds* opportunities;
production executes them separately (``/run`` → ``ExecutionEngine``). A day
loop that stops after the scan places no trades at all and reports a flawless
zero-trade backtest.

**Settlement runs after the decision.** A contract expiring today is still held
when the strategy looks at its book — that is what the live bot sees at 3:45pm —
and resolves against today's official close. Settling first removes the expiring
position before the scan, so the scanner's "already have a position on this
symbol" check waves through a fresh put on the same underlying, expiring the
same day, which then never settles.

**The opportunity blob is not simulated.** ``/scan`` and ``/run`` sit ~15
minutes apart in production, so live executes against fresher quotes than it
scanned on; the replay scans and executes on one snapshot. That was true before
FC-068 too — unchanged, and now stated in ``docs/BACKTEST_ENGINE.md``. For the
same reason ``OpportunityStore`` is never constructed here: the hand-off is
in-memory, so a replay writes no GCS blobs.

Nothing in here reimplements strategy logic. If a rule is wrong, it is wrong in
production too — which is the entire point of FC-032.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import structlog

from ...api.market_data import MarketDataManager
from ...data import analytics_writer as analytics_module
from ...data.options_scanner import OptionsScanner
from ...strategy.call_seller import CallSeller
from ...strategy.execution_engine import (
    ExecutionEngine,
    clear_failed_symbols,
    get_failed_symbols,
)
from ...strategy.put_seller import PutSeller
from ...strategy.wheel_engine import WheelEngine
from ...strategy.wheel_state_manager import WheelStateManager
from ...utils.config import Config
from ..data.chain_builder import UNIVERSE_DTE_BUFFER, ChainBuilder, ChainSnapshot
from ..data.alpaca_provider import UnadjustedCorporateAction, detect_split
from ..data.dividends import (
    DividendSchedule,
    load_default_schedule,
    should_assign_early,
)
from ..data.provider import OptionsDataProvider, StockBar
from .alpaca_adapter import BacktestAlpacaClient
from .broker import BacktestBroker
from .clock import SimClock
from .historical_earnings import HistoricalEarningsCalendar
from .no_op_analytics import NoOpAnalyticsWriter, NoOpTradeJournal
from .rejections import RejectionTally

logger = structlog.get_logger(__name__)


def _find_chain_quote(snapshot: Optional[ChainSnapshot], symbol: str):
    """The quote for one OCC symbol in a snapshot, or None if it did not trade."""
    if snapshot is None:
        return None
    for quote in snapshot.all_quotes():
        if quote.symbol == symbol:
            return quote
    return None


def restrict_symbols(config: Config, symbols: Sequence[str]) -> Config:
    """A copy of ``config`` whose universe is ``symbols``.

    ``evaluate`` mode runs one symbol at a time, but ``OptionsScanner`` scans
    ``config.stock_symbols``. Deep-copied so the caller's config is untouched.

    **The ``stocks:`` section may be ABSENT** (FC-096 Phase C, C2). It is on the
    wheel profile and deliberately not on ``config/covered_call.yaml``, whose
    universe is holdings-derived — so the old ``_config["stocks"]["symbols"] =``
    raised ``KeyError`` on the one profile Phase C exists to replay, before a
    single day was simulated. The section is CREATED when missing rather than
    guarded around: a replay is always scoped to its symbols, and a covered-call
    config that reached the scanner with no universe would scan nothing and read
    as "this strategy never found a candidate".
    """
    narrowed = copy.deepcopy(config)
    stocks = narrowed._config.get("stocks")
    if not isinstance(stocks, dict):
        stocks = {}
        narrowed._config["stocks"] = stocks
    stocks["symbols"] = list(symbols)
    return narrowed


#: The strategy profiles this engine knows how to replay. ``wheel`` is the
#: legacy path and every stored result predates the field, which is why absence
#: canonicalises to it everywhere (``identity.canonical_spec``).
WHEEL_STRATEGY = "wheel"
COVERED_CALL_STRATEGY = "covered_call"
KNOWN_STRATEGIES = (WHEEL_STRATEGY, COVERED_CALL_STRATEGY)


def replay_strategy(config: Config) -> str:
    """The strategy this config makes the replay run — ``config.strategy_id``.

    One reader, so "which strategy is this" is never answered two ways. An
    unrecognised ``strategy_id`` is returned AS IS rather than coerced to
    ``wheel``: a profile nobody has taught the engine about must show up in the
    stamps as itself, so the artifact says what ran.
    """
    return str(getattr(config, "strategy_id", WHEEL_STRATEGY) or WHEEL_STRATEGY)


def is_wheel(config: Config) -> bool:
    return replay_strategy(config) == WHEEL_STRATEGY


def spread_gate_would_suspend(config: Config) -> bool:
    """Whether a replay under ``config`` suspends the modelled spread gate.

    The PREDICATE, extracted so the footer and the replay cannot disagree about
    whether a suspension happened. Pure: it reads two values and opens nothing,
    which is why the runner can ask it without building a Simulator (whose
    constructor loads the earnings table and the dividend schedule).
    """
    if is_wheel(config):
        return False
    universe = config._config.get("universe")
    return isinstance(universe, dict) and universe.get("max_spread_pct") is not None


#: The covered-call replay's cash float (FC-096 Phase C, C3). It is NOT the
#: capital base — the lot is (see ``metrics.fitness.capital_base``). It exists
#: because a covered-call programme still needs cash to buy a call back on the
#: monitor leg and to pay the BTC side of a roll, and because pinning it small
#: keeps the stored equity return close to the lot return instead of diluting it
#: ~20x against the wheel's $100k default.
CC_CASH_FLOAT = 5_000.0

#: Shares in one synthetic lot. Fixed at 100 by signed decision D2 — one
#: contract of writable cover, and the number that makes windows
#: symbol-comparable. No knob without operator sign-off (plan §Design decisions).
SYNTHETIC_LOT_SHARES = 100

SYNTHETIC_LOT_PREMISE = (
    "Synthetic lot: 100 shares assumed held at the window-start close (FC-096 "
    "D2). No purchase was modelled and no cash moved; every figure measured "
    "against this lot is relative to a position the engine created."
)


@dataclass(frozen=True)
class Materialised:
    """Everything a replay needs that does NOT depend on the strategy config.

    FC-060 Layer 2. The expensive half of a run is data assembly — bars from the
    provider and chains from the parquet cache — and it is *config-independent*:
    the chain model fingerprint takes no ``settings.yaml`` input
    (``ChainBuilder._model_fingerprint``) and the strike window is derived from
    bars alone (``Simulator._strike_anchors``). So the same ``Materialised`` can
    be replayed under many strategy configs, which is what makes a scenario
    sweep affordable (see ``src/backtesting/scenarios``).

    **A replay must never mutate one.** ``chains`` is shared across every
    scenario in a sweep; a replay that edited a ``ChainSnapshot`` would silently
    contaminate every later scenario with the previous one's state. Both
    ``ChainSnapshot`` and ``ChainQuote`` are frozen dataclasses, and
    ``BacktestAlpacaClient`` only ever reads them (it appends to local lists it
    builds itself) — verified, and pinned by
    ``tests/test_backtest_materialise.py``.

    Attributes:
        symbols: the universe this was built for. A replay whose simulator
            carries a different universe is rejected — the chains would not
            cover it.
        start/end: the *requested* decision window (not ``days[0]``/``days[-1]``).
        stock_bars: symbol -> bars, INCLUDING the warm-up buffer before ``start``.
        days: the decision days (sessions inside ``[start, end]``), ascending.
        anchors: symbol -> ``(cost_basis_ceiling, low_anchor)``, the strike
            window the chains were fetched under.
        chains: symbol -> as_of -> snapshot.
        splits: symbol -> ``(date, ratio)`` for a split detected inside the
            WARM-UP buffer, which ``materialise`` tolerates and logs. A split in
            the decision window raises instead, so it can never appear here.
        max_dte: the DTE reach the chains were built with.
        built_at: wall-clock construction time (provenance only).
        model_fingerprints: symbol -> ``ChainBuilder._model_fingerprint``, so a
            reader can tell which pricing model produced these quotes.
    """

    symbols: List[str]
    start: date
    end: date
    stock_bars: Dict[str, List[StockBar]]
    days: List[date]
    anchors: Dict[str, "tuple[Optional[float], Optional[float]]"]
    chains: Dict[str, Dict[date, ChainSnapshot]]
    splits: Dict[str, Optional[tuple]] = field(default_factory=dict)
    max_dte: int = 7
    built_at: Optional[datetime] = None
    model_fingerprints: Dict[str, str] = field(default_factory=dict)


def narrow_to_dte(materialised: Materialised, max_dte: int) -> Materialised:
    """A read-only VIEW of ``materialised`` masked to ``max_dte``.

    FC-096 Phase A PR-2. A scenario sweep materialises once per (symbol,
    window), at the widest reach any arm asks for — that is what makes it
    affordable. Once DTE became sweepable, "widest" stopped being the same as
    "what this arm asked for", and the difference is not cosmetic:

    **Not every consumer of a chain is capped by the arm's own DTE target.** The
    entry paths are — ``_check_call_criteria_detailed`` treats
    ``call_target_dte`` as a hard ceiling — but the ROLLER is not. Its
    replacement search (``market_data.py``'s ``'roll'`` criteria profile) is
    bounded by ``old_expiry + rolling.max_extension_days``, never by
    ``*_target_dte``, and it picks the maximum net credit among whatever it is
    shown. So an arm replayed against a chain built for a LONGER arm sees roll
    candidates that its own configuration would never have produced, and its
    numbers move — measured: a ``base`` row went from 7 puts sold / $983 option
    P&L to 6 / $956 purely by adding a DTE-21 arm to the same spec, with
    ``scenario_hash`` and ``config_hash`` byte-identical either way. A sweep
    whose comparator changes depending on which OTHER arms are present is not a
    comparison.

    So each arm replays against a view masked to its own reach, and
    ``Simulator.replay``'s equality guard is satisfied by the view's own
    ``max_dte`` rather than by the sweep-wide one.

    The mask is ``dte <= max_dte + UNIVERSE_DTE_BUFFER`` — the same rule
    ``ChainBuilder.build`` fetches under and ``ChainStore._rows_to_quotes``
    re-applies on read, so a view is indistinguishable from a chain materialised
    at that reach in the first place.

    **Nothing is mutated, and nothing is copied that need not be.** ``Materialised``
    and ``ChainSnapshot`` are frozen and shared across every arm; this builds new
    snapshots holding the SAME ``ChainQuote`` objects (also frozen), and returns
    the input object unchanged when the mask cannot remove anything. That
    identity return is what keeps a homogeneous sweep byte-identical to a
    pre-PR-2 one: no new object, no re-sorted list, nothing to drift.

    Args:
        materialised: the shared window. Never mutated.
        max_dte: the arm's own reach.

    Returns:
        ``materialised`` itself when ``max_dte >= materialised.max_dte``
        (a mask that removes nothing), otherwise a narrowed copy.
    """
    if max_dte >= materialised.max_dte:
        return materialised
    cutoff = max_dte + UNIVERSE_DTE_BUFFER
    chains: Dict[str, Dict[date, ChainSnapshot]] = {}
    for symbol, by_day in materialised.chains.items():
        narrowed: Dict[date, ChainSnapshot] = {}
        for as_of, snapshot in by_day.items():
            narrowed[as_of] = ChainSnapshot(
                underlying=snapshot.underlying,
                as_of=snapshot.as_of,
                underlying_price=snapshot.underlying_price,
                # Order is preserved, not re-derived: the builder already sorted
                # these by strike and a filter keeps ties in place, so a view
                # cannot reorder a chain the arm would otherwise have seen.
                puts=[q for q in snapshot.puts if q.dte <= cutoff],
                calls=[q for q in snapshot.calls if q.dte <= cutoff],
            )
        chains[symbol] = narrowed
    return dataclasses_replace(materialised, chains=chains, max_dte=max_dte)


#: Coverage buckets, in the priority order ``_coverage_reason`` applies them.
#: Exported because the report, the row and the tests must all name the same
#: five strings, and a sixth spelling of "hold_uncovered" is how a split metric
#: quietly stops adding up.
COVERAGE_COVERED = "covered"
COVERAGE_HOLD_UNCOVERED = "hold_uncovered"
COVERAGE_EARNINGS_SPAN = "earnings_span"
COVERAGE_GATE_REJECTED = "gate_rejected"
COVERAGE_POST_CALL_AWAY = "post_call_away"
COVERAGE_REASONS = (
    COVERAGE_COVERED, COVERAGE_HOLD_UNCOVERED, COVERAGE_EARNINGS_SPAN,
    COVERAGE_GATE_REJECTED, COVERAGE_POST_CALL_AWAY,
)

#: Buckets excluded from the CC ``low_activity`` test (plan C3). Standing down
#: below basis is the strategy WORKING — the cost-basis floor refusing to sell
#: the shares at a loss — and an earnings-span day is a gate doing its job. A
#: coverage metric that punished either would demote exactly the symbols the
#: floor protected.
COVERAGE_NOT_A_STAND_DOWN = frozenset(
    {COVERAGE_HOLD_UNCOVERED, COVERAGE_EARNINGS_SPAN})


@dataclass(frozen=True)
class SyntheticLotPolicy:
    """How a covered-call replay seeds (and re-seeds) its stock leg.

    FC-096 Phase C §C2, with the operator's 2026-09-08 posture signed in as the
    default: **re-seed at the next close**. When the lot is called away the sim
    buys a fresh one at the next session's close and keeps writing, so every
    symbol is measured over the FULL window and the names called away earliest
    are not the ones with the shortest measurement. The consequence, which the
    report labels rather than hides: the stock leg is a CHAIN of lots, each with
    its own basis and its own ``synthetic_lot_open`` event.

    ``reseed=False`` is the rejected alternative, kept implementable because the
    metrics are defined to be well-formed under both and a future study may want
    the truncated posture as a comparison.

    Attributes:
        shares: 100, by signed decision. Not a sweepable knob.
        reseed: re-seed after a call-away (the signed posture) or stop.
        premise: the sentence stamped on every seeding event.
    """

    shares: int = SYNTHETIC_LOT_SHARES
    reseed: bool = True
    premise: str = SYNTHETIC_LOT_PREMISE


@dataclass
class DailyState:
    """One row of the equity curve."""

    day: date
    equity: float
    cash: float
    reserved_collateral: float
    open_options: int
    shares_held: Dict[str, int] = field(default_factory=dict)


@dataclass
class SimulationResult:
    symbols: List[str]
    start: date
    end: date
    starting_cash: float
    daily: List[DailyState]
    broker: BacktestBroker
    rejections: Dict[str, int] = field(default_factory=dict)
    candidate_days: int = 0
    dividends_credited: float = 0.0
    early_assignments: int = 0
    # ITM short calls sitting on an ex-date eve with no mark to price extrinsic
    # from, so the early-assignment test could not be applied. The residual of
    # C2, reported rather than assumed away.
    unpriced_ex_div_calls: int = 0
    # Roller activity. FC-078 revived the roller and made the replay's cadence
    # daily to match production, which flipped the FC-068 tripwire by design.
    # `roll_records` carries the executed rolls so the golden replay can assert
    # what actually matters now — every executed roll netted a credit and
    # increased the strike — instead of "the roller never fires".
    #
    # Each record is `CallRoller.execute_roll`'s SUCCESS dict — `success`,
    # `underlying`, `old_strike`, `new_strike`, `contracts`, `net_credit`,
    # `btc_order_id`, `stc_order_id` — with one field added by the replay:
    # `day`, the ISO decision date (FC-096 Phase B). Production does not carry a
    # date on the record because the log line's timestamp is the date; a stored
    # artifact read months later has no such context.
    rolls_evaluated: int = 0
    rolls_executed: int = 0
    roll_records: List[Dict[str, Any]] = field(default_factory=list)
    # FC-013 earnings-table coverage, both reported rather than assumed away.
    # `without_data`: the symbol is absent from the table entirely.
    # `past_horizon`: the symbol IS in the table but every date it carries is
    # already behind the simulated day — which otherwise answers exactly like
    # a genuinely-clear symbol, so the gate silently stops gating at the
    # table's edge. Non-empty means "refresh the table before trusting this
    # run's earnings behaviour", not "the run failed".
    earnings_symbols_without_data: List[str] = field(default_factory=list)
    earnings_symbols_past_horizon: List[str] = field(default_factory=list)

    # ---------------------------------------------------------------- #
    # FC-096 Phase C — the covered-call measurement. Every field below is
    # ZERO/EMPTY on a wheel replay (nothing seeds a lot, nothing runs the
    # monitor leg), which is what keeps the golden wheel numbers byte-identical
    # while the CC report has denominators it can defend.
    # ---------------------------------------------------------------- #
    #: The strategy the replay ran, off ``config.strategy_id``. Stamped rather
    #: than inferred downstream: a reader of a stored artifact must not have to
    #: guess which engine produced it from the shape of its ledger.
    strategy: str = WHEEL_STRATEGY
    #: The profile's call tenor, carried so a stored result can state the CC
    #: `insufficient` cutoff (a window shorter than one tenor) without holding
    #: the config it was replayed under.
    call_target_dte: int = 0
    #: How many synthetic lots were seeded. Under the signed re-seed posture
    #: this is 1 + (call-aways that had a later session to re-seed into), so it
    #: is a count of LOTS and never of symbols.
    synthetic_lots_opened: int = 0
    #: Σ over decision days of the seeded lot value held that day, divided by
    #: the decision-day count — the TIME-WEIGHTED average lot value, and the
    #: denominator of ``premium_yield_on_lot``. With a single never-called lot
    #: it equals that lot's value exactly; across a chain of lots it weights
    #: each by the days it was actually held, so a symbol called away on day 3
    #: and re-seeded on day 4 is not measured against two lots' worth of
    #: capital it never simultaneously had.
    time_weighted_lot_value: float = 0.0
    #: Decision days classified by WHY the lot was or was not covered, in the
    #: priority order ``simulator._coverage_reason`` documents. A single
    #: covered/uncovered ratio is forbidden in the CC report: standing down
    #: below basis is the strategy working, and one ratio cannot say so.
    coverage_by_reason: Dict[str, int] = field(default_factory=dict)
    #: Calls bought back by the CC-only monitor leg at a DTE-band profit target.
    #: 52% of real covered calls close early, so a replay that only ever let
    #: them expire measured a strategy nobody runs.
    calls_closed_early: int = 0
    #: Executed rolls SPLIT by what the stock was doing (FC-100 §Phase C
    #: hand-off, row 1). An ITM roll (stock/strike >= 1.0) is a defence against
    #: assignment; an OTM roll-out (0.98-1.0) is the roller re-writing the
    #: engine's own call before it was ever threatened, which must never be
    #: counted as defence. Moot on the CC profile at ``itm_trigger_ratio: 1.00``
    #: and load-bearing on the wheel's 0.98 (FC-112).
    itm_rolls: int = 0
    otm_roll_outs: int = 0

    @property
    def final_equity(self) -> float:
        return self.daily[-1].equity if self.daily else self.starting_cash

    @property
    def total_return(self) -> float:
        if not self.starting_cash:
            return 0.0
        return (self.final_equity - self.starting_cash) / self.starting_cash


class Simulator:
    """Replays the live wheel strategy over a historical window."""

    def __init__(
        self,
        config: Config,
        provider: OptionsDataProvider,
        builder: ChainBuilder,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        starting_cash: float = 100_000.0,
        max_dte: int = 7,
        fill_haircut: float = 0.25,
        fees_per_contract: float = 0.04,
        warmup_calendar_days: int = 60,
        earnings_calendar: Optional[object] = None,
        dividend_schedule: Optional[DividendSchedule] = None,
        synthetic_lots: Optional["SyntheticLotPolicy"] = None,
    ) -> None:
        self.config = restrict_symbols(config, symbols)
        # FC-096 Phase C. The strategy is read ONCE, off the resolved profile,
        # and every CC-only behaviour below keys on it. Reading `strategy_id`
        # at each use site is how one of them ends up disagreeing with the
        # others about which strategy this replay is.
        self.strategy = replay_strategy(self.config)
        # --- The spread-gate suspension (C3), and why it is loud ------------ #
        # `_check_call_criteria_detailed` reads `universe.max_spread_pct` off
        # MODELLED bid/ask. The model's half-spread is >= 5% of mark for an OTM
        # contract by construction, so the CC profile's 0.10 rejects every
        # premium-floor-clearing call: a 10-of-10 probe found no survivors, and
        # the arm would have read as "this strategy never found a candidate".
        # The gate is SUSPENDED for the replay (never for the live service) and
        # the suspension is stamped in the CC footer as MODEL_SPREAD_BIAS, the
        # same posture FC-097 took for the open-interest floor. A test pins the
        # suspension to the spread MODEL, so the day real spreads arrive it
        # fails loudly and the gate is restored deliberately rather than staying
        # off because nobody remembered it was.
        self.spread_gate_suspended = False
        if self.strategy != WHEEL_STRATEGY:
            universe = self.config._config.get("universe")
            if not isinstance(universe, dict):
                universe = {}
                self.config._config["universe"] = universe
            if universe.get("max_spread_pct") is not None:
                universe["max_spread_pct"] = None
                self.spread_gate_suspended = True
        # The seeding policy, or None for a replay that seeds nothing. Default
        # None on EVERY path: the wheel must not acquire a lot it never bought
        # because a default changed under it.
        self.synthetic_lots = synthetic_lots
        self.provider = provider
        self.builder = builder
        self.symbols = list(symbols)
        self.start = start
        self.end = end
        self.starting_cash = starting_cash
        self.max_dte = max_dte
        self.fill_haircut = fill_haircut
        self.fees_per_contract = fees_per_contract
        # Warm-up history, kept after FC-068 for a different reason than it was
        # introduced with. The original rationale was the gap detector's
        # positional ~30-bar lookback; the gap stages went with the engine path
        # and FC-069 deleted the module. What still needs history on day one is
        # `market_data.get_stock_metrics` /
        # `filter_suitable_stocks` — the volatility and average-volume metrics
        # stage 1 filters on. Load bars *before* `start` so the first decision
        # day has the lookback live would have had. 60 calendar days
        # comfortably covers a 30-session requirement.
        self.warmup_calendar_days = warmup_calendar_days
        # Live earnings gating asks Finnhub for the *next* earnings date, which
        # cannot be answered about a past decision. Default to the committed
        # point-in-time table; passing None skips the gate entirely, which makes
        # rolls more permissive than live (an optimistic bias), so that is
        # strictly opt-in and never the default.
        if earnings_calendar is None:
            earnings_calendar = HistoricalEarningsCalendar.from_table()
        self.earnings_calendar = earnings_calendar
        # Dividends are credited on ex-dates while shares are held, and drive
        # ex-div early assignment. Defaults to the committed table; an explicit
        # DividendSchedule.empty() restores the pre-FC-042 no-dividend model,
        # which is what the fitness benchmark must also be told (see
        # evaluate._score) so both legs stay on the same footing.
        if dividend_schedule is None:
            dividend_schedule = load_default_schedule()
        self.dividends = dividend_schedule

    # ------------------------------------------------------------------ #
    # Data loading
    # ------------------------------------------------------------------ #
    def _load_stock_bars(self) -> Dict[str, List[StockBar]]:
        """Bars from the warm-up start, so day one has a full lookback."""
        fetch_from = self.start - timedelta(days=self.warmup_calendar_days)
        return {s: self.provider.get_stock_bars(s, fetch_from, self.end) for s in self.symbols}

    def _trading_days(self, stock_bars: Dict[str, List[StockBar]]) -> List[date]:
        """Union of session dates inside the *decision* window, ascending.

        Warm-up bars are loaded and visible to the strategy (the adapter clips
        them to the simulated date), but they are never decision days.
        """
        days = {
            b.bar_date
            for bars in stock_bars.values()
            for b in bars
            if self.start <= b.bar_date <= self.end
        }
        return sorted(days)

    def _build_chains(
        self,
        stock_bars: Dict[str, List[StockBar]],
        days: Sequence[date],
        anchors: Optional[Dict[str, "tuple[Optional[float], Optional[float]]"]] = None,
    ) -> Dict[str, Dict[date, ChainSnapshot]]:
        """Build every (symbol, day) chain for the window.

        ``anchors`` is an out-parameter, filled in with the strike window each
        symbol's chains were fetched under so ``materialise`` can record it.
        Passing None keeps the pre-FC-060 signature working unchanged.
        """
        chains: Dict[str, Dict[date, ChainSnapshot]] = {}
        for symbol in self.symbols:
            closes = {b.bar_date: b.close for b in stock_bars.get(symbol, [])}
            ceiling, floor = self._strike_anchors(stock_bars.get(symbol, []))
            if anchors is not None:
                anchors[symbol] = (ceiling, floor)
            per_day: Dict[date, ChainSnapshot] = {}
            for day in days:
                if day not in closes:
                    continue  # symbol did not trade that session
                snap = self.builder.build(
                    symbol,
                    day,
                    self.max_dte,
                    underlying_price=closes[day],
                    cost_basis=ceiling,
                    low_anchor=floor,
                )
                if snap is not None:
                    per_day[day] = snap
            chains[symbol] = per_day
        return chains

    def _strike_anchors(
        self, bars: Sequence[StockBar]
    ) -> "tuple[Optional[float], Optional[float]]":
        """``(cost_basis_ceiling, low_anchor)`` for this symbol's strike window.

        Chains are built for the whole window *before* the day loop starts, so
        at build time there is no position to read a real cost basis from. What
        we can do is bound the prices any position can be struck against:

        * **Ceiling** — shares only ever arrive by put assignment, and the wheel
          only sells puts struck at or below spot, so no lot this run acquires
          can cost more than the highest close (assignment at strike K <= the
          close on the day it was sold, less premium). This keeps the call
          ladder above cost basis fetched even for a position that goes deeply
          underwater later.
        * **Floor** — the mirror case: a short put stays on the book while the
          underlying rallies, and its strike must remain in the chain or the
          position marks at zero and cannot be closed. The lowest close bounds
          the lowest strike the run can ever be short.

        Both are computed from **decision-window bars only**. Warm-up bars
        exist only to give day one the metric lookback live would have had, and
        a split inside the warm-up buffer is explicitly tolerated (see
        ``run``) — for NVDA that
        means a pre-split close of 1224.40 sitting in the same series as a ~180
        spot. Including it would set the ceiling ~7x too high and silently turn
        the strike filter into a no-op for every run starting within the warm-up
        buffer of the June 2024 split.

        Using the window's later closes to widen a *fetch* is not lookahead: it
        can only grow the set of contracts offered to the strategy, never change
        any contract's point-in-time price, and the alternative — a spot-centred
        window — is the one that would alter decisions by hiding contracts the
        strategy is holding. The cost is extra strikes fetched on trending
        symbols, which grows with window length; see the plan's follow-up note
        on building chains lazily per-day instead.
        """
        closes = [
            b.close for b in bars if b.close > 0 and self.start <= b.bar_date <= self.end
        ]
        if not closes:
            return None, None
        return max(closes), min(closes)

    # ------------------------------------------------------------------ #
    # Run = materialise + replay
    # ------------------------------------------------------------------ #
    def run(self) -> SimulationResult:
        """One replay of this simulator's config over its window.

        FC-060 Layer 2 split this into ``materialise()`` (provider + cache work,
        config-independent) and ``replay()`` (the day loop). ``run()`` is exactly
        their composition and its output is byte-identical to the pre-split
        version — proven by ``tests/test_backtest_materialise.py`` and by the
        report hashes in the FC-060 Layer 2 PR.
        """
        return self.replay(self.materialise())

    def materialise(self) -> Materialised:
        """Assemble every config-independent input the day loop needs.

        This is the whole of ``run()``'s former prologue: load bars (with the
        warm-up buffer), derive the decision days, refuse an unmodelled split in
        the decision window, and build the chains.

        None of it depends on ``self.config``: the chain model fingerprint takes
        no ``settings.yaml`` input and the strike window comes from bars alone.
        That is what licenses replaying one ``Materialised`` under many strategy
        configs — see ``src/backtesting/scenarios``. It is emphatically NOT a
        licence to vary ``put_target_dte`` (the chains are built to ``max_dte``,
        fixed here) or anything else that changes what the chain must contain;
        the scenario allowlist enforces that separately.
        """
        stock_bars = self._load_stock_bars()
        days = self._trading_days(stock_bars)
        if not days:
            raise ValueError(
                f"No trading days for {self.symbols} in {self.start}..{self.end}; "
                "refusing to report a zero-trade run as a successful backtest."
            )
        # Refuse a window containing an unmodelled corporate action. Raw bars
        # are correct for point-in-time chain work but cannot span a split: the
        # benchmark and the equity curve would both read a -90% crash that
        # never happened.
        splits: Dict[str, Optional[tuple]] = {}
        for symbol, bars in stock_bars.items():
            split = detect_split(bars)
            splits[symbol] = split
            if split is not None:
                split_date, ratio = split
                # A split inside the WARM-UP buffer is survivable: those bars
                # only feed the day-one metric lookback, and no equity,
                # benchmark or settlement number is computed from them.
                # Refusing the run would reject ~2 months of otherwise-
                # legitimate history and tell the user to avoid a date their
                # window already avoids.
                if split_date < self.start:
                    logger.warning(
                        "Split inside the warm-up window, not the decision "
                        "window: metrics over the first sessions read a "
                        "corporate action as a price move and will be "
                        "distorted. Decision-day results are unaffected.",
                        event_category="backtest",
                        event_type="split_in_warmup",
                        symbol=symbol, split_date=split_date.isoformat(),
                        ratio=round(ratio, 4),
                    )
                    continue
                raise UnadjustedCorporateAction(
                    f"{symbol} moved {ratio:.3f}x on {split_date} — a split or "
                    f"other corporate action the engine does not model. Prices "
                    f"before and after are in different units, so the "
                    f"buy-and-hold benchmark, equity curve and gap filter would "
                    f"all be wrong. Choose a window that does not span "
                    f"{split_date}."
                )

        anchors: Dict[str, "tuple[Optional[float], Optional[float]]"] = {}
        chains = self._build_chains(stock_bars, days, anchors)

        return Materialised(
            symbols=list(self.symbols),
            start=self.start,
            end=self.end,
            stock_bars=stock_bars,
            days=days,
            anchors=anchors,
            chains=chains,
            splits=splits,
            max_dte=self.max_dte,
            built_at=datetime.now(),
            model_fingerprints={
                s: self.builder._model_fingerprint(s) for s in self.symbols
            },
        )

    # ------------------------------------------------------------------ #
    # Replay
    # ------------------------------------------------------------------ #
    def replay(
        self,
        materialised: Materialised,
        *,
        fill_haircut: Optional[float] = None,
    ) -> SimulationResult:
        """Run the day loop against an already-materialised window.

        Args:
            materialised: the output of ``materialise()`` — bars, days, anchors
                and chains. **Never mutated.** The same object is replayed by
                every scenario in a sweep, so a mutation here would leak one
                scenario's state into the next.
            fill_haircut: override the simulator's fill assumption for this
                replay only (0 = mid, 1 = bid). ``None`` uses
                ``self.fill_haircut``. The simulator's own attribute is left
                alone, so replaying at the bid does not change what a later
                replay on the same object does.

        Raises:
            ValueError: if ``materialised`` was built for a different universe,
                window or DTE reach. Silently replaying against chains that do
                not cover this simulator's request would produce a confident,
                wrong answer — exactly what ``ChainStore._covers`` fails closed
                on one layer down.
        """
        if list(materialised.symbols) != list(self.symbols):
            raise ValueError(
                f"Materialised universe {materialised.symbols} does not match "
                f"this simulator's {self.symbols}; its chains would not cover "
                "the replay."
            )
        if (materialised.start, materialised.end) != (self.start, self.end):
            raise ValueError(
                f"Materialised window {materialised.start}..{materialised.end} "
                f"does not match this simulator's {self.start}..{self.end}."
            )
        if materialised.max_dte != self.max_dte:
            raise ValueError(
                f"Materialised chains reach {materialised.max_dte} DTE; this "
                f"simulator asks for {self.max_dte}. A wider reach needs a "
                "re-materialisation, not a replay."
            )

        stock_bars = materialised.stock_bars
        days = materialised.days
        chains = materialised.chains
        effective_haircut = (
            self.fill_haircut if fill_haircut is None else fill_haircut
        )

        broker = BacktestBroker(
            starting_cash=self.starting_cash,
            fees_per_contract=self.fees_per_contract,
            fill_haircut=effective_haircut,
        )
        client = BacktestAlpacaClient(broker, chains=chains, stock_bars=stock_bars)

        # Post-FC-068 the engine is housekeeping only: reconcile_positions()
        # before each cycle (as /run does) and the Friday roll.
        engine = WheelEngine(
            self.config,
            alpaca_client=client,
            # Its own bookkeeping instance, so a replay's reconciliation can
            # never share a scratch pad with anything else in the process.
            # (Pre-FC-069 this passed storage_bucket=None to keep the replay
            # off GCS; stage 2 deleted the persistence outright — there is no
            # longer a bucket to opt out of.)
            wheel_state=WheelStateManager(),
            allow_bigquery_cost_basis=False,
            earnings_calendar=self.earnings_calendar,
        )
        # ONE MarketDataManager, shared by the scanner and both sellers, on the
        # same injected adapter client — the single seam that redirects the
        # whole graph. (WheelEngine keeps its own for the roller.)
        market_data = MarketDataManager(client, self.config)
        # allow_bigquery=False: the cost-basis cross-check and the
        # uncovered_days lookup both query production data against
        # CURRENT_TIMESTAMP(). See OptionsScanner.__init__ and the plan's
        # replay-isolation table.
        # FC-013 discharges the seam obligation FC-068 created: post-FC-068 the
        # replay drives the scanner pipeline, so a scanner-layer gate is only
        # in the replay if it is handed the point-in-time calendar here. The
        # SAME instance already serves the roller through WheelEngine above —
        # one point-in-time truth per replay, and one place its horizon/gap
        # reporting accumulates. Note the scanner still consults
        # `config.earnings_enabled` itself (DD-8): injection supplies the data
        # source, never the policy, so a replay honours a live rolloff.
        scanner = OptionsScanner(client, market_data, self.config,
                                 allow_bigquery=False,
                                 earnings_calendar=self.earnings_calendar)
        # The scan only *finds* opportunities — production executes them in a
        # second phase (/run -> ExecutionEngine). A day loop that stops after
        # the scan places no trades at all.
        exec_engine = ExecutionEngine(
            client, self.config, logger, trade_journal=NoOpTradeJournal()
        )
        # Constructed exactly as /run builds them (cloud_run_server.py).
        # FC-069 item 8 (stage 1) deleted CallSeller's wheel_state parameter
        # outright — it was orphaned on every construction site, this one
        # included — so there is no longer a state layer for the replay to
        # diverge from.
        put_seller = PutSeller(client, market_data, self.config)
        call_seller = CallSeller(client, market_data, self.config)

        closes_by_day: Dict[date, Dict[str, float]] = {day: {} for day in days}
        for symbol, bars in stock_bars.items():
            for bar in bars:
                if bar.bar_date in closes_by_day:
                    closes_by_day[bar.bar_date][symbol] = bar.close

        daily: List[DailyState] = []
        sim_clock = SimClock(days)
        self._early_assignments = 0
        self._unpriced_ex_div_calls = 0
        self._rolls_evaluated = 0
        self._rolls_executed = 0
        self._roll_records: List[Dict[str, Any]] = []
        # FC-096 Phase C accumulators. All stay at their zero value on a wheel
        # replay, so the golden numbers are untouched by their existence.
        self._synthetic_lots_opened = 0
        self._lot_value_days = 0.0
        self._calls_closed_early = 0
        self._itm_rolls = 0
        self._otm_roll_outs = 0
        self._coverage: Dict[str, int] = {}
        self._seeded_symbols: Dict[str, bool] = {}

        # Swap the analytics singleton for a recorder: strategy code fetches it
        # from module scope, so there is no injection point. Restored on exit.
        no_op = NoOpAnalyticsWriter()
        previous_writer = analytics_module.set_analytics_writer(no_op)
        # ExecutionEngine._failed_symbols is a MODULE-GLOBAL set of
        # non-retryable option symbols. `/backtest/screen` lives on the live
        # trading server (disabled by default, opt-in via
        # ENABLE_SCREEN_ENDPOINT), so an in-server replay clearing it would
        # wipe the set `/run` depends on. Snapshot here, restore in the same
        # `finally` that restores the analytics singleton — the established
        # swap pattern. (Standing precondition either way: the endpoint stays
        # disabled on the trading service; the Cloud Run Job is the sanctioned
        # screen runner.) It also leaks across the 14 sequential per-symbol
        # runs of a screen today; the restore ends that too.
        preserved_failed_symbols = set(get_failed_symbols())
        tally = RejectionTally()
        tally.__enter__()
        try:
            for i, day in enumerate(sim_clock.steps()):
                # Dividends are credited at the OPEN of the ex-date, against
                # shares held at the previous close — which is exactly the real
                # ownership test (you must own before the ex-date to be on the
                # record). Crediting after settlement instead would pay a
                # dividend on shares a put assignment delivered on the ex-date
                # itself, which the real holder does not receive.
                self._credit_dividends(broker, day, days[i - 1] if i else None)

                # FC-096 Phase C §C2. Seed (and, under the signed posture,
                # RE-SEED) the synthetic lot BEFORE the scan, at TODAY's close,
                # so the first decision day already has cover to write against.
                #
                # Placing it here is what makes re-seeding land "at the next
                # session's close" without any call-away bookkeeping: a lot
                # called away is removed at `settle_expirations` below, i.e.
                # AFTER this point on that day, so the very next iteration finds
                # the symbol flat and seeds a fresh lot at that session's close.
                # The event chain therefore reads exactly as the decision
                # describes it, and nothing has to remember that a call-away
                # happened.
                self._seed_synthetic_lots(broker, day, closes_by_day[day])

                try:
                    # Production's `_failed_symbols` clears roughly daily (Cloud
                    # Run cold start). Clearing once per RUN instead would let a
                    # day-1 non-retryable failure suppress a symbol for a
                    # months-long window — a divergence from production, not
                    # fidelity to it. Per-day is as close to the production
                    # cadence as a deterministic replay gets.
                    clear_failed_symbols()

                    # Pre-trade housekeeping, exactly as production does before
                    # every cycle (cloud_run_server /run): diff the broker's
                    # positions against the in-request bookkeeping and emit the
                    # assignment/expiration telemetry. It is here because
                    # production runs it here, not because the scan depends on
                    # it — FC-068 removed the last trading-path reader of wheel
                    # state, and FC-069 item 8 deleted the phase gates that
                    # used to make this call load-bearing.
                    engine.reconcile_positions()

                    # /monitor, COVERED-CALL ONLY (FC-096 Phase C §C3).
                    #
                    # Production runs /monitor at 14:55, before /scan and /run,
                    # and 52% of real covered calls are closed there at a
                    # DTE-banded profit target rather than held to expiry. A
                    # replay without it measures a strategy nobody runs.
                    #
                    # Gated to the CC profile deliberately and NOT extended to
                    # the wheel: adding the leg to the wheel would change every
                    # stored wheel result in the project at once, which is a
                    # decision that deserves its own FC and its own re-baseline
                    # (the wheel's existing 52%-early-close footer line stays,
                    # naming the divergence it still carries).
                    self._run_monitor_leg(broker, client, call_seller, day)

                    # /scan, verbatim: default max_results on both legs, because
                    # cloud_run_server passes no args. Diverging would measure a
                    # different strategy. The call scan mints its own run_id.
                    opportunities = (
                        scanner.scan_for_put_opportunities()
                        + scanner.scan_for_call_opportunities()
                    )
                    self._execute_opportunities(
                        exec_engine, put_seller, call_seller, opportunities, client
                    )
                    # Production runs the roll cycle every trading day at 15:30
                    # ET, after the normal cycle. CallRoller executes its own
                    # BTC/STO legs, so unlike the scan it needs no separate
                    # execution phase.
                    #
                    # FC-078 §9: the replay mirrors production, so this is daily,
                    # not Friday-only. A Friday-only replay of a DAILY production
                    # roller would misstate roll frequency and credit capture in
                    # every future measurement — the simulator's whole job is
                    # replaying the production week as it is actually run.
                    #
                    # This is the FC-068 tripwire firing as designed: the golden
                    # replay's `rolls_executed == 0` assertion flips here rather
                    # than every backtest number changing silently.
                    rolls = engine.run_rolling_cycle() or {}
                    self._rolls_evaluated += int(rolls.get('rolls_evaluated', 0) or 0)
                    self._rolls_executed += int(rolls.get('rolls_executed', 0) or 0)
                    # The decision DAY is stamped here because here is the
                    # only place that knows it: `execute_roll`'s record carries
                    # `underlying`, the two strikes, contracts, the net credit
                    # and the two order ids — and no date at all, because in
                    # production the timestamp is the log line's. A replay's
                    # records are read months later off a stored artifact, where
                    # "which day did this roll happen" is the first question a
                    # chart marker asks. `**record` second so a future roller
                    # field named `day` wins rather than being silently
                    # overwritten by ours.
                    #
                    # FC-100 §Phase C hand-off row 1: every executed roll is
                    # SPLIT by what the stock was doing when it was taken. An
                    # ITM roll (stock/old_strike >= 1.0) is a defence against
                    # assignment; an OTM roll-out (0.98-1.0) is the roller
                    # re-writing a call that was never threatened, bypassing
                    # every entry gate. Counting the second as defence is the
                    # measurement error FC-112 exists to settle on the wheel,
                    # and it must not be baked in here.
                    #
                    # The ratio is recomputed from the day's close rather than
                    # read off the roller's record (which carries no stock
                    # price): the adapter's stock quote is bid == ask == close,
                    # so the roller's own mid IS this close — the same number it
                    # gated on, not an approximation of it.
                    for record in (rolls.get('roll_details') or []):
                        if not record.get('success'):
                            continue
                        stamped = self._stamp_roll_record(
                            record, day=day,
                            close=closes_by_day[day].get(record.get('underlying')))
                        if stamped.get('roll_kind') == 'itm_defence':
                            self._itm_rolls += 1
                        elif stamped.get('roll_kind') == 'otm_roll_out':
                            self._otm_roll_outs += 1
                        self._roll_records.append(stamped)
                except Exception:
                    logger.exception(
                        "Strategy cycle raised during replay",
                        event_category="backtest",
                        event_type="replay_cycle_error",
                        day=day.isoformat(),
                    )
                    raise

                # FC-096 Phase C §C3, the coverage split. Tallied HERE — after
                # the day's decisions, before settlement — because that is the
                # state the strategy left the book in: a call written today is
                # cover today, and a contract expiring tonight was still cover
                # while the decision was being taken.
                self._tally_coverage(broker, day, closes_by_day[day])

                # Settle *after* deciding. A contract expiring today is still held
                # when the strategy looks at its book — that is what the live bot
                # sees at 3:45pm — and it resolves against today's official close.
                #
                # Settling first instead removes the expiring position before
                # the scan, so the scanner's "already have a position on this
                # symbol" check waves through a brand-new put on the same
                # underlying, dated to expire the same day, which then never
                # settles at all.
                broker.settle_expirations(day, closes_by_day[day])

                # Ex-div early assignment, decided at tonight's close: a short
                # ITM call whose remaining extrinsic value is less than
                # tomorrow's dividend is assigned away tonight, so the shares —
                # and tomorrow's dividend credit above — are gone. Runs after
                # settlement so a call that already expired today is not
                # assigned twice.
                next_day = days[i + 1] if i + 1 < len(days) else None
                if next_day is not None:
                    self._assign_calls_before_ex_dividend(
                        broker, chains, closes_by_day[day], day, next_day
                    )

                daily.append(self._snapshot_state(day, broker, client, closes_by_day[day]))
        finally:
            tally.__exit__(None, None, None)
            analytics_module.set_analytics_writer(previous_writer)
            # Restore the module-global non-retryable set in place (the getter
            # hands back the real object, so mutating it *is* the restore).
            live_failed = get_failed_symbols()
            live_failed.clear()
            live_failed.update(preserved_failed_symbols)

        self._analytics = no_op
        return SimulationResult(
            symbols=self.symbols,
            start=days[0],
            end=days[-1],
            starting_cash=self.starting_cash,
            daily=daily,
            broker=broker,
            rejections=tally.summary(),
            candidate_days=tally.candidate_days,
            dividends_credited=sum(
                e.cash_delta for e in broker.ledger if e.kind == "dividend"
            ),
            early_assignments=self._early_assignments,
            unpriced_ex_div_calls=self._unpriced_ex_div_calls,
            rolls_evaluated=self._rolls_evaluated,
            rolls_executed=self._rolls_executed,
            roll_records=list(self._roll_records),
            earnings_symbols_without_data=sorted(
                getattr(self.earnings_calendar, 'symbols_without_data', set()) or []),
            earnings_symbols_past_horizon=sorted(
                getattr(self.earnings_calendar, 'symbols_past_horizon', set()) or []),
            # FC-096 Phase C. `time_weighted_lot_value` divides by the DECISION
            # DAY count, not by the days a lot happened to be held: a symbol
            # that held no lot for part of the window is measured over the whole
            # window, which is what makes the number a capital base rather than
            # a conditional average.
            strategy=self.strategy,
            call_target_dte=int(getattr(self.config, "call_target_dte", 0) or 0),
            synthetic_lots_opened=self._synthetic_lots_opened,
            time_weighted_lot_value=(
                round(self._lot_value_days / len(daily), 2) if daily else 0.0),
            coverage_by_reason={
                reason: self._coverage[reason]
                for reason in COVERAGE_REASONS if self._coverage.get(reason)
            },
            calls_closed_early=self._calls_closed_early,
            itm_rolls=self._itm_rolls,
            otm_roll_outs=self._otm_roll_outs,
        )

    # ------------------------------------------------------------------ #
    # Roll records (FC-078 stamp + FC-096 Phase C split)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _stamp_roll_record(record: Dict[str, Any], *, day: date,
                           close: Optional[float]) -> Dict[str, Any]:
        """The replay's three stamps on one executed roll's success record.

        ``day`` because ``execute_roll``'s record carries none — in production
        the log line's timestamp IS the date, and a stored artifact read months
        later has no such context.

        ``itm_ratio`` and ``roll_kind`` because a roll count alone cannot
        distinguish a DEFENCE (the stock is through the strike and assignment is
        the alternative) from a ROLL-OUT (the roller re-writing the engine's own
        freshly-sold call, which is OTM by construction and bypasses every entry
        gate on its way to a higher strike). FC-100 §Phase C hand-off requires
        the split: it is moot on the covered-call profile, whose
        ``itm_trigger_ratio`` is 1.00, and load-bearing on the wheel's 0.98,
        which is exactly what FC-112 is meant to settle with measurement.

        The ratio is computed from the day's CLOSE over the old strike. That is
        not an approximation of what the roller saw — it is the same number: the
        backtest adapter's stock quote is ``bid == ask == close``, so the mid the
        roller gated on IS this close.

        **All three are `setdefault`, ours first, the roller's second.** If
        ``CallRoller`` ever emits its own ``day``, ``itm_ratio`` or ``roll_kind``,
        the producer's value wins rather than being silently overwritten by the
        replay's reconstruction.
        """
        stamped: Dict[str, Any] = {'day': day.isoformat(), **record}
        old_strike = float(record.get('old_strike') or 0.0)
        ratio = close / old_strike if close and old_strike > 0 else None
        stamped.setdefault(
            'itm_ratio', None if ratio is None else round(ratio, 4))
        stamped.setdefault(
            'roll_kind',
            None if ratio is None
            else ('itm_defence' if ratio >= 1.0 else 'otm_roll_out'))
        return stamped

    # ------------------------------------------------------------------ #
    # The synthetic lot (FC-096 Phase C §C2)
    # ------------------------------------------------------------------ #
    def _seed_synthetic_lots(
        self, broker: BacktestBroker, day: date, closes: Dict[str, float]
    ) -> None:
        """Seed a lot on any symbol that is flat today, at today's close.

        A no-op with no policy, which is every wheel replay.

        The flat test is ``broker.shares(symbol) == 0``, and it does the work of
        three rules at once: it seeds each symbol on its FIRST traded day (a
        symbol with no bar has no close here and is skipped until it trades),
        it never double-seeds while a lot is held, and — because a call-away
        removes the shares at settlement, after this point in the day — it
        re-seeds on the session AFTER the lot was called away. That is the
        operator's signed posture expressed as a single invariant rather than as
        call-away bookkeeping that could get out of step with the ledger.

        ``reseed=False`` restores the rejected alternative by refusing to seed a
        symbol twice.
        """
        policy = self.synthetic_lots
        if policy is None:
            return
        for symbol in self.symbols:
            if broker.shares(symbol) > 0:
                continue
            close = closes.get(symbol)
            if not close or close <= 0:
                # No bar today (or an unusable one): the symbol did not trade,
                # so there is no close to assume a purchase at. Skipped, not
                # approximated — a lot seeded at a stale price would put a
                # fictional basis under the cost-basis floor.
                continue
            if not policy.reseed and self._seeded_symbols.get(symbol):
                continue
            broker.deposit_shares(symbol, policy.shares, float(close), day,
                                  premise=policy.premise)
            self._seeded_symbols[symbol] = True
            self._synthetic_lots_opened += 1

    def _lot_value(self, broker: BacktestBroker) -> float:
        """Today's seeded lot value across the universe: Σ shares × lot basis.

        Read off the broker's own lots rather than off the seeding price, so a
        chain of lots at different bases weights each one at what it actually
        cost, and a partially-disposed lot contributes only what remains.
        """
        total = 0.0
        for lots in broker.stock_lots.values():
            for lot in lots:
                total += lot.shares * lot.cost_basis
        return total

    # ------------------------------------------------------------------ #
    # The covered-call monitor leg (FC-096 Phase C §C3)
    # ------------------------------------------------------------------ #
    def _run_monitor_leg(
        self, broker: BacktestBroker, client: "BacktestAlpacaClient",
        call_seller, day: date,
    ) -> None:
        """`/monitor`'s call half, for the covered-call profile only.

        ``CallSeller.should_close_call_early`` is deterministic config math over
        the position's own ``unrealized_pl`` / ``market_value`` and the DTE band
        it falls in — exactly the numbers the adapter already reports — so this
        is the REAL predicate, not a re-implementation of it.

        The close is placed through ``place_option_order(side='buy')``, the same
        call production makes, so the fill goes through the broker's documented
        haircut model and lands a normal ``buy_to_close`` event. Production
        prices the limit at ``ask × 0.95`` floored at the bid; the EOD replay
        has one decision point per day and no intraday path along which to test
        whether a limit was touched, which is why ``place_option_order`` ignores
        the limit and fills at the haircut price for every order this engine has
        ever placed. Passing a limit here would imply a precision the engine does
        not have; the divergence is named in the CC footer instead.

        Failures are counted, never raised: a contract with no bar today cannot
        be closed, and that is a data fact about the day, not a broken run.
        """
        if self.strategy == WHEEL_STRATEGY:
            return
        # Snapshot the symbols first: the loop mutates `broker.options`.
        shorts = [
            (symbol, pos.contracts) for symbol, pos in broker.options.items()
            if pos.option_type == "call" and pos.contracts > 0
        ]
        for symbol, contracts in shorts:
            position = next(
                (p for p in client.get_positions() if p.get("symbol") == symbol),
                None,
            )
            if position is None:
                continue
            if not call_seller.should_close_call_early(position):
                continue
            result = client.place_option_order(
                symbol=symbol, qty=contracts, side="buy", order_type="market")
            if result.get("success"):
                self._calls_closed_early += 1

    # ------------------------------------------------------------------ #
    # The coverage split (FC-096 Phase C §C3)
    # ------------------------------------------------------------------ #
    def _tally_coverage(
        self, broker: BacktestBroker, day: date, closes: Dict[str, float]
    ) -> None:
        """Classify each symbol's decision day into exactly one coverage bucket.

        Only runs for a seeding replay: the buckets are statements about a lot,
        and a wheel replay has none until a put is assigned to it (whose
        coverage story is the wheel's own, unmeasured here).

        The classification is DERIVED from the state of the book, in the
        priority order below, and is documented as derived rather than read off
        the rejection tally — the tally counts a reason once per DAY across the
        whole run, and this has to answer per symbol per day:

        1. ``post_call_away`` — no shares. Under the signed re-seed posture this
           can only be a symbol that has not traded yet (or one whose re-seed
           had no close to land on), never a permanent state.
        2. ``covered`` — an open short call against the lot. The strategy is
           doing its job.
        3. ``hold_uncovered`` — the close is below the lot basis, so the
           cost-basis floor refuses every strike worth writing. NOT a failure:
           this is the floor protecting the shares, and it is excluded from the
           low-activity test for exactly that reason.
        4. ``earnings_span`` — an earnings event falls inside the tenor a fresh
           call would be written into (``call_target_dte``), which is the span
           the FC-013 gate refuses to write across.
        5. ``gate_rejected`` — everything else: the delta band, the premium
           floor, an empty chain. This is the only bucket that means "the
           strategy wanted to write and could not find a contract".
        """
        if self.synthetic_lots is None:
            return
        covered = {
            pos.underlying for pos in broker.options.values()
            if pos.option_type == "call" and pos.contracts > 0
        }
        for symbol in self.symbols:
            reason = self._coverage_reason(broker, symbol, closes, covered)
            self._coverage[reason] = self._coverage.get(reason, 0) + 1
        self._lot_value_days += self._lot_value(broker)

    def _coverage_reason(self, broker: BacktestBroker, symbol: str,
                         closes: Dict[str, float], covered: set) -> str:
        if broker.shares(symbol) <= 0:
            return COVERAGE_POST_CALL_AWAY
        if symbol in covered:
            return COVERAGE_COVERED
        basis = broker.average_cost_basis(symbol)
        close = closes.get(symbol)
        if basis is not None and close is not None and close < basis:
            return COVERAGE_HOLD_UNCOVERED
        if self._earnings_blocks(symbol):
            return COVERAGE_EARNINGS_SPAN
        return COVERAGE_GATE_REJECTED

    def _earnings_blocks(self, symbol: str) -> bool:
        """Would an earnings event fall inside a fresh call's tenor today?

        The live gate is a per-candidate SPAN predicate (does this contract
        expire past the event), so the closest honest day-level statement is
        "an event falls within ``call_target_dte`` days" — the tenor the profile
        writes at. Fails to ``False`` on anything unknown, which pushes the day
        into ``gate_rejected``: over-attributing to the earnings gate would let
        it take credit for stand-downs it had no part in.
        """
        if not self.config.earnings_enabled:
            return False
        calendar = self.earnings_calendar
        if calendar is None:
            return False
        try:
            return bool(calendar.earnings_within(
                symbol, int(self.config.call_target_dte)))
        except Exception:  # noqa: BLE001 - a classification must not fail a run
            return False

    # ------------------------------------------------------------------ #
    # Dividends
    # ------------------------------------------------------------------ #
    def _credit_dividends(
        self, broker: BacktestBroker, day: date, previous_day: Optional[date]
    ) -> None:
        """Credit dividends going ex in ``(previous_day, day]``, on shares held.

        The interval rather than an exact date match on ``day`` is deliberate,
        and it is what keeps the two legs on the same footing. ``days`` is the
        set of sessions for which this symbol has a bar; an ex-date landing on a
        session the symbol did not trade is absent from it. Matching exactly
        would drop that dividend from the wheel while the benchmark — which sums
        over the whole window via ``total_between`` — still collects it. The two
        would then be measured against different dividend streams, which is the
        precise failure this track exists to remove.

        Crediting it on the next session the wheel can be observed on is also
        correct on the ownership test: the holder held through the prior close.

        ``credit_dividend`` is a no-op when no shares are held, so this is safe
        to call unconditionally on every symbol every day. On the first day
        ``previous_day`` is None and the interval collapses to ``day`` alone —
        immaterial, since the broker starts flat.
        """
        lower = previous_day if previous_day is not None else day - timedelta(days=1)
        for symbol in self.symbols:
            per_share = self.dividends.total_between(symbol, lower, day)
            if per_share <= 0:
                continue
            amount = broker.credit_dividend(symbol, per_share, day)
            if amount:
                logger.debug(
                    "Dividend credited",
                    event_category="backtest",
                    event_type="dividend_credited",
                    symbol=symbol, day=day.isoformat(),
                    per_share=per_share, amount=round(amount, 2),
                )

    def _assign_calls_before_ex_dividend(
        self,
        broker: BacktestBroker,
        chains: Dict[str, Dict[date, ChainSnapshot]],
        closes: Dict[str, float],
        day: date,
        next_day: date,
    ) -> None:
        """Assign short ITM calls whose extrinsic value is below tomorrow's dividend.

        The mark comes from today's chain. When the contract did not trade today
        there is no mark, and the position is **left alone** rather than assigned.
        That is a real residual bias — an illiquid deep-ITM call genuinely has
        near-zero extrinsic and would be assigned in life — but the alternative
        is to treat "no data" as "zero extrinsic" and manufacture assignments
        from silence, which is worse than the bias it removes. Counted in the
        report's data-quality block so the size of it is visible rather than
        assumed.
        """
        for symbol in self.symbols:
            # Anything going ex before the next session the wheel could act on —
            # same interval convention as _credit_dividends, so a dividend that
            # is credited is also one that can trigger assignment.
            dividend = self.dividends.total_between(symbol, day, next_day)
            if dividend <= 0:
                continue
            spot = closes.get(symbol)
            if spot is None:
                continue
            snapshot = chains.get(symbol, {}).get(day)
            # Snapshot the calls first: assignment mutates broker.options.
            calls = [
                p for p in broker.options.values()
                if p.underlying == symbol and p.option_type == "call"
            ]
            for pos in calls:
                quote = _find_chain_quote(snapshot, pos.symbol)
                if quote is None:
                    if (spot - pos.strike) >= 0.01:
                        self._unpriced_ex_div_calls += 1
                    continue
                if not should_assign_early(
                    strike=pos.strike,
                    underlying_price=spot,
                    option_mark=quote.mark,
                    dividend=dividend,
                ):
                    continue
                # Two short calls on one underlying can both qualify on the same
                # ex-eve. Assigning the second after the first has consumed the
                # shares raises ValueError out of _remove_shares_fifo and kills
                # a multi-hour run. Assign only what is actually covered and say
                # so; the uncovered leftover is a cover-check bug elsewhere, and
                # this is not the place to discover it by crashing.
                if broker.shares(symbol) < 100 * pos.contracts:
                    logger.warning(
                        "Skipping ex-div early assignment: shares already gone",
                        event_category="backtest",
                        event_type="ex_dividend_assignment_uncovered",
                        symbol=pos.symbol, underlying=symbol,
                        day=day.isoformat(),
                        shares_held=broker.shares(symbol),
                        shares_needed=100 * pos.contracts,
                    )
                    continue
                if broker.assign_call_early(pos.symbol, day, reason="ex_dividend"):
                    self._early_assignments += 1
                    logger.info(
                        "Short call assigned early ahead of ex-dividend",
                        event_category="backtest",
                        event_type="ex_dividend_early_assignment",
                        symbol=pos.symbol, underlying=symbol,
                        day=day.isoformat(), ex_date=next_day.isoformat(),
                        strike=pos.strike, spot=round(spot, 2),
                        mark=round(quote.mark, 4),
                        extrinsic=round(max(0.0, quote.mark - (spot - pos.strike)), 4),
                        dividend=dividend,
                    )

    @staticmethod
    def _execute_opportunities(
        exec_engine, put_seller, call_seller, opportunities, client
    ) -> None:
        """The `/run` half, stage for stage.

        Order matches ``cloud_run_server``'s ``/run``: non-retryable filter →
        idempotency filter → buying power → one positions snapshot → rank →
        select_batch → execute_batch.

        ``filter_failed_opportunities`` is now CALLED (pre-FC-068 it was
        skipped, behind a docstring claiming it "reads the GCS opportunity
        store" — it does not; it reads a module-global set). The set is cleared
        at the top of every simulated day and restored around the run; see
        ``run()``.
        """
        if not opportunities:
            return

        opportunities, _ = exec_engine.filter_failed_opportunities(opportunities)
        if not opportunities:
            return

        positions = client.get_positions()
        # Production passes `get_option_POSITIONS()` here (cloud_run_server.py:458);
        # the replay passes the full position list. Equivalent ONLY because
        # `filter_duplicate_opportunities` matches on the OCC `option_symbol`,
        # which no equity symbol can collide with — so the extra stock rows are
        # inert. That equivalence is load-bearing: if the filter is ever changed
        # to match on the UNDERLYING, this line silently starts blocking every
        # covered call in the replay (the shares are always held when a call is
        # written). Narrow it to option positions at the same time.
        opportunities, _ = exec_engine.filter_duplicate_opportunities(opportunities, positions)
        if not opportunities:
            return

        account_info = client.get_account()
        available_bp = float(
            account_info.get("options_buying_power") or account_info["buying_power"]
        )
        # One snapshot for the whole cycle, as /run does (FC-038): the two
        # stages must agree on share availability.
        ranked = exec_engine.rank_opportunities(
            opportunities, put_seller, available_bp, positions=positions
        )
        selected, _ = exec_engine.select_batch(ranked, available_bp, positions=positions)
        if not selected:
            return

        exec_engine.execute_batch(selected, put_seller, call_seller=call_seller)

    @staticmethod
    def _snapshot_state(
        day: date,
        broker: BacktestBroker,
        client: BacktestAlpacaClient,
        closes: Dict[str, float],
    ) -> DailyState:
        # Equity comes from the adapter so the curve is marked exactly the way
        # the strategy saw its own account that day (chain marks where the
        # contract traded, intrinsic where it did not). Marking it differently
        # here would make the reported curve disagree with the decisions taken
        # against it.
        return DailyState(
            day=day,
            equity=client.get_account()["equity"],
            cash=broker.cash,
            reserved_collateral=broker.reserved_collateral,
            open_options=len(broker.options),
            shares_held={u: broker.shares(u) for u in broker.stock_lots},
        )
