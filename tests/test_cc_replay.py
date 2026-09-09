"""FC-096 Phase C — the covered-call selector, end to end.

Every test here drives the REAL pipeline on the covered-call profile: the real
`OptionsScanner` -> `ExecutionEngine` -> `CallSeller`, the real `CallRoller`
through `WheelEngine.run_rolling_cycle`, the real `should_close_call_early`. The
only inventions are the price path and the chain, which come from
`test_backtest_simulator`'s `ScriptedProvider` — the same canned data the golden
wheel replay uses, so a difference between the two paths is a difference in the
strategy and not in the fixture.

The three things this file exists to prove, because each of them was a defect
the plan's round-1 reviews found in the design rather than in the code:

1. The lot is an ASSUMPTION, not income. A no-trade window returns 0.0%.
2. `insufficient` does not invert for covered calls: a lot that was never
   called away is the programme working, not an unmeasurable window.
3. The replay's roller runs, at the profile's own 1.00 trigger, and its rolls
   are split into defences and roll-outs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.backtesting.data.chain_builder import ChainBuilder
from src.backtesting.data.dividends import DividendSchedule
from src.backtesting.engine.broker import BacktestBroker
from src.backtesting.engine.simulator import (
    CC_CASH_FLOAT,
    COVERAGE_COVERED,
    COVERAGE_HOLD_UNCOVERED,
    COVERAGE_NOT_A_STAND_DOWN,
    COVERAGE_POST_CALL_AWAY,
    SYNTHETIC_LOT_SHARES,
    Simulator,
    SyntheticLotPolicy,
    replay_strategy,
    restrict_symbols,
    spread_gate_would_suspend,
)
from src.backtesting.evaluate import _score
from src.backtesting.metrics.cycles import build_cycles
from src.utils.config import Config

from tests.test_backtest_simulator import (
    ScriptedProvider,
    _weekdays,
    dip_then_recovering_window,
)

CC_PROFILE = "config/covered_call.yaml"


# --------------------------------------------------------------------------- #
# Fixtures: a covered-call replay on the scripted chain
# --------------------------------------------------------------------------- #
def _cc_config() -> Config:
    return Config(CC_PROFILE)


def _cc_simulator(symbol, closes, expirations, days, *, config=None,
                  policy=None, max_dte=14, **kw):
    """A covered-call Simulator over the scripted chain.

    `max_dte` is 14, not the wheel's 7: the covered-call profile's
    `call_target_dte` is 14 and a 7-reach materialisation would hand it a chain
    with nothing it could select, which reads as "this strategy found no
    candidate" when what was missing was the contracts.
    """
    provider = ScriptedProvider(symbol, closes, expirations)
    builder = ChainBuilder(provider, risk_free_rate=0.04)
    return Simulator(
        config or _cc_config(), provider, builder, [symbol], days[0], days[-1],
        starting_cash=kw.pop("starting_cash", CC_CASH_FLOAT),
        max_dte=max_dte,
        synthetic_lots=(SyntheticLotPolicy() if policy is None else policy),
        dividend_schedule=DividendSchedule.empty(),
        **kw,
    )


def _expirations_past(days):
    """Fridays through the window AND for five weeks past its end.

    A ladder that stops at the window's last Friday leaves the final fortnight
    with only already-expired contracts — every candidate comes back `dte: -1`
    and the arm reads as "nothing qualified" when what was missing was the
    chain. Real chains always carry future expiries.
    """
    tail = _weekdays(days[-1] + timedelta(days=1), 25)
    return [d for d in days + tail if d.weekday() == 4]


def _rally_window():
    """A climb steep enough to take the stock THROUGH its own short strike.

    The gentle window below writes ~9% OTM (the [0.15, 0.25] delta band) and
    rises ~7.5/week, so its calls always expire worthless — which is the right
    fixture for "never called away is not insufficient" and the wrong one for
    everything that needs an assignment. At 3.0/day the stock gains ~15 a week
    against a ~9-wide strike, so the call goes ITM mid-life: the roller's 1.00
    trigger becomes reachable and, when it declines, the shares are called away
    and the re-seed chain runs.
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 40)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        closes[d] = 100.0 + i * 3.0
    return days, closes, _expirations_past(days)


@pytest.fixture(scope="module")
def cc_rally():
    days, closes, expirations = _rally_window()
    result = _cc_simulator("XYZ", closes, expirations, days).run()
    return days, closes, expirations, result


def _rising_window():
    """A gentle climb whose calls always expire worthless.

    100% covered, never called away — the shape that proves the covered-call
    verdict does NOT invert (a lot that was never assigned is the programme
    working, not an unmeasurable window).
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 40)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        closes[d] = 100.0 + i * 1.5
    return days, closes, _expirations_past(days)


def _flat_window():
    """Flat, so the written call DECAYS instead of moving against the seller.

    The two climbing windows cannot test profit-taking, and it took a trace to
    see why: in a rally a short call moves AGAINST the writer, so
    `should_close_call_early` is asked on every session and correctly answers
    False every time. Theta only shows up on a stock that is not running, which
    is also the regime the live /monitor leg does most of its work in.

    Held exactly at the seeding close, so the lot basis and the spot coincide
    and the cost-basis floor admits every OTM strike.
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 40)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        # A shallow saw-tooth, not a dead line: a perfectly constant series
        # gives the volatility screen a zero and stage 1 drops the symbol.
        closes[d] = 100.0 + (1.0 if i % 2 else -1.0)
    return days, closes, _expirations_past(days)


@pytest.fixture(scope="module")
def cc_flat():
    days, closes, expirations = _flat_window()
    result = _cc_simulator("XYZ", closes, expirations, days).run()
    return days, closes, expirations, result


