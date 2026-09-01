"""Simulator day loop (FC-032 Phase 3).

The golden path: the simulator drives the *real* production pipeline over
canned data — ``OptionsScanner`` → ``ExecutionEngine`` (filter → rank →
select_batch → execute_batch) → ``PutSeller``/``CallSeller``. Nothing here
stubs strategy logic; if a rule is wrong, it is wrong in production too.

FC-068 repointed the generation half off ``WheelEngine.run_strategy_cycle()``,
which production abandoned in 2025. These tests are the acceptance criteria of
that repoint: they passed against the engine path and must pass, with
assertions unweakened, against the pipeline that actually trades.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List

import pytest

from src.backtesting.data.chain_builder import STRIKE_WINDOW_PCT, ChainBuilder
from src.backtesting.data.dividends import Dividend, DividendSchedule
from src.backtesting.data.provider import OptionBar, OptionContract, StockBar
from src.backtesting.engine.no_op_analytics import NoOpAnalyticsWriter
from src.backtesting.engine.simulator import Simulator, restrict_symbols
from src.backtesting.metrics.cycles import build_cycles
from src.backtesting.metrics.fitness import compute_fitness
from src.data import analytics_writer as analytics_module
from src.utils import clock
from src.utils.config import Config


def _occ(underlying: str, exp: date, opt: str, strike: float) -> str:
    return f"{underlying}{exp:%y%m%d}{'P' if opt == 'put' else 'C'}{int(strike * 1000):08d}"


class ScriptedProvider:
    """Canned data: one underlying, a strike ladder, every contract trades daily."""

    def __init__(self, symbol: str, closes: Dict[date, float], expirations: List[date]):
        self.symbol = symbol
        self.closes = closes
        self.expirations = expirations

    def get_stock_bars(self, symbol, start, end):
        return [
            StockBar(symbol=symbol, bar_date=d, open=c, high=c, low=c, close=c, volume=5_000_000)
            for d, c in sorted(self.closes.items())
            if start <= d <= end
        ]

    def _ladder(self, spot: float) -> List[float]:
        base = round(spot)
        return [float(base + k) for k in range(-12, 13)]

    def get_contract_universe(
        self, underlying, as_of, max_dte, *, strike_gte=None, strike_lte=None
    ):
        # Deliberately IGNORES the strike bounds — the Protocol permits a
        # provider to return a superset, and the builder is responsible for
        # narrowing. Keeping one mock ignore them keeps that guarantee tested.
        spot = self.closes.get(as_of)
        if spot is None:
            return []
        out = []
        for exp in self.expirations:
            if not (as_of <= exp <= as_of + timedelta(days=max_dte)):
                continue
            for strike in self._ladder(spot):
                for opt in ("put", "call"):
                    out.append(
                        OptionContract(_occ(underlying, exp, opt, strike), underlying,
                                       exp, strike, opt)
                    )
        return out

    def get_option_bars(self, symbols, start, end):
        """Price every contract off the day's spot with a crude but monotone model."""
        out: Dict[str, List[OptionBar]] = {}
        for sym in symbols:
            for d in _daterange(start, end):
                spot = self.closes.get(d)
                if spot is None:
                    continue
                strike = int(sym[-8:]) / 1000.0
                is_put = sym[-9] == "P"
                exp = datetime.strptime(sym[len(self.symbol):len(self.symbol) + 6], "%y%m%d").date()
                dte = max((exp - d).days, 0)
                intrinsic = max(0.0, strike - spot) if is_put else max(0.0, spot - strike)
                # Enough time value that 0.10-0.20 delta strikes clear the $0.50 floor.
                time_value = 0.9 * (dte + 1) ** 0.5 * (spot / 100.0)
                moneyness = abs(spot - strike) / spot
                price = intrinsic + time_value * max(0.05, 1.0 - 6.0 * moneyness)
                out.setdefault(sym, []).append(
                    OptionBar(symbol=sym, bar_date=d, open=price, high=price, low=price,
                              close=round(price, 2), volume=500, trade_count=50, vwap=price)
                )
        return out


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _weekdays(start: date, n: int) -> List[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


@pytest.fixture
def falling_then_flat():
    """Price slides enough to put an assigned strike ITM, then stabilizes.

    Includes ~45 sessions of warm-up history before the first decision day.
    That was originally the gap detector's positional ~30-bar lookback;
    post-FC-068 the gap stages are gone (and FC-069 deleted the module), so
    what needs the history is stage 1's volatility and average-volume metrics
    (`market_data.filter_suitable_stocks`).
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 30)
    closes = {d: 100.0 for d in warmup}
    # -3/day for 10 sessions (100 -> 70), then flat. The strategy sells puts
    # ~8% OTM, so a gentler slide would never overtake the strike before expiry
    # and the assignment path would go untested. -3 was also chosen to stay
    # under the old 5% overnight-gap block (deleted by FC-069); the shape is
    # kept because the fixture's assignment timing is calibrated to it.
    for i, d in enumerate(days):
        closes[d] = 100.0 - min(i, 10) * 3.0
    expirations = [d for d in days if d.weekday() == 4]  # Fridays
    return days, closes, expirations


@pytest.fixture
def dip_then_recovering():
    """The fixture form of ``dip_then_recovering_window``. See that function."""
    return dip_then_recovering_window()


def dip_then_recovering_window():
    """Assigns a put, then RECOVERS so the call leg can actually run.

    `falling_then_flat` cannot test the call side and never could: it assigns
    at ~92 and leaves spot at 70, so the cost-basis floor blocks every covered
    call *even with correct routing*. That fixture documents the call-refusal
    path; this one exercises the call-sale path. (Pre-FC-068 the drawdown pause
    blocked it too; the pause is deleted with the engine path — FC-065 OQ-3 —
    and the floor alone still refuses, which is the live behaviour.)

    Shape: warm-up flat at 100 -> slide to put the ~8%-OTM strike ITM ->
    recover to just above cost basis and drift up, so that
      * spot >= basis, so strikes above the floor exist at all, and
      * strikes just above basis sit inside call_delta_range [0.15, 0.25], and
      * a later expiry finishes above the sold strike, so the call assigns.

    A plain function with a fixture wrapper, because this is the only window in
    the suite whose replay actually EXECUTES a roll, and `tests/
    test_sim_artifacts.py` needs the real records to pin the artifact's
    `roll_records` shape against the producer rather than against an invention.
    A second copy of the price path would be the drift this split exists to
    prevent.
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 45)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        if i <= 8:
            closes[d] = 100.0 - i * 3.0          # 100 -> 76, assigns ~92
        elif i <= 20:
            closes[d] = 76.0 + (i - 8) * 1.6     # recover 76 -> ~95
        else:
            closes[d] = 95.0 + (i - 20) * 0.9    # drift up through the strike
    expirations = [d for d in days if d.weekday() == 4]
    return days, closes, expirations


