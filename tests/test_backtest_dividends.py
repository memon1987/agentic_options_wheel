"""Dividends and ex-dividend early assignment (FC-042 Track C).

Two things are being defended here. First, that dividends reach BOTH legs — the
wheel via the broker ledger and the buy-and-hold benchmark via the same table —
because crediting only one flips the old bias instead of removing it. Second,
that early assignment fires on the actual economic condition (extrinsic below
the dividend) rather than on "is ITM", which would assign every ITM call every
quarter.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.dividends import (
    DIVIDEND_TABLE_PATH,
    ITM_THRESHOLD,
    Dividend,
    DividendSchedule,
    should_assign_early,
)
from src.backtesting.engine import broker as broker_module
from src.backtesting.engine.broker import BacktestBroker
from src.backtesting.metrics.cycles import build_cycles
from src.backtesting.metrics.fitness import BuyAndHold


def _schedule(**by_symbol) -> DividendSchedule:
    return DividendSchedule(
        {
            symbol: [Dividend(ex_date=d, amount=a) for d, a in rows]
            for symbol, rows in by_symbol.items()
        }
    )


class TestDividendSchedule:
    def test_amount_on_returns_the_rate_for_an_ex_date(self):
        s = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        assert s.amount_on("KMI", date(2025, 1, 30)) == pytest.approx(0.2875)

    def test_amount_on_is_zero_off_the_ex_date(self):
        s = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        assert s.amount_on("KMI", date(2025, 1, 29)) == 0.0
        assert s.amount_on("KMI", date(2025, 1, 31)) == 0.0

    def test_unknown_symbol_pays_nothing_and_is_recorded(self):
        s = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        assert s.amount_on("NVDA", date(2025, 1, 30)) == 0.0
        assert "NVDA" in s.symbols_without_data

    def test_a_regular_and_a_special_on_the_same_day_are_summed(self):
        """F pays supplementals; taking only the first would silently short-pay."""
        s = DividendSchedule(
            {
                "F": [
                    Dividend(date(2025, 2, 18), 0.15, special=False),
                    Dividend(date(2025, 2, 18), 0.15, special=True),
                ]
            }
        )
        assert s.amount_on("F", date(2025, 2, 18)) == pytest.approx(0.30)

    def test_total_between_excludes_the_entry_day(self):
        """A buyer at the entry close does not receive a dividend going ex that day.

        Off-by-one here hands buy-and-hold a free quarter on exactly the
        high-yield names this machinery exists to judge.
        """
        s = _schedule(VZ=[(date(2025, 1, 10), 0.6775), (date(2025, 4, 10), 0.6775)])
        total = s.total_between("VZ", date(2025, 1, 10), date(2025, 6, 30))
        assert total == pytest.approx(0.6775)

    def test_total_between_includes_the_exit_day(self):
        """A seller at the exit close held through the prior close and is on the record."""
        s = _schedule(VZ=[(date(2025, 4, 10), 0.6775)])
        total = s.total_between("VZ", date(2025, 1, 1), date(2025, 4, 10))
        assert total == pytest.approx(0.6775)

    def test_empty_schedule_pays_nothing(self):
        s = DividendSchedule.empty()
        assert s.amount_on("KMI", date(2025, 1, 30)) == 0.0
        assert s.total_between("KMI", date(2025, 1, 1), date(2025, 12, 31)) == 0.0


class TestCommittedTable:
    """The shipped table itself — these guard how it was BUILT, not just parsed."""

    def test_table_loads(self):
        s = DividendSchedule.from_table()
        assert s.has_symbol("KMI")
        assert len(s.all_for("KMI")) > 0

    def test_amounts_are_not_split_adjusted(self):
        """NVDA's pre-split dividend must read $0.04, not yfinance's $0.004.

        The engine replays unadjusted bars against as-listed strikes, so a
        split-adjusted dividend table would be in different units from the share
        counts it is multiplied by. yfinance back-adjusts through NVDA's June
        2024 10:1 split and would report 0.004 here. If someone ever rebuilds
        this table from yfinance, this test is what catches it.
        """
        s = DividendSchedule.from_table()
        pre_split = [d for d in s.all_for("NVDA") if d.ex_date < date(2024, 6, 10)]
        assert pre_split, "expected pre-split NVDA dividends in the table"
        for d in pre_split:
            assert d.amount == pytest.approx(0.04), (
                f"NVDA {d.ex_date} reads {d.amount}; 0.004 means the table was "
                f"rebuilt from a split-adjusted source"
            )

    def test_confirmed_non_payers_are_present_but_empty(self):
        """'Pays nothing' must be distinguishable from 'never fetched'."""
        payload = json.load(open(DIVIDEND_TABLE_PATH))
        assert payload["dividends"]["AMD"] == []
        assert payload["dividends"]["AMZN"] == []

    def test_etf_distributions_are_included(self):
        s = DividendSchedule.from_table()
        for etf in ("SPY", "QQQ", "IWM"):
            assert len(s.all_for(etf)) > 0, f"{etf} distributions missing"

    def test_trailing_edge_is_not_truncated_by_process_date(self):
        """Alpaca filters corporate actions on PROCESS date, not ex-date.

        The two can be six weeks apart, so querying the endpoint with the
        ex-date range you actually want silently drops dividends that went ex
        inside the window but settle after it. The first build of this table did
        exactly that and lost VZ 2026-07-10 (a full quarter, ~1.7% of price) and
        SPY 2026-06-18 ($1.90/share).

        That error is not symmetric: buy-and-hold holds shares every session and
        the wheel does not, so a missing dividend costs the benchmark more than
        the wheel — it FLATTERS the wheel, the exact bias this table removes.
        These two rows are the regression pins.
        """
        s = DividendSchedule.from_table()
        vz = {d.ex_date: d.amount for d in s.all_for("VZ")}
        assert date(2026, 7, 10) in vz, (
            "VZ 2026-07-10 missing — the fetch window was not padded past the "
            "process date, so the table is truncated at its trailing edge"
        )
        assert vz[date(2026, 7, 10)] == pytest.approx(0.7075)

        spy = {d.ex_date: d.amount for d in s.all_for("SPY")}
        assert date(2026, 6, 18) in spy, "SPY 2026-06-18 missing (process date 2026-07-31)"

    def test_no_ex_date_precedes_the_declared_since(self):
        """`since` must be a bound the table honours, not a claim about the query."""
        payload = json.load(open(DIVIDEND_TABLE_PATH))
        since = datetime.strptime(payload["since"], "%Y-%m-%d").date()
        until = datetime.strptime(payload["until"], "%Y-%m-%d").date()
        for symbol, rows in payload["dividends"].items():
            for row in rows:
                ex = datetime.strptime(row["ex_date"], "%Y-%m-%d").date()
                assert since <= ex <= until, f"{symbol} {ex} outside [{since}, {until}]"

    def test_income_names_have_plausible_yields(self):
        """A sanity floor: these are the names the whole track exists to judge."""
        s = DividendSchedule.from_table()
        for symbol in ("KMI", "VZ", "F", "PFE"):
            total = s.total_between(symbol, date(2025, 1, 1), date(2025, 12, 31))
            assert total > 0.5, f"{symbol} 2025 dividends look wrong: {total}"


class TestEarlyAssignmentRule:
    """extrinsic < dividend, and nothing else."""

    def test_fires_when_extrinsic_is_below_the_dividend(self):
        # Spot 30, strike 28 -> intrinsic 2.00. Mark 2.05 -> extrinsic 0.05.
        assert should_assign_early(
            strike=28.0, underlying_price=30.0, option_mark=2.05, dividend=0.2875
        )

    def test_does_not_fire_when_extrinsic_exceeds_the_dividend(self):
        # Same position, mark 2.50 -> extrinsic 0.50, well above the dividend.
        assert not should_assign_early(
            strike=28.0, underlying_price=30.0, option_mark=2.50, dividend=0.2875
        )

    def test_does_not_fire_on_an_otm_call(self):
        """No intrinsic to exercise into; the holder would be burning money."""
        assert not should_assign_early(
            strike=32.0, underlying_price=30.0, option_mark=0.10, dividend=0.2875
        )

    def test_does_not_fire_without_a_dividend(self):
        assert not should_assign_early(
            strike=28.0, underlying_price=30.0, option_mark=2.00, dividend=0.0
        )

    def test_at_the_money_by_less_than_a_cent_is_not_itm(self):
        assert not should_assign_early(
            strike=30.0, underlying_price=30.005, option_mark=0.0, dividend=0.2875
        )

    def test_itm_threshold_matches_the_broker(self):
        """Duplicated to keep `data` from importing upward into `engine`."""
        assert ITM_THRESHOLD == broker_module.ITM_THRESHOLD


class TestBrokerDividendCredit:
    def test_credits_only_shares_actually_held(self):
        b = BacktestBroker(starting_cash=10_000.0)
        assert b.credit_dividend("KMI", 0.2875, date(2025, 1, 30)) == 0.0
        assert not [e for e in b.ledger if e.kind == "dividend"]

    def test_dividend_reaches_the_cycle_table(self):
        """The ledger event must land in cycle.dividends, or attribution breaks."""
        b = BacktestBroker(starting_cash=10_000.0)
        b.sell_put_to_open(
            "KMI250117P00028000", "KMI", 28.0, date(2025, 1, 17), 1,
            mark=0.60, bid=0.55, opened=date(2025, 1, 10),
        )
        b.settle_expirations(date(2025, 1, 17), {"KMI": 27.0})  # assigned
        credited = b.credit_dividend("KMI", 0.2875, date(2025, 1, 30))
        assert credited == pytest.approx(28.75)  # 100 shares

        cycles = build_cycles(b.ledger)
        assert sum(c.dividends for c in cycles) == pytest.approx(28.75)

    def test_a_dividend_does_not_close_an_open_cycle(self):
        """Shares are still held, so the name is not flat."""
        b = BacktestBroker(starting_cash=10_000.0)
        b.sell_put_to_open(
            "KMI250117P00028000", "KMI", 28.0, date(2025, 1, 17), 1,
            mark=0.60, bid=0.55, opened=date(2025, 1, 10),
        )
        b.settle_expirations(date(2025, 1, 17), {"KMI": 27.0})
        b.credit_dividend("KMI", 0.2875, date(2025, 1, 30))
        cycles = build_cycles(b.ledger)
        assert len(cycles) == 1
        assert cycles[0].is_open


class TestBenchmarkDividends:
    def test_benchmark_collects_the_dividend_stream(self):
        b = BuyAndHold(
            shares=1000, entry_price=28.0, exit_price=28.0,
            starting_cash=28_000.0, dividends_per_share=1.165,
        )
        assert b.dividends == pytest.approx(1165.0)
        assert b.final_value == pytest.approx(29_165.0)
        # Flat price, so the entire return is the dividend.
        assert b.price_return == pytest.approx(0.0)
        assert b.total_return == pytest.approx(1165.0 / 28_000.0)

    def test_price_return_isolates_capital_appreciation(self):
        b = BuyAndHold(
            shares=100, entry_price=100.0, exit_price=110.0,
            starting_cash=10_000.0, dividends_per_share=2.0,
        )
        assert b.price_return == pytest.approx(0.10)
        assert b.total_return == pytest.approx(0.12)

    def test_default_is_no_dividends(self):
        """Callers that never pass a stream must behave exactly as before."""
        b = BuyAndHold(
            shares=100, entry_price=100.0, exit_price=110.0, starting_cash=10_000.0
        )
        assert b.dividends == 0.0
        assert b.total_return == pytest.approx(b.price_return)


def _call_quote(symbol: str, strike: float, spot: float, mark: float, day: date):
    return ChainQuote(
        symbol=symbol, underlying="KMI", as_of=day, expiration=date(2025, 2, 21),
        strike=strike, option_type="call", dte=22, underlying_price=spot,
        mark=mark, bid=mark - 0.02, ask=mark + 0.02,
        implied_volatility=0.25, delta=0.8, volume=100,
    )


class TestSimulatorEarlyAssignment:
    """The scripted ex-div scenario, driven through the simulator's own helper."""

    OCC = "KMI250221C00028000"
    EVE = date(2025, 1, 29)
    EX = date(2025, 1, 30)

    def _sim(self, dividend_amount: float = 0.2875):
        from src.backtesting.engine.simulator import Simulator

        sim = Simulator.__new__(Simulator)  # no data fetching needed
        sim.symbols = ["KMI"]
        sim.dividends = _schedule(KMI=[(self.EX, dividend_amount)])
        sim._early_assignments = 0
        sim._unpriced_ex_div_calls = 0
        return sim

    def _broker_holding_a_covered_call(self):
        b = BacktestBroker(starting_cash=10_000.0)
        b._add_stock("KMI", 100, 27.0, date(2025, 1, 17))
        b.sell_call_to_open(
            self.OCC, "KMI", 28.0, date(2025, 2, 21), 1,
            mark=2.05, bid=2.00, opened=date(2025, 1, 20),
        )
        return b

    def _chains(self, mark: float, spot: float):
        quote = _call_quote(self.OCC, 28.0, spot, mark, self.EVE)
        return {
            "KMI": {
                self.EVE: ChainSnapshot(
                    underlying="KMI", as_of=self.EVE,
                    underlying_price=spot, puts=[], calls=[quote],
                )
            }
        }

    def test_assigned_when_extrinsic_is_below_the_dividend(self):
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        # Spot 30 vs strike 28 -> intrinsic 2.00; mark 2.05 -> extrinsic 0.05.
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=2.05, spot=30.0), {"KMI": 30.0}, self.EVE, self.EX
        )
        assert sim._early_assignments == 1
        assert self.OCC not in b.options
        assert b.shares("KMI") == 0
        assigned = [e for e in b.ledger if e.kind == "call_assignment"]
        assert assigned and assigned[0].detail["reason"] == "ex_dividend"
        assert assigned[0].event_date == self.EVE

    def test_not_assigned_when_extrinsic_exceeds_the_dividend(self):
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        # mark 2.50 -> extrinsic 0.50, above the 0.2875 dividend.
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=2.50, spot=30.0), {"KMI": 30.0}, self.EVE, self.EX
        )
        assert sim._early_assignments == 0
        assert self.OCC in b.options
        assert b.shares("KMI") == 100

    def test_not_assigned_when_the_call_is_otm(self):
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=0.10, spot=27.0), {"KMI": 27.0}, self.EVE, self.EX
        )
        assert sim._early_assignments == 0
        assert self.OCC in b.options

    def test_no_ex_date_ahead_means_no_assignment(self):
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=2.05, spot=30.0), {"KMI": 30.0},
            date(2025, 2, 3), date(2025, 2, 4),  # the ex-date is long past
        )
        assert sim._early_assignments == 0
        assert self.OCC in b.options

    def test_an_ex_date_between_two_sessions_still_triggers(self):
        """The last session before the ex-date is the last chance to act.

        If the ex-date falls on a day with no session (or no bar for this
        symbol), the decision still has to be taken on the prior session —
        otherwise the wheel keeps shares a real account would have lost AND
        collects the dividend, which is the optimistic bias twice over.
        """
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=2.05, spot=30.0), {"KMI": 30.0},
            self.EVE, date(2025, 1, 31),  # spans the Jan 30 ex-date
        )
        assert sim._early_assignments == 1
        assert self.OCC not in b.options

    def test_an_unpriced_itm_call_is_left_alone_but_counted(self):
        """No mark means no extrinsic; assigning on silence would fabricate a trade."""
        sim = self._sim()
        b = self._broker_holding_a_covered_call()
        empty_chain = {
            "KMI": {
                self.EVE: ChainSnapshot(
                    underlying="KMI", as_of=self.EVE,
                    underlying_price=30.0, puts=[], calls=[],
                )
            }
        }
        sim._assign_calls_before_ex_dividend(
            b, empty_chain, {"KMI": 30.0}, self.EVE, self.EX
        )
        assert sim._early_assignments == 0
        assert sim._unpriced_ex_div_calls == 1
        assert self.OCC in b.options

    def test_a_put_is_never_early_assigned_for_a_dividend(self):
        """Only the call holder captures a dividend by exercising."""
        sim = self._sim()
        b = BacktestBroker(starting_cash=10_000.0)
        put = "KMI250221P00028000"
        b.sell_put_to_open(
            put, "KMI", 28.0, date(2025, 2, 21), 1,
            mark=0.10, bid=0.08, opened=date(2025, 1, 20),
        )
        sim._assign_calls_before_ex_dividend(
            b, self._chains(mark=2.05, spot=30.0), {"KMI": 30.0}, self.EVE, self.EX
        )
        assert sim._early_assignments == 0
        assert put in b.options


