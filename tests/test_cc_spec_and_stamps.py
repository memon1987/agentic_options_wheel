"""FC-096 Phase C — the spec field, the four entry points, and the stamps.

Split from `test_cc_replay.py` because these are contracts about IDENTITY and
EVIDENCE rather than about the strategy's numbers: what a spec keys to, which
profile each entry point resolves, what a stored artifact says about itself, and
what the two validators refuse. Each of them can be wrong while every number in
the replay is right, and the failure is silent in every case.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.backtesting.engine.simulator import (
    COVERED_CALL_STRATEGY,
    SimulationResult,
    WHEEL_STRATEGY,
)
from src.backtesting.reporting.artifact import (
    ArtifactMeta, bars_artifact, cell_artifact,
)
from src.backtesting.scenarios import identity as ident
from src.backtesting.scenarios.overrides import (
    PUT_SIDE_OVERRIDES, OverrideError, validate_override_key, validate_overrides,
)

import main as cli


# --------------------------------------------------------------------------- #
# 1. Identity — the byte-stability of every key already in the store
# --------------------------------------------------------------------------- #
LEGACY_SPEC = {
    "symbols": ["GOOGL", "UNH"],
    "start": "2025-09-02",
    "end": "2026-08-29",
    "holdout_start": "2026-06-01",
    "starting_cash": 100_000.0,
    "run_sensitivity": False,
    "scenarios": [{"name": "tighter",
                   "overrides": {"strategy.call_delta_range": [0.1, 0.2]},
                   "fill_haircut": None}],
}


def _key(spec):
    return ident.sweep_key(spec, engine_version="v", engine_identity="e")


class TestCanonicalisationByOmission:
    def test_the_canonical_dict_of_a_legacy_spec_is_unchanged(self):
        """Asserted on the canonical JSON, not on stored-row dedup.

        Any `src/**` change moves ENGINE identity anyway, so a dedup-level
        assertion would pass for the wrong reason. What must not move is the
        SPEC half of the key.
        """
        assert "strategy" not in ident.canonical_spec(LEGACY_SPEC)

    def test_an_explicit_wheel_folds_to_absence(self):
        """The half that is easy to miss and expensive to get wrong.

        Both validators now stamp `strategy` on every normalised spec, so
        WITHOUT this fold a dashboard submission would key differently from the
        identical CLI submission — and every run already in the store would
        stop matching its own key on the first Saturday after the merge.
        """
        explicit = dict(LEGACY_SPEC, strategy="wheel")
        assert "strategy" not in ident.canonical_spec(explicit)
        assert _key(explicit) == _key(LEGACY_SPEC)

    def test_an_empty_or_absent_value_also_folds(self):
        assert _key(dict(LEGACY_SPEC, strategy=None)) == _key(LEGACY_SPEC)
        assert _key(dict(LEGACY_SPEC, strategy="")) == _key(LEGACY_SPEC)

    def test_covered_call_is_written_and_moves_the_key(self):
        cc = dict(LEGACY_SPEC, strategy="covered_call")
        assert ident.canonical_spec(cc)["strategy"] == "covered_call"
        assert _key(cc) != _key(LEGACY_SPEC), (
            "a covered-call sweep must never dedup into the wheel run of the "
            "same window — `base_config_hash` is the second belt, this is the "
            "first")

    def test_the_enum_is_not_enforced_here(self):
        """`identity.py` is stdlib-only and flat-copied into the dashboard
        image; a validator maintained in two images is a validator that drifts.
        Canonicalisation must not refuse — the VALIDATORS do."""
        assert ident.canonical_spec(
            dict(LEGACY_SPEC, strategy="nonsense"))["strategy"] == "nonsense"

    # -- FC-116 T8 -------------------------------------------------------- #
    # Literal pins, computed on the tree BEFORE `roll_fill_mode` existed
    # (`1e292a2`) and pasted here. No test pinned a literal arm hash until
    # now, which is exactly how FC-116 could have shipped a payload that
    # writes `"roll_fill_mode": null` as a fixed key and silently moved EVERY
    # arm hash -> every `scenario_hash`, `sweep_key` and standing pin in the
    # store. A hash that moves here is that bug, not a rebase artefact: these
    # literals must survive every future change to `identity.py` unless the
    # store is deliberately being invalidated.
    LEGACY_ARM_HASH_NO_HAIRCUT = "c908ee20abc31f39"
    LEGACY_ARM_HASH_HAIRCUT_05 = "f1be989a61b68a05"
    LEGACY_TIGHTER_ARM_HASH = "245a9c1cf695f82e"
    LEGACY_CANONICAL_JSON = (
        '{"end": "2026-08-29", "holdout_start": "2026-06-01", '
        '"run_sensitivity": false, "scenarios": [{"hash": '
        '"245a9c1cf695f82e", "name": "tighter"}], "start": "2025-09-02", '
        '"starting_cash": 100000.0, "symbols": ["GOOGL", "UNH"]}'
    )

    def test_a_legacy_arm_hash_is_a_literal_and_does_not_move(self):
        assert ident.scenario_arm_hash(
            {"rolling.itm_trigger_ratio": 1.0}, None
        ) == self.LEGACY_ARM_HASH_NO_HAIRCUT
        assert ident.scenario_arm_hash(
            {"rolling.itm_trigger_ratio": 1.0}, 0.5
        ) == self.LEGACY_ARM_HASH_HAIRCUT_05
        assert ident.scenario_arm_hash(
            {"strategy.call_delta_range": [0.1, 0.2]}, None
        ) == self.LEGACY_TIGHTER_ARM_HASH

    def test_the_default_haircut_still_folds_to_the_omitted_hash(self):
        """The precedent the new fold copies: a spelled-out default and an
        omitted one are ONE arm."""
        assert ident.scenario_arm_hash(
            {"rolling.itm_trigger_ratio": 1.0}, ident.DEFAULT_FILL_HAIRCUT
        ) == self.LEGACY_ARM_HASH_NO_HAIRCUT

    def test_the_canonical_json_of_a_legacy_spec_is_a_literal(self):
        """Byte-level, not key-level: a new key added to the payload with a
        `null` value passes a `"x" not in canonical` check and still moves
        every stored key."""
        assert json.dumps(
            ident.canonical_spec(LEGACY_SPEC), sort_keys=True
        ) == self.LEGACY_CANONICAL_JSON

    # -- FC-116 T8, the new behaviour -------------------------------------- #
    def test_the_default_mode_folds_to_absence_not_to_null(self):
        """E1, and the reason the literals above exist.

        `scenario_arm_hash`'s payload is a FIXED two-key dict that always
        writes `"fill_haircut": null` for the default. Copying that shape for
        `roll_fill_mode` would change the payload BYTES of every arm ever
        hashed — every `scenario_hash`, `sweep_key` and standing pin in the
        store, a silent and total dedup miss. So the default is spelled by
        ABSENCE, exactly as a `"wheel"` strategy is in `canonical_spec`.
        """
        omitted = ident.scenario_arm_hash({"rolling.itm_trigger_ratio": 1.0}, None)
        explicit = ident.scenario_arm_hash(
            {"rolling.itm_trigger_ratio": 1.0}, None, "limit")
        assert omitted == explicit == self.LEGACY_ARM_HASH_NO_HAIRCUT
        assert ident.DEFAULT_ROLL_FILL_MODE == "limit"

    def test_the_haircut_mode_moves_the_arm_hash_and_the_sweep_key(self):
        """The regression arm must be a DIFFERENT arm, or the before/after
        would dedup into one cell and the comparison would be impossible."""
        honest = ident.scenario_arm_hash({"rolling.itm_trigger_ratio": 1.0}, None)
        legacy = ident.scenario_arm_hash(
            {"rolling.itm_trigger_ratio": 1.0}, None, "haircut")
        assert legacy != honest

        spec = dict(LEGACY_SPEC, scenarios=[
            dict(LEGACY_SPEC["scenarios"][0], roll_fill_mode="haircut")])
        assert _key(spec) != _key(LEGACY_SPEC)
        assert ident.canonical_spec(spec)["scenarios"][0]["hash"] != \
            self.LEGACY_TIGHTER_ARM_HASH

    def test_an_explicit_limit_keys_identically_to_an_omitted_one(self):
        """A console submission always stamps the field; a hand-written spec
        does not. Without the fold those two identical runs would key
        differently and never dedup against each other — the same failure the
        `strategy: wheel` fold exists to prevent."""
        spec = dict(LEGACY_SPEC, scenarios=[
            dict(LEGACY_SPEC["scenarios"][0], roll_fill_mode="limit")])
        assert _key(spec) == _key(LEGACY_SPEC)

    def test_a_base_arm_carrying_any_mode_is_not_the_implicit_base(self):
        """Folding it away would silently DROP the mode from the comparator
        every other row is read against.

        `limit` is refused too, not just `haircut` (review): the runner's
        `_with_base_first` rejects a declared `base` carrying ANY
        `roll_fill_mode`, so folding the default made `canonical_spec` agree
        with a spec the engine refuses — and a dedup hit would hand that
        submission a different run's numbers instead of the error it earned.
        """
        assert ident._is_implicit_base({"name": "base"})
        assert not ident._is_implicit_base(
            {"name": "base", "roll_fill_mode": "limit"})
        assert not ident._is_implicit_base(
            {"name": "base", "roll_fill_mode": "haircut"})

    def test_the_two_sides_refuse_the_same_arm(self):
        """The asymmetry this closes, stated as one assertion."""
        import pytest

        from src.backtesting.scenarios.runner import Scenario, _with_base_first

        for mode in ("limit", "haircut"):
            arm = {"name": "base", "roll_fill_mode": mode}
            assert not ident._is_implicit_base(arm), mode
            with pytest.raises(ValueError, match="roll_fill_mode"):
                _with_base_first([Scenario("base", {}, roll_fill_mode=mode)])

    def test_the_default_mode_is_the_same_string_in_every_copy(self):
        """Three copies that cannot import each other: `identity` (stdlib-only,
        flat-copied into the dashboard image), `evaluate` (the screen path),
        and `engine.broker` (which the adapter imports — it cannot reach
        `identity` without a circular import through
        `scenarios/__init__` -> runner -> simulator -> adapter)."""
        from src.backtesting import evaluate as ev
        from src.backtesting.engine import broker as bk

        assert (ident.DEFAULT_ROLL_FILL_MODE
                == ev.DEFAULT_ROLL_FILL_MODE
                == bk.ROLL_FILL_MODE_LIMIT
                == "limit")
        assert ident.ROLL_FILL_MODES == bk.ROLL_FILL_MODES == ("limit", "haircut")
        # The dashboard is NOT a fourth copy — it imports this one. Pinned
        # by identity in `test_dashboard_sweeps.py` (E4), which has the
        # path setup needed to import `services.sweeps`.

    def test_the_constant_is_the_same_string_in_all_three_copies(self):
        """It is spelled in three stdlib/engine modules that cannot import each
        other. This is the standing answer to that."""
        from src.backtesting.scenarios import overrides as ov

        assert ident.WHEEL_STRATEGY == ov.WHEEL_STRATEGY == WHEEL_STRATEGY
        assert ident.STRATEGIES == (WHEEL_STRATEGY, COVERED_CALL_STRATEGY)


# --------------------------------------------------------------------------- #
# 2. The four entry points
# --------------------------------------------------------------------------- #
class _Args:
    def __init__(self, config=None):
        self.config = config


class TestProfileResolution:
    def test_a_bare_spec_resolves_the_wheel_and_reuses_the_process_config(self):
        from src.utils.config import Config

        process = Config()
        resolved, strategy = cli.resolve_replay_config(_Args(), {}, process)
        assert strategy == "wheel"
        assert resolved is process, (
            "the wheel profile IS the process default; re-reading the same "
            "yaml would produce an object that must hash identically anyway")

    def test_a_covered_call_spec_resolves_the_covered_call_profile(self):
        from src.utils.config import Config

        resolved, strategy = cli.resolve_replay_config(
            _Args(), {"strategy": "covered_call"}, Config())
        assert strategy == "covered_call"
        assert resolved.strategy_id == "covered_call"
        assert str(resolved.config_path).endswith("covered_call.yaml")

    def test_a_supplied_config_is_an_override_and_names_the_strategy(self):
        """The CLI rollout command: `--scenarios <yaml> --config <profile>`,
        with no spec at all. Silence is not an assertion of `wheel`."""
        from src.utils.config import Config

        resolved, strategy = cli.resolve_replay_config(
            _Args(config="config/covered_call.yaml"), {}, Config())
        assert strategy == "covered_call"
        assert resolved.strategy_id == "covered_call"

    def test_a_contradiction_is_refused_loudly(self):
        from src.utils.config import Config

        with pytest.raises(SystemExit, match="Refusing rather than guessing"):
            cli.resolve_replay_config(
                _Args(config="config/settings.yaml"),
                {"strategy": "covered_call"}, Config())

    def test_the_override_is_keyed_on_the_FLAG_never_the_process_config(self):
        """The confirmation-pass fix, and it is the one that would have broken
        the deployed Jobs.

        The battery and the Job both pass a process `Config` POSITIONALLY into
        `run_sweep_cmd` and pass no `--config` at all. Reading that object as a
        supplied override would have made every covered-call standing item look
        like "you supplied the wheel profile and asked for covered_call" — a
        refusal on every deployed path.
        """
        from src.utils.config import Config

        wheel_process = Config()
        assert wheel_process.strategy_id == "wheel"
        resolved, strategy = cli.resolve_replay_config(
            _Args(config=None), {"strategy": "covered_call"}, wheel_process)
        assert strategy == "covered_call"
        assert resolved.strategy_id == "covered_call"

    def test_the_cli_config_flag_defaults_to_none(self):
        """A non-None default made every invocation look like a supplied
        override, which is what forced this change."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument('--config', default=None)
        assert parser.parse_args([]).config is None
        # And the process still resolves to the wheel yaml.
        assert cli.DEFAULT_CONFIG_FILE == "config/settings.yaml"
        assert cli.STRATEGY_CONFIG_FILES["covered_call"] == \
            "config/covered_call.yaml"

    def test_the_enum_refuses_an_unknown_strategy_with_a_usable_message(self):
        with pytest.raises(SystemExit, match="must be one of"):
            cli.spec_strategy({"strategy": "Covered_Call"})
        with pytest.raises(SystemExit, match="must be one of"):
            cli.spec_strategy({"strategy": "wheel2"})
        assert cli.spec_strategy({}) == "wheel"
        assert cli.spec_strategy({"strategy": "covered_call"}) == "covered_call"

    def test_the_battery_standing_set_states_its_strategy(self):
        """It is wheel-only BY CHOICE now; stating it is what makes the flip a
        one-line change and the stored spec self-describing."""
        from src.utils.config import Config

        specs = cli.battery_standing_specs(Config(), today=date(2026, 9, 5))
        assert specs
        assert all(s["strategy"] == "wheel" for s in specs)
        # ...and it costs nothing in the key.
        assert _key(specs[0]) == _key(
            {k: v for k, v in specs[0].items() if k != "strategy"})