def _simulator(symbol, closes, expirations, days, **kw):
    provider = ScriptedProvider(symbol, closes, expirations)
    builder = ChainBuilder(provider, risk_free_rate=0.04)
    config = Config()
    return Simulator(
        config, provider, builder, [symbol], days[0], days[-1],
        starting_cash=kw.pop("starting_cash", 50_000.0), max_dte=7, **kw
    )


class TestDayLoop:
    def test_run_produces_one_state_per_trading_day(self, falling_then_flat):
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        assert [s.day for s in result.daily] == days
        assert result.start == days[0] and result.end == days[-1]

    def test_clock_is_cleared_after_the_run(self, falling_then_flat):
        days, closes, exps = falling_then_flat
        _simulator("XYZ", closes, exps, days).run()
        assert not clock.is_frozen(), "a leaked freeze would corrupt later code"

    def test_empty_window_refuses_rather_than_reporting_zero_trades(self):
        """The old engine's cardinal sin: a 0-trade run that 'succeeded'."""
        provider = ScriptedProvider("XYZ", {}, [])
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        sim = Simulator(Config(), provider, builder, ["XYZ"],
                        date(2024, 6, 3), date(2024, 6, 28))
        with pytest.raises(ValueError, match="refusing to report a zero-trade run"):
            sim.run()

    def test_the_wheel_actually_turns(self, falling_then_flat):
        """A put is sold; the slide assigns it; shares appear. The engine's own code did it."""
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()

        kinds = [e.kind for e in result.broker.ledger]
        assert "sell_put_open" in kinds, f"no put was ever sold; ledger={kinds}"
        # The underlying falls 12%, so at least one short put must finish ITM.
        assert "put_assignment" in kinds, f"no assignment despite a 12% slide; ledger={kinds}"
        assert result.broker.shares("XYZ") > 0

    def test_no_contract_is_left_expired_but_unsettled(self, falling_then_flat):
        """Regression: settling before the decision let the engine open a fresh

        put expiring the same day, after that day's settlement had already run.
        It then sat on the book forever, corrupting equity.
        """
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        for symbol, pos in result.broker.options.items():
            assert pos.expiration >= days[-1], (
                f"{symbol} expired {pos.expiration} but is still open at {days[-1]}"
            )

    def test_no_new_put_is_opened_on_the_day_a_put_is_assigned(self, falling_then_flat):
        """Stage 6 sees the expiring position because settlement runs after the scan."""
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        assigned_days = {e.event_date for e in result.broker.ledger if e.kind == "put_assignment"}
        opened_days = {e.event_date for e in result.broker.ledger if e.kind == "sell_put_open"}
        assert not (assigned_days & opened_days), (
            "a put was sold on the same day another was assigned — the expiring "
            "position was invisible to the duplicate guard"
        )

    def test_equity_equals_cash_plus_marked_positions(self, falling_then_flat):
        """The reported curve must be arithmetic on the ledger, not a second opinion."""
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        final = result.daily[-1]
        expected = final.cash + result.broker.shares("XYZ") * closes[days[-1]]
        assert final.equity == pytest.approx(expected)

    def test_wheel_loses_less_than_buy_and_hold_through_the_crash(self, falling_then_flat):
        """Sanity anchor, not a performance claim: collateral sat in cash for most

        of a -30% slide, so the wheel must be well ahead of the underlying.
        """
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        underlying_return = (closes[days[-1]] - closes[days[0]]) / closes[days[0]]
        assert underlying_return == pytest.approx(-0.30)
        assert result.total_return > underlying_return

    def test_cash_ledger_is_conserved_no_phantom_margin(self, falling_then_flat):
        days, closes, exps = falling_then_flat
        result = _simulator("XYZ", closes, exps, days).run()
        for state in result.daily:
            assert state.reserved_collateral >= 0.0
            # Cash-secured: collateral never exceeds cash on hand.
            assert state.reserved_collateral <= state.cash + 1e-6