class TestSimulatorDividendCredit:
    def test_credits_on_the_ex_date_only(self):
        from src.backtesting.engine.simulator import Simulator

        sim = Simulator.__new__(Simulator)
        sim.symbols = ["KMI"]
        sim.dividends = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])

        b = BacktestBroker(starting_cash=10_000.0)
        b._add_stock("KMI", 200, 27.0, date(2025, 1, 17))

        sim._credit_dividends(b, date(2025, 1, 29), date(2025, 1, 28))
        assert not [e for e in b.ledger if e.kind == "dividend"]

        sim._credit_dividends(b, date(2025, 1, 30), date(2025, 1, 29))
        events = [e for e in b.ledger if e.kind == "dividend"]
        assert len(events) == 1
        assert events[0].cash_delta == pytest.approx(57.5)  # 200 * 0.2875

    def test_no_shares_means_no_credit(self):
        from src.backtesting.engine.simulator import Simulator

        sim = Simulator.__new__(Simulator)
        sim.symbols = ["KMI"]
        sim.dividends = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        b = BacktestBroker(starting_cash=10_000.0)
        sim._credit_dividends(b, date(2025, 1, 30), date(2025, 1, 29))
        assert b.cash == pytest.approx(10_000.0)

    def test_an_ex_date_on_a_missing_session_is_still_credited(self):
        """The wheel must collect every dividend the benchmark collects.

        ``days`` only contains sessions this symbol has a bar for. An ex-date
        landing on a session it did not trade is absent from that list, and an
        exact-date match would drop it from the wheel while the benchmark's
        total_between still counted it — putting the two legs back on different
        dividend streams, which is exactly the asymmetry this track removes.
        """
        from src.backtesting.engine.simulator import Simulator

        sim = Simulator.__new__(Simulator)
        sim.symbols = ["KMI"]
        sim.dividends = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        b = BacktestBroker(starting_cash=10_000.0)
        b._add_stock("KMI", 100, 27.0, date(2025, 1, 17))

        # Jan 30 is not a session here: the loop steps Jan 29 -> Jan 31.
        sim._credit_dividends(b, date(2025, 1, 31), date(2025, 1, 29))
        events = [e for e in b.ledger if e.kind == "dividend"]
        assert len(events) == 1
        assert events[0].cash_delta == pytest.approx(28.75)

    def test_a_dividend_is_never_credited_twice(self):
        """Consecutive intervals must not overlap at the boundary."""
        from src.backtesting.engine.simulator import Simulator

        sim = Simulator.__new__(Simulator)
        sim.symbols = ["KMI"]
        sim.dividends = _schedule(KMI=[(date(2025, 1, 30), 0.2875)])
        b = BacktestBroker(starting_cash=10_000.0)
        b._add_stock("KMI", 100, 27.0, date(2025, 1, 17))

        sim._credit_dividends(b, date(2025, 1, 30), date(2025, 1, 29))
        sim._credit_dividends(b, date(2025, 1, 31), date(2025, 1, 30))
        assert len([e for e in b.ledger if e.kind == "dividend"]) == 1