class TestSingleSymbolCommandsRefuseANonWheelProfile:
    """Review round 1 (LOW). `backtest` and `screen` build a Simulator with NO
    seeding policy — only `run_sweep` resolves one — so a covered-call profile
    would replay with no shares and report a verdict about a run it never had
    the inputs for. `screen` would also PERSIST that verdict to `backtest_runs`
    in the profile's own dataset."""

    class _Args:
        symbol = "GOOGL"
        start = "2025-09-02"
        end = "2026-08-29"

    def test_backtest_refuses_and_names_the_way_out(self):
        from src.utils.config import Config

        with pytest.raises(SystemExit) as exc:
            cli.run_backtest(self._Args(), Config("config/covered_call.yaml"), None)
        assert "does not support the 'covered_call' profile" in str(exc.value)
        assert "--command sweep" in str(exc.value)

    def test_screen_refuses_before_it_can_write_a_row(self):
        from src.utils.config import Config

        with pytest.raises(SystemExit) as exc:
            cli.run_screen_cmd(self._Args(), Config("config/covered_call.yaml"), None)
        assert "does not support the 'covered_call' profile" in str(exc.value)

    def test_the_wheel_is_unaffected(self):
        from src.utils.config import Config

        # Returns None (does not raise) — the guard is a no-op on the wheel.
        assert cli._refuse_non_wheel_single_symbol(Config(), "backtest") is None