class TestDividendsThroughTheDayLoop:
    """FC-042 Track C, end to end.

    The fixture's put is assigned 2024-06-07 at 92.00 and the run finishes still
    holding those 100 shares, which makes it a clean bed for ex-date behavior.
    """

    ASSIGNED = date(2024, 6, 7)

    def _run(self, fixture, ex_dates, amount=0.50):
        days, closes, exps = fixture
        schedule = DividendSchedule(
            {"XYZ": [Dividend(ex_date=d, amount=amount) for d in ex_dates]}
        )
        sim = _simulator("XYZ", closes, exps, days, dividend_schedule=schedule)
        return sim.run()

    def test_dividend_is_credited_while_shares_are_held(self, falling_then_flat):
        result = self._run(falling_then_flat, [date(2024, 6, 20)])
        events = [e for e in result.broker.ledger if e.kind == "dividend"]
        assert len(events) == 1
        assert events[0].shares == 100
        assert events[0].cash_delta == pytest.approx(50.0)
        assert result.dividends_credited == pytest.approx(50.0)

    def test_nothing_is_credited_before_assignment(self, falling_then_flat):
        """No shares yet, so no dividend — the pre-assignment days are flat."""
        result = self._run(falling_then_flat, [date(2024, 6, 5)])
        assert not [e for e in result.broker.ledger if e.kind == "dividend"]
        assert result.dividends_credited == 0.0

    def test_an_ex_date_on_the_assignment_day_pays_nothing(self, falling_then_flat):
        """The ownership test is the PREVIOUS close, and this is the crux of it.

        Shares arrive at that day's expiration settlement, i.e. after the stock
        already went ex. A holder who acquires on the ex-date does not collect;
        crediting here would be free money, and it would appear on every cycle
        whose assignment happened to land on an ex-date.
        """
        result = self._run(falling_then_flat, [self.ASSIGNED])
        assert not [e for e in result.broker.ledger if e.kind == "dividend"]

    def test_the_day_after_assignment_does_pay(self, falling_then_flat):
        """Bracketing the case above: one session later, the shares are on the record."""
        result = self._run(falling_then_flat, [date(2024, 6, 10)])
        events = [e for e in result.broker.ledger if e.kind == "dividend"]
        assert len(events) == 1
        assert events[0].cash_delta == pytest.approx(50.0)

    def test_dividends_land_in_equity_and_attribution_still_reconciles(
        self, falling_then_flat
    ):
        """The acceptance criterion: attribution must still sum to the equity change.

        A dividend that reaches cash but not the attribution table would make
        every percentage in the report wrong by exactly that amount.
        """
        days, closes, exps = falling_then_flat
        base = self._run(falling_then_flat, [])
        with_div = self._run(falling_then_flat, [date(2024, 6, 20)])

        # It is real money: equity is higher by exactly the dividend.
        assert with_div.final_equity - base.final_equity == pytest.approx(50.0)

        cycles = build_cycles(with_div.broker.ledger)
        prices = {d: closes[d] for d in days}
        report = compute_fitness(
            "XYZ", with_div.daily, cycles, with_div.starting_cash,
            benchmark_prices=prices,
            benchmark_dividends_per_share=0.50,
            data_quality={"decision_days": len(with_div.daily)},
        )
        assert report.dividends == pytest.approx(50.0)
        assert report.reconciliation_gap == pytest.approx(0.0, abs=0.01)

    def test_benchmark_collects_the_dividend_too(self, falling_then_flat):
        """Crediting only the wheel would invert the bias, not remove it."""
        days, closes, exps = falling_then_flat
        result = self._run(falling_then_flat, [date(2024, 6, 20)])
        cycles = build_cycles(result.broker.ledger)
        prices = {d: closes[d] for d in days}

        without = compute_fitness(
            "XYZ", result.daily, cycles, result.starting_cash,
            benchmark_prices=prices, data_quality={"decision_days": len(result.daily)},
        )
        with_ = compute_fitness(
            "XYZ", result.daily, cycles, result.starting_cash,
            benchmark_prices=prices, benchmark_dividends_per_share=0.50,
            data_quality={"decision_days": len(result.daily)},
        )
        assert with_.benchmark.dividends > 0
        assert with_.benchmark.total_return > without.benchmark.total_return
        # And the wheel's edge shrinks accordingly — that is the bias correction.
        assert with_.excess_return < without.excess_return

    def test_no_schedule_means_no_dividends_anywhere(self, falling_then_flat):
        """The pre-FC-042 model stays reachable and stays silent."""
        days, closes, exps = falling_then_flat
        sim = _simulator(
            "XYZ", closes, exps, days, dividend_schedule=DividendSchedule.empty()
        )
        result = sim.run()
        assert result.dividends_credited == 0.0
        assert result.early_assignments == 0