class TestReportRendersEveryVerdict:
    """render_markdown crashed on `insufficient` — the income names' usual verdict.

    Pre-existing on main: the badge lookup was a bare dict index over three of
    the four verdicts `FitnessReport.verdict()` can return. Any symbol that
    closed no cycle inside the window rendered a KeyError instead of a report,
    which is exactly F/PFE/KMI/VZ — the names Track C exists to make judgeable.
    """

    def _report(self, closed_cycle: bool):
        from src.backtesting.engine.simulator import DailyState
        from src.backtesting.metrics.fitness import compute_fitness

        days = [date(2025, 1, 2), date(2025, 6, 30)]
        daily = [
            DailyState(day=d, equity=eq, cash=eq, reserved_collateral=2800.0,
                       open_options=1, shares_held={})
            for d, eq in zip(days, (100_000.0, 103_000.0))
        ]
        cycles = []
        if closed_cycle:
            from src.backtesting.metrics.cycles import WheelCycle
            cycles = [WheelCycle(underlying="VZ", start=days[0], end=days[1],
                                 option_pnl=3000.0, max_collateral=2800.0)]
        return compute_fitness(
            "VZ", daily, cycles, 100_000.0,
            benchmark_prices={days[0]: 40.0, days[1]: 43.0},
            benchmark_dividends_per_share=1.355,
            data_quality={"decision_days": 2},
        )

    def test_insufficient_verdict_renders(self):
        from src.backtesting.reporting.report import render_markdown

        report = self._report(closed_cycle=False)
        assert report.verdict() == "insufficient"
        md = render_markdown(report)  # used to raise KeyError
        assert "INSUFFICIENT EVIDENCE" in md

    def test_benchmark_dividends_are_shown_to_the_reader(self):
        """The number must reach the page, not just the dataclass."""
        from src.backtesting.reporting.report import render_markdown

        md = render_markdown(self._report(closed_cycle=True))
        # $100k // $40 = 2500 whole shares, * $1.355 = $3,387.50, rendered to $0dp.
        assert "$3,388** of dividends" in md
        assert "$1.3550/share over 2500 shares" in md
        # And the price-only return is kept visible beside it, so a reader can
        # see how much of buy-and-hold's result is the dividend stream.
        assert "price return alone was +7.50%" in md

    def test_every_verdict_string_has_a_badge(self):
        from src.backtesting.reporting.report import render_markdown

        report = self._report(closed_cycle=True)
        for verdict in ("fit", "marginal", "unfit", "insufficient"):
            report.verdict = lambda v=verdict: v
            render_markdown(report)  # must not raise