class TestTheSimServiceResolvesPerStrategy:
    def test_it_caches_one_config_per_strategy(self):
        import deploy.sim_service as svc

        svc.reset_for_tests()
        try:
            wheel = svc.replay_config_for({"strategy": "wheel"})
            cc_one = svc.replay_config_for({"strategy": "covered_call"})
            cc_two = svc.replay_config_for({"strategy": "covered_call"})
            assert wheel.strategy_id == "wheel"
            assert cc_one.strategy_id == "covered_call"
            assert cc_one is cc_two, "the CC profile must be built once"
            assert wheel is svc.get_config(), (
                "the wheel branch is the PROCESS context on this deployment")
        finally:
            svc.reset_for_tests()

    def test_an_absent_strategy_is_the_wheel(self):
        import deploy.sim_service as svc

        svc.reset_for_tests()
        try:
            assert svc.replay_config_for({}).strategy_id == "wheel"
        finally:
            svc.reset_for_tests()

    def test_the_two_profiles_hash_differently(self):
        """`base_config_hash` is the dedup belt ACROSS strategies: it is the
        second guard behind `sweep_key`, and it is the one that still holds if
        a caller ever forgets to put `strategy` on the spec."""
        import deploy.sim_service as svc
        from src.backtesting.scenarios import persist as store

        svc.reset_for_tests()
        try:
            wheel = store.base_config_hash(
                store.base_config_snapshot(svc.replay_config_for({})))
            cc = store.base_config_hash(store.base_config_snapshot(
                svc.replay_config_for({"strategy": "covered_call"})))
            assert wheel != cc
        finally:
            svc.reset_for_tests()