class TestNoProductionSideEffects:
    def test_analytics_singleton_is_restored_after_the_run(self, falling_then_flat):
        days, closes, exps = falling_then_flat
        before = analytics_module._instance
        _simulator("XYZ", closes, exps, days).run()
        assert analytics_module._instance is before

    def test_analytics_singleton_is_restored_even_when_the_run_raises(self):
        provider = ScriptedProvider("XYZ", {}, [])
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        before = analytics_module._instance
        sim = Simulator(Config(), provider, builder, ["XYZ"],
                        date(2024, 6, 3), date(2024, 6, 28))
        with pytest.raises(ValueError):
            sim.run()
        assert analytics_module._instance is before

    @pytest.mark.real_bq_lookup
    def test_scanner_cannot_query_bigquery_during_a_replay(self):
        """A replay must never mix production trade history into its floor.

        FC-068 moved this contract from CallSeller to OptionsScanner: the
        seller's resolver was deleted with the engine path, and the scanner is
        now the sole producer — and it hardcoded ``allow_bigquery=True``
        (the PR #75 reviewer's finding), so a repointed replay would have read
        production ``trades_from_activities`` against ``CURRENT_TIMESTAMP()``
        on every simulated day.

        Marked ``real_bq_lookup`` deliberately: the conftest hermeticity guard
        patches the chokepoint on the CLASS, so with it in place this passes
        whether or not ``__init__`` forwards the gate. Opting out is what makes
        the *production-code* gate the thing being tested. The discriminating
        assertion is that no BigQuery client is ever constructed.
        """
        from unittest.mock import Mock, patch

        from src.data.options_scanner import OptionsScanner

        scanner = OptionsScanner(Mock(), Mock(), Mock(spec=Config),
                                 allow_bigquery=False)

        assert scanner.cost_basis_resolver.allow_bigquery is False
        with patch('google.cloud.bigquery.Client') as mock_client:
            # "None" is "no comparison available" — it leaves the simulated
            # broker's floor untouched rather than vetoing it.
            assert scanner.cost_basis_resolver._lookup_assignment_basis(
                "XYZ", 100) is None
        mock_client.assert_not_called()

    def test_the_simulator_builds_its_scanner_with_the_gate_shut(self, falling_then_flat):
        """The other half of the link: the test above pins the gate working,
        this pins the simulator actually passing it. Constructing the scanner
        with the default would leave the gate perfect and unreached."""
        from unittest.mock import patch

        import src.backtesting.engine.simulator as simulator_module

        days, closes, exps = falling_then_flat
        seen = []
        real = simulator_module.OptionsScanner

        def spy(*args, **kwargs):
            seen.append(kwargs.get('allow_bigquery'))
            return real(*args, **kwargs)

        with patch.object(simulator_module, 'OptionsScanner', spy):
            _simulator("XYZ", closes, exps, days).run()

        assert seen == [False], f"simulator built its scanner with {seen!r}"

    @pytest.mark.real_bq_lookup
    def test_uncovered_days_resolver_is_gated_in_replay(self):
        """Chokepoint #2 (FC-065 Phase 4): one batched BigQuery query per scan
        for every held symbol. Ungated, a replay issues it on every simulated
        day. ``None`` is the honest answer — "could not tell", not zero."""
        from unittest.mock import Mock, patch

        from src.data.options_scanner import OptionsScanner

        scanner = OptionsScanner(Mock(), Mock(), Mock(spec=Config),
                                 allow_bigquery=False)

        assert scanner.uncovered_days_resolver.allow_bigquery is False
        with patch('google.cloud.bigquery.Client') as mock_client:
            resolved = scanner.uncovered_days_resolver.resolve(["XYZ"], set())
        mock_client.assert_not_called()
        assert resolved.get("XYZ") is None

    def test_replay_decision_rows_reach_only_the_noop_writer(self, dip_then_recovering):
        """FC-065 Phase 4's scan-stage decision records fire inside
        ``scan_for_call_opportunities`` regardless of who calls it, and
        ``DecisionRecorder.flush`` resolves the analytics writer from module
        scope. The simulator's singleton swap is the only thing between a
        replay and the production ``decision_events`` table.

        Uses the fixture that actually holds shares — with no held symbol the
        recorder writes nothing and this passes vacuously.
        """
        days, closes, exps = dip_then_recovering
        before = analytics_module._instance
        sim = _simulator("XYZ", closes, exps, days)
        result = sim.run()

        assert result.broker.ledger, "fixture never traded"
        # Rows WERE produced — otherwise this passes by writing nothing.
        recorded = sim._analytics.calls_named("write_decision_events")
        assert recorded, (
            "no decision rows during the replay — the assertion below would "
            "pass vacuously"
        )
        assert any(call[1] and call[1][0] for call in recorded), (
            "write_decision_events was called with an empty batch only"
        )
        # And nothing escaped: the production singleton is the object it was
        # before the run, untouched throughout.
        assert analytics_module._instance is before
        assert isinstance(sim._analytics, NoOpAnalyticsWriter)

    def test_call_roller_cannot_query_bigquery_during_a_replay(self):
        """FC-065 Phase 2: the roller resolves its floor through the same shared
        resolver now, and ``run_rolling_cycle()`` runs *inside* the replay
        (Fridays). Without the gate forwarded, a replay's roller would query
        production trade history against CURRENT_TIMESTAMP() — the identical
        hazard the seller's gate exists for."""
        from unittest.mock import Mock, patch

        from src.strategy.wheel_engine import WheelEngine
        from src.strategy.wheel_state_manager import WheelStateManager

        config = Mock()
        config.rolling_enabled = True
        config.earnings_enabled = False
        alpaca = Mock()
        alpaca.get_positions.return_value = []
        alpaca.get_orders.return_value = []

        engine = WheelEngine(config, alpaca_client=alpaca,
                             wheel_state=WheelStateManager(),
                             allow_bigquery_cost_basis=False)

        with patch('src.strategy.wheel_engine.CallRoller') as mock_roller:
            engine.run_rolling_cycle()

        assert mock_roller.call_args.kwargs['allow_bigquery_cost_basis'] is False

    @pytest.mark.real_bq_lookup
    def test_the_rollers_replay_gate_reaches_its_own_resolver(self):
        """The test above pins the *engine* handing the flag to ``CallRoller``.
        This pins ``CallRoller`` handing it to the resolver — the other half of
        the same link, and the half that was untested: hardcoding
        ``allow_bigquery=True`` inside ``CallRoller.__init__`` (ignoring the
        parameter entirely) passed the whole suite while a replay would read
        production trade history. Seller-style: no BigQuery client is built at
        all. (``real_bq_lookup`` opts out of the conftest stub so the real
        method runs and the gate is what stops it.)"""
        from unittest.mock import Mock, patch

        from src.strategy.call_roller import CallRoller

        # (alpaca, market_data, config, risk_manager, earnings_calendar) —
        # FC-078 dropped the wheel_state parameter that used to sit at index 3.
        roller = CallRoller(Mock(), Mock(), Mock(), Mock(), Mock(),
                            allow_bigquery_cost_basis=False)

        assert roller.cost_basis_resolver.allow_bigquery is False
        with patch('google.cloud.bigquery.Client') as mock_client:
            assert roller.cost_basis_resolver._lookup_assignment_basis(
                'XYZ', 100) is None
        mock_client.assert_not_called()

    def test_the_live_failed_symbol_set_is_restored_after_a_replay(self, falling_then_flat):
        """`/backtest/screen` lives on the LIVE trading server (disabled by
        default, opt-in via ENABLE_SCREEN_ENDPOINT). ExecutionEngine's
        `_failed_symbols` is a module global, so an in-server replay clearing it
        would wipe the non-retryable set `/run` depends on — and it also leaked
        across the 14 sequential per-symbol runs of a screen.

        The set is also NOT the replay's to inherit: a live non-retryable symbol
        must not suppress a simulated one."""
        from src.strategy.execution_engine import (
            clear_failed_symbols, get_failed_symbols)

        days, closes, exps = falling_then_flat
        clear_failed_symbols()
        get_failed_symbols().add("LIVE_SENTINEL_240607P00090000")
        try:
            _simulator("XYZ", closes, exps, days).run()
            assert "LIVE_SENTINEL_240607P00090000" in get_failed_symbols(), (
                "the replay wiped the live non-retryable set"
            )
        finally:
            clear_failed_symbols()

    def test_failed_symbols_are_cleared_each_simulated_day(self, falling_then_flat):
        """Production's `_failed_symbols` clears roughly daily (Cloud Run cold
        start). Clearing once per RUN instead would let a day-1 non-retryable
        failure suppress a symbol for a months-long window — a divergence from
        production, not fidelity to it."""
        from unittest.mock import patch

        import src.backtesting.engine.simulator as simulator_module

        days, closes, exps = falling_then_flat
        clears = []
        real = simulator_module.clear_failed_symbols

        def counting():
            clears.append(1)
            return real()

        with patch.object(simulator_module, "clear_failed_symbols", counting):
            result = _simulator("XYZ", closes, exps, days).run()

        assert len(clears) == len(result.daily), (
            f"expected one clear per simulated day ({len(result.daily)}), "
            f"got {len(clears)} — a once-per-run clear is not production's cadence"
        )

    def test_scanner_bigquery_stays_enabled_by_default(self):
        """The gate must not leak into production. ``/scan`` constructs
        ``OptionsScanner(alpaca_client, market_data, config)`` with no keyword,
        so the default is what the live path gets — flipping it would silently
        disable the divergence cross-check and the ``uncovered_days`` label on
        every real scan."""
        from unittest.mock import Mock

        from src.data.options_scanner import OptionsScanner
        from src.strategy.call_roller import CallRoller

        scanner = OptionsScanner(Mock(), Mock(), Mock(spec=Config))
        assert scanner.allow_bigquery is True
        assert scanner.cost_basis_resolver.allow_bigquery is True
        assert scanner.uncovered_days_resolver.allow_bigquery is True

        roller = CallRoller(Mock(), Mock(), Mock(), Mock(), Mock())
        assert roller.cost_basis_resolver.allow_bigquery is True