@pytest.fixture(scope="module")
def cc_rising():
    days, closes, expirations = _rising_window()
    result = _cc_simulator("XYZ", closes, expirations, days).run()
    return days, closes, expirations, result


# --------------------------------------------------------------------------- #
# 1. The profile loads and the replay is call-only
# --------------------------------------------------------------------------- #
class TestTheProfileIsUsable:
    def test_restrict_symbols_tolerates_a_profile_with_no_stocks_section(self):
        """The rev-1 miss (plan §C2), and it was fatal on day zero.

        `config/covered_call.yaml` has no `stocks:` — its live universe is
        holdings-derived — so `_config["stocks"]["symbols"] = ...` raised
        KeyError before a single day was replayed.
        """
        config = _cc_config()
        assert "stocks" not in config._config, (
            "the covered-call profile grew a `stocks:` section; this test is "
            "now vacuous — point it at a profile that still has none")
        narrowed = restrict_symbols(config, ["XYZ"])
        assert narrowed.stock_symbols == ["XYZ"]
        # The caller's config is untouched, as it is on the wheel path.
        assert "stocks" not in config._config

    def test_the_replay_writes_calls_and_never_a_put(self, cc_rising):
        _days, _closes, _exps, result = cc_rising
        kinds = [e.kind for e in result.broker.ledger]
        assert "sell_call_open" in kinds, f"no call was ever written: {kinds}"
        assert "sell_put_open" not in kinds, (
            "a covered-call replay wrote a PUT — the scan is supposed to be "
            f"call-only on this profile. ledger={kinds}")
        assert "put_assignment" not in kinds

    def test_the_first_event_on_the_symbol_is_the_seeding(self, cc_rising):
        _days, _closes, _exps, result = cc_rising
        assert result.broker.ledger[0].kind == "synthetic_lot_open"
        assert result.strategy == "covered_call"

    def test_the_spread_gate_is_suspended_and_the_suspension_is_pinned(self):
        """The suspension is a MODEL fact, and this is its tripwire.

        `universe.max_spread_pct` is read off modelled bid/ask whose half-spread
        is >= 5% of mark for an OTM contract, so 0.10 rejects every
        floor-clearing call by construction. When real spreads arrive, the
        premise stops holding and this test fails LOUDLY so the gate is restored
        deliberately rather than staying off because nobody remembered it was.
        """
        config = _cc_config()
        assert config._config["universe"]["max_spread_pct"] == 0.10, (
            "the profile's spread gate moved; re-derive the suspension")
        assert spread_gate_would_suspend(config) is True

        sim = _cc_simulator("XYZ", *_rising_window()[1:], _rising_window()[0])
        assert sim.spread_gate_suspended is True
        assert sim.config._config["universe"]["max_spread_pct"] is None
        # The caller's profile is NOT mutated — the live service still gates.
        assert config._config["universe"]["max_spread_pct"] == 0.10

    def test_the_wheel_never_suspends_the_gate(self):
        wheel = Config()
        assert spread_gate_would_suspend(wheel) is False
        assert replay_strategy(wheel) == "wheel"


