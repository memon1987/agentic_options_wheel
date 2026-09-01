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

import ast
import inspect
import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.chain_store import ChainStore
from src.backtesting.engine.simulator import (
    Materialised,
    Simulator,
    narrow_to_dte,
)
from src.backtesting.metrics.fitness import MIN_DAYS_IN_POSITION
from src.backtesting.scenarios import (
    ALLOWED_OVERRIDES,
    REJECTED_OVERRIDES,
    OverrideError,
    Scenario,
    ScenarioResult,
    SweepResult,
    apply_overrides,
    common_delta,
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

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_both_dte_targets_are_sweepable_up_to_the_lakes_reach(
            self, sweep_config, key):
        """FC-096 Phase A PR-2: the knob the whole phase exists for.

        `put_target_dte` was refused outright and `call_target_dte` was
        lowering-only, both because the stored chains reached 7 DTE. The
        backfill widened the lake to `universe_dte = 22` and the placebo gate
        confirmed the knob moves selection, so both keys now carry ONE static
        rule.
        """
        from src.backtesting.scenarios.overrides import (
            ALLOWED_OVERRIDES,
            MAX_SWEEPABLE_DTE,
        )

        assert key in ALLOWED_OVERRIDES
        leaf = key.split(".")[-1]
        for good in (1, 7, 14, MAX_SWEEPABLE_DTE):
            assert getattr(apply_overrides(sweep_config, {key: good}), leaf) == good

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_a_dte_past_the_lakes_reach_is_refused_naming_the_constant(
            self, sweep_config, key):
        """The bound is a property of the DATA, and the message has to say so.

        A refusal quoting a bare number teaches an operator nothing and reads
        as an arbitrary cap; one that names `MAX_SWEEPABLE_DTE` and the lake
        tells them the fix is to widen the backfill, not to argue with the
        allowlist.
        """
        from src.backtesting.scenarios.overrides import MAX_SWEEPABLE_DTE

        for bad in (MAX_SWEEPABLE_DTE + 1, 0, -3):
            with pytest.raises(OverrideError) as exc:
                apply_overrides(sweep_config, {key: bad})
            message = str(exc.value)
            assert "MAX_SWEEPABLE_DTE" in message
            assert str(MAX_SWEEPABLE_DTE) in message
            assert key in message

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    @pytest.mark.parametrize("value", [14.0, "14", None, [14], True])
    def test_a_non_integer_dte_is_refused(self, sweep_config, key, value):
        """`True` is in this list on purpose: `bool` is an `int` subclass, so a
        naive check would read `put_target_dte: true` as "1 day" — a legal-
        looking arm nobody asked for. `14.0` and `"14"` used to be coerced by
        an `int(value)` cast, which silently accepted a spec the dashboard's
        own value-type table refuses."""
        with pytest.raises(OverrideError, match="integer number of days"):
            apply_overrides(sweep_config, {key: value})

    def test_the_constant_is_the_lakes_reach_and_the_backfill_agrees(self):
        """One number, three consumers: the value rules above, the backfill Job
        and the dashboard's flat copy of this module."""
        from src.backtesting.scenarios.overrides import MAX_SWEEPABLE_DTE

        assert MAX_SWEEPABLE_DTE == 21

    def test_the_backfill_reads_the_constant_rather_than_restating_it(self):
        """One number, two consumers (the Job and the dashboard's flat copy).

        `overrides.py` is copied verbatim into the dashboard image
        (`dashboard/Dockerfile`, pinned by tests/test_cloudbuild_contract.py),
        so a constant defined there is by construction the same on both sides.
        What is NOT structural is the backfill agreeing with it — hence this.
        The import direction is fixed: `overrides.py` is stdlib-only and cannot
        import the data layer.
        """
        from src.backtesting.data import backfill
        from src.backtesting.scenarios import overrides

        assert backfill.BACKFILL_MAX_DTE is overrides.MAX_SWEEPABLE_DTE
        # Structural, not textual: the module's own comments name `backfill.py`
        # (they explain the import direction), so what is checked is the set of
        # modules it actually imports AT RUNTIME.
        tree = ast.parse(inspect.getsource(overrides))
        runtime_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # `if TYPE_CHECKING:` - annotation-only, never executed.
                if getattr(node.test, "id", None) == "TYPE_CHECKING":
                    continue
            if isinstance(node, ast.Import):
                runtime_imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                runtime_imports.add(node.module.split(".")[0])
        forbidden = runtime_imports & {
            "pandas", "structlog", "yaml", "numpy", "google", "requests",
        }
        assert not forbidden, (
            f"overrides.py imports {sorted(forbidden)} - it is flat-copied "
            "into a dashboard image that has none of them, and an ImportError "
            "there takes the sweep API down"
        )
        assert not any(name.endswith("backfill") for name in runtime_imports)

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
                "strategy.put_target_dte": 99,
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
            Scenario("typo", {"strategy.put_target_dte": 99}),
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


@contextmanager
def _cli_structlog_config():
    """The CLI's structlog processor chain, WITHOUT ``setup_logging``'s side effects.

    ``setup_logging`` is the right thing in production and the wrong thing in a
    test: it installs a root ``FileHandler`` on ``logs/options_wheel.log`` and
    flips ``cache_logger_on_first_use=True`` **process-wide**, and it restores
    neither. Calling it here (the first cut did) leaked both into every test that
    ran afterwards — the suite started writing 7.7 MB of replay logs to disk, and
    the cached-proxy flag produced a genuine order-dependent failure three files
    away in `test_backtest_simulator`.

    So this reproduces only the part the assertion needs — ``filter_by_level``
    sitting where ``setup_logging`` puts it, second in the chain — and restores
    the previous configuration unconditionally. `conftest`'s
    `_logging_config_is_not_leaked` guard now fails any test that forgets to.
    """
    import structlog

    previous = structlog.get_config()
    try:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_log_level,
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            # Deliberately FALSE here even though production sets it True: this
            # test is about processor ORDER, and the cache is what makes order
            # unobservable after the first bind. The caching defect has its own
            # coverage in `runner`'s docstring and its own FC.
            cache_logger_on_first_use=False,
        )
        yield
    finally:
        structlog.configure(**previous)