class TestTheReplayRunsTheProductionStages:
    """FC-068's whole premise is that the replay runs the *live* pipeline, and
    nothing was pinning that it runs ALL of it, in order, on production's
    arguments.

    Two adversarial-review mutations survived the 988-test suite:
      * deleting the ``filter_duplicate_opportunities`` stage from
        ``_execute_opportunities`` — production runs it on every cycle
        (``cloud_run_server.py:458``), and the replay would silently stop;
      * overriding both day-loop scans with ``max_results=50`` — ``/scan``
        passes no args (``cloud_run_server.py:193,200``), so a replay with a
        wider cap measures a different strategy. The plan's own words:
        "Diverging would measure a different strategy."

    Neither is caught by outcome assertions: both change WHAT is measured
    without breaking the ledger, the equity curve or the tally, which is
    exactly the class of silent drift this FC exists to end.
    """

    class _StageSpy:
        """A recording stand-in for ExecutionEngine.

        Deliberately a spy rather than a ``wraps=`` mock on the real class: the
        contract being pinned is *which stages the replay invokes and in what
        order*, and a pass-through would let a dropped stage still produce a
        plausible run.
        """

        def __init__(self):
            self.calls = []
            self.positions_seen = []

        def filter_failed_opportunities(self, opportunities):
            self.calls.append("filter_failed_opportunities")
            return opportunities, 0

        def filter_duplicate_opportunities(self, opportunities, positions):
            self.calls.append("filter_duplicate_opportunities")
            self.positions_seen.append(id(positions))
            return opportunities, 0

        def rank_opportunities(self, opportunities, put_seller,
                               available_bp, positions=None):
            self.calls.append("rank_opportunities")
            self.positions_seen.append(id(positions))
            self.available_bp = available_bp
            return list(opportunities)

        def select_batch(self, ranked, available_bp, positions=None):
            self.calls.append("select_batch")
            self.positions_seen.append(id(positions))
            return list(ranked), available_bp

        def execute_batch(self, selected, put_seller, call_seller=None):
            self.calls.append("execute_batch")
            self.selected = list(selected)
            self.put_seller = put_seller
            self.call_seller = call_seller
            return [], 0

    class _Client:
        def __init__(self):
            self._positions = [{"symbol": "XYZ", "qty": 100.0,
                                "asset_class": "us_equity"}]

        def get_positions(self):
            return self._positions

        def get_account(self):
            return {"buying_power": 50_000.0, "options_buying_power": 40_000.0}

    def test_the_run_half_invokes_every_production_stage_in_order(self):
        """Kills N1. The order is ``/run``'s, stage for stage."""
        spy = self._StageSpy()
        client = self._Client()
        put_seller, call_seller = object(), object()
        opportunities = [{"type": "put", "symbol": "XYZ",
                          "option_symbol": "XYZ240607P00090000",
                          "strike_price": 90.0, "premium": 1.0}]

        Simulator._execute_opportunities(
            spy, put_seller, call_seller, opportunities, client)

        assert spy.calls == [
            "filter_failed_opportunities",
            "filter_duplicate_opportunities",
            "rank_opportunities",
            "select_batch",
            "execute_batch",
        ], f"replay stage sequence diverged from /run: {spy.calls}"
        # FC-038: ONE positions snapshot for the whole cycle. Re-fetching
        # between sizing and selection lets a fill land in between and produce
        # a selection the sizing stage never sanctioned.
        assert len(set(spy.positions_seen)) == 1, (
            "the duplicate filter, ranking and selection did not share one "
            "positions snapshot"
        )
        # options_buying_power wins over buying_power, as /run does.
        assert spy.available_bp == 40_000.0
        # Both sellers reach execution, or the call leg silently dies again
        # (FC-048).
        assert spy.put_seller is put_seller and spy.call_seller is call_seller

    def test_the_day_loop_scans_on_production_defaults(self, falling_then_flat):
        """Kills N2. ``/scan`` passes no ``max_results``; neither may the replay.

        Asserted on the call arguments rather than on any outcome, because a
        wider cap does not break a run — it quietly widens the candidate pool
        every simulated day, which is a different strategy measured under the
        same name.
        """
        from unittest.mock import patch

        import src.backtesting.engine.simulator as simulator_module

        days, closes, exps = falling_then_flat
        put_calls, call_calls = [], []
        real_cls = simulator_module.OptionsScanner

        class RecordingScanner(real_cls):
            def scan_for_put_opportunities(self, *args, **kwargs):
                put_calls.append((args, kwargs))
                return super().scan_for_put_opportunities(*args, **kwargs)

            def scan_for_call_opportunities(self, *args, **kwargs):
                call_calls.append((args, kwargs))
                return super().scan_for_call_opportunities(*args, **kwargs)

        with patch.object(simulator_module, "OptionsScanner", RecordingScanner):
            result = _simulator("XYZ", closes, exps, days).run()

        assert len(put_calls) == len(result.daily) > 0, "the put scan did not run daily"
        assert len(call_calls) == len(result.daily), "the call scan did not run daily"
        assert all(a == () and k == {} for a, k in put_calls), (
            f"put scan was called with an override: "
            f"{[c for c in put_calls if c != ((), {})][:3]}"
        )
        assert all(a == () and k == {} for a, k in call_calls), (
            f"call scan was called with an override: "
            f"{[c for c in call_calls if c != ((), {})][:3]}"
        )