# --------------------------------------------------------------------------- #
# 2. The lot: seeding, the re-seed chain, and the cycle table
# --------------------------------------------------------------------------- #
class TestTheSyntheticLot:
    def test_the_lot_is_100_shares_at_the_first_days_close(self, cc_rising):
        days, closes, _exps, result = cc_rising
        seed = result.broker.ledger[0]
        assert seed.shares == SYNTHETIC_LOT_SHARES == 100
        assert seed.price == closes[days[0]]
        assert seed.cash_delta == 0.0, "the lot was never bought; no cash moved"
        assert seed.detail["premise"]
        assert seed.detail["lot_value"] == pytest.approx(closes[days[0]] * 100)

    def test_the_lot_carries_a_real_basis_so_the_floor_and_roller_work(self):
        """FC-100 §Phase C hand-off row 2, and it is silent when it breaks.

        `CostBasisResolver` reads `avg_entry_price` off the position. A lot with
        no usable basis resolves `no_broker_basis`, the roller declines EVERY
        position, and the replay reports "no rolls" — which is
        indistinguishable from a roller that ran and found nothing.
        """
        from src.backtesting.engine.alpaca_adapter import BacktestAlpacaClient
        from src.utils import clock

        seeded_on = date(2024, 6, 3)
        broker = BacktestBroker(starting_cash=CC_CASH_FLOAT)
        broker.deposit_shares("XYZ", 100, 123.45, seeded_on, premise="test")
        client = BacktestAlpacaClient(broker, chains={}, stock_bars={})
        # The adapter reads the FROZEN clock rather than a settable attribute,
        # so a unit test has to freeze it exactly as the day loop does.
        with clock.frozen(datetime(2024, 6, 3, 15, 30)):
            position = next(p for p in client.get_positions()
                            if p["symbol"] == "XYZ")
        assert position["avg_entry_price"] == pytest.approx(123.45)
        assert position["avg_entry_price"] > 0

    def test_a_non_positive_basis_is_refused_not_booked(self):
        broker = BacktestBroker(starting_cash=1000.0)
        with pytest.raises(ValueError, match="non-positive basis"):
            broker.deposit_shares("XYZ", 100, 0.0, date(2024, 6, 3),
                                  premise="test")

    def test_the_lot_is_re_seeded_at_the_next_close_after_a_call_away(
            self, cc_rally):
        """The operator's signed posture (2026-09-08), on a real replay.

        The window climbs monotonically, so the short call is assigned; the
        shares leave at settlement, and the NEXT session's top-of-loop finds the
        symbol flat and seeds a fresh lot at that day's close.
        """
        days, closes, _exps, result = cc_rally
        ledger = result.broker.ledger
        call_aways = [e for e in ledger if e.kind == "call_assignment"]
        seeds = [e for e in ledger if e.kind == "synthetic_lot_open"]
        assert call_aways, "the rising window never called the lot away"
        assert len(seeds) == len(call_aways) + 1, (
            f"expected one seed per call-away plus the opening one; "
            f"{len(seeds)} seeds against {len(call_aways)} call-aways")
        assert result.synthetic_lots_opened == len(seeds)

        for away in call_aways:
            later = [s for s in seeds if s.event_date > away.event_date]
            if not later:
                continue  # called away on the last session: nothing to re-seed
            reseed = min(later, key=lambda e: e.event_date)
            nxt = days[days.index(away.event_date) + 1]
            assert reseed.event_date == nxt, (
                "the re-seed must land on the NEXT session, not later")
            assert reseed.price == closes[nxt], (
                "the re-seed must be at that session's CLOSE")

    def test_each_lot_of_the_chain_is_its_own_cycle_with_its_own_basis(
            self, cc_rally):
        _days, _closes, _exps, result = cc_rally
        cycles = build_cycles(result.broker.ledger)
        seeded = [c for c in cycles if c.synthetic_lots]
        assert len(seeded) >= 2, (
            "the re-seed chain should produce more than one cycle")
        # Every seeded cycle carries a REAL basis (so call_assignment can book
        # stock P&L against it) and is never mislabelled as put-assigned.
        for cycle in seeded:
            assert cycle.cost_basis and cycle.cost_basis > 0
            assert cycle.assigned is False, (
                "a seeded lot is not a put assignment; `assigned` must stay "
                "False or every CC assignment_rate reads 100%")
        # The bases differ: a chain of lots bought at different closes.
        bases = {round(c.cost_basis, 4) for c in seeded}
        assert len(bases) > 1, f"all lots share one basis: {bases}"

    def test_a_called_away_cycle_books_real_stock_pnl(self, cc_rally):
        _days, _closes, _exps, result = cc_rally
        cycles = build_cycles(result.broker.ledger)
        away = [c for c in cycles if c.called_away]
        assert away
        for cycle in away:
            assert cycle.exit_price is not None
            assert cycle.stock_pnl == pytest.approx(
                (cycle.exit_price - cycle.cost_basis) * 100, abs=1e-6)

    def test_reseed_disabled_stops_at_the_first_call_away(self):
        """The rejected alternative, kept implementable and asserted.

        MUTATION: flip the built default to `reseed=False` and
        `test_the_lot_is_re_seeded_at_the_next_close_after_a_call_away` fails.
        """
        days, closes, expirations = _rally_window()
        result = _cc_simulator(
            "XYZ", closes, expirations, days,
            policy=SyntheticLotPolicy(reseed=False)).run()
        seeds = [e for e in result.broker.ledger
                 if e.kind == "synthetic_lot_open"]
        assert len(seeds) == 1
        assert result.synthetic_lots_opened == 1
        assert result.coverage_by_reason.get(COVERAGE_POST_CALL_AWAY, 0) > 0, (
            "with no re-seed, the days after a call-away are post_call_away")

    def test_a_wheel_replay_seeds_nothing(self):
        """The golden regression: a wheel ledger contains no seeding event."""
        from tests.test_backtest_simulator import _simulator as wheel_simulator

        days, closes, expirations = dip_then_recovering_window()
        result = wheel_simulator("XYZ", closes, expirations, days).run()
        kinds = {e.kind for e in result.broker.ledger}
        assert "synthetic_lot_open" not in kinds
        assert result.strategy == "wheel"
        assert result.synthetic_lots_opened == 0
        assert result.time_weighted_lot_value == 0.0
        assert result.coverage_by_reason == {}
        assert result.calls_closed_early == 0
        for cycle in build_cycles(result.broker.ledger):
            assert cycle.synthetic_lots == 0


# --------------------------------------------------------------------------- #
# 3. The denominators — the defect that made a no-trade window the best cell
# --------------------------------------------------------------------------- #
def _scored(days, closes, expirations, result):
    from tests.test_backtest_simulator import ScriptedProvider as _SP

    bars = _SP("XYZ", closes, expirations).get_stock_bars(
        "XYZ", min(closes), max(closes))
    return _score("XYZ", result, bars, CC_CASH_FLOAT, DividendSchedule.empty())