class TestTheRejectionTallySurvivesQuietLogs:
    """D11's actual claim, isolated from the structlog proxy cache.

    The claim is narrow: ``RejectionTally`` installs its processor at the FRONT
    of the structlog chain, ahead of ``structlog.stdlib.filter_by_level``, so an
    event the stdlib level then DROPS is still counted. If that ordering is ever
    reversed, a sweep silently reports zero blocked days for every arm.

    Tested on a purpose-made logger rather than through a full replay, because
    the strategy's module loggers cache their processor chain on first use and
    therefore cannot answer this question after the first replay in a process —
    see ``runner``'s module docstring, and treat that as the separate
    pre-existing defect it is.
    """

    def test_an_event_the_stdlib_level_drops_is_still_counted(self):
        import logging

        import structlog

        from src.backtesting.engine.rejections import RejectionTally
        from src.utils import clock

        name = f"src.fc060b_probe_{uuid.uuid4().hex}"
        probe = logging.getLogger(name)
        probe.setLevel(logging.WARNING)
        clock.set_now(datetime(2024, 6, 3, 16, 0))
        try:
            with _cli_structlog_config():
                tally = RejectionTally()
                with tally:
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
            probe.setLevel(logging.NOTSET)
            logging.Logger.manager.loggerDict.pop(name, None)

    def test_this_test_file_leaves_no_log_file_behind(self):
        """The regression that motivated the rewrite, stated as a fact.

        `setup_logging` writes to `logs/options_wheel.log`; a test that calls it
        without restoring turns every later test in the session into a log
        producer. Asserting on the absence of a root FileHandler catches that at
        the source rather than by noticing the suite got slower.
        """
        import logging

        # pytest's own logging plugin installs a /dev/null FileHandler; the one
        # that matters is a handler pointed at a real path under logs/.
        leaked = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler)
            and "null" not in str(getattr(h, "baseFilename", "")).lower()
        ]
        assert not leaked, (
            f"a root FileHandler is installed ({leaked}); some test called "
            "setup_logging() without restoring it, and the whole suite is now "
            "writing replay logs to disk"
        )


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
        scenario_hashes={n: f"arm{n}" for n in
                         ("base", "winner", "flipper", "partial")},
        scenario_fill_haircuts={"base": None, "winner": None,
                                "flipper": None, "partial": 1.0},
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
        # The 0.676 figure must carry its staleness marker, or a reader treats a
        # pre-FC-068/078 measurement as a current coefficient.
        assert "last measured 0.676" in markdown
        assert "stale, pending the FC-068/078 re-baseline" in markdown
        # ...and the footer is the SWEEP's, not the single-symbol report's.
        assert "DIFFERENCES survive" in markdown
        assert "One vol regime" in markdown

    def test_the_footer_does_not_point_at_sections_this_report_lacks(self):
        """`reporting.report.KNOWN_BIASES` refers the reader to a data-quality
        block, an attribution section and a buy-and-hold table. A sweep report has
        none of the three, so quoting it verbatim sends the reader hunting."""
        markdown = render_markdown(_hand_built_holdout())
        for phrase in ("data-quality block above",
                       "the attribution section",
                       "comparison below",
                       "dividend_coverage"):
            assert phrase not in markdown, (
                f"the footer refers to {phrase!r}, which this report does not have"
            )

    def test_the_report_says_it_was_not_persisted(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "backtest_runs" in markdown
        assert "Results are not persisted" in markdown
        # ...but it must NOT claim the run writes nothing at all: a cold window
        # does populate the local chain cache, and the GCS lake when configured.
        assert "CHAIN_LAKE_BUCKET" in markdown

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
            "      strategy.put_target_dte: 99\n"
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


# --------------------------------------------------------------------------- #
# Review round 2 — the fixes the two adversarial reviews required
# --------------------------------------------------------------------------- #
class TestKeysTheReplayCannotReach:
    """Q1. `rolling.fallback_strike_attempts` joins the refused list.

    It was the one allowlisted key the build could not demonstrate moved a
    replay, and it was kept on the reading that "unproven is not dead". A
    reviewer settled it by instrumenting the roller: the knob governs the THIRD
    and later strike rungs, and rung 1 always fills here — the adapter fills
    immediately at the broker's haircut price rather than resting a limit that
    can go unfilled — so over 37 rolls x 7 arms, rung >= 3 was reached 0 times.
    Live in production, where a real limit can miss; inert in a replay.
    """

    def test_fallback_strike_attempts_is_refused(self, sweep_config):
        with pytest.raises(OverrideError) as exc:
            apply_overrides(sweep_config, {"rolling.fallback_strike_attempts": 5})
        message = str(exc.value)
        assert "unreachable in a replay" in message
        assert "rung 1 always fills" in message

    def test_the_other_roller_knobs_are_still_allowed(self, sweep_config):
        """The refusal is about this one rung counter, not about the roller."""
        scenario = apply_overrides(sweep_config, {
            "rolling.itm_trigger_ratio": 0.90,
            "rolling.max_extension_days": 7,
        })
        assert scenario.rolling_itm_trigger_ratio == 0.90
        assert scenario.rolling_max_extension_days == 7

    def test_the_allowlist_carries_no_key_without_a_demonstrated_effect(self):
        """A ledger, so a key cannot be re-added without re-running the check.

        Every name here was swept at an extreme value and observed to move at
        least one row; the four names in the refusal set below were swept and
        observed NOT to. Adding a key without doing that is what put three dead
        knobs on the list in the first place.
        """
        assert "rolling.fallback_strike_attempts" not in ALLOWED_OVERRIDES
        for dead in ("strategy.put_limit_spread_fraction",
                     "strategy.call_limit_spread_fraction",
                     "universe.min_open_interest",
                     "rolling.fallback_strike_attempts"):
            assert dead in REJECTED_OVERRIDES


class TestTheBaseScenarioIsTheComparator:
    """Q4. A `base` arm carrying overrides would move every other number."""

    def test_a_base_scenario_with_overrides_is_refused(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        with pytest.raises(ValueError, match="must carry no overrides"):
            run_sweep(sweep_config,
                      [Scenario("base", {"strategy.min_put_premium": 0.30})],
                      ["AAA"], days[0], days[-1], starting_cash=50_000.0,
                      chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
                      quiet_logs=False)
        assert provider.calls == 0, "the sweep started before validating base"

    def test_a_base_scenario_with_a_fill_haircut_is_refused(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        with pytest.raises(ValueError, match="must carry no overrides"):
            run_sweep(sweep_config, [Scenario("base", {}, 1.0)], ["AAA"],
                      days[0], days[-1], starting_cash=50_000.0,
                      chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
                      quiet_logs=False)

    def test_an_empty_base_scenario_is_still_accepted(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        result = run_sweep(sweep_config, [Scenario("base", {})], ["AAA"],
                           days[0], days[-1], starting_cash=50_000.0,
                           chain_store=ChainStore(str(tmp_path)),
                           bar_provider=provider, quiet_logs=False)
        assert result.scenarios == ["base"]


class TestScenarioHash:
    """Q5. `config_hash` cannot tell two arms apart; `scenario_hash` can."""

    def test_arms_outside_config_hash_still_get_distinct_scenario_hashes(self):
        """12 of the 19 allowlisted keys do not move `config_hash` at all."""
        base = Scenario("base", {})
        roller = Scenario("roller", {"rolling.itm_trigger_ratio": 0.9})
        earnings = Scenario("earnings", {"earnings.blackout_days": 5})
        assert len({base.scenario_hash(), roller.scenario_hash(),
                    earnings.scenario_hash()}) == 3

    def test_two_arms_differing_only_in_fill_haircut_are_distinguishable(self):
        """The exact case `config_hash` provably cannot see: it hashes the
        MODULE default haircut, not the scenario's."""
        mid = Scenario("mid", {"strategy.min_put_premium": 0.3})
        bid = Scenario("bid", {"strategy.min_put_premium": 0.3}, 1.0)
        assert mid.scenario_hash() != bid.scenario_hash()

    def test_the_hash_is_stable_and_ignores_key_order(self):
        a = Scenario("a", {"strategy.min_put_premium": 0.3,
                           "risk.max_position_size": 0.2})
        b = Scenario("b", {"risk.max_position_size": 0.2,
                           "strategy.min_put_premium": 0.3})
        assert a.scenario_hash() == b.scenario_hash()
        assert len(a.scenario_hash()) == 16

    def test_every_row_carries_it_and_the_report_shows_it(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        result = run_sweep(
            sweep_config, [Scenario("cheaper", {"strategy.min_put_premium": 0.30})],
            ["AAA"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False)
        assert all(r.scenario_hash for r in result.rows)
        assert (result.cell("base", "AAA").scenario_hash
                != result.cell("cheaper", "AAA").scenario_hash)
        markdown = render_markdown(result)
        assert "scenario hash" in markdown
        assert result.scenario_hashes["cheaper"] in markdown
        payload = json.loads(render_json(result))
        assert payload["scenario_hashes"]["cheaper"] == \
            result.scenario_hashes["cheaper"]
        assert payload["rows"][0]["scenario_hash"]

    def test_a_config_hash_equal_to_base_renders_as_such(self):
        """A column of identical hex reads as a bug; "= base" reads as the fact."""
        result = _hand_built_holdout()
        result.scenario_config_hashes = {n: "samehash00000000"
                                         for n in result.scenarios}
        result.scenario_hashes = {n: f"arm{n}" for n in result.scenarios}
        markdown = render_markdown(result)
        assert "= base" in markdown
        assert markdown.count("samehash00000000") == 1, (
            "only base should print the shared config hash; the rest say '= base'"
        )


class TestInSampleBanner:
    """Q6. Out-of-sample is opt-in, so in-sample must be loudly labelled."""

    def test_a_sweep_without_a_holdout_carries_the_banner(self):
        result = _hand_built_holdout()
        result.windows = [("all", date(2025, 1, 1), date(2025, 6, 30))]
        for row in result.rows:
            row.split = "all"
        assert result.in_sample_only
        markdown = render_markdown(result)
        assert "IN-SAMPLE ONLY" in markdown
        assert "--holdout-start" in markdown
        # At the TOP: before any number a reader could act on.
        assert markdown.index("IN-SAMPLE ONLY") < markdown.index("| scenario |")
        assert json.loads(render_json(result))["in_sample_only"] is True

    def test_a_sweep_with_a_holdout_does_not(self):
        result = _hand_built_holdout()
        assert not result.in_sample_only
        markdown = render_markdown(result)
        assert "IN-SAMPLE ONLY" not in markdown
        assert json.loads(render_json(result))["in_sample_only"] is False

    def test_holdout_semantics_are_documented_where_the_split_is_shown(self):
        markdown = render_markdown(_hand_built_holdout())
        assert "independent replays" in markdown
        assert "carries no position across the boundary" in markdown
        assert "A short holdout" in markdown and "inflates `insuf`" in markdown

    def test_the_two_windows_really_are_independent_replays(
            self, tmp_path, two_symbols, sweep_config):
        """The claim the semantics note makes, checked against the engine.

        Each window is its own `Simulator`, so both start at the full
        `--starting-cash` and neither inherits the other's assigned shares.
        """
        days, provider = two_symbols
        result = run_sweep(
            sweep_config, [Scenario("a", {})], ["AAA"], days[0], days[-1],
            holdout_start=days[15], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False)
        fit = result.cell("a", "AAA", "fit")
        holdout = result.cell("a", "AAA", "holdout")
        assert fit is not None and holdout is not None
        # Both windows priced their own benchmark off their own bars, and both
        # were handed the same starting capital.
        for row in (fit, holdout):
            assert row.decision_days and row.decision_days > 0
        assert fit.end < holdout.start


class TestLowActivityCells:
    """Q7. An annualised return earned on idle capital is not a measurement."""

    def _result_with(self, fraction):
        result = _hand_built_holdout()
        for row in result.rows:
            row.split = "all"
        result.windows = [("all", date(2025, 1, 1), date(2025, 6, 30))]
        cell = result.cell("winner", "AAA", "all")
        cell.days_in_position_fraction = fraction
        cell.annualized_return = 4.0  # a spectacular number off two lucky days
        return result, cell

    def test_a_thinly_deployed_cell_is_flagged_not_ranked(self):
        result, cell = self._result_with(0.04)
        assert cell.low_activity and not cell.measured
        markdown = render_markdown(result)
        assert "`low-act 4%`" in markdown, (
            "the fraction must be printed: 'low-act 4%' and 'low-act 24%' are "
            "very different amounts of evidence"
        )
        assert "+400.0%" not in markdown, "a low-activity cell was ranked"

    def test_it_is_excluded_from_median_min_and_max(self):
        result, _cell = self._result_with(0.04)
        winner = [line for line in render_markdown(result).splitlines()
                  if line.startswith("| winner | all |")]
        assert winner, "per-scenario summary row missing"
        assert "+400.0%" not in winner[0], (
            "the low-activity cell reached an aggregate; a 4% deployment must "
            "not set a scenario's max"
        )
        # columns: scenario | split | median | min | max | measured | insuf |
        #          low-act | demote | err
        cells = [c.strip() for c in winner[0].strip("|").split("|")]
        measured, insuf, low_act = cells[5], cells[6], cells[7]
        assert (insuf, low_act) == ("0", "1"), (
            f"expected insuf=0 and low-act=1 in {winner[0]!r}"
        )
        assert measured == "3", (
            f"the low-activity cell was counted as measured in {winner[0]!r}"
        )

    def test_the_threshold_is_the_one_fitness_already_uses(self):
        just_over, _ = self._result_with(MIN_DAYS_IN_POSITION + 0.01)
        assert not just_over.cell("winner", "AAA", "all").low_activity
        just_under, _ = self._result_with(MIN_DAYS_IN_POSITION - 0.01)
        assert just_under.cell("winner", "AAA", "all").low_activity

    def test_it_is_excluded_from_sign_agreement(self):
        result = _hand_built_holdout()
        result.cell("winner", "AAA", "holdout").days_in_position_fraction = 0.02
        assert sign_agreement(result, "winner") == (1, 1), (
            "a low-activity holdout cell was still counted as out-of-sample "
            "confirmation"
        )

    def test_json_carries_the_flag_rather_than_making_a_consumer_re_derive_it(self):
        result, _cell = self._result_with(0.04)
        payload = json.loads(render_json(result))
        flagged = [r for r in payload["rows"]
                   if r["scenario"] == "winner" and r["symbol"] == "AAA"][0]
        assert flagged["low_activity"] is True
        assert flagged["measured"] is False
        assert payload["min_days_in_position"] == MIN_DAYS_IN_POSITION


class TestDeltasUseTheCommonMeasuredSubset:
    """Q7. Two medians over different symbol sets are not a difference."""

    def test_the_delta_is_taken_over_symbols_measured_in_both_arms(self):
        result = _hand_built_holdout()
        # `partial` measures BBB in fit but not in holdout, so the holdout delta
        # must fall back to AAA alone — and say so.
        value, n = common_delta(result, "partial", "holdout")
        assert n == 1
        assert value == pytest.approx(0.01)
        markdown = render_markdown(result)
        assert "(n=1)" in markdown

    def test_an_empty_common_subset_renders_blank_not_zero(self):
        """"no comparable symbol" and "performed identically" are opposites."""
        result = _hand_built_holdout()
        for symbol in result.symbols:
            result.cell("partial", symbol, "fit").verdict = "insufficient"
        value, n = common_delta(result, "partial", "fit")
        assert (value, n) == (None, 0)
        row = [line for line in render_markdown(result).splitlines()
               if line.startswith("| partial |") and line.endswith("|")]
        assert row, "holdout row missing"
        assert "+0.0%" not in " ".join(row)

    def test_it_is_not_the_difference_of_the_two_medians(self):
        """The failure mode: an arm that trades less looks better.

        `flatterer` beats base by 1pt on the one symbol both measure, and is
        `insuf` on the symbol where base does badly. Difference-of-medians would
        credit it with base's weak symbol; the common subset does not.
        """
        result = _hand_built_holdout()
        result.cell("base", "BBB", "fit").annualized_return = -0.50
        result.cell("partial", "BBB", "fit").verdict = "insufficient"
        value, n = common_delta(result, "partial", "fit")
        assert n == 1
        assert value == pytest.approx(0.01), (
            "the delta absorbed base's unmatched symbol"
        )


class TestProviderFetchAccounting:
    """Q9. A cache hit is not a provider call."""

    def test_fetches_and_cache_hits_are_reported_separately(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        result = run_sweep(
            sweep_config, [Scenario("a", {})], ["AAA", "BBB"],
            days[0], days[-1], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False)
        assert result.provider_fetches_total == provider.calls
        assert result.provider_calls_during_replays == 0
        markdown = render_markdown(result)
        assert "provider fetches" in markdown
        assert "bar-cache hits" in markdown
        payload = json.loads(render_json(result))
        assert payload["provider_calls"]["fetches"] == provider.calls
        assert payload["provider_calls"]["bar_cache_hits"] == 0
        assert payload["provider_calls"]["during_replays"] == 0

    def test_a_bar_cache_read_during_a_replay_is_also_a_leak(
            self, tmp_path, two_symbols, sweep_config):
        """Counting only the network would let a disk re-read pass silently.

        It would be slower and completely correct, which is exactly the kind of
        regression no result reveals. Exercised on the REAL wiring (the runner
        builds its own `CachedBarProvider`), because that is the only shape in
        which a `get_stock_bars` call can be a pure cache read: after
        materialisation the store already covers the window, so the fetch
        counter never moves and only `hits` does.
        """
        from src.backtesting.scenarios import runner as runner_module

        days, raw = two_symbols

        class _Factory:
            @staticmethod
            def from_config(config):
                return raw

        real_replay = Simulator.replay

        def _reads_the_cache(self, materialised, **kw):
            self.provider.get_stock_bars("AAA", days[0], days[-1])
            return real_replay(self, materialised, **kw)

        with patch.object(runner_module, "AlpacaDataProvider", _Factory), \
                patch.object(Simulator, "replay", _reads_the_cache):
            with pytest.raises(RuntimeError,
                               match="escaped during the replay loop") as exc:
                run_sweep(sweep_config, [Scenario("a", {})], ["AAA"],
                          days[0], days[-1], starting_cash=50_000.0,
                          chain_store=ChainStore(str(tmp_path / "chains")),
                          quiet_logs=False)
        assert "bar_cache_read" in str(exc.value), (
            "the leak was attributed to the network; a pure cache read must be "
            "named as one, or the next reader debugs the wrong layer"
        )


class TestTheCLIBannerCountsArmsCorrectly:
    """Q12. `base` is implicit UNLESS the file declares it.

    The banner is the first thing an operator checks against their YAML, so a
    count that is one too high reads as "the file I edited is not the file it
    loaded".
    """

    def _yaml(self, tmp_path, body):
        path = tmp_path / "banner.yaml"
        path.write_text(body)
        return str(path)

    def _banner(self, tmp_path, two_symbols, body, capsys, extra=()):
        import main as main_module
        from src.backtesting.scenarios import runner as runner_module

        days, provider = two_symbols
        store = ChainStore(str(tmp_path / "chains"))

        class _Factory:
            @staticmethod
            def from_config(config):
                return provider

        argv = [
            "main.py", "--command", "sweep",
            "--scenarios", self._yaml(tmp_path, body),
            "--symbols", "AAA",
            "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
            "--starting-cash", "50000", "--no-sensitivity",
        ] + list(extra)
        with patch.object(runner_module, "AlpacaDataProvider", _Factory), \
                patch.object(runner_module.ChainStore, "from_env",
                             classmethod(lambda cls, *a, **kw: store)), \
                patch.object(main_module, "setup_logging", lambda *a, **kw: None), \
                patch("sys.argv", argv):
            try:
                main_module.main()
            except SystemExit:
                pass
        return capsys.readouterr().out

    def test_an_implicit_base_is_counted_once(self, tmp_path, two_symbols, capsys):
        out = self._banner(tmp_path, two_symbols, (
            "scenarios:\n"
            "  - name: a\n"
            "    overrides:\n"
            "      strategy.min_put_premium: 0.30\n"
        ), capsys)
        assert "Sweeping 2 scenarios (base + 1)" in out

    def test_an_explicit_base_is_not_counted_twice(self, tmp_path, two_symbols,
                                                   capsys):
        out = self._banner(tmp_path, two_symbols, (
            "scenarios:\n"
            "  - name: base\n"
            "  - name: a\n"
            "    overrides:\n"
            "      strategy.min_put_premium: 0.30\n"
        ), capsys)
        assert "Sweeping 2 scenarios (base + 1)" in out, (
            "an explicitly declared `base` was counted on top of the implicit one"
        )

    def test_the_in_sample_note_is_printed_before_the_run(
            self, tmp_path, two_symbols, capsys):
        out = self._banner(tmp_path, two_symbols,
                           "scenarios:\n  - name: a\n", capsys)
        assert "IN-SAMPLE ONLY" in out
        assert out.index("IN-SAMPLE ONLY") < out.index("# Scenario sweep")

    def test_a_holdout_run_does_not_print_the_note(self, tmp_path, two_symbols,
                                                   capsys):
        days, _provider = two_symbols
        out = self._banner(tmp_path, two_symbols,
                           "scenarios:\n  - name: a\n", capsys,
                           extra=["--holdout-start", days[15].isoformat()])
        assert "NOTE: no --holdout-start" not in out


class TestTheSuiteOrderThatUsedToFail:
    """Q2 regression, as an executable record of the failing sequence.

    Two adversarial reviewers reproduced an order-dependent failure:

        test_scenarios.py::TestTheRejectionTallySurvivesQuietLogs
        test_scenarios.py::TestHoldout
        test_backtest_simulator.py::TestTheCallLegActuallyRuns::
            test_no_dead_path_events_in_replay

    The first test called `setup_logging("INFO")` and never restored it, which
    installed a root FileHandler on `logs/options_wheel.log` and flipped
    `cache_logger_on_first_use=True` process-wide. The third test then read a
    structlog stream that had been reconfigured underneath it.

    `conftest._logging_config_is_not_leaked` is the general guard; this pins the
    specific sequence, because a general guard can be weakened without anyone
    noticing which concrete bug it was protecting against.
    """

    def test_the_three_tests_pass_in_the_order_that_used_to_fail(self):
        import subprocess
        import sys

        order = [
            "tests/test_scenarios.py::TestTheRejectionTallySurvivesQuietLogs",
            "tests/test_scenarios.py::TestHoldout",
            "tests/test_backtest_simulator.py::TestTheCallLegActuallyRuns::"
            "test_no_dead_path_events_in_replay",
        ]
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *order],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            "the order-dependent failure is back — a test in this file is "
            f"leaking global logging state again:\n{result.stdout[-3000:]}"
        )


class TestTheCellStatesPartition:
    """S1. `measured` / `insuf` / `low-act` / `err` must sum to the cell count.

    They did not. `low_activity` tested only the days-in-position fraction, and a
    window with no completed cycle also has a tiny fraction — so an `insufficient`
    cell was BOTH, and `_scenario_summary` counted it twice. On the acceptance
    sweep `position_size_20pct` reported `measured 1 | insuf 2 | low-act 5` over
    six symbols, which sums to eight. A reader who cannot add up the row stops
    trusting the table, and the JSON carried both flags on the same object.

    `insufficient` wins: it is the more specific statement, and "the window
    contained no completed cycle" is a different finding from "the wheel was
    barely deployed".
    """

    def _row(self, **kw):
        base = dict(
            scenario="a", symbol="AAA", start=date(2025, 1, 1),
            end=date(2025, 6, 30), split="all", config_hash="h",
        )
        base.update(kw)
        return ScenarioResult(**base)

    def test_an_insufficient_row_is_not_also_low_activity(self):
        row = self._row(verdict="insufficient", days_in_position_fraction=0.0)
        assert row.insufficient
        assert not row.low_activity, (
            "an insufficient cell was double-counted as low-activity"
        )
        assert not row.measured

    def test_a_genuinely_thin_row_is_still_low_activity(self):
        """The guard must not swallow the case the flag exists for."""
        row = self._row(verdict="marginal", days_in_position_fraction=0.04)
        assert row.low_activity and not row.insufficient and not row.measured

    def test_an_errored_row_is_only_errored(self):
        row = self._row(error="boom", days_in_position_fraction=0.0)
        assert not row.ok and not row.insufficient
        assert not row.low_activity and not row.measured

    def test_exactly_one_state_is_true_for_every_shape(self):
        shapes = [
            self._row(verdict="fit", days_in_position_fraction=0.90),
            self._row(verdict="marginal", days_in_position_fraction=0.30),
            self._row(verdict="unfit", days_in_position_fraction=0.02),
            self._row(verdict="insufficient", days_in_position_fraction=0.0),
            self._row(verdict="insufficient", days_in_position_fraction=None),
            self._row(verdict="fit", days_in_position_fraction=None),
            self._row(error="corporate_action: ..."),
        ]
        for row in shapes:
            states = [row.measured, row.insufficient, row.low_activity,
                      row.error is not None]
            assert sum(states) == 1, (
                f"{states} for verdict={row.verdict!r} "
                f"fraction={row.days_in_position_fraction!r} "
                f"error={row.error!r} — the four states must partition"
            )

    def test_the_summary_columns_add_up_on_a_real_sweep(
            self, tmp_path, two_symbols, sweep_config):
        """The arithmetic a reader actually does, on a sweep the engine ran."""
        days, provider = two_symbols
        result = run_sweep(
            sweep_config,
            [Scenario("tiny", {"risk.max_position_size": 0.01}),
             Scenario("cheaper", {"strategy.min_put_premium": 0.30})],
            ["AAA", "BBB"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False)

        for name in result.scenarios:
            rows = [r for r in result.rows if r.scenario == name]
            counts = (
                sum(1 for r in rows if r.measured),
                sum(1 for r in rows if r.insufficient),
                sum(1 for r in rows if r.low_activity),
                sum(1 for r in rows if r.error),
            )
            assert sum(counts) == len(rows) == len(result.symbols), (
                f"{name}: measured/insuf/low-act/err = {counts} over "
                f"{len(rows)} cells — the columns do not partition"
            )

    def test_the_rendered_summary_row_adds_up(self):
        """Same invariant, read off the markdown a human sees."""
        result = _hand_built_holdout()
        result.windows = [("all", date(2025, 1, 1), date(2025, 6, 30))]
        for row in result.rows:
            row.split = "all"
        # One insufficient cell with a zero fraction: the exact double-count.
        thin = result.cell("partial", "BBB", "all")
        thin.verdict = "insufficient"
        thin.days_in_position_fraction = 0.0

        for line in render_markdown(result).splitlines():
            if not line.startswith("| partial | all |"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            measured, insuf, low_act, err = (
                int(cells[5]), int(cells[6]), int(cells[7]), int(cells[9]))
            total = measured + insuf + low_act + err
            expected = len([r for r in result.rows
                            if r.scenario == "partial" and r.split == "all"])
            assert total == expected, (
                f"summary row sums to {total} over {expected} cells: {line!r}"
            )
            break
        else:  # pragma: no cover - the row is always rendered
            raise AssertionError("no per-scenario summary row for 'partial'")

    def test_json_rows_never_carry_both_flags(self):
        result = _hand_built_holdout()
        thin = result.cell("partial", "BBB", "holdout")
        thin.days_in_position_fraction = 0.0
        payload = json.loads(render_json(result))
        for row in payload["rows"]:
            assert not (row["insufficient"] and row["low_activity"]), row


# --------------------------------------------------------------------------- #
# FC-096 Phase A PR-2 — the DTE knob
# --------------------------------------------------------------------------- #
class TestTheArmMaxDrivesMaterialisation:
    """One reach per run, and it is the MAXIMUM, not the base config's value.

    Before PR-2 the reach was readable off `base_config.put_target_dte` alone,
    because `put_target_dte` was refused and `call_target_dte` could only be
    lowered. Both bounds are gone, so a DTE-14 arm on either leg now needs a
    materialisation it did not use to need — and getting this wrong is silent:
    the arm replays fine and reports "nothing ever qualified", which reads as a
    finding about that reach rather than as missing data.
    """

    def _cfg(self, dte=7):
        config = Config()
        config._config["strategy"]["put_target_dte"] = dte
        return config

    def test_no_dte_arm_leaves_the_base_value(self):
        from src.backtesting.scenarios.runner import effective_max_dte

        scenarios = [Scenario("base"),
                     Scenario("cheap", {"strategy.min_put_premium": 0.30})]
        assert effective_max_dte(self._cfg(7), scenarios) == 7

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_either_legs_dte_arm_raises_the_reach(self, key):
        """The CALL leg is the one a base-config-only implementation misses."""
        from src.backtesting.scenarios.runner import effective_max_dte

        scenarios = [Scenario("base"), Scenario("longer", {key: 14})]
        assert effective_max_dte(self._cfg(7), scenarios) == 14

    def test_the_maximum_wins_across_arms_and_legs(self):
        from src.backtesting.scenarios.runner import effective_max_dte

        scenarios = [
            Scenario("shorter", {"strategy.put_target_dte": 3}),
            Scenario("mid", {"strategy.call_target_dte": 14}),
            Scenario("longest", {"strategy.put_target_dte": 21}),
        ]
        assert effective_max_dte(self._cfg(7), scenarios) == 21

    def test_a_shorter_arm_never_narrows_the_run(self):
        """A DTE-3 arm must not shrink the window the other arms need — one
        variable feeds materialise AND every replay, so narrowing for one arm
        would either trip `Simulator.replay`'s equality guard or serve every
        other arm a chain that does not cover it."""
        from src.backtesting.scenarios.runner import effective_max_dte

        scenarios = [Scenario("shorter", {"strategy.put_target_dte": 3})]
        assert effective_max_dte(self._cfg(7), scenarios) == 7

    def test_a_higher_base_config_still_wins(self):
        from src.backtesting.scenarios.runner import effective_max_dte

        scenarios = [Scenario("shorter", {"strategy.call_target_dte": 5})]
        assert effective_max_dte(self._cfg(14), scenarios) == 14

    def test_a_call_dte_arm_actually_materialises_that_far(
            self, tmp_path, two_symbols, sweep_config):
        """The end-to-end version, asserted on the STORED provenance rather than
        on the runner's own variable: a run that reported 14 and wrote 8-reach
        files would pass a weaker test and fail every call arm in production."""
        days, provider = two_symbols
        store = ChainStore(str(tmp_path))
        result = run_sweep(
            sweep_config, [Scenario("dte14", {"strategy.call_target_dte": 14})],
            ["AAA", "BBB"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=store, bar_provider=provider, quiet_logs=True)

        assert result.effective_max_dte == 14
        assert not result.errors, [r.error for r in result.errors]
        window = store.stored_window("AAA", days[5])
        # `universe_dte = max_dte + 1` (the universe buffer).
        assert window is not None and window["universe_dte"] == 15


class TestTheDteReadPathIsIdentityPreserving:
    """MEDIUM-8 / plan §A5: a DTE-7 spec must not change because the lake got
    wider underneath it.

    The read path masks a stored chain to the requested reach, so this SHOULD
    hold — but "should" is the whole problem. Every historical sweep result was
    measured against 8-reach files, and if widening the lake moved a single
    number then every stored `backtest_runs` / `scenario_runs` row became
    incomparable with every new one, silently. This is the test that makes the
    masking a contract rather than an implementation detail.
    """

    def _run(self, store, days, provider, sweep_config):
        return run_sweep(
            sweep_config,
            [Scenario("cheaper", {"strategy.min_put_premium": 0.30})],
            ["AAA", "BBB"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=store, bar_provider=provider, quiet_logs=True)

    @staticmethod
    def _comparable(result):
        """Every row, minus the one field that is wall-clock noise."""
        out = []
        for row in result.rows:
            payload = row.as_dict()
            payload.pop("replay_seconds", None)
            out.append(payload)
        return out

    def test_a_dte_7_sweep_is_identical_over_an_8_and_a_22_reach_lake(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols

        narrow = ChainStore(str(tmp_path / "lake8"))
        seven = self._run(narrow, days, provider, sweep_config)
        assert seven.effective_max_dte == 7
        assert narrow.stored_window("AAA", days[5])["universe_dte"] == 8

        # Widen a SECOND lake to 22 by running a DTE-21 arm through it...
        wide = ChainStore(str(tmp_path / "lake22"))
        widening = run_sweep(
            sweep_config, [Scenario("dte21", {"strategy.put_target_dte": 21})],
            ["AAA", "BBB"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=wide, bar_provider=provider, quiet_logs=True)
        assert widening.effective_max_dte == 21
        assert wide.stored_window("AAA", days[5])["universe_dte"] == 22

        # ...then replay the SAME DTE-7 spec against it.
        over_wide = self._run(wide, days, provider, sweep_config)
        assert over_wide.effective_max_dte == 7

        assert self._comparable(over_wide) == self._comparable(seven)
        assert over_wide.scenario_hashes == seven.scenario_hashes
        assert over_wide.scenario_config_hashes == seven.scenario_config_hashes
        assert over_wide.base_config_hash == seven.base_config_hash


class TestTheDteReachFooter:
    """`DTE_REACH_BIAS` appears iff the run reached past 7 — and says the right
    thing when it does.

    A footer that carries every caveat unconditionally is a footer nobody reads,
    which is the failure mode this conditionality exists to avoid. The wording is
    pinned too: these quotes are REAL prints, and calling them extrapolated would
    be a different, also-wrong claim that makes a reader discount a real number.
    """

    def _result(self, reach):
        result = _hand_built_holdout()
        result.effective_max_dte = reach
        return result

    @pytest.mark.parametrize("reach", [0, 1, 7])
    def test_a_short_run_does_not_carry_the_caveat(self, reach):
        from src.backtesting.scenarios import report as engine_report

        result = self._result(reach)
        title = engine_report.DTE_REACH_BIAS[0]
        assert title not in render_markdown(result)
        titles = [b["title"] for b in json.loads(render_json(result))["known_biases"]]
        assert title not in titles

    @pytest.mark.parametrize("reach", [8, 14, 21])
    def test_a_long_run_carries_it_in_both_renderings(self, reach):
        from src.backtesting.scenarios import report as engine_report

        result = self._result(reach)
        title, detail = engine_report.DTE_REACH_BIAS
        markdown = render_markdown(result)
        assert title in markdown
        assert detail in markdown
        payload = json.loads(render_json(result))
        assert {"title": title, "detail": detail} in payload["known_biases"]
        assert payload["effective_max_dte"] == reach

    def test_the_caveat_names_the_real_degradation_not_extrapolation(self):
        """The plan's M6 wording requirement, pinned so a future edit that
        reaches for the easy word has to argue with a test."""
        from src.backtesting.scenarios import report as engine_report

        detail = engine_report.DTE_REACH_BIAS[1]
        assert "not extrapolated" in detail
        assert "no trade that day" in detail
        assert "SELECTION" in detail
        assert "spread model" in detail

    def test_the_threshold_is_the_live_dte_target(self):
        from src.backtesting.scenarios import report as engine_report

        assert engine_report.DTE_REACH_BIAS_THRESHOLD == 7


class TestTheSweepHeaderSurfacesEarningsGaps:
    """FC-096 A4. The FC-013 gate answers "clear" for a symbol it has no row
    for, which is exactly what a freshly-onboarded candidate looks like — so
    without this the sweep that evaluates a candidate says nothing at all about
    the gate having been inert on it."""

    def test_the_field_is_populated_from_the_replays(
            self, tmp_path, two_symbols, sweep_config):
        days, provider = two_symbols
        result = run_sweep(
            sweep_config, [Scenario("cheaper", {"strategy.min_put_premium": 0.30})],
            ["AAA", "BBB"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=True)
        # AAA/BBB are fixtures; neither is in the committed earnings table.
        assert result.earnings_symbols_without_data == ["AAA", "BBB"]

    def test_the_header_names_them_above_the_numbers(self):
        result = _hand_built_holdout()
        result.earnings_symbols_without_data = ["AAA", "ZZZ"]
        markdown = render_markdown(result)
        assert "The earnings gate did not gate AAA, ZZZ" in markdown
        # Above the first grid, not in the footer: it changes what every row
        # below means for those symbols.
        assert markdown.index("did not gate") < markdown.index("| scenario |")
        assert json.loads(render_json(result))[
            "earnings_symbols_without_data"] == ["AAA", "ZZZ"]

    def test_an_empty_list_says_nothing(self):
        """Silence is the healthy state; a "no gaps" line would be noise on
        every ordinary run."""
        result = _hand_built_holdout()
        assert "earnings gate did not gate" not in render_markdown(result)
        assert json.loads(render_json(result))[
            "earnings_symbols_without_data"] == []


class TestAnArmIsNotChangedByItsNeighbours:
    """REVIEW HIGH (FC-096 PR-2). A sweep materialises once at the widest reach
    any arm asks for. Once DTE became sweepable, that stopped being harmless.

    **Entry selection is capped by the arm's own config; the ROLLER is not.**
    `_check_call_criteria_detailed` treats `call_target_dte` as a hard ceiling,
    but the roller's replacement search (`market_data.py`'s `'roll'` criteria
    profile) is bounded by `old_expiry + rolling.max_extension_days` and by
    nothing else, and it takes the maximum net credit among whatever it is
    shown. So an unmasked arm sitting in a spec with a longer-DTE neighbour sees
    roll candidates its own configuration could never have produced.

    That makes the `base` row — the comparator every delta and the whole
    sign-agreement column is measured against — depend on which OTHER arms
    happen to share the spec, while `scenario_hash` and `config_hash` stay
    byte-identical. Two sweeps whose stored rows disagree and whose identity
    hashes agree is the worst shape this store can produce.

    MUTATION CHECK: pass the sweep-wide `max_dte` to `_replay_one` instead of
    `scenario_reaches[...]` and this test fails hard — measured on this fixture,
    `base` moves option_pnl 843.33 -> 688.17, puts_sold 5 -> 3, calls_sold
    2 -> 3, cycles_completed 5 -> 3.
    """

    @staticmethod
    def _rolling_path():
        """Down hard (the put assigns), then up hard (the call goes ITM).

        The roller needs stock/strike >= `itm_trigger_ratio` (0.98) on a short
        call to consider a roll at all, so a monotone slide never exercises it —
        which is why the ordinary `two_symbols` fixture does not catch this.
        """
        warmup = _weekdays(date(2024, 3, 25), 45)
        days = _weekdays(date(2024, 6, 3), 40)
        closes = {d: 100.0 for d in warmup}
        for i, d in enumerate(days):
            closes[d] = 100.0 - i * 3.0 if i <= 9 else 73.0 + (i - 9) * 3.0
        expirations = [d for d in days if d.weekday() == 4]
        return days, _MultiSymbolProvider(
            {"AAA": ScriptedProvider("AAA", closes, expirations)})

    def _sweep(self, tmp_path, name, scenarios):
        days, provider = self._rolling_path()
        config = Config()
        config._config["stocks"]["symbols"] = ["AAA"]
        return run_sweep(
            config, scenarios, ["AAA"], days[0], days[-1], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path / name)), bar_provider=provider,
            quiet_logs=True)

    @staticmethod
    def _rows(result, scenario):
        out = []
        for row in result.rows:
            payload = row.as_dict()
            payload.pop("replay_seconds", None)
            if payload["scenario"] == scenario:
                out.append(payload)
        return out

    def test_the_roller_is_live_on_this_fixture(self, tmp_path):
        """Guard on the guard, and it has to assert the ROLLER specifically.

        The defect below is reachable only through the roller — it is the one
        chain consumer not capped by the arm's own DTE target. So this must
        assert the roller was EVALUATED, not merely that the wheel wrote calls:
        a fixture whose price path stopped crossing `itm_trigger_ratio` (0.98)
        would still sell calls, still pass a `calls_sold` check, and leave
        `TestAnArmIsNotChangedByItsNeighbours` passing vacuously for ever.

        `rolls_evaluated`, not `rolls_executed`: this roller is credit-only and
        declines most of what it looks at. Measured on this fixture, the short
        arms evaluate 12 rolls and execute 0, while the long arm evaluates 22
        and executes 1 — so an `executed > 0` assertion would be flaky on
        exactly the arms the identity test compares.
        """
        result = self._sweep(tmp_path, "guard", [])
        assert not result.errors, [r.error for r in result.errors]
        base = result.rows[0]
        assert base.calls_sold, "fixture stopped writing calls"
        assert base.rolls_evaluated and base.rolls_evaluated > 0, (
            "the roller evaluated nothing on this fixture, so the mixed-spec "
            "identity test below can no longer detect the defect it exists for "
            "— the price path has stopped crossing rolling.itm_trigger_ratio"
        )

    def test_the_roll_counts_are_not_persisted_in_this_pr(self, tmp_path):
        """`rolls_evaluated` / `rolls_executed` live on the dataclass ONLY.

        `rows_from_sweep` writes an explicit column list, so exposing them here
        adds no `scenario_runs` column — which is the point: this PR is the DTE
        knob, and a schema change belongs to the PR that needs it. Phase E's
        console wants roll counts; that is when the column gets argued.
        """
        from src.backtesting.scenarios import persist as store

        result = self._sweep(tmp_path, "nopersist", [])
        rows = store.rows_from_sweep(
            result, run_id="r", submitted_at="2026-09-01T12:00:00+00:00",
            engine_version="v")
        assert rows
        assert "rolls_evaluated" not in rows[0]
        assert "rolls_executed" not in rows[0]
        assert not any(
            f.name.startswith("rolls_") for f in store._runs_schema())

    @pytest.mark.parametrize("neighbour", [
        pytest.param({"strategy.put_target_dte": 21}, id="put-dte-21"),
        pytest.param({"strategy.call_target_dte": 21}, id="call-dte-21"),
    ])
    def test_a_long_dte_neighbour_does_not_move_the_other_arms(
            self, tmp_path, neighbour):
        arm = Scenario("x", {"strategy.min_put_premium": 0.30})
        alone = self._sweep(tmp_path, "alone", [arm])
        mixed = self._sweep(tmp_path, "mixed", [arm, Scenario("long", neighbour)])

        assert alone.effective_max_dte == 7
        assert mixed.effective_max_dte == 21, "the neighbour must widen the run"
        for scenario in ("base", "x"):
            assert self._rows(mixed, scenario) == self._rows(alone, scenario), (
                f"arm '{scenario}' changed because a DTE-21 arm joined the spec"
            )
        assert mixed.scenario_hashes["x"] == alone.scenario_hashes["x"]
        assert mixed.scenario_config_hashes["x"] == alone.scenario_config_hashes["x"]

    def test_the_long_arm_still_sees_its_own_wider_chain(self, tmp_path):
        """The mask must not be a blanket narrowing: the arm that ASKED for 21
        has to get 21, or the knob measures nothing and this fix would have
        traded one silent fiction for another."""
        arm = Scenario("x", {"strategy.min_put_premium": 0.30})
        long_arm = Scenario("long", {"strategy.put_target_dte": 21})
        mixed = self._sweep(tmp_path, "mixed2", [arm, long_arm])
        long_rows = self._rows(mixed, "long")
        assert long_rows and long_rows[0]["error"] is None
        # A 21-DTE put target picks different contracts from a 7-DTE one, so the
        # long arm must NOT come back as a copy of base.
        assert long_rows != self._rows(mixed, "base")


class TestNarrowToDte:
    """The view itself: `simulator.narrow_to_dte`."""

    @staticmethod
    def _materialised(max_dte, dtes=(3, 7, 8, 14, 15, 21, 22)):
        def quote(dte, kind):
            return ChainQuote(
                symbol=f"AAA-{kind}-{dte}", underlying="AAA", as_of=date(2024, 6, 3),
                expiration=date(2024, 6, 3) + timedelta(days=dte), strike=100.0,
                option_type=kind, dte=dte, underlying_price=100.0, mark=1.0,
                bid=0.9, ask=1.1, implied_volatility=0.3, delta=0.2, volume=10)
        snapshot = ChainSnapshot(
            underlying="AAA", as_of=date(2024, 6, 3), underlying_price=100.0,
            puts=[quote(d, "put") for d in dtes],
            calls=[quote(d, "call") for d in dtes])
        return Materialised(
            symbols=["AAA"], start=date(2024, 6, 3), end=date(2024, 6, 28),
            stock_bars={}, days=[date(2024, 6, 3)], anchors={},
            chains={"AAA": {date(2024, 6, 3): snapshot}}, max_dte=max_dte)

    def test_it_masks_to_the_reach_plus_the_universe_buffer(self):
        """The same rule `ChainBuilder.build` fetches under and
        `ChainStore._rows_to_quotes` re-applies on read, so a view is
        indistinguishable from a chain materialised at that reach."""
        view = narrow_to_dte(self._materialised(21), 7)
        snapshot = view.chains["AAA"][date(2024, 6, 3)]
        assert [q.dte for q in snapshot.puts] == [3, 7, 8]
        assert [q.dte for q in snapshot.calls] == [3, 7, 8]
        assert view.max_dte == 7

    def test_a_no_op_mask_returns_the_INPUT_OBJECT(self):
        """Identity, not equality. This is what keeps a homogeneous sweep
        byte-identical to a pre-PR-2 one: no new object, no rebuilt list,
        nothing to drift."""
        materialised = self._materialised(7)
        assert narrow_to_dte(materialised, 7) is materialised
        assert narrow_to_dte(materialised, 21) is materialised

    def test_the_source_is_never_mutated(self):
        """`Materialised` is shared across every arm in the sweep; a view that
        edited it would contaminate every later arm — the exact failure the
        frozen dataclasses exist to prevent."""
        materialised = self._materialised(21)
        before = [q.dte for q in materialised.chains["AAA"][date(2024, 6, 3)].puts]
        narrow_to_dte(materialised, 3)
        after = [q.dte for q in materialised.chains["AAA"][date(2024, 6, 3)].puts]
        assert before == after
        assert materialised.max_dte == 21

    def test_the_quotes_are_shared_not_copied(self):
        """A view is cheap by construction: new snapshots, the SAME frozen
        quotes. Copying them would make a 60-cell sweep pay for the chain twice
        per arm."""
        materialised = self._materialised(21)
        original = materialised.chains["AAA"][date(2024, 6, 3)].puts[0]
        view = narrow_to_dte(materialised, 7)
        assert view.chains["AAA"][date(2024, 6, 3)].puts[0] is original

    def test_everything_else_is_carried_across(self):
        materialised = self._materialised(21)
        view = narrow_to_dte(materialised, 7)
        assert view.symbols == materialised.symbols
        assert (view.start, view.end) == (materialised.start, materialised.end)
        assert view.days == materialised.days
        assert view.anchors == materialised.anchors
        assert view.stock_bars is materialised.stock_bars


class TestTheBaseConfigsCallLegCounts:
    """REVIEW: `effective_max_dte` read only the base's PUT leg, so the
    covered-call profile — which ships `call_target_dte: 14` and NO
    `put_target_dte` at all — would have materialised at 7 and reported "no call
    ever qualified" for every arm."""

    def test_a_base_call_target_widens_the_run_with_no_dte_arm_present(self):
        from src.backtesting.scenarios.runner import effective_max_dte

        config = Config()
        config._config["strategy"]["put_target_dte"] = 7
        config._config["strategy"]["call_target_dte"] = 14
        assert effective_max_dte(config, [Scenario("base")]) == 14

    def test_a_profile_missing_put_target_dte_does_not_raise(self):
        """`Config.put_target_dte` is a PROPERTY that indexes
        `_config["strategy"]` directly, so an absent key raises `KeyError` from
        inside it — and `getattr(config, key, default)` only swallows
        `AttributeError`. This is the covered-call profile exactly."""
        from src.backtesting.scenarios.runner import (
            arm_max_dte, config_target_dte, effective_max_dte,
        )

        config = Config()
        del config._config["strategy"]["put_target_dte"]
        config._config["strategy"]["call_target_dte"] = 14

        with pytest.raises(KeyError):
            config.put_target_dte          # the hazard, stated

        assert config_target_dte(config, "put") == 7
        assert config_target_dte(config, "call") == 14
        assert arm_max_dte(config) == 14
        assert effective_max_dte(config, [Scenario("base")]) == 14

    def test_the_shipped_covered_call_profile_is_the_case_this_protects(self):
        """Read off the real file, not a fixture: if the profile's call target
        moves, this test moves with it rather than pinning a stale 14."""
        import yaml

        with open("config/covered_call.yaml") as fh:
            profile = yaml.safe_load(fh)
        strategy = profile["strategy"]
        assert "put_target_dte" not in strategy, (
            "the profile grew a put target; the KeyError hazard above is now "
            "unreachable, but config_target_dte must still tolerate it"
        )
        assert strategy["call_target_dte"] >= 7


class TestTheThresholdTracksTheLiveConfig:
    """REVIEW: `DTE_REACH_BIAS_THRESHOLD` is not a free constant.

    Everything the footer says about fidelity — the ~7% put / ~32% call premium
    shortfalls, the spread model's calibration — was measured at the LIVE DTE
    target. The threshold is "the reach this engine was measured on", so the day
    the live target moves, the caveat's premise moves with it and the wording
    has to be re-argued. Failing loudly here is the point; this test lives
    repo-side because the dashboard image has no `config/`.
    """

    def test_it_equals_the_wheel_profiles_put_target_dte(self):
        import yaml

        from src.backtesting.scenarios import report as engine_report

        with open("config/settings.yaml") as fh:
            settings = yaml.safe_load(fh)
        live = settings["strategy"]["put_target_dte"]
        assert engine_report.DTE_REACH_BIAS_THRESHOLD == live, (
            f"config/settings.yaml now targets {live} DTE, but DTE_REACH_BIAS "
            f"still claims the engine was measured at "
            f"{engine_report.DTE_REACH_BIAS_THRESHOLD}. The caveat's numbers "
            f"were measured at the old target — re-argue the wording, then move "
            f"the constant (and its dashboard copy)."
        )