class TestEvaluateWiring:
    """`evaluate._score` is the ONLY production path that feeds both legs.

    The reviewer's finding: zeroing `bench_divs` in `_score` — deleting the
    benchmark's entire dividend stream, the single change Track C exists to make
    — passed the whole suite, because every other test hand-passes
    `benchmark_dividends_per_share` into `compute_fitness` and so pins the
    arithmetic while leaving the wiring untested. These tests pin the wiring.
    """

    def _result(self, symbol="VZ", start=date(2025, 1, 2), end=date(2025, 6, 30)):
        from src.backtesting.engine.simulator import DailyState, SimulationResult
        from src.backtesting.engine.broker import BacktestBroker

        daily = [
            DailyState(day=d, equity=eq, cash=eq, reserved_collateral=2800.0,
                       open_options=0, shares_held={})
            for d, eq in ((start, 100_000.0), (end, 101_000.0))
        ]
        return SimulationResult(
            symbols=[symbol], start=start, end=end, starting_cash=100_000.0,
            daily=daily, broker=BacktestBroker(starting_cash=100_000.0),
        )

    class _Provider:
        def __init__(self, prices):
            self._prices = prices

        def get_stock_bars(self, symbol, start, end):
            from src.backtesting.data.provider import StockBar
            return [
                StockBar(symbol=symbol, bar_date=d, open=p, high=p, low=p,
                         close=p, volume=1_000_000)
                for d, p in self._prices.items()
            ]

        def bars(self, symbol="VZ"):
            """The bars `_score` now takes directly (FC-060 Layer 2, D6).

            `_score` used to fetch them from a provider; it is handed the
            already-materialised bars instead, so the scoring pass makes no
            network call of its own. These tests pin dividend *wiring*, which
            is unchanged — only where the price series comes from moved.
            """
            return self.get_stock_bars(symbol, date.min, date.max)

    def test_benchmark_dividends_are_actually_wired_from_the_table(self):
        """The number must come from the schedule, not from a default of zero."""
        from src.backtesting.evaluate import _score

        result = self._result()
        provider = self._Provider({date(2025, 1, 2): 40.0, date(2025, 6, 30): 43.0})
        report = _score("VZ", result, provider.bars(), 100_000.0,
                        DividendSchedule.from_table())

        assert report.benchmark is not None
        assert report.benchmark.dividends_per_share > 0, (
            "the benchmark collected no dividends on a known payer — the "
            "schedule is not reaching compute_fitness"
        )
        assert report.benchmark.dividends > 0

    def test_the_benchmark_interval_matches_the_prices_it_is_valued_on(self):
        """`total_between` and `_buy_and_hold` must span the SAME days.

        They agree today by construction, not by contract: one is given
        result.start/result.end, the other reads daily[0].day/daily[-1].day.
        If those ever diverge the benchmark collects a dividend stream from one
        window and a price move from another.
        """
        from src.backtesting.evaluate import _score

        start, end = date(2025, 1, 2), date(2025, 6, 30)
        result = self._result(start=start, end=end)
        provider = self._Provider({start: 40.0, end: 43.0})
        schedule = DividendSchedule.from_table()
        report = _score("VZ", result, provider.bars(), 100_000.0, schedule)

        assert report.benchmark.dividends_per_share == pytest.approx(
            schedule.total_between("VZ", result.daily[0].day, result.daily[-1].day)
        )

    def test_a_symbol_absent_from_the_table_is_reported_not_silent(self):
        """$0 dividends must not read as 'this symbol pays nothing'."""
        from src.backtesting.evaluate import _score

        result = self._result(symbol="ZZZZ")
        provider = self._Provider({date(2025, 1, 2): 40.0, date(2025, 6, 30): 43.0})
        report = _score("ZZZZ", result, provider.bars(), 100_000.0,
                        DividendSchedule.from_table())

        coverage = report.data_quality["dividend_coverage"]
        assert coverage["symbol_in_table"] is False
        assert "NOT IN THE DIVIDEND TABLE" in coverage["status"]

    def test_an_unloaded_table_is_reported_not_silent(self):
        from src.backtesting.evaluate import _score

        result = self._result()
        provider = self._Provider({date(2025, 1, 2): 40.0, date(2025, 6, 30): 43.0})
        report = _score("VZ", result, provider.bars(), 100_000.0, DividendSchedule.empty())

        coverage = report.data_quality["dividend_coverage"]
        assert coverage["table_loaded"] is False
        assert "NO TABLE LOADED" in coverage["status"]

    def test_a_window_past_the_tables_coverage_is_reported(self):
        from src.backtesting.evaluate import _score

        start, end = date(2025, 1, 2), date(2099, 1, 1)
        result = self._result(start=start, end=end)
        provider = self._Provider({start: 40.0, end: 43.0})
        report = _score("VZ", result, provider.bars(), 100_000.0,
                        DividendSchedule.from_table())

        coverage = report.data_quality["dividend_coverage"]
        assert coverage["window_exceeds_table"] is True
        assert "covers ex-dates only through" in coverage["status"]

    def test_incomplete_coverage_is_shouted_in_the_rendered_report(self):
        """It has to reach the page, not just the dict."""
        from src.backtesting.evaluate import _score
        from src.backtesting.reporting.report import render_markdown

        result = self._result(symbol="ZZZZ")
        provider = self._Provider({date(2025, 1, 2): 40.0, date(2025, 6, 30): 43.0})
        report = _score("ZZZZ", result, provider.bars(), 100_000.0,
                        DividendSchedule.from_table())
        md = render_markdown(report)
        assert "Dividend data is incomplete for this run" in md

    def test_complete_coverage_does_not_shout(self):
        from src.backtesting.evaluate import _score
        from src.backtesting.reporting.report import render_markdown

        result = self._result()
        provider = self._Provider({date(2025, 1, 2): 40.0, date(2025, 6, 30): 43.0})
        report = _score("VZ", result, provider.bars(), 100_000.0,
                        DividendSchedule.from_table())
        md = render_markdown(report)
        assert "Dividend data is incomplete for this run" not in md