# --------------------------------------------------------------------------- #
# 3. Override refusal parity, both directions, at both ends
# --------------------------------------------------------------------------- #
class TestStrategyConditionalRefusals:
    @pytest.mark.parametrize("key", sorted(PUT_SIDE_OVERRIDES))
    def test_a_put_key_is_refused_for_covered_call_and_allowed_for_wheel(self, key):
        value = {"strategy.put_delta_range": [0.1, 0.2],
                 "strategy.min_put_premium": 0.5,
                 "strategy.put_target_dte": 7,
                 "earnings.blackout_days": 3}[key]
        # Wheel: nothing new is refused.
        validate_override_key(key, value, strategy="wheel")
        # Covered call: refused, and the reason says WHY rather than "not allowed".
        with pytest.raises(OverrideError) as exc:
            validate_override_key(key, value, strategy="covered_call")
        assert "writes no puts" in str(exc.value)
        assert "would return the base row" in str(exc.value)

    def test_a_call_key_is_allowed_on_both(self):
        for strategy in ("wheel", "covered_call"):
            validate_override_key("strategy.call_delta_range", [0.1, 0.2],
                                  strategy=strategy)
            validate_override_key("rolling.itm_trigger_ratio", 1.0,
                                  strategy=strategy)

    def test_the_default_is_the_wheel_so_every_existing_caller_is_unchanged(self):
        validate_overrides({"strategy.put_delta_range": [0.1, 0.2]})

    def test_an_unconditionally_refused_key_keeps_its_own_reason(self):
        """Order matters: a key refused for everyone must not be re-explained
        as a strategy problem."""
        with pytest.raises(OverrideError) as exc:
            validate_override_key("strategy_id", "covered_call",
                                  strategy="covered_call")
        assert "chosen by the spec's `strategy` field" in str(exc.value)
        assert "writes no puts" not in str(exc.value)

    def test_the_dashboard_refuses_in_byte_identical_words(self):
        """Both ends import the SAME flat-copied module, and this is what
        proves the API and the Job say the same thing."""
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        spec = {
            "symbols": ["GOOGL"], "start": "2025-09-02", "end": "2026-08-29",
            "strategy": "covered_call",
            "scenarios": [{"name": "arm", "overrides": {
                "strategy.put_delta_range": [0.1, 0.2]}}],
        }
        with pytest.raises(S.SweepValidationError) as api:
            S.validate_spec(spec)
        with pytest.raises(OverrideError) as job:
            validate_override_key("strategy.put_delta_range", [0.1, 0.2],
                                  strategy="covered_call")
        assert str(job.value) in str(api.value)

    def test_the_dashboard_accepts_the_same_arm_for_the_wheel(self):
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        spec = {
            "symbols": ["GOOGL"], "start": "2025-09-02", "end": "2026-08-29",
            "scenarios": [{"name": "arm", "overrides": {
                "strategy.put_delta_range": [0.1, 0.2]}}],
        }
        assert S.validate_spec(spec)["strategy"] == "wheel"

    def test_whitespace_is_stripped_identically_on_both_sides(self):
        """Review round 1 (LOW). Both validators `.strip()`, and this pins it.

        If one side stripped and the other did not, `"  covered_call  "` would
        normalise to different values, canonicalise to different keys, and the
        two entry points would silently stop deduping against each other.
        """
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        for raw in ("  covered_call  ", " wheel ", "covered_call\t"):
            spec = {"symbols": ["GOOGL"], "start": "2025-09-02",
                    "end": "2026-08-29", "scenarios": [], "strategy": raw}
            assert S.validate_spec(spec)["strategy"] == cli.spec_strategy(spec)

    def test_the_dashboard_refuses_an_unknown_strategy(self):
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        spec = {"symbols": ["GOOGL"], "start": "2025-09-02",
                "end": "2026-08-29", "scenarios": [], "strategy": "Wheel"}
        with pytest.raises(S.SweepValidationError, match="must be one of"):
            S.validate_spec(spec)