class TestTheCapitalBase:
    def test_a_no_trade_window_returns_zero_not_plus_the_lot(self):
        """THE regression this whole section exists for (plan §C3, trader B2).

        The lot arrives at `cash_delta = 0` and is then marked to market, so a
        base of `starting_cash` alone books the entire lot as profit. A window
        in which the strategy never traded returned +2,900% and ranked first.
        """
        days, closes, expirations = _flat_window()
        # An ODD number of sessions, so the saw-tooth ends on the price it
        # started at. `total_return` is the EQUITY return and therefore
        # legitimately carries the lot's own price move — on the full 40-day
        # window that is a real +1.34%, which would mask the defect this test
        # is looking for. Ending flat isolates the lot-as-profit term: anything
        # other than 0.0% here IS the lot leaking into the numerator.
        days = days[:39]
        assert closes[days[0]] == closes[days[-1]]
        # A premium floor nothing can clear: the strategy writes NOTHING, so
        # every cent of a reported return would have to be the lot.
        config = _cc_config()
        config._config["strategy"]["min_call_premium"] = 10_000.0
        result = _cc_simulator("XYZ", closes, expirations, days,
                               config=config).run()
        assert not [e for e in result.broker.ledger
                    if e.kind == "sell_call_open"], "the fixture traded"

        report = _scored(days, closes, expirations, result)
        assert report.lot_capital > 0, "no lot was seeded; the test is vacuous"
        assert report.total_return == pytest.approx(0.0, abs=1e-9), (
            f"a no-trade covered-call window returned "
            f"{report.total_return:.2%} — the lot is being counted as profit")
        assert report.net_premium == 0.0
        assert report.premium_yield_on_lot == pytest.approx(0.0)

        # And the size of the defect, stated: without the fix the SAME window
        # reports the whole lot as gain.
        naive = (report.final_equity - report.starting_cash) / report.starting_cash
        assert naive > 1.5, (
            "sanity: dividing by the cash float alone should still look "
            "spectacular, which is why the base had to move")

    def test_the_base_is_the_LOT_and_the_cash_float_is_excluded(self, cc_rising):
        """Review round 1, H1 — one denominator on both sides of the page.

        The first cut used `starting_cash + lot_capital`, so the cell divided by
        $15,000 while the lot-sized benchmark divided by $10,000. Two
        denominators on one page make `excess_return` a comparison of
        differently-scaled numbers. The $5,000 is a LIQUIDITY RESERVE (the
        monitor leg's buy-backs, a roll's BTC before its STO credit), not
        capital the strategy is measured on: it stays in `starting_cash`, which
        is stamped separately, and out of every ratio.
        """
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        assert report.starting_cash == CC_CASH_FLOAT == 5_000.0
        assert report.lot_capital == pytest.approx(closes[days[0]] * 100)
        assert report.capital_base == pytest.approx(closes[days[0]] * 100), (
            "the cash float must not be in the denominator")
        assert report.benchmark.starting_cash == report.capital_base, (
            "the benchmark must divide by the SAME base as the cell, or "
            "excess_return subtracts two differently-scaled ratios")

    def test_a_window_that_beat_its_benchmark_reports_a_positive_excess(
            self, cc_rising):
        """The consequence H1 exists for, on a real replay.

        With the float in the denominator the cell's return was scaled down by
        2/3 against a benchmark that was not, and this window — which genuinely
        out-earned holding — reported a NEGATIVE excess.
        """
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        assert report.excess_return is not None
        assert report.excess_return > 0, (
            f"lot return {report.total_return:.2%} vs benchmark "
            f"{report.benchmark.total_return:.2%}")

    def test_the_benchmark_holds_exactly_100_shares_at_an_awkward_price(self):
        """M7: `int(cash // entry)` drops a share on ~48% of prices.

        143.37 is the reviewer's case: `int(14337 // 143.37) == 99`, which
        understates the benchmark by one share's move for a lot that is 100
        shares by signed decision.
        """
        from src.backtesting.engine.simulator import DailyState
        from src.backtesting.metrics.fitness import _lot_buy_and_hold

        days = [date(2024, 6, 3), date(2024, 6, 28)]
        daily = [DailyState(day=d, equity=1.0, cash=1.0, reserved_collateral=0.0,
                            open_options=0, shares_held={}) for d in days]
        prices = {days[0]: 143.37, days[1]: 150.00}
        bench = _lot_buy_and_hold(daily, prices, 143.37 * 100)
        assert bench.shares == SYNTHETIC_LOT_SHARES == 100
        assert int(143.37 * 100 // 143.37) == 99, "the old floor's answer"
        assert bench.total_return == pytest.approx(
            (150.00 - 143.37) * 100 / (143.37 * 100))

    def test_attribution_still_reconciles_across_a_chain_of_lots(
            self, cc_rally):
        """The Σ-vs-time-weighted split, checked where it actually bites.

        The numerator subtracts Σ seeded lot values and the denominator uses the
        time-weighted average. Using the time-weighted number in BOTH places
        would leave a reconciliation gap the size of the lots the chain no
        longer holds.
        """
        days, closes, expirations, result = cc_rally
        report = _scored(days, closes, expirations, result)
        assert report.synthetic_lots_opened >= 2
        assert report.lot_capital_injected > report.lot_capital, (
            "a chain of lots must inject more than it ever held at once")
        assert report.reconciliation_gap == pytest.approx(0.0, abs=1e-6), (
            f"attribution does not reconcile: {report.reconciliation_gap}")

    def test_the_benchmark_is_the_same_lot_not_a_full_investment(
            self, cc_rising):
        """§C3 / trader M7. A $100k buy-and-hold buys ~30x the shares, so
        `excess_return` would compare two different investments and the verdict
        would read the size difference as skill."""
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        assert report.benchmark is not None
        assert report.benchmark.shares == SYNTHETIC_LOT_SHARES
        assert report.benchmark.starting_cash == pytest.approx(
            closes[days[0]] * 100)
        assert report.benchmark.entry_price == closes[days[0]]

    def test_the_wheel_base_is_exactly_its_starting_cash(self):
        """The golden guarantee: no wheel number moves."""
        from tests.test_backtest_simulator import ScriptedProvider as _SP
        from tests.test_backtest_simulator import _simulator as wheel_simulator

        days, closes, expirations = dip_then_recovering_window()
        result = wheel_simulator("XYZ", closes, expirations, days).run()
        bars = _SP("XYZ", closes, expirations).get_stock_bars(
            "XYZ", min(closes), max(closes))
        report = _score("XYZ", result, bars, 50_000.0)
        assert report.is_wheel
        assert report.lot_capital == 0.0
        assert report.lot_capital_injected == 0.0
        assert report.capital_base == 50_000.0
        assert report.total_pnl == report.final_equity - 50_000.0
        assert report.premium_yield_on_lot is None
        assert report.net_basis_reduction is None
        assert report.coverage_fraction is None


# --------------------------------------------------------------------------- #
# 4. The verdict, which INVERTS if the wheel's rules are re-pointed
# --------------------------------------------------------------------------- #
class TestTheCoveredCallVerdict:
    def test_never_called_away_is_not_insufficient(self, cc_rising):
        """The trader review's B3, and the sharpest of the design defects.

        The wheel's first question is "did a cycle close". On a covered-call
        replay a lot that was never called away is the programme working
        PERFECTLY — it kept the shares and kept the premium — and the wheel
        branch would have stamped `insufficient` on exactly that outcome.
        """
        days, closes, expirations, result = cc_rising
        assert not [e for e in result.broker.ledger
                    if e.kind == "call_assignment"], (
            "the gentle window called the lot away; it is the wrong fixture "
            "for this test")
        report = _scored(days, closes, expirations, result)
        assert report.closed_cycles == [], (
            "no cycle closed — which is the state the wheel branch calls "
            "insufficient, and this test exists to prove the CC branch does not")
        assert report.verdict() != "insufficient"
        assert not any(r.startswith("INSUFFICIENT")
                       for r in report.verdict_reasons())
        # Review round 1, H2: assert the VERDICT, not merely that it is not
        # `insufficient`. This window kept its shares, kept its premium and beat
        # the same lot held — the best outcome a covered-call programme has —
        # and it must read as `fit`, with no reason at all.
        assert report.verdict() == "fit", report.verdict_reasons()
        assert report.verdict_reasons()[0].startswith("OK:")

    def test_a_window_that_beat_holding_does_NOT_warn_that_holding_was_better(
            self, cc_rising):
        """The plan's own comparison was wrong, and both reviewers found it.

        §C3 said to WARN when `premium_yield_on_lot` trails the lot's
        buy-and-hold. Premium-only against total-return is not a comparison: on
        any rising lot the premium leg loses by construction, so the WARN fired
        on windows the strategy actually won and asserted, falsely, that holding
        would have done better.
        """
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        assert report.excess_return > 0
        assert not any("would have done better" in r
                       for r in report.verdict_reasons())
        assert not any("writing cost more" in r
                       for r in report.verdict_reasons())

    def test_the_warn_fires_on_total_return_when_writing_actually_cost_upside(
            self, cc_rally):
        """The rally: the call-away drag is real, so the lot under this
        programme genuinely trails the lot held. THAT is the warning."""
        days, closes, expirations, result = cc_rally
        report = _scored(days, closes, expirations, result)
        assert report.excess_return < 0, (
            "the rally window should surrender upside to call-aways")
        warns = [r for r in report.verdict_reasons() if "writing cost more" in r]
        assert warns, report.verdict_reasons()
        assert "surrendered upside" in warns[0]

    def test_the_yield_is_reported_as_a_yield_never_as_the_warn_driver(
            self, cc_rising):
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        ok = report.verdict_reasons()[0]
        assert "annualized yield on the lot" in ok
        assert report.premium_yield_on_lot is not None

    def test_a_window_shorter_than_one_tenor_is_insufficient(self):
        days, closes, expirations = _flat_window()
        short_days = days[:8]
        result = _cc_simulator("XYZ", closes, expirations, short_days).run()
        report = _scored(short_days, closes, expirations, result)
        assert report.data_quality["call_target_dte"] == 14
        assert report.verdict() == "insufficient"
        assert any("shorter than one" in r for r in report.verdict_reasons())

    def test_a_losing_premium_programme_is_blocked(self, cc_rising):
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)
        report.option_pnl = -1.0
        assert report.verdict() == "unfit"
        assert any("paid to write calls" in r for r in report.verdict_reasons())

    def test_low_coverage_blocks_but_below_basis_days_do_not_count_against_it(
            self, cc_rising):
        """`low_activity` for CC excludes the stand-downs that are the guards
        working — the plan's explicit instruction, and the difference between
        demoting a symbol and crediting the floor that protected it."""
        days, closes, expirations, result = cc_rising
        report = _scored(days, closes, expirations, result)

        # 2 covered of 4 writable days = 50%, ABOVE the floor, even though 96
        # of the 100 days in the window were stand-downs.
        report.coverage_by_reason = {
            COVERAGE_COVERED: 2,
            "gate_rejected": 2,
            COVERAGE_HOLD_UNCOVERED: 90,
            "earnings_span": 6,
        }
        assert report.coverage_fraction == pytest.approx(0.5)
        assert not any("could have written one" in r
                       for r in report.verdict_reasons())

        # Now the SAME 2 covered days against 20 real gate rejections.
        report.coverage_by_reason = {
            COVERAGE_COVERED: 2, "gate_rejected": 20,
            COVERAGE_HOLD_UNCOVERED: 90,
        }
        assert report.coverage_fraction == pytest.approx(2 / 22)
        assert report.verdict() == "unfit"
        assert any("could have written one" in r
                   for r in report.verdict_reasons())

    def test_the_two_excluded_buckets_are_named_not_guessed(self):
        assert COVERAGE_NOT_A_STAND_DOWN == frozenset(
            {COVERAGE_HOLD_UNCOVERED, "earnings_span"})


