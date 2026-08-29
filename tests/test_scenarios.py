"""The scenario sweep (FC-060 Layer 2, D2/D3/D7/D8/D9).

A sweep is a multiple-comparisons machine pointed at a strategy that trades real
money, so most of what is pinned here is not "does it compute a number" but "can
it produce a number that means something other than what it says":

* an override that silently does nothing (a key the replay never reads, or one
  shadowed by an environment variable) — refused, with the reason;
* an override that quietly changes what the chain contains — refused, with the
  reason;
* a replay that re-fetches, which would make the whole cost model a fiction
  while leaving the results correct and therefore invisible;
* an ``insufficient`` cell rendered as a return, which lets "nothing happened"
  contribute to a ranking;
* one bad arm taking the other fifty-nine down with it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src.backtesting.data.chain_store import ChainStore
from src.backtesting.engine.simulator import Simulator
from src.backtesting.scenarios import (
    OverrideError,
    Scenario,
    ScenarioResult,
    SweepResult,
    apply_overrides,
    render_json,
    render_markdown,
    run_sweep,
    sign_agreement,
    validate_overrides,
)
from src.utils.config import Config

from .test_backtest_simulator import ScriptedProvider, _weekdays


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class _MultiSymbolProvider:
    """Two symbols, each backed by a ``ScriptedProvider``, with a call counter."""

    def __init__(self, per_symbol):
        self._per_symbol = per_symbol
        self.calls = 0
        self.stock_bar_calls = []

    def _root(self, occ: str) -> str:
        return next(s for s in self._per_symbol if occ.startswith(s))

    def get_stock_bars(self, symbol, start, end):
        self.calls += 1
        self.stock_bar_calls.append((symbol, start, end))
        return self._per_symbol[symbol].get_stock_bars(symbol, start, end)

    def get_contract_universe(self, underlying, *args, **kwargs):
        self.calls += 1
        return self._per_symbol[underlying].get_contract_universe(
            underlying, *args, **kwargs)

    def get_option_bars(self, symbols, start, end):
        self.calls += 1
        out = {}
        for occ in symbols:
            out.update(
                self._per_symbol[self._root(occ)].get_option_bars([occ], start, end)
            )
        return out


@pytest.fixture
def two_symbols():
    """AAA and BBB over 30 sessions: a slide that assigns, then flat.

    Same shape as ``falling_then_flat`` — enough to place trades on both symbols
    so a sweep has something to rank, short enough to run sixty replays in a
    unit test.
    """
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 30)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        closes[d] = 100.0 - min(i, 10) * 3.0
    expirations = [d for d in days if d.weekday() == 4]
    providers = {
        "AAA": ScriptedProvider("AAA", closes, expirations),
        "BBB": ScriptedProvider("BBB", closes, expirations),
    }
    return days, _MultiSymbolProvider(providers)


@pytest.fixture
def sweep_config():
    config = Config()
    config._config["stocks"]["symbols"] = ["AAA", "BBB"]
    return config


# --------------------------------------------------------------------------- #
# 7. apply_overrides + the allowlist
# --------------------------------------------------------------------------- #
class TestApplyOverrides:
    def test_a_dotted_key_lands_on_a_deep_copy(self, sweep_config):
        scenario = apply_overrides(sweep_config, {
            "strategy.put_delta_range": [0.15, 0.25],
            "risk.max_position_size": 0.25,
        })
        assert scenario.put_delta_range == [0.15, 0.25]
        assert scenario.max_position_size == 0.25
        assert sweep_config.put_delta_range == [0.10, 0.20], "base was mutated"
        assert sweep_config.max_position_size == 0.35, "base was mutated"

    def test_two_scenarios_do_not_share_state(self, sweep_config):
        a = apply_overrides(sweep_config, {"strategy.min_put_premium": 0.30})
        b = apply_overrides(sweep_config, {"strategy.min_put_premium": 0.90})
        assert (a.min_put_premium, b.min_put_premium) == (0.30, 0.90)
        assert sweep_config.min_put_premium == 0.50
        # A nested list must not be shared either: mutating one arm's band in
        # place would silently move the other's.
        a._config["strategy"]["put_delta_range"].append(0.99)
        assert 0.99 not in sweep_config.put_delta_range
        assert 0.99 not in b.put_delta_range

    def test_a_missing_section_is_created_not_rejected(self, sweep_config):
        """`universe:` is absent from the wheel profile; its accessors default.

        An "absent" section is a legitimate starting state, which is exactly why
        the allowlist — not the current shape of `_config` — decides legality.
        """
        assert "universe" not in sweep_config._config
        scenario = apply_overrides(sweep_config, {
            "universe.excluded_symbols": ["NVDA"],
            "universe.max_spread_pct": 0.10,
        })
        assert scenario.excluded_symbols == {"NVDA"}
        assert scenario.max_spread_pct == 0.10
        assert sweep_config.excluded_symbols == set()
        assert sweep_config.max_spread_pct is None

    def test_an_unknown_key_raises_and_names_itself(self, sweep_config):
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"strategy.min_put_premuim": 0.3})
        assert "strategy.min_put_premuim" in str(exc.value)
        assert "not a known selection-only key" in str(exc.value)

    def test_put_target_dte_is_refused_with_the_chain_reach_reason(self, sweep_config):
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"strategy.put_target_dte": 14})
        message = str(exc.value)
        assert "universe_dte=8" in message
        assert "re-materialisation" in message

    def test_a_longer_call_target_dte_is_refused_but_a_shorter_one_is_not(
            self, sweep_config):
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"strategy.call_target_dte": 14})
        message = str(exc.value)
        assert "does NOT widen the chain" in message
        assert "not in the cached files" in message
        # Downward is a real question and stays askable.
        shortened = apply_overrides(sweep_config, {"strategy.call_target_dte": 4})
        assert shortened.call_target_dte == 4

    def test_profit_taking_is_refused_because_the_replay_never_reads_it(
            self, sweep_config):
        with pytest.raises(OverrideError) as exc:
            apply_overrides(
                sweep_config, {"risk.profit_taking.min_profit_target": 0.2})
        message = str(exc.value)
        assert "/monitor" in message
        assert "identical row" in message

    @pytest.mark.parametrize("key,value", [
        ("risk.use_call_stop_loss", True),
        ("alpaca.paper_trading", False),
        ("stocks.symbols", ["SPY"]),
        ("strategy_id", "covered_call"),
        ("bigquery_dataset", "somewhere_else"),
    ])
    def test_the_rest_of_the_refusal_list(self, sweep_config, key, value):
        with pytest.raises(OverrideError):
            apply_overrides(sweep_config, {key: value})

    @pytest.mark.parametrize("key,value,fragment", [
        ("strategy.put_limit_spread_fraction", 0.0, "does not honour limit prices"),
        ("strategy.call_limit_spread_fraction", 0.5, "fill_haircut"),
        ("universe.min_open_interest", 500, "hardcodes `open_interest: 0`"),
    ])
    def test_keys_the_replay_does_not_honour_are_refused(
            self, sweep_config, key, value, fragment):
        """Selection-only is NECESSARY but not SUFFICIENT.

        These three were on the plan's D3 allowlist and are correctly classified
        as selection-only — none invalidates a chain. They are still dead:
        `BacktestAlpacaClient.place_option_order` records the limit price and
        fills at `mid - haircut x half-spread` regardless, and
        `get_options_chain` hardcodes `open_interest: 0` so any floor rejects
        every call. Measured, not reasoned: a `put_limit_spread_fraction: 0.0`
        arm returned results byte-identical to base on all six
        effective-universe symbols over a year.
        """
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {key: value})
        assert fragment in str(exc.value)

    def test_an_env_shadowed_key_is_refused_rather_than_silently_ignored(
            self, sweep_config, monkeypatch):
        """EARNINGS_ENABLED wins over the yaml key (FC-013 DD-7).

        With it exported, an `earnings.enabled` arm is identical to base — and
        the sweep would report the two as tied, which is a false finding rather
        than a missing one.
        """
        assert apply_overrides(sweep_config, {"earnings.enabled": False}) is not None
        monkeypatch.setenv("EARNINGS_ENABLED", "true")
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"earnings.enabled": False})
        assert "EARNINGS_ENABLED" in str(exc.value)
        assert "silently ignored" in str(exc.value)

    def test_roller_enabled_is_shadowed_the_same_way(self, sweep_config, monkeypatch):
        monkeypatch.setenv("ROLLER_ENABLED", "false")
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"rolling.enabled": False})
        assert "ROLLER_ENABLED" in str(exc.value)

    def test_validation_happens_before_anything_is_written(self, sweep_config):
        """A scenario must never be half-applied.

        The bad key is second, so a naive implementation writes the first one
        into the copy and then raises — leaving a config that is neither the base
        nor the scenario if the caller catches and continues.
        """
        with pytest.raises(OverrideError):
            apply_overrides(sweep_config, {
                "strategy.min_put_premium": 0.30,
                "strategy.put_target_dte": 14,
            })
        assert sweep_config.min_put_premium == 0.50

    def test_the_allowlist_covers_every_key_the_example_file_uses(self):
        """The shipped example must not demonstrate a refused override."""
        import yaml

        with open("examples/scenarios_example.yaml") as fh:
            payload = yaml.safe_load(fh)
        for entry in payload["scenarios"]:
            validate_overrides(entry.get("overrides") or {})


# --------------------------------------------------------------------------- #
# 8. run_sweep
# --------------------------------------------------------------------------- #
class TestRunSweep:
    def _sweep(self, tmp_path, two_symbols, sweep_config, scenarios, **kw):
        days, provider = two_symbols
        return provider, run_sweep(
            sweep_config, scenarios, ["AAA", "BBB"], days[0], days[-1],
            starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)),
            bar_provider=provider,
            quiet_logs=False,
            **kw,
        )

    def test_one_materialisation_per_symbol_and_zero_calls_during_replays(
            self, tmp_path, two_symbols, sweep_config):
        """The whole cost model, asserted on a counter.

        MUTATION CHECK: make `_replay_one` build its own chains (or drop the
        `materialised` argument so `run()` is called) and the runner raises
        rather than quietly taking sixty times as long.
        """
        scenarios = [
            Scenario("tighter", {"strategy.put_delta_range": [0.15, 0.25]}),
            Scenario("cheaper", {"strategy.min_put_premium": 0.30}),
        ]
        provider, result = self._sweep(tmp_path, two_symbols, sweep_config, scenarios)

        assert result.provider_calls_during_replays == 0
        assert len(provider.stock_bar_calls) == 2, (
            f"expected one bar fetch per symbol, got {provider.stock_bar_calls}"
        )
        assert set(result.materialise_seconds) == {"AAA:all", "BBB:all"}
        # 3 scenarios (base is implicit) x 2 symbols
        assert len(result.rows) == 6
        assert len(result.replay_seconds) == 6

    def test_base_is_implicit_and_runs_first(self, tmp_path, two_symbols, sweep_config):
        scenarios = [Scenario("tighter", {"strategy.put_delta_range": [0.15, 0.25]})]
        _provider, result = self._sweep(tmp_path, two_symbols, sweep_config, scenarios)
        assert result.scenarios == ["base", "tighter"]
        assert [r.scenario for r in result.rows[:2]] == ["base", "tighter"]
        assert result.scenario_overrides["base"] == {}

    def test_an_explicit_base_is_reordered_not_duplicated(
            self, tmp_path, two_symbols, sweep_config):
        scenarios = [
            Scenario("tighter", {"strategy.put_delta_range": [0.15, 0.25]}),
            Scenario("base", {}),
        ]
        _provider, result = self._sweep(tmp_path, two_symbols, sweep_config, scenarios)
        assert result.scenarios == ["base", "tighter"]

    def test_one_row_per_scenario_and_symbol(self, tmp_path, two_symbols, sweep_config):
        scenarios = [
            Scenario("a", {"strategy.min_put_premium": 0.30}),
            Scenario("b", {"strategy.min_put_premium": 0.90}),
        ]
        _provider, result = self._sweep(tmp_path, two_symbols, sweep_config, scenarios)
        keys = {(r.scenario, r.symbol, r.split) for r in result.rows}
        assert keys == {
            (s, sym, "all") for s in ("base", "a", "b") for sym in ("AAA", "BBB")
        }

    def test_an_override_actually_changes_the_result(
            self, tmp_path, two_symbols, sweep_config):
        """Otherwise every test above passes on a runner that ignores overrides."""
        scenarios = [Scenario("no_trades", {"strategy.min_put_premium": 999.0})]
        _provider, result = self._sweep(tmp_path, two_symbols, sweep_config, scenarios)
        base = result.cell("base", "AAA")
        blocked = result.cell("no_trades", "AAA")
        assert base.puts_sold > 0, "fixture placed no puts; the test is vacuous"
        assert blocked.puts_sold == 0, "the premium floor override did not apply"
        assert result.scenario_config_hashes["base"] != \
            result.scenario_config_hashes["no_trades"]

    def test_a_raising_scenario_records_an_error_and_the_others_complete(
            self, tmp_path, two_symbols, sweep_config):
        """One bad arm must not lose the rest their results."""
        real_replay = Simulator.replay

        def _boom(self, materialised, **kw):
            if self.config.min_put_premium == 99.0:
                raise RuntimeError("scenario blew up mid-replay")
            return real_replay(self, materialised, **kw)

        scenarios = [
            Scenario("boom", {"strategy.min_put_premium": 99.0}),
            Scenario("fine", {"strategy.min_put_premium": 0.30}),
        ]
        with patch.object(Simulator, "replay", _boom):
            _provider, result = self._sweep(
                tmp_path, two_symbols, sweep_config, scenarios)

        errored = result.errors
        assert {r.scenario for r in errored} == {"boom"}
        assert len(errored) == 2, "both symbols should have recorded the failure"
        assert all("scenario blew up mid-replay" in (r.error or "") for r in errored)
        assert all(r.verdict is None for r in errored)
        for name in ("base", "fine"):
            for symbol in ("AAA", "BBB"):
                assert result.cell(name, symbol).ok, f"{name}/{symbol} lost its result"

    def test_a_bad_override_raises_before_any_replay_starts(
            self, tmp_path, two_symbols, sweep_config):
        """Milliseconds, not after eight arms have been replayed."""
        days, provider = two_symbols
        scenarios = [
            Scenario("fine", {"strategy.min_put_premium": 0.30}),
            Scenario("typo", {"strategy.put_target_dte": 14}),
        ]
        with pytest.raises(OverrideError):
            run_sweep(sweep_config, scenarios, ["AAA", "BBB"], days[0], days[-1],
                      starting_cash=50_000.0, chain_store=ChainStore(str(tmp_path)),
                      bar_provider=provider, quiet_logs=False)
        assert provider.calls == 0, "a replay started before validation finished"

    def test_duplicate_scenario_names_are_refused(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        with pytest.raises(ValueError, match="duplicate scenario name"):
            run_sweep(sweep_config,
                      [Scenario("x", {}), Scenario("x", {"risk.max_position_size": 0.2})],
                      ["AAA"], days[0], days[-1], starting_cash=50_000.0,
                      chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
                      quiet_logs=False)

    def test_a_failed_materialisation_errors_every_scenario_for_that_window(
            self, tmp_path, two_symbols, sweep_config):
        """A never-measured cell must never read as a measured one."""
        days, provider = two_symbols
        with patch.object(Simulator, "materialise",
                          side_effect=RuntimeError("no data for you")):
            result = run_sweep(
                sweep_config, [Scenario("a", {"strategy.min_put_premium": 0.30})],
                ["AAA"], days[0], days[-1], starting_cash=50_000.0,
                chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
                quiet_logs=False)
        assert len(result.rows) == 2
        assert all(r.error and "no data for you" in r.error for r in result.rows)

    def test_a_replay_that_reaches_the_provider_is_a_hard_failure(
            self, tmp_path, two_symbols, sweep_config):
        """MUTATION CHECK, from the other direction.

        The counter must not merely be logged: a leak has to stop the sweep,
        because the numbers it produces stay correct while the cost model it is
        built on is void.
        """
        days, provider = two_symbols
        real_replay = Simulator.replay

        def _leaky(self, materialised, **kw):
            self.provider.get_stock_bars("AAA", days[0], days[-1])
            return real_replay(self, materialised, **kw)

        with patch.object(Simulator, "replay", _leaky):
            with pytest.raises(RuntimeError, match="escaped during the replay loop"):
                run_sweep(sweep_config, [Scenario("a", {})], ["AAA"],
                          days[0], days[-1], starting_cash=50_000.0,
                          chain_store=ChainStore(str(tmp_path)),
                          bar_provider=provider, quiet_logs=False)

    def test_quieting_the_logs_does_not_change_a_single_number(
            self, tmp_path, two_symbols, sweep_config):
        """D11: silencing INFO is an output-volume change, nothing more."""
        days, provider = two_symbols

        def _rows(quiet, sub):
            result = run_sweep(
                sweep_config, [Scenario("a", {"strategy.min_put_premium": 0.30})],
                ["AAA"], days[0], days[-1], starting_cash=50_000.0,
                chain_store=ChainStore(str(tmp_path / sub)),
                bar_provider=_MultiSymbolProvider(provider._per_symbol),
                quiet_logs=quiet)
            return [
                {k: v for k, v in r.as_dict().items() if k != "replay_seconds"}
                for r in result.rows
            ]

        loud = _rows(False, "loud")
        quiet = _rows(True, "quiet")
        assert loud == quiet
        assert any(r["puts_sold"] for r in loud), "fixture traded nothing"

    def test_log_levels_are_restored_afterwards(self, tmp_path, two_symbols,
                                                sweep_config):
        import logging

        before = logging.getLogger("src").level
        self._sweep(tmp_path, two_symbols, sweep_config, [Scenario("a", {})])
        assert logging.getLogger("src").level == before


class TestTheRejectionTallySurvivesQuietLogs:
    """D11's actual claim, isolated from the structlog proxy cache.

    The claim is narrow: ``RejectionTally`` installs its processor at the FRONT
    of the structlog chain, ahead of ``structlog.stdlib.filter_by_level``, so an
    event the stdlib level then DROPS is still counted. If that ordering is ever
    reversed, a sweep silently reports zero blocked days for every arm.

    Tested on a logger created inside the tally rather than through a full
    replay, because the strategy's module loggers cache their processor chain on
    first use and therefore cannot answer this question after the first replay in
    a process — see ``runner``'s module docstring, and treat that as the separate
    pre-existing defect it is.
    """

    def test_an_event_the_stdlib_level_drops_is_still_counted(self, monkeypatch):
        import itertools
        import logging

        import structlog

        from src.backtesting.engine.rejections import RejectionTally
        from src.utils import clock
        from src.utils.logger import setup_logging

        setup_logging("INFO")  # the config the CLI actually runs under
        name = f"src.fc060b_probe_{next(itertools.count())}_{id(self)}"
        logging.getLogger(name).setLevel(logging.WARNING)
        clock.set_now(__import__("datetime").datetime(2024, 6, 3, 16, 0))
        try:
            tally = RejectionTally()
            with tally:
                # Bound inside the tally, exactly as a fresh process's strategy
                # loggers are: the chain it caches includes the tally.
                structlog.get_logger(name).info(
                    "blocked",
                    event_type="stage_7_complete_not_found",
                )
            assert tally.summary() == {
                "no put cleared delta/DTE/premium (stage 7)": 1
            }, (
                "an INFO event the stdlib WARNING level drops was not counted — "
                "the tally's processor is no longer ahead of filter_by_level, so "
                "every quieted sweep now reports zero blocked days"
            )
        finally:
            clock.set_now(None)
            logging.getLogger(name).setLevel(logging.NOTSET)


# --------------------------------------------------------------------------- #
# 9. Holdout
# --------------------------------------------------------------------------- #
class TestHoldout:
    def test_two_windows_are_materialised_and_rows_carry_their_split(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        split_at = days[15]
        result = run_sweep(
            sweep_config, [Scenario("a", {"strategy.min_put_premium": 0.30})],
            ["AAA"], days[0], days[-1], holdout_start=split_at,
            starting_cash=50_000.0, chain_store=ChainStore(str(tmp_path)),
            bar_provider=provider, quiet_logs=False)

        assert [w[0] for w in result.windows] == ["fit", "holdout"]
        assert result.windows[0][2] == split_at - timedelta(days=1)
        assert result.windows[1][1] == split_at
        assert set(result.materialise_seconds) == {"AAA:fit", "AAA:holdout"}
        assert {r.split for r in result.rows} == {"fit", "holdout"}
        assert result.has_holdout
        assert result.provider_calls_during_replays == 0
        # Two windows means two bar fetches, not one per scenario.
        assert len(provider.stock_bar_calls) == 2

    def test_the_windows_do_not_overlap(self, tmp_path, two_symbols, sweep_config):
        """An overlapping split lets an arm be chosen on data it is 'validated' on."""
        days, provider = two_symbols
        result = run_sweep(
            sweep_config, [Scenario("a", {})], ["AAA"], days[0], days[-1],
            holdout_start=days[15], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False)
        (_, fit_start, fit_end), (_, hold_start, hold_end) = result.windows
        assert fit_end < hold_start
        assert fit_start == days[0] and hold_end == days[-1]

    def test_a_holdout_outside_the_window_is_refused(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        with pytest.raises(ValueError, match="must fall inside"):
            run_sweep(sweep_config, [Scenario("a", {})], ["AAA"],
                      days[0], days[-1], holdout_start=days[-1] + timedelta(days=30),
                      starting_cash=50_000.0, chain_store=ChainStore(str(tmp_path)),
                      bar_provider=provider, quiet_logs=False)

    def test_sign_agreement_measures_the_delta_against_base_not_the_raw_return(self):
        """A scenario positive in both windows has shown nothing on its own."""
        result = _hand_built_holdout()
        # `winner` beats base in both windows -> agrees on both symbols.
        assert sign_agreement(result, "winner") == (2, 2)
        # `flipper` beats base in the fit window and loses in the holdout on
        # AAA, agrees on BBB.
        assert sign_agreement(result, "flipper") == (1, 2)
        # A symbol whose holdout cell is `insufficient` is excluded from BOTH
        # the numerator and the denominator, never counted as agreement.
        assert sign_agreement(result, "partial") == (1, 1)


def _hand_built_holdout() -> SweepResult:
    """A deterministic SweepResult, so the report assertions test the REPORT."""
    def row(scenario, symbol, split, ann, verdict="marginal", **kw):
        return ScenarioResult(
            scenario=scenario, symbol=symbol,
            start=date(2025, 1, 1), end=date(2025, 6, 30), split=split,
            config_hash="deadbeefdeadbeef", verdict=verdict,
            annualized_return=ann, decision_days=120, demote=(verdict == "unfit"),
            **kw
        )

    rows = []
    for split in ("fit", "holdout"):
        for symbol in ("AAA", "BBB"):
            rows.append(row("base", symbol, split, 0.10))
    # winner: +2pts in both windows, both symbols
    for split in ("fit", "holdout"):
        for symbol in ("AAA", "BBB"):
            rows.append(row("winner", symbol, split, 0.12))
    # flipper: +2pts fit / -2pts holdout on AAA; +2/+2 on BBB
    rows.append(row("flipper", "AAA", "fit", 0.12))
    rows.append(row("flipper", "AAA", "holdout", 0.08))
    rows.append(row("flipper", "BBB", "fit", 0.12))
    rows.append(row("flipper", "BBB", "holdout", 0.12))
    # partial: BBB never completed a cycle in the holdout
    rows.append(row("partial", "AAA", "fit", 0.11))
    rows.append(row("partial", "AAA", "holdout", 0.11))
    rows.append(row("partial", "BBB", "fit", 0.11))
    rows.append(row("partial", "BBB", "holdout", None, verdict="insufficient"))

    return SweepResult(
        rows=rows,
        scenarios=["base", "winner", "flipper", "partial"],
        symbols=["AAA", "BBB"],
        windows=[("fit", date(2025, 1, 1), date(2025, 3, 31)),
                 ("holdout", date(2025, 4, 1), date(2025, 6, 30))],
        base_config_hash="basehash00000000",
        scenario_config_hashes={n: f"hash{n}" for n in
                                ("base", "winner", "flipper", "partial")},
        scenario_overrides={"base": {}, "winner": {"risk.max_position_size": 0.2},
                            "flipper": {"strategy.min_put_premium": 0.3},
                            "partial": {"strategy.max_stock_price": 800.0}},
        materialise_seconds={"AAA:fit": 1.0, "AAA:holdout": 1.0},
        replay_seconds={"base:AAA:fit": 0.1},
        wall_seconds=12.5,
        starting_cash=100_000.0,
    )


# --------------------------------------------------------------------------- #
# 10. The report
# --------------------------------------------------------------------------- #
class TestReport:
    def test_the_per_symbol_grid_is_always_present(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "| scenario | AAA | BBB |" in markdown
        # One grid per window.
        assert markdown.count("| scenario | AAA | BBB |") == 2

    def test_an_insufficient_cell_is_flagged_never_rendered_as_a_return(self):
        """'nothing happened' must not contribute a number to a ranking."""
        result = _hand_built_holdout()
        markdown = render_markdown(result)
        assert "`insuf`" in markdown
        # ...and it is excluded from the median rather than counted as zero.
        holdout_partial = [
            line for line in markdown.splitlines()
            if line.startswith("| partial | holdout |")
        ]
        assert holdout_partial, "per-scenario summary row is missing"
        assert "| 1 | 1 |" in holdout_partial[0], (
            f"expected measured=1, insuf=1 in {holdout_partial[0]!r}"
        )
        assert "+11.0%" in holdout_partial[0], "median must ignore the insuf cell"

    def test_a_missing_number_renders_as_a_dash_not_zero_percent(self):
        """'+0.0%' reads as 'measured, exactly flat' — a different claim."""
        result = _hand_built_holdout()
        result.rows = [r for r in result.rows if r.scenario != "base"]
        result.scenarios = ["winner"]
        markdown = render_markdown(result)
        assert "+0.0%" not in markdown

    def test_the_bias_footer_and_the_cross_scenario_caveat_are_present(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "Known biases" in markdown
        assert "FC-056" in markdown
        assert "biased against the call-heavier one" in markdown
        assert "Premium understated" in markdown

    def test_the_report_says_it_was_not_persisted(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "backtest_runs" in markdown
        assert "Not persisted" in markdown

    def test_the_holdout_table_carries_sign_agreement(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "## Fit vs holdout" in markdown
        assert "sign agreement" in markdown
        flipper = [line for line in markdown.splitlines()
                   if line.startswith("| flipper |") and line.endswith("1/2 |")]
        assert flipper, (
            "no holdout row for 'flipper' carried its sign agreement; it beats "
            "base in the fit window on both symbols and loses on AAA out of "
            "sample, so the column must read 1/2"
        )

    def test_the_scenario_definitions_show_every_override(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "`risk.max_position_size` = `0.2`" in markdown
        assert "_(base — no overrides)_" in markdown

    def test_errors_get_their_own_section_and_a_warning_banner(self):
        result = _hand_built_holdout()
        result.rows.append(ScenarioResult(
            scenario="winner", symbol="AAA", start=date(2025, 1, 1),
            end=date(2025, 3, 31), split="fit", config_hash="x",
            error="corporate_action: XYZ moved 0.101x on 2024-06-10",
        ))
        markdown = render_markdown(result)
        assert "## Errors" in markdown
        assert "corporate_action" in markdown
        assert "not** implicitly fine" in markdown

    def test_json_round_trips_and_carries_the_provenance(self):
        result = _hand_built_holdout()
        payload = json.loads(render_json(result))
        assert payload["persisted"] is False
        assert payload["symbols"] == ["AAA", "BBB"]
        assert payload["base_config_hash"] == "basehash00000000"
        assert len(payload["rows"]) == len(result.rows)
        assert payload["rows"][0]["start"] == "2025-01-01"
        assert payload["sign_agreement"]["flipper"] == {
            "agreeing": 1, "comparable": 2}
        assert payload["timing"]["wall_seconds"] == 12.5
        assert payload["known_biases"], "the JSON must carry the biases too"
        # `insufficient` is an explicit field, not something a consumer has to
        # re-derive from a null return.
        partial = [r for r in payload["rows"]
                   if r["scenario"] == "partial" and r["symbol"] == "BBB"
                   and r["split"] == "holdout"][0]
        assert partial["insufficient"] is True

    def test_a_single_window_sweep_omits_the_holdout_table(self):
        result = _hand_built_holdout()
        result.windows = [("all", date(2025, 1, 1), date(2025, 6, 30))]
        for row in result.rows:
            row.split = "all"
        markdown = render_markdown(result)
        assert "## Fit vs holdout" not in markdown
        assert json.loads(render_json(result))["sign_agreement"] is None


# --------------------------------------------------------------------------- #
# 11. The CLI
# --------------------------------------------------------------------------- #
class TestSweepCLI:
    def _yaml(self, tmp_path, body):
        path = tmp_path / "scenarios_fc060b.yaml"
        path.write_text(body)
        return str(path)

    def _invoke(self, tmp_path, two_symbols, argv):
        import main as main_module
        from src.backtesting.scenarios import runner as runner_module

        days, provider = two_symbols
        store = ChainStore(str(tmp_path / "chains"))

        class _Factory:
            @staticmethod
            def from_config(config):
                return provider

        with patch.object(runner_module, "AlpacaDataProvider", _Factory), \
                patch.object(runner_module.ChainStore, "from_env",
                             classmethod(lambda cls, *a, **kw: store)), \
                patch.object(main_module, "setup_logging", lambda *a, **kw: None), \
                patch("sys.argv", argv):
            try:
                main_module.main()
                return 0
            except SystemExit as exc:
                return exc.code or 0

    def test_a_two_scenario_file_writes_both_artifacts(self, tmp_path, two_symbols):
        days, _provider = two_symbols
        scenarios = self._yaml(tmp_path, (
            "scenarios:\n"
            "  - name: tighter\n"
            "    overrides:\n"
            "      strategy.put_delta_range: [0.15, 0.25]\n"
            "  - name: cheaper\n"
            "    overrides:\n"
            "      strategy.min_put_premium: 0.30\n"
        ))
        out = tmp_path / "sweep.md"
        json_out = tmp_path / "sweep.json"
        code = self._invoke(tmp_path, two_symbols, [
            "main.py", "--command", "sweep", "--scenarios", scenarios,
            "--symbols", "AAA,BBB",
            "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
            "--starting-cash", "50000", "--no-sensitivity",
            "--out", str(out), "--json-out", str(json_out),
        ])
        assert code == 0
        assert out.exists() and json_out.exists()
        payload = json.loads(json_out.read_text())
        assert payload["scenarios"] == ["base", "tighter", "cheaper"]
        assert len(payload["rows"]) == 6
        assert payload["provider_calls"]["during_replays"] == 0
        assert "| scenario | AAA | BBB |" in out.read_text()

    def test_an_errored_scenario_exits_non_zero(self, tmp_path, two_symbols):
        days, _provider = two_symbols
        scenarios = self._yaml(tmp_path, (
            "scenarios:\n"
            "  - name: boom\n"
            "    overrides:\n"
            "      strategy.min_put_premium: 99.0\n"
        ))
        real_replay = Simulator.replay

        def _boom(self, materialised, **kw):
            if self.config.min_put_premium == 99.0:
                raise RuntimeError("scenario blew up mid-replay")
            return real_replay(self, materialised, **kw)

        out = tmp_path / "sweep.md"
        with patch.object(Simulator, "replay", _boom):
            code = self._invoke(tmp_path, two_symbols, [
                "main.py", "--command", "sweep", "--scenarios", scenarios,
                "--symbols", "AAA",
                "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
                "--starting-cash", "50000", "--no-sensitivity",
                "--out", str(out),
            ])
        assert code == 1, "a sweep with an unmeasured cell must not exit 0"
        assert "## Errors" in out.read_text()

    def test_a_refused_override_fails_the_command(self, tmp_path, two_symbols):
        days, _provider = two_symbols
        scenarios = self._yaml(tmp_path, (
            "scenarios:\n"
            "  - name: longer_dte\n"
            "    overrides:\n"
            "      strategy.put_target_dte: 14\n"
        ))
        code = self._invoke(tmp_path, two_symbols, [
            "main.py", "--command", "sweep", "--scenarios", scenarios,
            "--symbols", "AAA",
            "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
        ])
        assert code == 1


class TestScenarioFileParsing:
    def _load(self, tmp_path, body):
        from main import load_scenarios

        path = tmp_path / "s.yaml"
        path.write_text(body)
        return load_scenarios(str(path))

    def test_a_minimal_file_parses(self, tmp_path):
        scenarios = self._load(tmp_path, (
            "scenarios:\n"
            "  - name: a\n"
            "    overrides:\n"
            "      strategy.min_put_premium: 0.30\n"
        ))
        assert [s.name for s in scenarios] == ["a"]
        assert scenarios[0].overrides == {"strategy.min_put_premium": 0.30}
        assert scenarios[0].fill_haircut is None

    def test_a_scenario_may_carry_only_a_fill_haircut(self, tmp_path):
        """`fill_haircut` is a scenario FIELD, not a config key (D3).

        `config_hash` hashes the module-level default, so two arms differing only
        in haircut would share a hash and be indistinguishable in any record.
        """
        scenarios = self._load(tmp_path,
                               "scenarios:\n  - name: at_the_bid\n    fill_haircut: 1.0\n")
        assert scenarios[0].fill_haircut == 1.0
        assert scenarios[0].overrides == {}

    @pytest.mark.parametrize("body,fragment", [
        ("nothing: here\n", "top-level 'scenarios:'"),
        ("scenarios: 3\n", "must be a list"),
        ("scenarios:\n  - overrides: {}\n", "has no 'name'"),
        ("scenarios:\n  - name: a\n    overrides: 3\n", "non-mapping"),
        ("scenarios:\n  - name: a\n    fill_haircut: 3.0\n", "outside [0, 1]"),
        ("scenarios:\n  - name: a\n    overides: {}\n", "unknown field"),
    ])
    def test_a_malformed_file_fails_loudly(self, tmp_path, body, fragment):
        with pytest.raises(SystemExit) as exc:
            self._load(tmp_path, body)
        assert fragment in str(exc.value)