# --------------------------------------------------------------------------- #
# 4. The stamps — precondition added 2026-09-03 (Phase E PR-2 review)
# --------------------------------------------------------------------------- #
def _result(strategy=WHEEL_STRATEGY, starting_cash=100_000.0):
    from src.backtesting.engine.broker import BacktestBroker
    from src.backtesting.engine.simulator import DailyState

    broker = BacktestBroker(starting_cash=starting_cash)
    return SimulationResult(
        symbols=["AAA"], start=date(2024, 6, 3), end=date(2024, 6, 4),
        starting_cash=starting_cash,
        daily=[DailyState(day=date(2024, 6, 3), equity=starting_cash, cash=starting_cash,
                          reserved_collateral=0.0, open_options=0, shares_held={})],
        broker=broker, strategy=strategy,
    )


class TestTheCapitalBaseStamp:
    def test_a_wheel_cell_without_a_stamp_falls_back_to_starting_cash(self):
        """Unchanged behaviour: the wheel's capital base IS its starting cash,
        so the fallback there is a restatement of a fact."""
        payload = cell_artifact(_result(), ArtifactMeta(
            strategy="wheel", starting_cash=100_000.0))
        assert payload["provenance"]["capital_base"] == 100_000.0
        assert payload["provenance"]["strategy"] == "wheel"

    def test_a_covered_call_cell_without_a_stamp_carries_NULL(self):
        """THE precondition (2026-09-03).

        As shipped, the writer fell back to `starting_cash` for ANY strategy —
        so a covered-call cell that forgot the stamp shipped `capital_base` =
        the $5,000 buy-back float, the console trusted that positive number as
        authoritative, and every ratio on the page was divided by a number ~20x
        too small while looking entirely plausible. `null` is a state the
        console renders (it suppresses the ratios and says why); a wrong number
        is not.
        """
        payload = cell_artifact(_result(strategy="covered_call", starting_cash=5_000.0),
                                ArtifactMeta(strategy="covered_call",
                                             starting_cash=5_000.0))
        assert payload["provenance"]["capital_base"] is None, (
            "a covered-call artifact with no explicit base must REFUSE, not "
            "guess the cash float")
        assert payload["provenance"]["starting_cash"] == 5_000.0
        assert payload["provenance"]["strategy"] == "covered_call"

    def test_the_refusal_reads_the_RESULT_when_the_meta_says_nothing(self):
        payload = cell_artifact(_result(strategy="covered_call", starting_cash=5_000.0),
                                ArtifactMeta(starting_cash=5_000.0))
        assert payload["provenance"]["capital_base"] is None

    def test_an_explicit_base_is_always_honoured(self):
        payload = cell_artifact(_result(strategy="covered_call", starting_cash=5_000.0),
                                ArtifactMeta(strategy="covered_call",
                                             starting_cash=5_000.0,
                                             capital_base=15_000.0))
        assert payload["provenance"]["capital_base"] == 15_000.0

    def test_the_runner_stamps_the_scored_reports_own_base(self):
        """Read off `FitnessReport.capital_base` rather than re-derived, so the
        console divides by the number the VERDICT was reached with."""
        import inspect

        from src.backtesting.scenarios import runner

        source = inspect.getsource(runner._emit_artifact)
        assert "report.capital_base" in source
        assert "capital_base=starting_cash" not in source, (
            "the wheel's starting cash must not be hardcoded as the base again")

    def test_the_bars_sidecar_carries_both_stamps(self):
        from src.backtesting.data.provider import StockBar
        from src.backtesting.engine.simulator import DailyState

        days = [date(2024, 6, 3), date(2024, 6, 4)]
        bars = [StockBar(symbol="AAA", bar_date=d, open=100.0, high=101.0,
                         low=99.0, close=100.0 + i, volume=5_000_000)
                for i, d in enumerate(days)]
        daily = [DailyState(day=d, equity=1.0, cash=1.0, reserved_collateral=0.0,
                            open_options=0, shares_held={}) for d in days]
        payload = bars_artifact(bars, "AAA", ("all", days[0], days[-1]),
                                daily=daily, benchmark=None, dividends=None,
                                strategy="covered_call", capital_base=15_000.0)
        assert payload["provenance"]["strategy"] == "covered_call"
        assert payload["provenance"]["capital_base"] == 15_000.0

    def test_a_wheel_sidecar_stamps_nothing_new(self):
        from src.backtesting.data.provider import StockBar
        from src.backtesting.engine.simulator import DailyState

        days = [date(2024, 6, 3)]
        bars = [StockBar(symbol="AAA", bar_date=days[0], open=100.0, high=101.0,
                         low=99.0, close=100.0, volume=5_000_000)]
        daily = [DailyState(day=days[0], equity=1.0, cash=1.0,
                            reserved_collateral=0.0, open_options=0,
                            shares_held={})]
        payload = bars_artifact(bars, "AAA", ("all", days[0], days[0]),
                                daily=daily, benchmark=None, dividends=None)
        assert payload["provenance"]["strategy"] is None
        assert payload["provenance"]["capital_base"] is None