# --------------------------------------------------------------------------- #
# 5. Coverage split, monitor leg, and the roll split
# --------------------------------------------------------------------------- #
class TestCoverageIsSplitByReason:
    def test_every_decision_day_lands_in_exactly_one_bucket(self, cc_rally):
        days, _closes, _exps, result = cc_rally
        assert sum(result.coverage_by_reason.values()) == len(days), (
            "the buckets are a PARTITION of the decision days or they are "
            f"decoration: {result.coverage_by_reason} over {len(days)} days")

    def test_a_covered_day_is_recorded_as_covered(self, cc_rising):
        _days, _closes, _exps, result = cc_rising
        assert result.coverage_by_reason.get(COVERAGE_COVERED, 0) > 0
        # The gentle window holds a call almost every day and never stands down
        # below basis (it only ever rises).
        assert COVERAGE_HOLD_UNCOVERED not in result.coverage_by_reason

    def test_below_basis_days_are_recorded_as_hold_uncovered(self):
        """The floor refusing to sell the shares at a loss is its OWN bucket,
        never lumped in with "no contract qualified"."""
        warmup = _weekdays(date(2024, 3, 25), 45)
        days = _weekdays(date(2024, 6, 3), 30)
        closes = {d: 100.0 for d in warmup}
        for i, d in enumerate(days):
            # Seeded at 100 on day one, then straight down: every later day is
            # below the lot's basis.
            closes[d] = 100.0 - i * 2.0
        result = _cc_simulator("XYZ", closes, _expirations_past(days),
                               days).run()
        assert result.coverage_by_reason.get(COVERAGE_HOLD_UNCOVERED, 0) > 0, (
            f"a window that fell 60% below basis recorded no below-basis "
            f"stand-down: {result.coverage_by_reason}")


    def test_the_floor_predicate_separates_all_three_states(self):
        """M4, corrected by the confirmation pass (2026-09-09).

        The predicate must fire ONLY on the floor's own case. The first cut
        answered `False` for three unrelated states and the caller turned every
        one of them into `hold_uncovered` — including "the band was empty" and
        "there was no chain", neither of which the floor had any part in. Since
        `hold_uncovered` is EXCLUDED from the coverage denominator, that misfile
        INFLATED coverage, and it did so on days the stock was far ABOVE its
        basis, where the floor cannot be the reason at all.
        """
        from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
        from src.backtesting.engine.simulator import Simulator

        days, closes, expirations = _rising_window()
        sim = _cc_simulator("XYZ", closes, expirations, days)

        def _call(strike, delta):
            return ChainQuote(
                symbol=f"XYZ240607C{int(strike * 1000):08d}", underlying="XYZ",
                as_of=date(2024, 6, 3), expiration=date(2024, 6, 7),
                strike=strike, option_type="call", dte=4,
                underlying_price=90.0, mark=1.1, bid=1.0, ask=1.2,
                implied_volatility=0.3, delta=delta, volume=100)

        def _snap(quotes):
            return ChainSnapshot(underlying="XYZ", as_of=date(2024, 6, 3),
                                 underlying_price=90.0, puts=[], calls=quotes)

        # 1. In-band strikes exist and ALL sit below basis -> the floor's case.
        assert Simulator._floor_blocks_writing(
            sim, _snap([_call(95.0, 0.20), _call(98.0, 0.15)]), 100.0)

        # 2. An in-band strike clears the basis -> something else refused it.
        assert not Simulator._floor_blocks_writing(
            sim, _snap([_call(95.0, 0.20), _call(105.0, 0.15)]), 100.0)

        # 3. The band is EMPTY -> a selection failure, NOT the floor. This is
        #    the regression: every one of these strikes clears the basis by a
        #    mile, so calling it a floor stand-down is exactly backwards.
        assert not Simulator._floor_blocks_writing(
            sim, _snap([_call(150.0, 0.80), _call(160.0, 0.90)]), 100.0), (
            "an empty delta band is the selector finding nothing; the floor "
            "never got a candidate to refuse")

        # 4. No chain at all -> a data fact, and it must stay in the denominator.
        assert not Simulator._floor_blocks_writing(sim, None, 100.0)

    def test_the_CLASSIFIER_uses_the_chain_and_not_the_close(self):
        """The classifier consults the chain, and files each state correctly.

        Pinning the helper alone proved it worked and NOT that
        `_coverage_reason` consults it — reverting the classifier to
        `close < basis` left every assertion passing. This drives the
        classifier, on books where the answers differ.
        """
        from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
        from src.backtesting.engine.broker import BacktestBroker
        from src.backtesting.engine.simulator import (
            COVERAGE_GATE_REJECTED, Simulator,
        )

        days, closes, expirations = _rising_window()
        sim = _cc_simulator("XYZ", closes, expirations, days)

        broker = BacktestBroker(starting_cash=CC_CASH_FLOAT)
        broker.deposit_shares("XYZ", 100, 100.0, date(2024, 6, 3),
                              premise="test")

        def _call(strike, delta):
            return ChainQuote(
                symbol=f"XYZ240607C{int(strike * 1000):08d}", underlying="XYZ",
                as_of=date(2024, 6, 3), expiration=date(2024, 6, 7),
                strike=strike, option_type="call", dte=4, underlying_price=90.0,
                mark=1.1, bid=1.0, ask=1.2, implied_volatility=0.3,
                delta=delta, volume=100)

        def _snap(quotes):
            return ChainSnapshot(underlying="XYZ", as_of=date(2024, 6, 3),
                                 underlying_price=90.0, puts=[], calls=quotes)

        # Spot 90, basis 100 — BELOW basis, so the old `close < basis` proxy
        # said `hold_uncovered` no matter what the chain held.
        below = {"XYZ": 90.0}

        # In-band strikes, all under the basis: the floor really is the reason.
        assert Simulator._coverage_reason(
            sim, broker, "XYZ", below, set(),
            snapshot=_snap([_call(95.0, 0.20)])
        ) == COVERAGE_HOLD_UNCOVERED

        # Below basis, but an in-band strike CLEARS it: not the floor.
        assert Simulator._coverage_reason(
            sim, broker, "XYZ", below, set(),
            snapshot=_snap([_call(105.0, 0.20)])
        ) != COVERAGE_HOLD_UNCOVERED, (
            "below basis with a floor-clearing in-band strike available is NOT "
            "the floor standing the strategy down — something else refused it")

        # THE REGRESSION (confirmation pass): every strike clears the basis by a
        # mile and none is in the band. The floor had nothing to do with it, and
        # filing it as `hold_uncovered` drops the day from the coverage
        # denominator — flattering coverage on a stock far ABOVE its basis.
        above = {"XYZ": 150.0}
        assert Simulator._coverage_reason(
            sim, broker, "XYZ", above, set(),
            snapshot=_snap([_call(150.0, 0.80), _call(160.0, 0.90)])
        ) == COVERAGE_GATE_REJECTED

        # No chain at all is a data fact, and it stays in the denominator too.
        assert Simulator._coverage_reason(
            sim, broker, "XYZ", above, set(), snapshot=None
        ) == COVERAGE_GATE_REJECTED

    def test_an_above_basis_empty_band_lands_in_the_denominator(self):
        """The confirmation pass's own probe, pinned as a regression test.

        The rising window under a [0.10, 0.20] arm: on four sessions the basis
        is 100 against closes of 145-154, every listed call clears the basis,
        and none falls in the band. Those days were being filed as
        `hold_uncovered` and DROPPED from the coverage denominator, reporting
        100% coverage where the truth is 90% — and the CLI printed "below basis
        4" about a stock 50% ABOVE its basis.
        """
        from src.backtesting.scenarios.overrides import apply_overrides
        from src.backtesting.engine.simulator import COVERAGE_GATE_REJECTED

        days, closes, expirations = _rising_window()
        arm = apply_overrides(_cc_config(),
                              {"strategy.call_delta_range": [0.10, 0.20]})
        result = _cc_simulator("XYZ", closes, expirations, days,
                               config=arm).run()

        assert COVERAGE_HOLD_UNCOVERED not in result.coverage_by_reason, (
            f"a stock far ABOVE its basis cannot be held back BY the basis "
            f"floor: {result.coverage_by_reason}")
        assert result.coverage_by_reason[COVERAGE_GATE_REJECTED] == 4
        assert result.coverage_by_reason[COVERAGE_COVERED] == 36
        assert sum(result.coverage_by_reason.values()) == len(days)

        report = _scored(days, closes, expirations, result)
        assert report.coverage_fraction == pytest.approx(0.9), (
            "the four empty-band days must be IN the denominator; excluding "
            "them reports 100% coverage for a strategy covered on 36 of 40 days")

    def test_the_footers_never_flattering_claim_is_TRUE_of_the_classifier(self):
        """The footer claims a DIRECTION; this checks the code has it.

        `SYNTHETIC_LOT_BIAS` says the split "errs toward `gate_rejected` …
        harsher, never flattering". That sentence was FALSE as first written:
        an empty band and an absent chain both fell into `hold_uncovered`, which
        is excluded from the denominator, so the error ran the flattering way.
        Asserting the words alone would have passed throughout.

        Every state that is not unambiguously the floor's must therefore land in
        a bucket the denominator can see.
        """
        from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
        from src.backtesting.engine.broker import BacktestBroker
        from src.backtesting.engine.simulator import (
            COVERAGE_NOT_A_STAND_DOWN, Simulator,
        )
        from src.backtesting.scenarios.report import SYNTHETIC_LOT_BIAS

        detail = SYNTHETIC_LOT_BIAS[1]
        assert "gate_rejected" in detail and "harsher" in detail

        days, closes, expirations = _rising_window()
        sim = _cc_simulator("XYZ", closes, expirations, days)
        broker = BacktestBroker(starting_cash=CC_CASH_FLOAT)
        broker.deposit_shares("XYZ", 100, 100.0, date(2024, 6, 3),
                              premise="test")

        def _snap(*strikes_deltas):
            return ChainSnapshot(
                underlying="XYZ", as_of=date(2024, 6, 3), underlying_price=90.0,
                puts=[], calls=[ChainQuote(
                    symbol=f"XYZ240607C{int(k * 1000):08d}", underlying="XYZ",
                    as_of=date(2024, 6, 3), expiration=date(2024, 6, 7),
                    strike=k, option_type="call", dte=4, underlying_price=90.0,
                    mark=1.1, bid=1.0, ask=1.2, implied_volatility=0.3,
                    delta=d, volume=100) for k, d in strikes_deltas])

        # Every book in which the floor is NOT unambiguously the reason must
        # produce a bucket that COUNTS toward coverage.
        ambiguous = [
            ("empty band, all above basis", _snap((150.0, 0.80))),
            ("empty chain", _snap()),
            ("no chain at all", None),
            ("in-band strike clears basis", _snap((105.0, 0.20))),
        ]
        for label, snapshot in ambiguous:
            reason = Simulator._coverage_reason(
                sim, broker, "XYZ", {"XYZ": 90.0}, set(), snapshot=snapshot)
            assert reason not in COVERAGE_NOT_A_STAND_DOWN, (
                f"{label}: filed as {reason!r}, which is EXCLUDED from the "
                f"coverage denominator — that is the flattering direction the "
                f"footer promises the code does not take")