class TestNoOpAnalytics:
    def test_records_calls_and_returns_none(self):
        w = NoOpAnalyticsWriter()
        assert w.write_trade_event(symbol="XYZ", qty=1) is None
        assert w.anything_at_all("a") is None
        assert len(w.calls) == 2
        assert w.calls_named("write_trade_event")[0][2] == {"symbol": "XYZ", "qty": 1}


class TestRestrictSymbols:
    def test_narrows_universe_without_mutating_the_original(self):
        config = Config()
        original = list(config.stock_symbols)
        narrowed = restrict_symbols(config, ["NVDA"])
        assert narrowed.stock_symbols == ["NVDA"]
        assert config.stock_symbols == original


class TestAnalyticsIsolationIsReal:
    """Reviewers proved these paths were untested: mutating the analytics swap
    to a no-op left every simulator test green, meaning a replay writing
    simulated fills into the PRODUCTION analytics singleton would not be caught.
    """

    def test_noop_writer_is_installed_for_the_whole_run(self, falling_then_flat):
        """Assert the singleton is swapped *while the day loop runs*.

        Checked from inside the loop rather than by spying on
        get_analytics_writer: the strategy only reaches that call on certain
        branches, so a spy can pass vacuously by never firing. What matters is
        that any call made at any point during the run would land on the no-op.
        """
        days, closes, exps = falling_then_flat
        seen = []

        sim = _simulator("XYZ", closes, exps, days)
        original = sim._execute_opportunities

        def observing(exec_engine, put_seller, call_seller, opportunities, client):
            seen.append(type(analytics_module.get_analytics_writer()).__name__)
            return original(exec_engine, put_seller, call_seller,
                            opportunities, client)

        sim._execute_opportunities = observing
        sim.run()

        assert seen, "day loop never ran"
        assert set(seen) == {"NoOpAnalyticsWriter"}, (
            f"strategy code would have reached {set(seen)} — a real writer sends "
            "simulated trades to production analytics"
        )

    def test_singleton_restored_when_the_run_raises_AFTER_the_swap(self):
        """The old test raised ~40 lines BEFORE the swap, so the finally never ran."""
        days = _weekdays(date(2024, 6, 3), 10)
        closes = {d: 100.0 for d in days}
        exps = [d for d in days if d.weekday() == 4]
        before = analytics_module._instance

        sim = _simulator("XYZ", closes, exps, days)
        original = sim._execute_opportunities

        def boom(*a, **kw):
            raise RuntimeError("blew up inside the day loop")

        sim._execute_opportunities = boom
        with pytest.raises(RuntimeError, match="blew up inside the day loop"):
            sim.run()

        assert analytics_module._instance is before
        assert not clock.is_frozen()


