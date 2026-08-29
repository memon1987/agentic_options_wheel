"""The materialise/replay split (FC-060 Layer 2, D1/D6).

``Simulator.run()`` was cut in two so that a scenario sweep can pay for data
assembly once and replay it many times. The split is only worth anything if it
is *invisible*: the whole engine's value is that a backtest number can be
compared with the one before it, and a refactor that shifted a fill by a cent
would silently invalidate every stored `backtest_runs` row.

These tests are the acceptance criteria for that. They compare whole
``SimulationResult`` objects — ledger, equity curve, rejection tally, roll
records — not headline returns, because a headline return is exactly the thing
that stays plausible while the mechanism underneath it changes.

The mutation that motivates test 2: make ``replay`` mutate a chain (append or
drop a quote from a ``ChainSnapshot``'s list) and the second replay diverges
from a fresh run. Nothing else in the suite catches it, because a single-replay
run is unaffected — and a sweep shares one ``Materialised`` across sixty.
"""

from __future__ import annotations

import copy
from datetime import date
from unittest.mock import patch

import pytest

from src.backtesting.data.chain_builder import ChainBuilder
from src.backtesting.data.chain_store import ChainStore
from src.backtesting.engine.simulator import Materialised, Simulator
from src.utils.config import Config

from .test_backtest_simulator import (  # noqa: F401 - fixtures are used by name
    ScriptedProvider,
    dip_then_recovering,
    falling_then_flat,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _simulator(symbol, closes, expirations, days, **kw):
    provider = ScriptedProvider(symbol, closes, expirations)
    builder = ChainBuilder(provider, risk_free_rate=0.04)
    return Simulator(
        Config(), provider, builder, [symbol], days[0], days[-1],
        starting_cash=kw.pop("starting_cash", 50_000.0), max_dte=7, **kw
    )


def _fingerprint(result) -> dict:
    """Everything a reader would act on, in a comparable shape.

    Deliberately not ``==`` on the dataclass: ``SimulationResult`` carries a
    ``BacktestBroker`` whose identity comparison would pass trivially for two
    different runs and fail trivially for two identical ones.
    """
    return {
        "symbols": list(result.symbols),
        "start": result.start,
        "end": result.end,
        "starting_cash": result.starting_cash,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "daily": [
            (d.day, d.equity, d.cash, d.reserved_collateral, d.open_options,
             dict(d.shares_held))
            for d in result.daily
        ],
        "ledger": [
            (e.kind, getattr(e, "symbol", None), getattr(e, "day", None),
             e.cash_delta, getattr(e, "quantity", None))
            for e in result.broker.ledger
        ],
        "rejections": dict(result.rejections),
        "candidate_days": result.candidate_days,
        "dividends_credited": result.dividends_credited,
        "early_assignments": result.early_assignments,
        "unpriced_ex_div_calls": result.unpriced_ex_div_calls,
        "rolls_evaluated": result.rolls_evaluated,
        "rolls_executed": result.rolls_executed,
        "roll_records": [dict(r) for r in result.roll_records],
        "earnings_symbols_without_data": list(result.earnings_symbols_without_data),
        "earnings_symbols_past_horizon": list(result.earnings_symbols_past_horizon),
    }


def _chain_fingerprint(materialised: Materialised) -> dict:
    """Every quote in every chain, plus the bars and days, as plain data."""
    return {
        "days": list(materialised.days),
        "anchors": dict(materialised.anchors),
        "max_dte": materialised.max_dte,
        "bars": {
            symbol: [
                (b.bar_date, b.open, b.high, b.low, b.close, b.volume)
                for b in bars
            ]
            for symbol, bars in materialised.stock_bars.items()
        },
        "chains": {
            symbol: {
                day: [
                    (q.symbol, q.option_type, q.strike, q.dte, q.mark, q.bid,
                     q.ask, q.delta, q.implied_volatility, q.volume,
                     q.underlying_price)
                    for q in snap.all_quotes()
                ]
                for day, snap in per_day.items()
            }
            for symbol, per_day in materialised.chains.items()
        },
    }


# --------------------------------------------------------------------------- #
# 1. run() is exactly replay(materialise())
# --------------------------------------------------------------------------- #
class TestRunIsMaterialisePlusReplay:
    def test_the_split_is_invisible_on_the_put_fixture(self, falling_then_flat):
        days, closes, exps = falling_then_flat
        direct = _simulator("XYZ", closes, exps, days).run()

        sim = _simulator("XYZ", closes, exps, days)
        split = sim.replay(sim.materialise())

        assert _fingerprint(split) == _fingerprint(direct)

    def test_the_split_is_invisible_on_the_call_fixture(self, dip_then_recovering):
        """The call leg is where a refactor is most likely to drift.

        `falling_then_flat` never writes a covered call (the cost-basis floor
        refuses every strike), so it cannot catch a split that broke call
        selection, rolling or early assignment.
        """
        days, closes, exps = dip_then_recovering
        direct = _simulator("XYZ", closes, exps, days).run()

        sim = _simulator("XYZ", closes, exps, days)
        split = sim.replay(sim.materialise())

        fingerprint = _fingerprint(split)
        assert fingerprint == _fingerprint(direct)
        assert fingerprint["ledger"], "fixture placed no trades; the test is vacuous"

    def test_run_delegates_rather_than_duplicating_the_prologue(self, falling_then_flat):
        """`run()` must BE the composition, not a parallel copy of it.

        A `run()` that reimplemented the prologue would pass the equality tests
        above forever and drift the moment either half changed.
        """
        days, closes, exps = falling_then_flat
        sim = _simulator("XYZ", closes, exps, days)
        produced = []
        real_materialise = sim.materialise

        def _record():
            out = real_materialise()
            produced.append(out)
            return out

        with patch.object(sim, "materialise", side_effect=_record) as m, \
                patch.object(sim, "replay", wraps=sim.replay) as r:
            sim.run()
        assert m.call_count == 1
        assert r.call_count == 1
        assert produced and r.call_args[0][0] is produced[0], (
            "run() replayed something other than the object materialise() "
            "returned — it is not the composition of the two halves"
        )

    def test_an_empty_window_still_refuses_in_materialise(self):
        """The zero-trade guard moved into materialise; it must still fire."""
        provider = ScriptedProvider("XYZ", {}, [])
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        sim = Simulator(Config(), provider, builder, ["XYZ"],
                        date(2024, 6, 3), date(2024, 6, 28))
        with pytest.raises(ValueError, match="refusing to report a zero-trade run"):
            sim.materialise()
        with pytest.raises(ValueError, match="refusing to report a zero-trade run"):
            sim.run()


# --------------------------------------------------------------------------- #
# 2. Two replays on one Materialised, and the no-mutation contract
# --------------------------------------------------------------------------- #
class TestReplayingOneMaterialisedTwice:
    def test_mid_and_bid_each_match_a_fresh_run(self, dip_then_recovering):
        days, closes, exps = dip_then_recovering
        fresh_mid = _simulator("XYZ", closes, exps, days, fill_haircut=0.25).run()
        fresh_bid = _simulator("XYZ", closes, exps, days, fill_haircut=1.0).run()

        sim = _simulator("XYZ", closes, exps, days, fill_haircut=0.25)
        materialised = sim.materialise()
        replay_mid = sim.replay(materialised)
        replay_bid = sim.replay(materialised, fill_haircut=1.0)

        assert _fingerprint(replay_mid) == _fingerprint(fresh_mid)
        assert _fingerprint(replay_bid) == _fingerprint(fresh_bid)
        assert _fingerprint(fresh_mid) != _fingerprint(fresh_bid), (
            "the two haircuts produced identical runs; this fixture cannot "
            "distinguish them, so the test above proves nothing"
        )

    def test_a_haircut_override_does_not_stick_to_the_simulator(self, dip_then_recovering):
        """`replay(..., fill_haircut=1.0)` is per-call, not a setter.

        If it mutated `self.fill_haircut`, the mid/bid/mid sequence a sweep does
        would silently return bid numbers for the third pass.
        """
        days, closes, exps = dip_then_recovering
        sim = _simulator("XYZ", closes, exps, days, fill_haircut=0.25)
        materialised = sim.materialise()
        first = sim.replay(materialised)
        sim.replay(materialised, fill_haircut=1.0)
        third = sim.replay(materialised)
        assert sim.fill_haircut == 0.25
        assert _fingerprint(third) == _fingerprint(first)

    def test_replay_never_mutates_the_materialised(self, dip_then_recovering):
        """The contract a sweep depends on: chains are shared, so they are read-only.

        MUTATION CHECK: make `replay` (or anything it hands the chains to) append
        to, drop from, or reorder a `ChainSnapshot`'s put/call list and this
        fails. Deep-compares every quote field, not object identity, so a
        replacement quote is caught as well as an in-place edit.
        """
        days, closes, exps = dip_then_recovering
        sim = _simulator("XYZ", closes, exps, days)
        materialised = sim.materialise()

        before = copy.deepcopy(_chain_fingerprint(materialised))
        sim.replay(materialised)
        sim.replay(materialised, fill_haircut=1.0)
        after = _chain_fingerprint(materialised)

        assert after == before, "a replay mutated the Materialised it was handed"
        # Identity too: the broker/adapter get the SAME snapshot objects, so a
        # copy-on-read that quietly fixed a mutation bug would also be caught.
        assert all(
            isinstance(snap.puts, list) and isinstance(snap.calls, list)
            for per_day in materialised.chains.values()
            for snap in per_day.values()
        )

    def test_a_replay_refuses_a_materialised_from_another_window(self, falling_then_flat):
        """Fail closed, exactly as ChainStore._covers does one layer down."""
        days, closes, exps = falling_then_flat
        sim = _simulator("XYZ", closes, exps, days)
        materialised = sim.materialise()

        other = _simulator("XYZ", closes, exps, days[:15])
        with pytest.raises(ValueError, match="does not match this simulator"):
            other.replay(materialised)

    def test_a_replay_refuses_a_materialised_with_a_shorter_dte_reach(
            self, falling_then_flat):
        days, closes, exps = falling_then_flat
        provider = ScriptedProvider("XYZ", closes, exps)
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        narrow = Simulator(Config(), provider, builder, ["XYZ"], days[0], days[-1],
                           starting_cash=50_000.0, max_dte=3).materialise()
        wide = Simulator(Config(), provider, builder, ["XYZ"], days[0], days[-1],
                         starting_cash=50_000.0, max_dte=7)
        with pytest.raises(ValueError, match="DTE"):
            wide.replay(narrow)


# --------------------------------------------------------------------------- #
# 3. evaluate_symbol materialises once
# --------------------------------------------------------------------------- #
class _CountingProvider:
    """A ScriptedProvider that counts the calls that reach it."""

    def __init__(self, inner):
        self._inner = inner
        self.stock_bar_calls = []
        self.universe_calls = 0
        self.option_bar_calls = 0

    def get_stock_bars(self, symbol, start, end):
        self.stock_bar_calls.append((symbol, start, end))
        return self._inner.get_stock_bars(symbol, start, end)

    def get_contract_universe(self, *a, **kw):
        self.universe_calls += 1
        return self._inner.get_contract_universe(*a, **kw)

    def get_option_bars(self, *a, **kw):
        self.option_bar_calls += 1
        return self._inner.get_option_bars(*a, **kw)


class TestEvaluateSymbolMaterialisesOnce:
    def _run(self, tmp_path, fixture, **kw):
        from src.backtesting.evaluate import evaluate_symbol

        days, closes, exps = fixture
        provider = _CountingProvider(ScriptedProvider("XYZ", closes, exps))
        config = Config()
        config._config["stocks"]["symbols"] = ["XYZ"]
        report, sensitivity = evaluate_symbol(
            "XYZ", days[0], days[-1], config=config,
            starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)),
            bar_provider=provider,
            **kw,
        )
        return provider, report, sensitivity

    def test_bars_are_fetched_once_for_the_whole_evaluation(
            self, tmp_path, dip_then_recovering):
        """Was FOUR calls: two `_load_stock_bars` and two `_closes` (D6).

        The two that came from `_score` are the reason a warm run still touched
        the network; they are gone, and the two replays now share one
        materialisation.
        """
        provider, report, sensitivity = self._run(tmp_path, dip_then_recovering)
        assert sensitivity is not None, "sensitivity pass did not run"
        assert len(provider.stock_bar_calls) == 1, (
            f"expected exactly one bar fetch, got {provider.stock_bar_calls}"
        )

    def test_chains_are_built_once_across_both_fill_passes(
            self, tmp_path, dip_then_recovering):
        with patch.object(
            Simulator, "_build_chains", autospec=True,
            side_effect=Simulator._build_chains,
        ) as spy:
            self._run(tmp_path, dip_then_recovering)
        assert spy.call_count == 1, (
            f"_build_chains ran {spy.call_count} times; the mid and bid passes "
            "are re-materialising instead of sharing one Materialised"
        )

    def test_the_scored_report_is_unchanged_by_reusing_the_bars(
            self, tmp_path, dip_then_recovering):
        """`_score` reads the materialised bars instead of re-fetching them.

        The clipped slice must equal what a fresh fetch of
        [result.start, result.end] returned, or the buy-and-hold benchmark
        silently changes its entry price.
        """
        provider, report, _ = self._run(tmp_path, dip_then_recovering)
        assert report.benchmark is not None
        days, closes, _exps = dip_then_recovering
        assert report.benchmark.entry_price == pytest.approx(closes[days[0]])
        # A warm-up bar must NOT have leaked into the benchmark window.
        assert report.start == days[0]