class TestTheMonitorLeg:
    def test_it_closes_a_decayed_call_at_the_bands_target(self, cc_flat):
        """52% of real covered calls close early; a replay without this leg
        measures a strategy nobody runs."""
        _days, _closes, _exps, result = cc_flat
        assert result.calls_closed_early > 0, (
            "the monitor leg never closed a call on a flat window, where theta "
            "is the whole story")
        assert any(e.kind == "buy_to_close" for e in result.broker.ledger)

    def test_it_uses_the_real_predicate_and_the_profiles_own_bands(self):
        """Not a re-implementation: the same function `/monitor` calls."""
        import inspect

        from src.backtesting.engine.simulator import Simulator as S

        source = inspect.getsource(S._run_monitor_leg)
        assert "should_close_call_early" in source
        assert _cc_config().profit_taking_dte_bands, (
            "the CC profile has no DTE bands, so the leg would fall through to "
            "the static target and this test would prove nothing")

    def test_the_wheel_replay_does_not_run_it(self):
        """Adding this leg to the wheel would move every stored wheel result at
        once — its own FC, its own re-baseline."""
        from tests.test_backtest_simulator import _simulator as wheel_simulator

        days, closes, expirations = dip_then_recovering_window()
        result = wheel_simulator("XYZ", closes, expirations, days).run()
        assert result.calls_closed_early == 0

    def test_it_never_runs_on_puts(self):
        """MUTATION: drop the `option_type == 'call'` filter and this fails.

        A monitor leg that evaluated puts would hand a put position to
        `should_close_call_early`, which is the FC-045 misroute in reverse.
        """
        import inspect

        from src.backtesting.engine.simulator import Simulator as S

        source = inspect.getsource(S._run_monitor_leg)
        assert 'option_type == "call"' in source
        # And on the CC path there are no puts to route: proven by the ledger.