class TestStrikeWindowCoversAssignedPositions:
    """FC-042 A2 at simulator scale.

    Chains are built for the whole window before the day loop starts, so there
    is no live position to read a cost basis from. The simulator bounds it
    instead — see ``Simulator._strike_anchors``.
    """

    def test_anchors_bracket_every_price_a_position_can_be_struck_against(
        self, falling_then_flat
    ):
        """The property, not the implementation: no strike can escape the window.

        Puts are sold at or below spot and assigned lots cost at most that
        strike, so every price any position is struck against on any decision
        day must lie between the two anchors.
        """
        days, closes, expirations = falling_then_flat
        sim = _simulator("XYZ", closes, expirations, days)
        bars = sim._load_stock_bars()["XYZ"]
        ceiling, floor = sim._strike_anchors(bars)

        decision_closes = [closes[d] for d in days]
        assert ceiling >= max(decision_closes)
        assert floor <= min(decision_closes)

    def test_no_bars_yields_no_anchors(self, falling_then_flat):
        days, closes, expirations = falling_then_flat
        sim = _simulator("XYZ", closes, expirations, days)
        assert sim._strike_anchors([]) == (None, None)

    def test_warmup_bars_cannot_inflate_the_ceiling(self, falling_then_flat):
        """A split inside the warm-up buffer must not neuter the strike filter.

        Warm-up bars are tolerated across a split (the simulator only warns),
        so NVDA's pre-split 1224.40 can sit in the same series as a ~180 spot.
        Reading it as a cost basis would push the window's upper bound ~7x too
        high and turn the filter into a no-op for the whole run.
        """
        days, closes, expirations = falling_then_flat
        sim = _simulator("XYZ", closes, expirations, days)
        bars = sim._load_stock_bars()["XYZ"]
        pre_split = StockBar("XYZ", days[0] - timedelta(days=40),
                             1224.40, 1224.40, 1224.40, 1224.40, 1_000_000)
        ceiling, _ = sim._strike_anchors([pre_split, *bars])
        assert ceiling < 200.0, "warm-up close leaked into the cost-basis ceiling"

    def test_an_open_short_put_survives_a_rally_out_of_the_window(
        self, falling_then_flat
    ):
        """The lower-bound mirror of the covered-call case.

        A put sold near 0.93x spot leaves a spot-centred window once the
        underlying rallies ~24%. The contract is then missing from the chain of
        a position that is still open, which marks it at 0.00 and makes it
        impossible to close — a winning early close silently becomes a
        hold-to-expiry.
        """
        days = _weekdays(date(2024, 6, 3), 30)
        warmup = _weekdays(date(2024, 3, 25), 45)
        closes = {d: 100.0 for d in warmup}
        for i, d in enumerate(days):  # 100 -> 160, a 60% rally
            closes[d] = 100.0 + min(i, 20) * 3.0
        expirations = [d for d in days if d.weekday() == 4]

        class WideLadderProvider(ScriptedProvider):
            def _ladder(self, spot):
                return [float(k) for k in range(50, 251)]

        provider = WideLadderProvider("XYZ", closes, expirations)
        sim = Simulator(
            Config(), provider, ChainBuilder(provider, risk_free_rate=0.04),
            ["XYZ"], days[0], days[-1], starting_cash=50_000.0, max_dte=7,
        )
        stock_bars = sim._load_stock_bars()
        chains = sim._build_chains(stock_bars, sim._trading_days(stock_bars))["XYZ"]

        early_strike = 93.0  # a put sold near the start, ~0.93x spot of 100
        peak_days = [d for d in chains if closes[d] == max(closes[x] for x in days)]
        assert peak_days
        assert max(closes[d] for d in days) * (1 - STRIKE_WINDOW_PCT) > early_strike, (
            "precondition: a spot-centred window would drop the open put"
        )
        for day in peak_days:
            assert [q for q in chains[day].puts if q.strike == early_strike], (
                f"{day}: the still-open short put fell out of the chain"
            )

    def test_calls_above_cost_basis_survive_a_60_percent_drawdown(
        self, falling_then_flat
    ):
        """The failure this exists to prevent, end to end.

        Price slides 100 -> 70 and a put assigned near 92 leaves the position
        well underwater. Every covered call the strategy may write is struck at
        or above ~92, but a spot-centred window on a 70 handle stops at 87.50 —
        so the eligible call ladder would come back empty and the replay would
        report a position that simply never rolled.
        """
        days, closes, expirations = falling_then_flat

        class WideLadderProvider(ScriptedProvider):
            """A full ladder, not one pinned to +/-12 around spot.

            ScriptedProvider's default ladder is itself narrower than the
            window under test, so with it this assertion would pass or fail on
            the mock's shape rather than on the filter's.
            """

            def _ladder(self, spot):
                return [float(k) for k in range(50, 151)]

        provider = WideLadderProvider("XYZ", closes, expirations)
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        sim = Simulator(
            Config(), provider, builder, ["XYZ"], days[0], days[-1],
            starting_cash=50_000.0, max_dte=7,
        )
        stock_bars = sim._load_stock_bars()
        chains = sim._build_chains(stock_bars, sim._trading_days(stock_bars))["XYZ"]

        trough = min(closes[d] for d in days)
        trough_days = [d for d in chains if closes.get(d) == trough]
        assert trough_days, "fixture must reach a trough inside the decision window"
        assert trough * (1 + STRIKE_WINDOW_PCT) < 92.0, (
            "precondition: a spot-centred window would clip the eligible calls"
        )

        for day in trough_days:
            above_basis = [q for q in chains[day].calls if q.strike >= 92.0]
            assert above_basis, (
                f"{day}: no covered call at or above cost basis in the chain"
            )