class TestTheMovedGate:
    def test_the_component_string_is_pinned_verbatim(self):
        """BigQuery views group error events by `component`. "Correcting" it to
        `strategy_gates` would split one series in two, and nothing queries the
        new half."""
        from src.strategy.strategy_gates import GATE_EVENT_COMPONENT

        assert GATE_EVENT_COMPONENT == "cloud_run_server"

    def test_the_re_export_keeps_the_servers_public_shape(self):
        import deploy.cloud_run_server as server
        from src.strategy.strategy_gates import call_only_opportunities

        call = {"option_symbol": "META260220C00600000", "symbol": "META"}
        put = {"option_symbol": "AAPL260220P00170000", "symbol": "AAPL"}
        from unittest.mock import Mock

        assert server._call_only_opportunities([call, put], "covered_call",
                                               Mock()) == [call]
        assert call_only_opportunities([call, put], "covered_call",
                                       Mock()) == [call]

    def test_the_engine_can_import_it_without_a_flask_app(self):
        """The whole reason for the move: `deploy.cloud_run_server` builds a
        Flask app, reads credentials and constructs an AlpacaClient at import
        time, so the replay could never have imported the gate from there."""
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c",
             "import src.strategy.strategy_gates as g; "
             "assert g.GATE_EVENT_COMPONENT == 'cloud_run_server'; "
             "import sys; assert 'flask' not in sys.modules"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# FC-116 T9 — the CLI and the ENGINE VERSION