class TestTheRollSplit:
    def test_the_replay_rolls_through_the_real_roller_at_1_00(self, cc_rally):
        """FC-100 wired rolling on this profile, so the replay must model it.

        A roller that never fires here would be indistinguishable from a roller
        that fired and declined — which is exactly what an absent
        `avg_entry_price` produces (`cost_basis_unresolved`, silently).
        """
        _days, _closes, _exps, result = cc_rally
        assert _cc_config().rolling_enabled is True
        assert _cc_config().rolling_itm_trigger_ratio == 1.00
        assert result.rolls_evaluated > 0
        assert result.rolls_executed > 0, (
            "the roller evaluated positions and executed nothing on a window "
            "that took the stock through its own strike — check the lot's "
            "avg_entry_price, which fails closed and silently")

    def test_a_through_the_strike_lot_rolls_and_a_98_8_percent_one_does_not(self):
        """The 1.00 trigger, both directions, against the REAL `should_roll`.

        0.988 is the live GOOGL example from FC-100 DD-1: eligible at the
        wheel's 0.98 and NOT at the covered-call profile's 1.00, which is the
        whole point of the stated difference.
        """
        from src.strategy.call_roller import CallRoller

        config = _cc_config()
        roller = CallRoller.__new__(CallRoller)
        roller.config = config
        position = {"symbol": "XYZ240607C00100000", "qty": -1}

        through, reason = CallRoller.should_roll(
            roller, position, {}, 105.0, set())
        assert through is True and reason == "eligible"

        at_par, reason = CallRoller.should_roll(
            roller, position, {}, 100.0, set())
        assert at_par is True, "stock AT the strike is ratio 1.0 — eligible"

        just_below, reason = CallRoller.should_roll(
            roller, position, {}, 98.8, set())
        assert just_below is False and reason == "not_itm_enough", (
            "a 98.8% lot must NOT roll on the covered-call profile — at 0.98 "
            "the roller would re-write the engine's own freshly-sold call")

    def test_executed_rolls_are_split_into_defences_and_roll_outs(
            self, cc_rally):
        _days, _closes, _exps, result = cc_rally
        assert result.itm_rolls + result.otm_roll_outs == result.rolls_executed
        assert result.otm_roll_outs == 0, (
            "at itm_trigger_ratio 1.00 an OTM roll-out is impossible; a "
            "non-zero count here is a finding, not a rounding artefact")
        assert result.itm_rolls == result.rolls_executed

    def test_every_stored_roll_record_carries_the_split(self, cc_rally):
        _days, _closes, _exps, result = cc_rally
        assert result.roll_records
        for record in result.roll_records:
            assert record["roll_kind"] in ("itm_defence", "otm_roll_out")
            assert record["itm_ratio"] >= 1.0
            assert date.fromisoformat(record["day"])

    def test_the_split_is_computed_from_the_ratio_the_roller_gated_on(self):
        """MUTATION: merge the two buckets (count every roll as a defence) and
        `test_executed_rolls_are_split_into_defences_and_roll_outs` still
        passes on the CC profile — so the boundary is pinned directly here."""
        from src.backtesting.engine.simulator import Simulator as S

        base = {"success": True, "underlying": "XYZ", "old_strike": 100.0}
        assert S._stamp_roll_record(base, day=date(2024, 6, 3), close=100.0
                                    )["roll_kind"] == "itm_defence"
        assert S._stamp_roll_record(base, day=date(2024, 6, 3), close=99.99
                                    )["roll_kind"] == "otm_roll_out"
        # No close: the ratio is UNKNOWN, and unknown is not a bucket.
        unknown = S._stamp_roll_record(base, day=date(2024, 6, 3), close=None)
        assert unknown["roll_kind"] is None
        assert unknown["itm_ratio"] is None
