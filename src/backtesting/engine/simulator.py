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
from ..data.chain_builder import ChainBuilder, ChainSnapshot
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
    """
    narrowed = copy.deepcopy(config)
    narrowed._config["stocks"]["symbols"] = list(symbols)
    return narrowed


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
    ) -> None:
        self.config = restrict_symbols(config, symbols)
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
                    self._roll_records.extend(
                        r for r in (rolls.get('roll_details') or [])
                        if r.get('success'))
                except Exception:
                    logger.exception(
                        "Strategy cycle raised during replay",
                        event_category="backtest",
                        event_type="replay_cycle_error",
                        day=day.isoformat(),
                    )
                    raise

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
        )

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