# --------------------------------------------------------------------------- #
class TestTheCliAcceptsTheRollFillMode:
    """The CLI is one of three validators that must agree (the API and the
    console are the others). A key accepted by one and refused by another is a
    spec that runs from the terminal and 400s from the console."""

    def _arms(self, entry):
        return cli.scenarios_from_spec({"scenarios": [entry]})

    def test_both_values_are_accepted_and_carried_onto_the_arm(self):
        for mode in ("limit", "haircut"):
            arm, = self._arms({"name": "a", "roll_fill_mode": mode})
            assert arm.roll_fill_mode == mode

    def test_an_omitted_mode_stays_none_and_resolves_later(self):
        """`None` is not `"limit"` on the dataclass, deliberately: the RUNNER
        resolves it, and the resolved value is what reaches the row. Writing
        the default in here would make a stored spec claim the submitter asked
        for something they did not."""
        arm, = self._arms({"name": "a"})
        assert arm.roll_fill_mode is None

    def test_an_unknown_mode_is_refused_by_name(self):
        with pytest.raises(SystemExit) as exc:
            self._arms({"name": "a", "roll_fill_mode": "mid"})
        assert "roll_fill_mode" in str(exc.value)
        assert "'mid'" in str(exc.value)

    def test_a_misspelled_field_is_still_refused(self):
        """The unknown-field set had to GROW for this PR; a regression that
        widened it instead would let every typo through silently."""
        with pytest.raises(SystemExit) as exc:
            self._arms({"name": "a", "roll_fill_modes": "haircut"})
        assert "unknown field" in str(exc.value)


class TestTheEngineVersionMovedAndStayedInSync:
    """FC-116 D3. Rows written before and after this PR are NON-COMPARABLE on
    every roll-bearing row. The identity hash would invalidate dedup anyway —
    `sweep_key` mixes in the content hash of `src/**` — but the VERSION is what
    makes the boundary queryable (`WHERE engine_version = ...`). FC-048 did not
    bump, and the docs call that boundary "timestamp-only" as the regret."""

    def test_all_three_copies_are_the_fc116_version(self):
        from src.backtesting import screen
        from src.backtesting.scenarios import engine_identity

        assert (screen.ENGINE_VERSION
                == engine_identity.ENGINE_VERSION
                == "fc-116-roll-limit-fills")

    def test_the_dashboard_copy_agrees(self):
        import sys
        from pathlib import Path

        backend = str(Path("dashboard/backend").resolve())
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from services import sweeps as dash

        from src.backtesting import screen
        assert dash.ENGINE_VERSION == screen.ENGINE_VERSION