class TestTheCallLegActuallyRuns:
    """FC-048: every backtest before this modelled a put-only wheel.

    Covered-call opportunities carried 'strategy': 'sell_call' but no 'type',
    and ExecutionEngine routed on `opp.get('type', 'put')` — so every call was
    handed to put_seller and rejected. No test caught it because the golden
    fixture could not reach the call path at all.
    """

    def test_a_covered_call_is_sold_after_assignment(self, dip_then_recovering):
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        kinds = [e.kind for e in result.broker.ledger]
        assert "put_assignment" in kinds, f"fixture never assigned; ledger={kinds}"
        assert "sell_call_open" in kinds, (
            f"assigned shares but NO covered call was sold — the FC-048 "
            f"misroute, or a gate blocking the call leg. ledger={kinds}"
        )
        # The call must come after the assignment that created the shares.
        assert kinds.index("sell_call_open") > kinds.index("put_assignment")

    def test_no_call_is_sold_below_cost_basis(self, dip_then_recovering):
        """Enabling the call leg must not enable selling below basis.

        Re-based by FC-068 from ``event.price`` (the strike, and the *cash*
        number) onto ``detail['basis']`` (the premium-netted lot basis, and the
        number the floor actually gates on). The two are deliberately different
        now: netting the premium into ``event.price`` would double-count it in
        every assigned cycle, so both ride the event explicitly.

        Note this is the *weaker* of the two assertions after netting — the
        netted basis is lower, so a strike passing here would also have passed
        the old strike-based test. That is correct: the point is to assert the
        floor production enforces, not a stricter one the backtest invented.
        """
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        assigns = [e for e in result.broker.ledger if e.kind == "put_assignment"]
        calls = [e for e in result.broker.ledger if e.kind == "sell_call_open"]
        if not (assigns and calls):
            pytest.skip("covered by test_a_covered_call_is_sold_after_assignment")
        basis = assigns[0].detail["basis"]
        assert basis < assigns[0].price, (
            "basis is not premium-netted — FC-068's broker change is missing"
        )
        for c in calls:
            strike = float(c.symbol[-8:]) / 1000.0
            assert strike >= basis, (
                f"covered call struck at {strike} below cost basis {basis}"
            )

    def test_attribution_conserves_through_a_full_netted_cycle(self, dip_then_recovering):
        """The double-count guard for FC-068's premium netting.

        `option_pnl` counts the put premium at `sell_to_open`; `stock_pnl` is
        booked against the cycle's cost basis, which `metrics/cycles.py`
        derives from `event.price`. If the netting had been pushed into
        `event.price` or `cash_delta` instead of the lot basis, the premium
        would be counted twice — once as option P&L, again through a lowered
        stock basis — and the attribution would stop reconciling to the equity
        change. That is exactly what this asserts.
        """
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        cycles = build_cycles(result.broker.ledger)
        assert any(c.called_away for c in cycles), (
            "no completed wheel — the double-count would not be reachable"
        )
        report = compute_fitness(
            "XYZ", result.daily, cycles, result.starting_cash,
            benchmark_prices={d: closes[d] for d in days},
            data_quality={"decision_days": len(result.daily)},
        )
        assert report.reconciliation_gap == pytest.approx(0.0, abs=0.01), (
            f"attribution does not reconcile to the equity change "
            f"(gap={report.reconciliation_gap}) — a premium double-count looks "
            f"exactly like this"
        )

    def test_no_opportunity_dies_at_the_router(self, dip_then_recovering):
        """Guards a half-fix: calls found but still rejected at execution."""
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        bad = {k: v for k, v in result.rejections.items()
               if "wrong_seller" in k or "unroutable" in k}
        assert not bad, f"opportunities died at the router: {bad}"

    def test_the_wheel_completes_a_full_cycle(self, dip_then_recovering):
        """The FC-032-planned golden path, finally real.

        put sold -> assigned -> covered call written -> called away. Before
        FC-048 the ledger stopped at the assignment on every symbol, in every
        window, in every backtest this project has ever run.
        """
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        kinds = [e.kind for e in result.broker.ledger]
        for expected in ("sell_put_open", "put_assignment",
                         "sell_call_open", "call_assignment"):
            assert expected in kinds, (
                f"incomplete wheel: {expected!r} missing from ledger={kinds}"
            )
        # And in that order.
        assert (kinds.index("sell_put_open") < kinds.index("put_assignment")
                < kinds.index("sell_call_open") < kinds.index("call_assignment"))

    def test_the_roll_seat_is_occupied_daily_and_every_roll_nets_a_credit(
            self, dip_then_recovering):
        """T-15 — the FC-068 tripwire, FIRED.

        This test used to assert ``rolls_executed == 0``. That was the whole
        point of it: the seat was retained because the production ``/roll``
        scheduler invokes this exact code, and the assertion made the seat
        load-bearing instead of silent, so that reviving the roller would flip a
        test rather than quietly change every backtest number.

        FC-078 is that revival, and the flip is intentional. Two things changed
        together: the replay's cadence is now **daily**, mirroring production
        (a Friday-only replay of a daily roller would misstate roll frequency
        and credit capture in every future measurement), and the roller can
        actually execute (the quote-key mismatch that made it a guaranteed
        no-op is fixed).

        So the assertion moves from "it never fires" to what actually protects
        money now: **every executed roll in a replay satisfied the credit
        invariant and increased the strike.** A replay that rolls at a debit, or
        rolls down, is the regression worth catching — not a replay that rolls.

        *Mutation:* restore the Friday-only guard in the simulator → the daily
        cadence assertion fails.
        """
        days, closes, exps = dip_then_recovering
        result = _simulator("XYZ", closes, exps, days).run()

        assert result.rolls_evaluated > 0, (
            "the roll seat is empty: run_rolling_cycle() is not being called, "
            "or it is still gated to one weekday")
        # Daily cadence: with a Friday-only guard the evaluation count cannot
        # exceed the number of Fridays in the window.
        fridays = sum(1 for d in days if d.weekday() == 4)
        assert result.rolls_evaluated > fridays, (
            f"only {result.rolls_evaluated} evaluations over {len(days)} "
            f"sessions ({fridays} Fridays) — the replay is still Friday-only")

        for record in result.roll_records:
            assert record['new_strike'] > record['old_strike'], (
                f"a replay roll went sideways or down: {record}")
            assert record['net_credit'] >= 0, (
                f"a replay roll netted a DEBIT — the credit invariant is not "
                f"holding end to end: {record}")

    def test_no_dead_path_events_in_replay(self, dip_then_recovering):
        """A half-deletion would leave the engine path partly alive.

        None of these event types has an emitter after FC-068: the drawdown
        pause (deleted per FC-065 OQ-3), stages 2/4/5/6 (gap filter, wheel
        state, the engine's duplicate guard), and the two put-seller events
        whose home was ``find_put_opportunity``. Seeing any of them in a replay
        means something was resurrected or never removed.
        """
        days, closes, exps = dip_then_recovering
        seen = []

        import structlog

        def capture(_logger, _name, event_dict):
            et = event_dict.get("event_type")
            if et:
                seen.append(et)
            return event_dict

        prev = structlog.get_config()
        structlog.configure(processors=[capture] + list(prev.get("processors", [])))
        try:
            result = _simulator("XYZ", closes, exps, days).run()
        finally:
            structlog.configure(**prev)

        assert result.broker.ledger, "fixture never traded"
        dead = {
            "covered_call_drawdown_pause",
            "covered_call_quote_missing",
            "stock_filtered_by_gap_risk",
            "rejected_high_gap_frequency",
            "stage_4_blocked",
            "stage_4_passed",
            "stage_5_check",
            "stage_5_blocked",
            "stage_6_blocked",
            "stage_6_passed",
            "stage_3_no_limit",
            "stage_9_limit_reached",
            "no_suitable_puts",
            "position_size_validation_failed",
            "put_blocked_by_wheel_state",
            "strategy_cycle_started",
            "strategy_cycle_completed",
            "call_opportunity_evaluation",
        }
        assert not (dead & set(seen)), (
            f"dead engine-path events still emitted in a replay: "
            f"{sorted(dead & set(seen))}"
        )
        # And the replay is not vacuously silent — the live vocabulary is there.
        assert "stage_1_complete" in seen or "stock_rejected_filter" in seen
