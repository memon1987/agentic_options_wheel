"""The sweep API's pure logic (FC-060 D5-D11), tested where CI actually runs.

FastAPI is not installed in the image Cloud Build tests with, so a rule that
lives in `routers/v2.py` is a rule nobody checks. Everything worth getting wrong
therefore lives in `dashboard/backend/services/sweeps.py` and is exercised here:
the allowlist agreement with the runner, the caps, the token compare, the
one-at-a-time gate, the launch body, and — the largest piece — `shape_results`,
which is asserted EQUAL to `report.py` on a real sweep rather than merely
plausible.

Same import trick as `tests/test_dashboard_pause_alert.py`: put
`dashboard/backend` on the path and import `services.*` directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root
# kept ahead of it, so `import main` still resolves to the CLI rather than
# to dashboard/backend/main.py for every module collected after this one.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()

from services import sweeps as S  # noqa: E402
from services import sweep_report_text as T  # noqa: E402

from src.backtesting.scenarios import persist as store  # noqa: E402
from src.backtesting.scenarios import report as engine_report  # noqa: E402
from src.backtesting.scenarios.overrides import (  # noqa: E402
    ALLOWED_OVERRIDES, REJECTED_OVERRIDES, OverrideError, validate_override_key,
)
from src.backtesting.scenarios.runner import ScenarioResult, SweepResult  # noqa: E402
from src.backtesting.screen import ENGINE_VERSION  # noqa: E402


# A stand-in for what `cloudbuild.yaml`'s `compute-engine-identity` step bakes
# into the dashboard image. Deliberately NOT the repo's real identity: a test
# that happened to compute the same value both sides would pass on a dashboard
# that computed the hash itself, which is precisely what it must not do (it
# ships no `src/` tree).
BAKED_IDENTITY = "0123456789abcdef"


def spec(**overrides):
    base = {
        "symbols": ["AAPL", "NVDA"],
        "start": "2025-08-01",
        "end": "2026-07-31",
        "scenarios": [{"name": "tighter",
                       "overrides": {"strategy.min_put_premium": 0.75}}],
    }
    base.update(overrides)
    return base


# ==========================================================================
# The copies that could not be imports. Each is guarded byte-for-byte.
# ==========================================================================
class TestTheReportProseIsNotAFork:
    """`services/sweep_report_text.py` is a copy of `report.py`'s prose.

    D11 sanctions the copy (the dashboard image cannot import `report.py` — it
    imports `runner.py`, which imports the simulator and pandas). What it does
    not sanction is a copy that drifts: `/sims` and `sweep.md` would then warn
    their readers in different words about the same number, and the newer,
    truer caveat would be the one the dashboard did not have.

    This suite is step 1 of every Cloud Build, so drift fails the build.
    """

    @pytest.mark.parametrize("name", [
        "CROSS_SCENARIO_CAVEAT", "IN_SAMPLE_BANNER", "HOLDOUT_SEMANTICS",
        "TALLY_CAVEAT", "SWEEP_BIASES",
        # FC-096 Phase A PR-2. Pinned here as a CONSTANT; whether it is emitted
        # is derived separately on each side — see TestTheDteReachCaveatIsNotAFork.
        "DTE_REACH_BIAS", "DTE_REACH_BIAS_THRESHOLD",
    ])
    def test_matches_report_py_exactly(self, name):
        assert getattr(T, name) == getattr(engine_report, name), (
            f"services/sweep_report_text.{name} has drifted from "
            f"src/backtesting/scenarios/report.py. Copy the new value across in "
            f"this commit."
        )

    def test_base_scenario_name_matches(self):
        from src.backtesting.scenarios.runner import BASE_SCENARIO_NAME
        assert T.BASE_SCENARIO_NAME == BASE_SCENARIO_NAME

    def test_activity_floor_matches(self):
        from src.backtesting.metrics.fitness import MIN_DAYS_IN_POSITION
        assert T.MIN_DAYS_IN_POSITION == MIN_DAYS_IN_POSITION


class TestTheEngineVersionIsNotAFork:
    def test_matches_screen_py(self):
        """`ENGINE_VERSION` is half of `sweep_key` (D4). A dashboard that
        disagreed would compute a key nothing ever matches, so the dedup would
        never fire — silently, at the cost of a full replay every time."""
        assert S.ENGINE_VERSION == ENGINE_VERSION


class TestTheEngineIdentityIsReadNeverComputed:
    """FC-096 Phase B B1: where the dashboard's half of `sweep_key` comes from.

    The image ships two flat stdlib modules and no `src/` tree, so it cannot
    hash one. `cloudbuild.yaml` runs the SHARED module against the checkout that
    builds the image and bakes the answer in as `ENGINE_IDENTITY`. Two failure
    modes are worth tests: computing it here (drift), and falling back to a
    plausible wrong value when it is absent (a silent wrong dedup).
    """

    def test_it_reads_the_baked_in_env_var(self, monkeypatch):
        monkeypatch.setenv("ENGINE_IDENTITY", BAKED_IDENTITY)
        S._reset_engine_identity_warning()
        assert S.engine_identity_from_env() == BAKED_IDENTITY

    def test_whitespace_is_stripped(self, monkeypatch):
        """A build-arg read out of a file arrives with the trailing newline the
        module's `print` put there. A key over `"abc\n"` is a different key."""
        monkeypatch.setenv("ENGINE_IDENTITY", f"  {BAKED_IDENTITY}\n")
        S._reset_engine_identity_warning()
        assert S.engine_identity_from_env() == BAKED_IDENTITY

    def test_an_absent_env_disables_the_hint_loudly(self, monkeypatch, caplog):
        """It must return None and SAY SO — never key over the empty string.

        `sweep_key(..., engine_identity="")` is a perfectly valid-looking 16-hex
        string that is identical across genuinely different engine builds. A
        dashboard that fell back to it would eventually point an operator at
        another engine's numbers, with nothing in the UI to say so. Losing the
        hint costs one container start; guessing costs correctness.
        """
        import logging

        monkeypatch.delenv("ENGINE_IDENTITY", raising=False)
        S._reset_engine_identity_warning()
        with caplog.at_level(logging.ERROR, logger=S.__name__):
            assert S.engine_identity_from_env() is None
        assert any(r.levelno >= logging.ERROR
                   and "sweep_dedup_hint_disabled" in r.getMessage()
                   for r in caplog.records), caplog.text

    def test_an_empty_env_is_treated_as_absent(self, monkeypatch):
        """`ARG ENGINE_IDENTITY=""` with no `--build-arg` sets it to empty, not
        unset. The two must not behave differently."""
        monkeypatch.setenv("ENGINE_IDENTITY", "")
        S._reset_engine_identity_warning()
        assert S.engine_identity_from_env() is None

    def test_the_absence_is_logged_once_per_process(self, monkeypatch, caplog):
        """One line per process, not one per submit: an ERROR that repeats a
        thousand times a day is an ERROR nobody reads."""
        import logging

        monkeypatch.delenv("ENGINE_IDENTITY", raising=False)
        S._reset_engine_identity_warning()
        with caplog.at_level(logging.ERROR, logger=S.__name__):
            for _ in range(5):
                S.engine_identity_from_env()
        hits = [r for r in caplog.records
                if "sweep_dedup_hint_disabled" in r.getMessage()]
        assert len(hits) == 1, f"{len(hits)} log lines for one absent env"

    def test_this_module_does_not_reimplement_the_hash(self):
        """Parity is by shared-module INVOCATION at build time. A second
        implementation would drift, and its drift is silent: the dashboard would
        compute a key nothing ever writes and the dedup would just stop firing.
        """
        import inspect

        source = inspect.getsource(S)
        assert "hashlib" not in source, (
            "services/sweeps.py must not hash anything itself — the engine "
            "identity is computed by cloudbuild and read from the env"
        )
        assert "os.walk" not in source and "rglob" not in source

    def test_the_key_the_dashboard_computes_is_the_key_the_job_computes(self):
        """One identity in, one key out, on both sides. This is the property the
        whole build-arg exists to provide: a divergence here means the dedup
        never fires across the dashboard/Job pair and nothing says so."""
        from src.backtesting.scenarios.identity import sweep_key as engine_key

        normalised = S.validate_spec(spec())
        assert S.compute_sweep_key(normalised, BAKED_IDENTITY) == engine_key(
            normalised, engine_version=ENGINE_VERSION,
            engine_identity=BAKED_IDENTITY)

    def test_a_different_identity_is_a_different_key(self):
        normalised = S.validate_spec(spec())
        assert (S.compute_sweep_key(normalised, BAKED_IDENTITY)
                != S.compute_sweep_key(normalised, "fedcba9876543210"))

    def test_the_submitted_row_carries_the_identity(self):
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec()), sweep_key_value="k",
            submitted_at="2026-09-01T12:00:00+00:00", git_commit="abc",
            engine_identity=BAKED_IDENTITY)
        assert row["engine_identity"] == BAKED_IDENTITY
        # ...and the commit is still stored. It stopped being identity; it did
        # not stop being provenance.
        assert row["git_commit"] == "abc"

    def test_the_submitted_row_tolerates_a_missing_identity(self):
        """The degraded image still writes a row. The Job's own `running`/`done`
        rows carry the correct identity, and readers take the latest row — so
        the run is still dedup-reachable once it finishes."""
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec()), sweep_key_value=None,
            submitted_at="2026-09-01T12:00:00+00:00", git_commit="abc")
        assert row["engine_identity"] is None
        assert row["sweep_key"] is None

    def test_the_allowlist_payload_exposes_the_identity(self, monkeypatch):
        """So the UI can explain a missing `prior_done_run_id` instead of
        looking broken."""
        monkeypatch.setenv("ENGINE_IDENTITY", BAKED_IDENTITY)
        S._reset_engine_identity_warning()
        assert S.allowlist_payload()["engine_identity"] == BAKED_IDENTITY


class TestTheStatusOrderingIsNotAFork:
    def test_order_by_matches_the_writers(self):
        assert S.LATEST_STATUS_ORDER_BY == store.LATEST_STATUS_ORDER_BY

    def test_status_rank_matches_the_writers(self):
        assert S.STATUS_RANK == store.STATUS_RANK

    def test_terminal_statuses_match(self):
        assert set(S.TERMINAL_STATUSES) == set(store.TERMINAL_STATUSES)


class TestTheSubmittedRowMatchesTheJobsRowShape:
    def test_same_columns_as_the_jobs_status_row(self):
        """A column the API omits renders blank for every dashboard-launched
        sweep while looking correct for CLI ones — the hardest kind of bug to
        notice, because half the table is right."""
        api = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec()), sweep_key_value="k",
            submitted_at="2026-08-29T12:00:00+00:00", git_commit="abc")
        job = store.status_row(
            run_id="r", status=store.STATUS_RUNNING,
            submitted_at="2026-08-29T12:00:00+00:00", spec=S.validate_spec(spec()))
        assert set(api) == set(job)

    def test_every_column_is_declared_in_the_schema(self):
        pytest.importorskip("google.cloud.bigquery")
        declared = {f.name for f in store._sweeps_schema()}
        api = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec()), sweep_key_value="k",
            submitted_at="2026-08-29T12:00:00+00:00", git_commit=None)
        assert set(api) <= declared, set(api) - declared

    def test_scope_columns_are_denormalised_out_of_the_spec(self):
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec(holdout_start="2026-05-01")),
            sweep_key_value="k", submitted_at="2026-08-29T12:00:00+00:00",
            git_commit=None)
        assert row["symbols"] == ["AAPL", "NVDA"]
        assert row["window_start"] == "2025-08-01"
        assert row["holdout_start"] == "2026-05-01"
        assert row["in_sample_only"] is False
        assert row["scenario_count"] == 2       # tighter + implicit base
        assert row["cell_count"] == 2 * 2 * 2   # arms x symbols x splits


# ==========================================================================
# (5) Validation: allowlist agreement, caps, dates, token, launch
# ==========================================================================
class TestAllowlistAgreementWithTheRunner:
    """The API must accept exactly what the Job accepts. Parametrised over
    EVERY key on both lists, so a key added to one and not the other fails."""

    @pytest.mark.parametrize("key", sorted(ALLOWED_OVERRIDES))
    def test_every_allowed_key_is_accepted(self, key):
        value = _plausible_value(key)
        S.validate_spec(spec(scenarios=[{"name": "x", "overrides": {key: value}}]))

    @pytest.mark.parametrize("key", sorted(REJECTED_OVERRIDES))
    def test_every_rejected_key_is_refused_with_the_runners_reason(self, key):
        with pytest.raises(S.SweepValidationError) as exc:
            S.validate_spec(spec(scenarios=[{"name": "x", "overrides": {key: 1}}]))
        # Not merely refused — refused with the SPECIFIC reason. "not allowed"
        # invites the reader to assume the sweep is being conservative when in
        # fact the arm would have measured fiction.
        try:
            validate_override_key(key, 1)
        except OverrideError as expected:
            assert str(expected) in str(exc.value)

    def test_an_unknown_key_is_refused(self):
        with pytest.raises(S.SweepValidationError, match="not a known"):
            S.validate_spec(spec(scenarios=[
                {"name": "x", "overrides": {"strategy.invented_knob": 1}}]))

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_the_dte_range_is_the_engines_rule_not_a_second_copy(self, key):
        """FC-096 Phase A PR-2. The API honours `1 <= n <= MAX_SWEEPABLE_DTE`
        because it calls the runner's validator, not because it re-states the
        bound. `OVERRIDE_VALUE_TYPES` says only "a whole number" for these keys
        precisely so there is one definition of the range."""
        from src.backtesting.scenarios.overrides import MAX_SWEEPABLE_DTE

        for good in (1, 7, 14, MAX_SWEEPABLE_DTE):
            S.validate_spec(spec(scenarios=[
                {"name": "x", "overrides": {key: good}}]))
        for bad in (0, -1, MAX_SWEEPABLE_DTE + 1):
            with pytest.raises(S.SweepValidationError,
                               match="MAX_SWEEPABLE_DTE"):
                S.validate_spec(spec(scenarios=[
                    {"name": "x", "overrides": {key: bad}}]))
        # ...and a non-integer, which the value-type table also refuses. Either
        # message is fine; being accepted is not.
        for bad in (14.0, "14", True):
            with pytest.raises(S.SweepValidationError):
                S.validate_spec(spec(scenarios=[
                    {"name": "x", "overrides": {key: bad}}]))

    def test_a_dte_spec_survives_end_to_end(self):
        """The whole point of PR-2: a DTE arm submitted through the API is
        normalised and hashable, not refused. An allowlisted key missing from
        `OVERRIDE_VALUE_TYPES` is refused by `validate_override_value` with a
        message about the API rather than the knob, which is how the console's
        flagship control would have died silently."""
        out = S.validate_spec(spec(scenarios=[
            {"name": "dte14", "overrides": {"strategy.put_target_dte": 14}},
            {"name": "dte21", "overrides": {"strategy.call_target_dte": 21}},
        ]))
        assert out["scenarios"][0]["overrides"] == {"strategy.put_target_dte": 14}
        assert out["scenarios"][1]["overrides"] == {"strategy.call_target_dte": 21}
        assert S.compute_sweep_key(out, "abc")


def _plausible_value(key):
    if key.endswith("_range"):
        return [0.10, 0.20]
    if key in ("strategy.put_target_dte", "strategy.call_target_dte"):
        return 14
    if key == "universe.excluded_symbols":
        return ["F"]
    if key in ("earnings.enabled", "rolling.enabled"):
        return False
    if key in ("earnings.blackout_days", "rolling.max_extension_days"):
        return 3
    if key == "strategy.min_avg_volume":
        return 1_000_000
    if key in ("strategy.min_stock_price", "strategy.max_stock_price"):
        return 500
    return 0.5


class TestCaps:
    def test_symbol_cap(self):
        many = [f"SYM{chr(65 + i)}" for i in range(S.MAX_SYMBOLS + 1)]
        with pytest.raises(S.SweepValidationError, match="exceeds the cap"):
            S.validate_spec(spec(symbols=many))

    def test_scenario_cap(self):
        arms = [{"name": f"a{i}"} for i in range(S.MAX_SCENARIOS + 1)]
        with pytest.raises(S.SweepValidationError, match="exceeds the cap"):
            S.validate_spec(spec(scenarios=arms))

    def test_cell_cap_counts_the_implicit_base_and_both_splits(self):
        """12 symbols x 2 splits x 11 arms = 264 > 240. The implicit `base` is
        the arm most easily forgotten, and forgetting it under-counts the budget
        by one whole symbol-column."""
        arms = [{"name": f"a{i}"} for i in range(10)]
        with pytest.raises(S.SweepValidationError, match="over the cap of 240"):
            S.validate_spec(spec(symbols=[f"SY{chr(65 + i)}" for i in range(12)],
                                 scenarios=arms, holdout_start="2026-05-01"))

    def test_window_cap(self):
        with pytest.raises(S.SweepValidationError, match="over the"):
            S.validate_spec(spec(start="2020-01-01", end="2026-07-31"))

    def test_starting_cash_bounds(self):
        with pytest.raises(S.SweepValidationError, match="outside"):
            S.validate_spec(spec(starting_cash=500))
        with pytest.raises(S.SweepValidationError, match="outside"):
            S.validate_spec(spec(starting_cash=5_000_000))

    def test_defaults_are_filled_in_not_left_absent(self):
        out = S.validate_spec(spec())
        assert out["starting_cash"] == S.DEFAULT_STARTING_CASH
        assert out["run_sensitivity"] is False
        assert out["holdout_start"] is None


class TestDates:
    def test_end_must_follow_start(self):
        with pytest.raises(S.SweepValidationError, match="must be after"):
            S.validate_spec(spec(start="2026-07-31", end="2025-08-01"))

    def test_non_iso_dates_are_refused(self):
        with pytest.raises(S.SweepValidationError, match="ISO date"):
            S.validate_spec(spec(end="31/07/2026"))

    def test_holdout_must_fall_inside_the_window(self):
        with pytest.raises(S.SweepValidationError, match="must fall inside"):
            S.validate_spec(spec(holdout_start="2024-01-01"))

    def test_a_short_holdout_is_refused_with_the_insuf_reason(self):
        """A three-week holdout comes back `insuf` on symbols that traded
        perfectly well, and an `insuf` column is read as a verdict on the arm."""
        with pytest.raises(S.SweepValidationError, match="at least 60"):
            S.validate_spec(spec(holdout_start="2026-07-15"))

    def test_a_valid_holdout_passes(self):
        out = S.validate_spec(spec(holdout_start="2026-05-01"))
        assert out["holdout_start"] == "2026-05-01"


class TestScenarioShape:
    def test_base_is_reserved(self):
        with pytest.raises(S.SweepValidationError, match="reserved"):
            S.validate_spec(spec(scenarios=[{"name": "base"}]))

    def test_duplicate_names_refused(self):
        with pytest.raises(S.SweepValidationError, match="duplicate"):
            S.validate_spec(spec(scenarios=[{"name": "x"}, {"name": "x"}]))

    def test_fill_haircut_bounds(self):
        with pytest.raises(S.SweepValidationError, match="outside"):
            S.validate_spec(spec(scenarios=[{"name": "x", "fill_haircut": 1.5}]))
        S.validate_spec(spec(scenarios=[{"name": "x", "fill_haircut": 1.0}]))

    def test_unknown_scenario_field_refused(self):
        with pytest.raises(S.SweepValidationError, match="unknown field"):
            S.validate_spec(spec(scenarios=[{"name": "x", "haircut": 1.0}]))

    def test_implausible_symbol_refused(self):
        with pytest.raises(S.SweepValidationError, match="plausible ticker"):
            S.validate_spec(spec(symbols=["AAPL; DROP TABLE"]))

    def test_duplicate_symbol_refused(self):
        with pytest.raises(S.SweepValidationError, match="twice"):
            S.validate_spec(spec(symbols=["AAPL", "aapl"]))

    def test_unknown_top_level_field_refused(self):
        with pytest.raises(S.SweepValidationError, match="unknown field"):
            S.validate_spec(spec(holdout="2026-05-01"))

    def test_every_preset_validates(self):
        """A preset that produced a 422 would teach the operator the allowlist
        is broken rather than that the preset is."""
        arms = [{k: v for k, v in p.items() if k in ("name", "overrides", "fill_haircut")}
                for p in S.PRESETS]
        S.validate_spec(spec(scenarios=arms))


class TestOverrideValueValidation:
    """The allowlist says which knobs may be turned; this says what a legal
    setting of one is.

    Without it `{"strategy.min_put_premium": "not-a-number"}` is accepted and
    the arm either dies three minutes into a container start with a TypeError
    from deep inside the strategy, or — worse — does not die: a string where a
    float is expected can compare false everywhere and read as "this floor
    rejected everything", which is a finding, not a bug report.
    """

    def arm(self, key, value):
        return spec(scenarios=[{"name": "x", "overrides": {key: value}}])

    def test_a_string_where_a_number_belongs_is_422(self):
        with pytest.raises(S.SweepValidationError, match="must be a number"):
            S.validate_spec(self.arm("strategy.min_put_premium", "not-a-number"))

    def test_a_nested_dict_is_422(self):
        with pytest.raises(S.SweepValidationError, match="must be a number"):
            S.validate_spec(self.arm("strategy.min_put_premium", {"lo": 1}))

    def test_a_bool_is_not_a_number(self):
        """`bool` is an `int` subclass in Python; without the explicit check
        `min_put_premium: true` would silently mean a $1.00 floor."""
        with pytest.raises(S.SweepValidationError, match="must be a number"):
            S.validate_spec(self.arm("strategy.min_put_premium", True))

    def test_a_number_where_a_bool_belongs_is_422(self):
        with pytest.raises(S.SweepValidationError, match="true or false"):
            S.validate_spec(self.arm("earnings.enabled", 1))

    def test_a_float_where_a_day_count_belongs_is_422(self):
        with pytest.raises(S.SweepValidationError, match="whole number of days"):
            S.validate_spec(self.arm("earnings.blackout_days", 2.5))

    @pytest.mark.parametrize("band", [
        pytest.param(0.2, id="scalar"),
        pytest.param([0.2], id="one element"),
        pytest.param([0.2, 0.1], id="inverted"),
        pytest.param([0.2, 1.4], id="above 1"),
        pytest.param([-0.1, 0.2], id="below 0"),
        pytest.param(["a", "b"], id="strings"),
        pytest.param([0.2, 0.2], id="empty band"),
    ])
    def test_a_malformed_delta_band_is_422(self, band):
        with pytest.raises(S.SweepValidationError):
            S.validate_spec(self.arm("strategy.call_delta_range", band))

    def test_a_well_formed_band_passes(self):
        S.validate_spec(self.arm("strategy.call_delta_range", [0.15, 0.25]))

    def test_excluded_symbols_must_be_tickers(self):
        with pytest.raises(S.SweepValidationError, match="list of tickers"):
            S.validate_spec(self.arm("universe.excluded_symbols", "F"))
        S.validate_spec(self.arm("universe.excluded_symbols", ["F", "PFE"]))

    def test_every_allowlisted_key_has_a_declared_value_type(self):
        """Otherwise `validate_override_value` refuses it outright — a knob
        nobody can set is worse than one nobody validated, so this failing is
        the signal to add the row rather than to loosen the check."""
        missing = set(ALLOWED_OVERRIDES) - set(S.OVERRIDE_VALUE_TYPES)
        assert missing == set(), missing

    def test_no_value_type_describes_a_key_that_is_not_allowed(self):
        extra = set(S.OVERRIDE_VALUE_TYPES) - set(ALLOWED_OVERRIDES)
        assert extra == set(), extra


class TestScenarioNames:
    def test_the_rule_is_the_engines_not_a_copy(self):
        """`main._scenarios_from_entries` applies the same rule to the CLI/YAML
        path. A second copy would let a `--persist` sweep land a name this API
        would have refused, and the results view would then have to render a
        column header it was designed never to receive."""
        from src.backtesting.scenarios import identity

        assert S.MAX_SCENARIO_NAME_CHARS is identity.MAX_SCENARIO_NAME_CHARS
        assert S.SCENARIO_NAME_RE is identity.SCENARIO_NAME_RE

    def test_the_cli_refuses_what_the_api_refuses(self):
        import main as main_mod

        for bad in ("has space", "-leading", "a" * 41, "semi;colon"):
            with pytest.raises(SystemExit):
                main_mod._scenarios_from_entries([{"name": bad}], "sweep spec")
        # ...and accepts what the API accepts.
        assert len(main_mod._scenarios_from_entries(
            [{"name": "tighter_puts"}, {"name": "v1.2"}], "spec")) == 2

    def test_the_pattern_is_served_to_the_form(self):
        payload = S.allowlist_payload()["caps"]
        assert payload["scenario_name_pattern"] == S.SCENARIO_NAME_RE.pattern
        assert payload["max_scenario_name_chars"] == S.MAX_SCENARIO_NAME_CHARS

    def test_an_over_long_name_is_422(self):
        long = "a" * (S.MAX_SCENARIO_NAME_CHARS + 1)
        with pytest.raises(S.SweepValidationError, match="the cap is"):
            S.validate_spec(spec(scenarios=[{"name": long}]))

    def test_a_name_at_the_cap_passes(self):
        S.validate_spec(spec(scenarios=[{"name": "a" * S.MAX_SCENARIO_NAME_CHARS}]))

    @pytest.mark.parametrize("name", [
        "has space", "semi;colon", "-leading", "quote'd", "new\nline", "sym$bol",
    ])
    def test_a_malformed_name_is_422(self, name):
        with pytest.raises(S.SweepValidationError, match="must start alphanumeric"):
            S.validate_spec(spec(scenarios=[{"name": name}]))

    @pytest.mark.parametrize("name", [
        "tighter_puts", "ceiling-1000", "v1.2", "A1",
    ])
    def test_ordinary_names_pass(self, name):
        S.validate_spec(spec(scenarios=[{"name": name}]))


class TestForce:
    """`force` skips the dedup lookup and is EXCLUDED from `sweep_key`."""

    def test_force_must_be_a_bool(self):
        with pytest.raises(S.SweepValidationError, match="true or false"):
            S.validate_spec(spec(force="yes"))

    def test_force_survives_normalisation(self):
        assert S.validate_spec(spec(force=True))["force"] is True

    def test_force_is_absent_when_not_asked_for(self):
        assert "force" not in S.validate_spec(spec())

    def test_force_does_not_change_the_key(self):
        """A forced re-run must key IDENTICALLY to the run it is deliberately
        reproducing. If it did not, the two would never be comparable and a
        second force would not dedup either."""
        plain = S.compute_sweep_key(S.validate_spec(spec()), "abc")
        forced = S.compute_sweep_key(S.validate_spec(spec(force=True)), "abc")
        assert plain == forced


class TestTheToken:
    def test_matching_token_passes(self):
        assert S.token_matches("s3cret", "s3cret") is True

    def test_wrong_token_fails(self):
        assert S.token_matches("s3cret", "other") is False

    def test_a_prefix_does_not_pass(self):
        """The mutation guard: replace `hmac.compare_digest` with `in` or a
        prefix compare and this fails."""
        assert S.token_matches("s3c", "s3cret") is False
        assert S.token_matches("s3cretXXX", "s3cret") is False

    def test_a_non_ascii_bearer_is_rejected_not_a_500(self):
        """`hmac.compare_digest` on `str` raises TypeError for non-ASCII.

        The bearer is attacker-controlled input on a publicly reachable service
        (FC-094), so `Authorization: Bearer \u00e9` was a one-request uncaught
        500, not a hypothetical. Comparing bytes makes it an ordinary 401.
        """
        assert S.token_matches("s3cr\u00e9t", "s3cret") is False
        assert S.token_matches("s3cret", "s3cr\u00e9t") is False
        assert S.token_matches("\u00e9\u00e9\u00e9", "\u00e9\u00e9\u00e9") is True
        assert S.token_matches("\U0001f512", "s3cret") is False

    def test_an_unconfigured_token_never_matches(self):
        """Fail CLOSED. `None == None` would otherwise let an empty
        Authorization header submit sweeps on a service with no secret wired."""
        assert S.token_matches(None, None) is False
        assert S.token_matches("", "") is False
        assert S.token_matches("anything", None) is False
        assert S.token_matches(None, "configured") is False

    @pytest.mark.parametrize("header,expected", [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER   abc  ", "abc"),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ])
    def test_bearer_extraction(self, header, expected):
        assert S.extract_bearer(header) == expected

    def test_uses_constant_time_comparison(self):
        """Read the source: the dashboard is publicly reachable (FC-094), so a
        timing oracle here turns a 32-character secret into 32 guesses."""
        src = Path(S.__file__).read_text()
        assert "hmac.compare_digest" in src


class TestTheOneAtATimeGate:
    def now(self):
        return datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    def row(self, status, minutes_ago=5, run_id="r1"):
        return {"run_id": run_id, "status": status,
                "submitted_at": (self.now() - timedelta(minutes=minutes_ago)).isoformat()}

    def test_a_running_sweep_blocks(self):
        """The mutation guard for the 409. Two executions of a single-vCPU Job
        contend on one chain cache and each reports the other's fetches."""
        blocking = S.blocking_sweep([self.row("running")], now=self.now())
        assert blocking is not None and blocking["run_id"] == "r1"

    def test_a_submitted_sweep_blocks_too(self):
        assert S.blocking_sweep([self.row("submitted")], now=self.now()) is not None

    @pytest.mark.parametrize("status", ["done", "failed", "deduplicated"])
    def test_terminal_sweeps_do_not_block(self, status):
        assert S.blocking_sweep([self.row(status)], now=self.now()) is None

    def test_a_long_running_sweep_still_blocks(self):
        """The lock must outlive a legitimate COLD sweep.

        The first cut expired it at 1 h, measured from `submitted_at`. Cold
        materialisation is ~5.5 min/symbol, so a 12-symbol year is comfortably
        past that — the lock would have released mid-replay and let a second
        execution contend with the first for the same chain cache, each
        reporting the other's fetches as its own.
        """
        alive = self.row("running", minutes_ago=90)
        assert S.blocking_sweep([alive], now=self.now()) is not None

    def test_the_lock_expires_once_cloud_run_must_have_killed_the_task(self):
        """Past `--task-timeout` + grace there is no process behind the row.

        A Job killed before its `finally` (eviction, cancelled execution) leaves
        a `running` row nothing will ever terminalise, and a permanent lock on a
        dead run takes the feature offline with no way back except a manual
        BigQuery insert.
        """
        beyond = (S.JOB_TASK_TIMEOUT_SECONDS + S.STALE_GRACE_MINUTES * 60) / 60 + 1
        dead = self.row("running", minutes_ago=beyond)
        assert S.blocking_sweep([dead], now=self.now()) is None

        just_inside = self.row(
            "running",
            minutes_ago=(S.JOB_TASK_TIMEOUT_SECONDS
                         + S.STALE_GRACE_MINUTES * 60) / 60 - 1)
        assert S.blocking_sweep([just_inside], now=self.now()) is not None

    def test_liveness_is_measured_from_written_at_not_submitted_at(self):
        """`written_at` is the last sign of life; `submitted_at` is when it
        started. A run that reported in five minutes ago is alive however long
        ago it was submitted — and every row of a submission shares
        `submitted_at`, so that column cannot say anything about liveness."""
        old_submit = (self.now() - timedelta(days=2)).isoformat()
        recent_write = (self.now() - timedelta(minutes=5)).isoformat()
        row = {"run_id": "r", "status": "running",
               "submitted_at": old_submit, "written_at": recent_write}
        assert S.blocking_sweep([row], now=self.now()) is not None

    def test_a_submitted_row_releases_the_lock_when_it_is_declared_stuck(self):
        """A launch that produced no `running` row within ten minutes is not
        running, and holding the lock for another three hours on it was the
        round-1 regression: the row rendered "stuck — check the execution" while
        the endpoint went on refusing every submit."""
        stuck = self.row("submitted", minutes_ago=S.STUCK_AFTER_MINUTES + 1)
        assert S.is_stuck(stuck, now=self.now()) is True
        assert S.blocking_sweep([stuck], now=self.now()) is None

    def test_a_submitted_row_inside_the_container_start_window_still_blocks(self):
        """Container start is 3-4 minutes; releasing during it would let a
        second execution start alongside the one that is booting."""
        booting = self.row("submitted", minutes_ago=S.STUCK_AFTER_MINUTES - 1)
        assert S.is_stuck(booting, now=self.now()) is False
        assert S.blocking_sweep([booting], now=self.now()) is not None

    def test_a_running_row_keeps_the_longer_rule(self):
        """The two states expire differently on purpose: a `running` sweep may
        legitimately be replaying a cold window for hours."""
        long_run = self.row("running", minutes_ago=S.STUCK_AFTER_MINUTES + 30)
        assert S.blocking_sweep([long_run], now=self.now()) is not None

    def stamped(self, status, *, submitted_minutes_ago, written_minutes_ago):
        """A row whose two clocks DIFFER — the real shape after a launch.

        The API writes a second `submitted` row once `jobs.run` returns, to
        record `execution_name`, so `written_at` moves while `submitted_at`
        (the partition key and the submission's identity) does not. Every test
        that uses one timestamp for both cannot see the two readers disagreeing.
        """
        return {
            "run_id": "r1", "status": status,
            "submitted_at": (self.now() - timedelta(
                minutes=submitted_minutes_ago)).isoformat(),
            "written_at": (self.now() - timedelta(
                minutes=written_minutes_ago)).isoformat(),
        }

    def test_the_lock_and_the_stuck_label_agree_for_every_state(self):
        """A row that renders `stuck` must not still be holding the lock.

        The two are read side by side in the UI, and they used to disagree for
        up to the launch latency: `is_stuck` read `submitted_at` while
        `blocking_sweep` read `written_at`. Both now read `_last_seen`, so this
        holds even when the two timestamps differ — which is the normal case,
        not the exotic one.
        """
        cases = [
            # (status, submitted_minutes_ago, written_minutes_ago)
            ("submitted", S.STUCK_AFTER_MINUTES + 1, S.STUCK_AFTER_MINUTES + 1),
            # Submitted long ago, but the launch row landed seconds later: NOT
            # stuck and still holding the lock. Reading `submitted_at` here
            # would have called it stuck while the lock stayed on.
            ("submitted", S.STUCK_AFTER_MINUTES + 5, 1),
            ("running", 400, (S.JOB_TASK_TIMEOUT_SECONDS
                              + S.STALE_GRACE_MINUTES * 60) / 60 + 1),
            ("running", 400, 5),
        ]
        for status, submitted_ago, written_ago in cases:
            row = self.stamped(status, submitted_minutes_ago=submitted_ago,
                               written_minutes_ago=written_ago)
            stuck = S.is_stuck(row, now=self.now())
            blocked = S.blocking_sweep([row], now=self.now()) is not None
            assert stuck is not blocked, (status, submitted_ago, written_ago)

    def test_both_readers_use_the_same_clock(self):
        """The launch-latency window, pinned directly: a `submitted` row that is
        old by `submitted_at` but fresh by `written_at` is neither stuck nor
        released."""
        fresh_launch = self.stamped(
            "submitted",
            submitted_minutes_ago=S.STUCK_AFTER_MINUTES + 5,
            written_minutes_ago=1)
        assert S.is_stuck(fresh_launch, now=self.now()) is False
        assert S.blocking_sweep([fresh_launch], now=self.now()) is not None

    def test_empty_history_does_not_block(self):
        assert S.blocking_sweep([], now=self.now()) is None

    def test_a_bigquery_datetime_is_handled_not_just_a_string(self):
        row = {"run_id": "r", "status": "running",
               "submitted_at": self.now() - timedelta(minutes=5)}
        assert S.blocking_sweep([row], now=self.now()) is not None


class TestStuckDetection:
    def test_submitted_past_the_window_is_stuck(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        old = {"status": "submitted",
               "submitted_at": (now - timedelta(minutes=11)).isoformat()}
        assert S.is_stuck(old, now=now) is True

    def test_container_start_is_not_stuck(self):
        """Container start is 3-4 minutes; flagging at 5 would cry wolf on
        every single sweep."""
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        young = {"status": "submitted",
                 "submitted_at": (now - timedelta(minutes=4)).isoformat()}
        assert S.is_stuck(young, now=now) is False

    def test_a_running_row_is_not_stuck_while_the_task_could_still_be_alive(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        row = {"status": "running",
               "written_at": (now - timedelta(hours=2)).isoformat(),
               "submitted_at": (now - timedelta(hours=2)).isoformat()}
        assert S.is_stuck(row, now=now) is False

    def test_a_running_row_past_the_task_timeout_is_stuck(self):
        """Cloud Run has killed the task by now, so nothing will ever write its
        terminal row. Saying so is the honest alternative to polling an API
        (`run.executions.get`) this service account is not proven to hold."""
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        dead = now - timedelta(seconds=S.JOB_TASK_TIMEOUT_SECONDS
                               + S.STALE_GRACE_MINUTES * 60 + 60)
        row = {"status": "running", "written_at": dead.isoformat(),
               "submitted_at": dead.isoformat()}
        assert S.is_stuck(row, now=now) is True

    @pytest.mark.parametrize("status", ["done", "failed", "deduplicated"])
    def test_a_terminal_row_is_never_stuck(self, status):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        row = {"status": status,
               "written_at": (now - timedelta(days=30)).isoformat(),
               "submitted_at": (now - timedelta(days=30)).isoformat()}
        assert S.is_stuck(row, now=now) is False


class TestTheLaunchBody:
    def test_shape_is_cloud_run_v2_container_overrides(self):
        body = S.launch_body(spec_json='{"a":1}', run_id="rid",
                             submitted_at="2026-08-29T12:00:00+00:00")
        env = body["overrides"]["containerOverrides"][0]["env"]
        assert {e["name"] for e in env} == {
            "SWEEP_SPEC_JSON", "SWEEP_RUN_ID", "SWEEP_SUBMITTED_AT",
            "SWEEP_SUBMITTED_VIA"}
        assert next(e for e in env if e["name"] == "SWEEP_SPEC_JSON")["value"] == '{"a":1}'
        # No `args` override: the Job's own args already say
        # `--command sweep --spec-env SWEEP_SPEC_JSON`, and overriding them here
        # would put the entry point in two places.
        assert "args" not in body["overrides"]["containerOverrides"][0]

    def test_the_job_reads_exactly_these_variable_names(self):
        """The other half of the contract. A rename on one side is a silent
        no-op on the other: the Job would replay the previous execution's spec,
        or none, and nothing in either log would say why.

        `SWEEP_SPEC_JSON` is named in `cloudbuild.yaml` rather than in `main.py`
        — the Job's own `--spec-env SWEEP_SPEC_JSON` argument is what binds the
        name — so it is checked there.
        """
        repo = Path(__file__).resolve().parents[1]
        body = S.launch_body(spec_json="{}", run_id="r",
                             submitted_at="2026-08-29T12:00:00+00:00")
        names = {e["name"] for e in body["overrides"]["containerOverrides"][0]["env"]}

        main_src = (repo / "main.py").read_text()
        for name in names - {"SWEEP_SPEC_JSON"}:
            assert f"'{name}'" in main_src or f'"{name}"' in main_src, name

        assert "--spec-env,SWEEP_SPEC_JSON" in (repo / "cloudbuild.yaml").read_text()

    def test_url_is_the_v2_jobs_run_endpoint(self):
        url = S.job_run_url("proj", "backtest-sweep")
        assert url == ("https://run.googleapis.com/v2/projects/proj/locations/"
                       "us-central1/jobs/backtest-sweep:run")

    def test_the_403_message_names_the_grant(self):
        """A 403 from jobs.run has exactly one cause and a console-only fix
        (the Cloud Resource Manager API is disabled on this project)."""
        detail = S.forbidden_detail("proj", "backtest-sweep", "sa@example.com")
        assert "run.jobs.run" in detail
        assert "sa@example.com" in detail
        assert "console" in detail.lower()
        assert "Nothing was launched" in detail


class TestTheDedupPredicate:
    """`status = 'done'` alone is not "we already have this answer"."""

    def test_requires_no_errored_cells(self):
        """A run every arm of which errored is a page of `err`, not a result.
        Serving it means the operator never learns it is worth re-running."""
        assert "error_cells = 0" in S.done_by_key_sql("p.d")

    def test_requires_the_cells_to_have_landed(self):
        """A run whose `scenario_runs` insert failed has an EMPTY grid; the
        cached answer would be nothing at all."""
        sql = S.done_by_key_sql("p.d")
        assert "rows_persisted = cell_count" in sql
        assert "rows_persisted IS NOT NULL" in sql

    def test_requires_the_effective_config_to_match(self):
        """`sweep_key` covers the spec, the engine version and the commit. It
        cannot see an operator flipping EARNINGS_ENABLED on the Job between two
        otherwise identical submissions — that value is not in the yaml it
        hashes."""
        assert "base_config_hash = @base_config_hash" in S.done_by_key_sql("p.d")

    def test_a_null_config_hash_is_a_no_op_not_a_wipeout(self):
        """The API cannot compute the Job's effective config; when it has never
        seen a run on this commit it passes NULL, and the JOB's own lookup —
        which knows the real hash — is the exact backstop."""
        assert "@base_config_hash IS NULL OR" in S.done_by_key_sql("p.d")

    def test_matches_the_writers_predicate(self):
        """Two dedup implementations, one meaning. The Job's must not be looser
        than the API's, or a Job launched by hand would reuse a run the API
        would have refused."""
        import inspect
        job_sql = inspect.getsource(store.ScenarioRunWriter.find_done_sweep)
        for clause in ("error_cells = 0", "rows_persisted = cell_count",
                       "base_config_hash = @base_config_hash"):
            assert clause in job_sql, clause


class TestSql:
    def test_predicates_are_pushed_inside_the_window_function(self):
        sql = S.done_by_key_sql("p.d")
        inner = sql.split("WHERE rn = 1")[0]
        assert "sweep_key = @sweep_key" in inner, (
            "the sweep_key predicate must be inside the subquery: run_id and "
            "sweep_key are the clustering keys, and filtering outside makes "
            "BigQuery rank every run in the table on every dedup lookup"
        )

    def test_no_query_interpolates_a_value(self):
        """Values are bound as parameters, never formatted into the text."""
        for sql in (S.recent_sweeps_sql("p.d"), S.one_sweep_sql("p.d"),
                    S.done_by_key_sql("p.d"), S.sweep_rows_sql("p.d")):
            assert "@" in sql

    def test_reads_never_touch_backtest_runs(self):
        """The screen's table stays the screen's: its documented demotion query
        takes the latest `run_kind='full'` row, and a hypothetical must never be
        able to displace a real one."""
        for sql in (S.recent_sweeps_sql("p.d"), S.one_sweep_sql("p.d"),
                    S.done_by_key_sql("p.d"), S.sweep_rows_sql("p.d")):
            assert "backtest_runs" not in sql


# ==========================================================================
# (6) shape_results == report.py
# ==========================================================================
def _sample_sweep() -> SweepResult:
    """A sweep with a fit/holdout split and every cell state represented.

    Deliberately messy: base measures NVDA in fit but not in holdout, one arm is
    low-activity on one symbol, one errors. That asymmetry is exactly what the
    common-subset delta and the sign-agreement denominator exist to handle, and a
    clean fixture would let a wrong implementation pass.
    """
    def c(scenario, symbol, split, ann, *, verdict="fit", frac=0.6, err=None):
        return ScenarioResult(
            scenario=scenario, symbol=symbol,
            start=date(2025, 8, 1), end=date(2026, 4, 30) if split == "fit"
            else date(2026, 7, 31),
            split=split, config_hash="cfg", scenario_hash=f"h-{scenario}",
            verdict=verdict, demote=(verdict == "unfit"),
            total_return=ann, annualized_return=ann,
            days_in_position_fraction=frac, decision_days=180,
            cycles_completed=3, cycles_open=0, puts_sold=12, calls_sold=5,
            error=err, replay_seconds=1.1,
        )

    rows = []
    for split in ("fit", "holdout"):
        rows += [
            c("base", "AAPL", split, 0.10 if split == "fit" else 0.06),
            c("base", "NVDA", split,
              0.20 if split == "fit" else None,
              verdict="fit" if split == "fit" else "insufficient"),
            c("base", "UNH", split, 0.02, verdict="marginal"),
            c("tighter", "AAPL", split, 0.14 if split == "fit" else 0.03),
            c("tighter", "NVDA", split, 0.30 if split == "fit" else 0.25),
            c("tighter", "UNH", split, 0.09, frac=0.05),   # low activity
            c("wider", "AAPL", split, 0.08, verdict="unfit"),
            c("wider", "NVDA", split, None, err="RuntimeError: boom"),
            c("wider", "UNH", split, 0.11 if split == "fit" else -0.04),
        ]
    return SweepResult(
        rows=rows,
        scenarios=["base", "tighter", "wider"],
        symbols=["AAPL", "NVDA", "UNH"],
        windows=[("fit", date(2025, 8, 1), date(2026, 4, 30)),
                 ("holdout", date(2026, 5, 1), date(2026, 7, 31))],
        base_config_hash="basehash",
        scenario_config_hashes={n: "cfg" for n in ("base", "tighter", "wider")},
        scenario_hashes={n: f"h-{n}" for n in ("base", "tighter", "wider")},
        scenario_overrides={"base": {}, "tighter": {"strategy.min_put_premium": 0.75},
                            "wider": {"strategy.min_put_premium": 0.25}},
        scenario_fill_haircuts={"base": None, "tighter": None, "wider": None},
        wall_seconds=120.0, starting_cash=100_000.0,
    )


@pytest.fixture(scope="module")
def shaped_and_engine():
    """`shape_results` over the SAME sweep the engine reports on.

    The stored rows are produced by `rows_from_sweep`, so this exercises the real
    round trip — dataclass -> BigQuery row -> API shape — rather than a
    hand-written fixture that could quietly agree with a wrong implementation.
    """
    sweep = _sample_sweep()
    rows = store.rows_from_sweep(
        sweep, run_id="rid", submitted_at="2026-08-29T12:00:00+00:00",
        engine_version=ENGINE_VERSION)
    sweep_row = {
        "run_id": "rid", "status": "done", "in_sample_only": False,
        "spec_json": json.dumps({
            "symbols": ["AAPL", "NVDA", "UNH"],
            "scenarios": [{"name": "tighter"}, {"name": "wider"}],
        }),
    }
    return S.shape_results(sweep_row, rows), sweep, json.loads(
        engine_report.render_json(sweep))


class TestShapeResultsEqualsTheEngineReport:
    def test_sign_agreement_matches(self, shaped_and_engine):
        shaped, sweep, rendered = shaped_and_engine
        for name in sweep.scenarios:
            agreeing, comparable = engine_report.sign_agreement(sweep, name)
            assert shaped["sign_agreement"][name] == {
                "agreeing": agreeing, "comparable": comparable}, name
        # And equal to what the CLI's own JSON export carries.
        assert shaped["sign_agreement"] == rendered["sign_agreement"]

    def test_delta_vs_base_matches(self, shaped_and_engine):
        shaped, sweep, rendered = shaped_and_engine
        for split, _s, _e in sweep.windows:
            for name in sweep.scenarios:
                value, n = engine_report.common_delta(sweep, name, split)
                got = shaped["delta_vs_base"][split][name]
                assert got["symbols"] == n, (split, name)
                if value is None:
                    assert got["median"] is None
                else:
                    assert got["median"] == pytest.approx(value), (split, name)
        assert set(shaped["delta_vs_base"]) == set(rendered["delta_vs_base"])

    def test_median_min_max_and_the_state_counts_match(self, shaped_and_engine):
        shaped, sweep, _ = shaped_and_engine
        by_key = {(r["scenario"], r["split"]): r for r in shaped["summary"]}
        for split, _s, _e in sweep.windows:
            for name in sweep.scenarios:
                rows = [r for r in sweep.rows
                        if r.scenario == name and r.split == split]
                measured = [r for r in rows if r.measured]
                values = [r.annualized_return for r in measured
                          if r.annualized_return is not None]
                got = by_key[(name, split)]
                assert got["measured"] == len(measured)
                assert got["insufficient"] == sum(1 for r in rows if r.insufficient)
                assert got["low_activity"] == sum(1 for r in rows if r.low_activity)
                assert got["errors"] == sum(1 for r in rows if r.error)
                if values:
                    assert got["median"] == pytest.approx(median(values))
                    assert got["min"] == pytest.approx(min(values))
                    assert got["max"] == pytest.approx(max(values))
                else:
                    assert got["median"] is None

    def test_the_state_counts_partition_every_cell(self, shaped_and_engine):
        shaped, sweep, _ = shaped_and_engine
        for row in shaped["summary"]:
            total = (row["measured"] + row["insufficient"]
                     + row["low_activity"] + row["errors"])
            assert total == len(sweep.symbols), row

    def test_in_sample_flags_match(self, shaped_and_engine):
        shaped, sweep, rendered = shaped_and_engine
        assert shaped["in_sample_only"] == sweep.in_sample_only == rendered[
            "in_sample_only"]
        assert shaped["in_sample_banner"] is None
        assert shaped["holdout_semantics"] == engine_report.HOLDOUT_SEMANTICS

    def test_the_bias_footer_is_served_verbatim(self, shaped_and_engine):
        shaped, _sweep, rendered = shaped_and_engine
        assert shaped["known_biases"] == rendered["known_biases"]
        assert shaped["cross_scenario_caveat"] == rendered["cross_scenario_caveat"]
        assert shaped["rejection_tally_caveat"] == rendered["rejection_tally_caveat"]


class TestShapeResultsPrBContract:
    """The fields PR-B's page renders directly. Added in review round 1 so the
    UI never recomputes something the store already holds."""

    def test_scenario_hashes_come_from_the_persisted_rows(self, shaped_and_engine):
        shaped, sweep, _ = shaped_and_engine
        assert shaped["scenario_hashes"] == {
            name: f"h-{name}" for name in sweep.scenarios}

    def test_scenario_config_hashes_come_from_the_persisted_rows(
            self, shaped_and_engine):
        shaped, sweep, _ = shaped_and_engine
        assert shaped["scenario_config_hashes"] == {
            name: "cfg" for name in sweep.scenarios}

    def test_windows_are_per_split_iso_dates(self, shaped_and_engine):
        shaped, _sweep, _ = shaped_and_engine
        assert shaped["windows"] == {
            "fit": {"start": "2025-08-01", "end": "2026-04-30"},
            "holdout": {"start": "2025-08-01", "end": "2026-07-31"},
        }

    def test_a_non_done_run_has_empty_grid_splits_and_windows(self):
        """A `submitted` or `running` sweep has a status row and no cells. The
        page has to render that, not 500."""
        shaped = S.shape_results({"run_id": "r", "status": "running"}, [])
        assert shaped["grid"] == {}
        assert shaped["splits"] == []
        assert shaped["windows"] == {}
        assert shaped["scenario_hashes"] == {}
        assert shaped["scenario_config_hashes"] == {}


class TestSymbolOrderIsDeclarationOrder:
    """Pinned on BOTH sides. The grid's columns are read in the order the
    operator typed their universe; if the store sorted and the shaper did not
    (or the reverse), a dashboard-launched sweep would silently render its
    columns transposed relative to the spec that produced it."""

    UNSORTED = ["NVDA", "AAPL", "UNH"]

    def test_the_api_row_keeps_declaration_order(self):
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(spec(symbols=self.UNSORTED)),
            sweep_key_value="k", submitted_at="2026-08-29T12:00:00+00:00",
            git_commit=None)
        assert row["symbols"] == self.UNSORTED

    def test_the_job_row_keeps_declaration_order(self):
        row = store.status_row(
            run_id="r", status=store.STATUS_RUNNING,
            submitted_at="2026-08-29T12:00:00+00:00",
            spec={"symbols": self.UNSORTED})
        assert row["symbols"] == self.UNSORTED

    def test_the_job_row_de_duplicates_in_place(self):
        row = store.status_row(
            run_id="r", status=store.STATUS_RUNNING,
            submitted_at="2026-08-29T12:00:00+00:00",
            spec={"symbols": ["NVDA", "aapl", "NVDA"]})
        assert row["symbols"] == ["NVDA", "AAPL"]

    def test_the_shaper_keeps_declaration_order(self):
        shaped = S.shape_results(
            {"run_id": "r", "spec_json": json.dumps({"symbols": self.UNSORTED})},
            [])
        assert shaped["symbols"] == self.UNSORTED


class TestShapeResultsGrid:
    def test_the_grid_is_always_present(self, shaped_and_engine):
        """There is no shape in which a caller gets aggregates without the
        per-symbol cells: one blended number hides "one symbol carried the arm"
        and "better everywhere by a hair" equally, and those are opposite
        findings deserving opposite actions."""
        shaped, _sweep, _ = shaped_and_engine
        assert set(shaped["grid"]) == {"fit", "holdout"}
        for split in shaped["grid"]:
            for scenario in shaped["scenarios"]:
                assert set(shaped["grid"][split][scenario]) == set(shaped["symbols"])

    def test_base_is_first_and_declaration_order_is_kept(self, shaped_and_engine):
        shaped, _sweep, _ = shaped_and_engine
        assert shaped["scenarios"] == ["base", "tighter", "wider"]
        assert shaped["symbols"] == ["AAPL", "NVDA", "UNH"]

    def test_every_cell_carries_its_state_not_just_a_number(self, shaped_and_engine):
        shaped, _sweep, _ = shaped_and_engine
        grid = shaped["grid"]["holdout"]
        assert grid["base"]["NVDA"]["state"] == "insufficient"
        assert grid["tighter"]["UNH"]["state"] == "low_activity"
        assert grid["wider"]["NVDA"]["state"] == "error"
        assert grid["base"]["AAPL"]["state"] == "measured"

    def test_a_low_activity_cell_keeps_its_fraction(self, shaped_and_engine):
        """"low-act 4%" and "low-act 24%" are very different amounts of
        evidence; collapsing them into one label hides which cells are nearly
        usable."""
        shaped, _sweep, _ = shaped_and_engine
        cell = shaped["grid"]["fit"]["tighter"]["UNH"]
        assert cell["days_in_position_fraction"] == pytest.approx(0.05)

    def test_non_measured_cells_never_reach_an_aggregate(self, shaped_and_engine):
        """`tighter`'s UNH cell is low-activity at +9%, which would drag its
        median up if it counted. It must not."""
        shaped, _sweep, _ = shaped_and_engine
        row = next(r for r in shaped["summary"]
                   if r["scenario"] == "tighter" and r["split"] == "fit")
        assert row["measured"] == 2
        assert row["median"] == pytest.approx(median([0.14, 0.30]))

    def test_base_has_no_delta_against_itself(self, shaped_and_engine):
        shaped, _sweep, _ = shaped_and_engine
        for row in shaped["summary"]:
            if row["scenario"] == "base":
                assert row["delta_vs_base"] is None

    def test_an_empty_run_shapes_without_raising(self):
        """A sweep still `running` has a status row and no cells. The page has
        to render that, not 500."""
        shaped = S.shape_results({"run_id": "r", "status": "running"}, [])
        assert shaped["grid"] == {}
        assert shaped["summary"] == []
        assert shaped["sign_agreement"] is None
        assert shaped["in_sample_only"] is True


class TestTheDashboardImageLayout:
    """`services/sweeps.py` must import with NO `src` package on the path.

    Everything else in this file runs in the repo, where the real
    `src.backtesting.scenarios.*` modules resolve — so every other test here
    passes even if the image's fallback import is broken, and the failure would
    surface as a dashboard that 500s on `/api/v2/sweeps/allowlist` in
    production. This test reproduces the image's flat layout in a temp dir and
    imports there, in a subprocess with `src` deliberately unreachable.

    It also proves the two engine modules are genuinely dependency-free: the
    subprocess has no PyYAML-backed `Config`, no structlog configuration and no
    settings file, exactly like the image.
    """

    def _image_dir(self, tmp_path):
        import shutil
        repo = Path(__file__).resolve().parents[1]
        img = tmp_path / "app"
        shutil.copytree(repo / "dashboard" / "backend", img)
        # The two COPY lines from dashboard/Dockerfile, by hand.
        shutil.copy(repo / "src/backtesting/scenarios/overrides.py",
                    img / "scenario_overrides.py")
        shutil.copy(repo / "src/backtesting/scenarios/identity.py",
                    img / "scenario_identity.py")
        return img

    def test_imports_and_validates_with_no_engine_present(self, tmp_path):
        import subprocess
        import sys as _sys

        img = self._image_dir(tmp_path)
        program = """
import json, sys
import services.sweeps as S
assert S.sweep_key.__module__ == "scenario_identity", S.sweep_key.__module__
assert S.validate_override_key.__module__ == "scenario_overrides"
spec = S.validate_spec({"symbols": ["AAPL"], "start": "2025-08-01",
                        "end": "2026-07-31",
                        "scenarios": [{"name": "t", "overrides":
                                       {"strategy.min_put_premium": 0.75}}]})
try:
    S.validate_spec({"symbols": ["AAPL"], "start": "2025-08-01",
                     "end": "2026-07-31",
                     "scenarios": [{"name": "t", "overrides":
                                    {"strategy.put_target_dte": 99}}]})
    raise SystemExit("a rejected override was accepted")
except S.SweepValidationError as exc:
    assert "MAX_SWEEPABLE_DTE" in str(exc), str(exc)
print(json.dumps({"key": S.compute_sweep_key(spec, "abc123"),
                  "allowed": len(S.allowlist_payload()["allowed"])}))
"""
        proc = subprocess.run(
            [_sys.executable, "-c", program], cwd=str(img), capture_output=True,
            text=True,
            # PYTHONPATH empty and cwd=img: `src` is not importable, so the
            # fallback branch is the only one that can succeed.
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "",
                 "HOME": str(tmp_path)},
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["allowed"] == len(ALLOWED_OVERRIDES)
        # And the key it computes is the SAME key the repo computes. If it were
        # not, the dashboard's dedup lookup would never match a Job's row.
        here = S.compute_sweep_key(
            S.validate_spec(spec(scenarios=[
                {"name": "t", "overrides": {"strategy.min_put_premium": 0.75}}],
                symbols=["AAPL"])), "abc123")
        assert payload["key"] == here


# The endpoint tests import routers.v2, which needs FastAPI — present in the
# dashboard image but NOT in the bot CI image where this suite runs. Skip is
# CLASS-scoped for the reason tests/test_dashboard_pause_alert.py gives: a
# module-level importorskip would abort collection of the whole file and
# silently skip every pure test above it too, turning CI green while testing
# nothing.
_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


class FakeBQ:
    """A BigQueryService stand-in that records writes and can be made to fail."""

    def __init__(self, *, prior=None, history=None, raises=None):
        self.rows = []
        self.prior = prior
        self.history = history or []
        self.raises = raises

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def find_done_sweep(self, key, base_config_hash=None,
                        engine_identity=None):
        self._maybe_raise()
        return self.prior

    def get_recent_sweeps(self, limit=25):
        self._maybe_raise()
        return list(self.history)

    def get_sweep(self, run_id):
        self._maybe_raise()
        return None

    def get_sweep_rows(self, run_id):
        self._maybe_raise()
        return []

    def insert_sweep_status(self, row):
        self.rows.append(row)


@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestTheSubmitEndpoint:
    """The one route that CAUSES something, and every way its launch can fail.

    Each of these used to end as an uncaught 500 with the `submitted` row left
    non-terminal — which holds the one-at-a-time lock until the stale cutoff and
    hands the operator a stack trace instead of a reason.
    """

    TOKEN = "s3cret-token-value"

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    @pytest.fixture
    def wired(self, monkeypatch):
        import routers.v2 as v2

        monkeypatch.setenv("SWEEP_SUBMIT_TOKEN", self.TOKEN)
        monkeypatch.setenv("GIT_COMMIT", "cafebabe")
        # What cloudbuild's `compute-engine-identity` step bakes in. Set here
        # rather than per test because EVERY submit path reads it now: an image
        # without it is the degraded case, and it has its own tests below.
        monkeypatch.setenv("ENGINE_IDENTITY", BAKED_IDENTITY)
        S._reset_engine_identity_warning()
        bq = FakeBQ()
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: bq)
        return v2, bq

    def submit(self, v2, body=None, token=None):
        return self._run(v2.submit_sweep(
            spec=body or spec(),
            authorization=f"Bearer {self.TOKEN if token is None else token}"))

    # -- transport failures ------------------------------------------------
    def test_an_httpx_timeout_becomes_502_and_a_failed_row(self, wired, monkeypatch):
        """The headline fix. `httpx` RAISES on a timeout rather than returning a
        response, so an escaping `ReadTimeout` was a 500 — and the `submitted`
        row it had already written was never terminalised, wedging the gate."""
        import httpx
        from fastapi import HTTPException

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def timeout(*_a, **_k):
            raise httpx.ReadTimeout("timed out")

        monkeypatch.setattr(httpx.AsyncClient, "post", timeout)

        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 502
        assert "ReadTimeout" in str(exc.value.detail)
        # ...and it says the ambiguity out loud rather than guessing.
        assert "may or may not have started" in str(exc.value.detail)

        statuses = [r["status"] for r in bq.rows]
        assert statuses == ["submitted", "failed"], statuses
        assert "ReadTimeout" in bq.rows[-1]["error"]
        assert bq.rows[-1]["finished_at"] is not None

    def test_the_failed_row_reopens_the_gate(self, wired, monkeypatch):
        """A terminal row is what stops the next submit getting a 409 for an
        hour over a launch that never happened."""
        import httpx
        from fastapi import HTTPException

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def refused(*_a, **_k):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx.AsyncClient, "post", refused)
        with pytest.raises(HTTPException):
            self.submit(v2)

        # `get_recent_sweeps` returns the LATEST row per run_id (that is what
        # the window function in `recent_sweeps_sql` is for), so the gate sees
        # the terminal row, not the `submitted` one underneath it.
        assert bq.rows[-1]["status"] in S.TERMINAL_STATUSES
        assert S.blocking_sweep([bq.rows[-1]]) is None

    def test_a_credential_failure_becomes_502_not_500(self, wired, monkeypatch):
        from fastapi import HTTPException

        v2, bq = wired

        def boom():
            raise RuntimeError("metadata server unreachable")

        monkeypatch.setattr(v2, "_access_token", boom)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 502
        assert "credential" in str(exc.value.detail)
        assert [r["status"] for r in bq.rows] == ["submitted", "failed"]

    def test_an_unexpected_transport_error_is_still_502(self, wired, monkeypatch):
        """Nothing may escape as a 500 — the `submitted` row is already written
        by the time the POST happens."""
        import httpx
        from fastapi import HTTPException

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def weird(*_a, **_k):
            raise ValueError("something nobody predicted")

        monkeypatch.setattr(httpx.AsyncClient, "post", weird)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 502
        assert [r["status"] for r in bq.rows] == ["submitted", "failed"]

    def test_a_403_names_the_grant(self, wired, monkeypatch):
        import httpx
        from fastapi import HTTPException

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def forbidden(*_a, **_k):
            return httpx.Response(403, text="forbidden")

        monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 502
        assert "run.jobs.run" in str(exc.value.detail)
        assert [r["status"] for r in bq.rows] == ["submitted", "failed"]

    # -- auth --------------------------------------------------------------
    def test_a_wrong_token_is_401_and_writes_nothing(self, wired):
        from fastapi import HTTPException

        v2, bq = wired
        with pytest.raises(HTTPException) as exc:
            self.submit(v2, token="wrong")
        assert exc.value.status_code == 401
        assert bq.rows == []

    def test_a_non_ascii_token_is_401_not_500(self, wired):
        """`hmac.compare_digest` on `str` raises TypeError for non-ASCII; from
        inside a handler that is an uncaught 500 on attacker-controlled input."""
        from fastapi import HTTPException

        v2, _bq = wired
        with pytest.raises(HTTPException) as exc:
            self.submit(v2, token="s3cr\u00e9t")
        assert exc.value.status_code == 401

    def test_an_unconfigured_secret_is_503_and_writes_nothing(
            self, wired, monkeypatch):
        from fastapi import HTTPException

        v2, bq = wired
        monkeypatch.delenv("SWEEP_SUBMIT_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 503
        assert "sweeps are disabled" in str(exc.value.detail)
        assert bq.rows == []

    # -- store not created yet --------------------------------------------
    def test_missing_tables_are_503_with_the_rollout_step(self, wired, monkeypatch):
        """The tables are created by the JOB's writer, so between merge and the
        operator's first execution they do not exist. A bare 500 says "the
        dashboard is broken"; the truth is "step 3 has not been run"."""
        from fastapi import HTTPException
        from google.cloud.exceptions import NotFound

        v2, _bq = wired
        broken = FakeBQ(raises=NotFound("Not found: Table scenario_sweeps"))
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: broken)

        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 503
        assert "rollout step 3" in str(exc.value.detail)

    def test_missing_tables_are_503_on_the_list_route(self, wired, monkeypatch):
        from fastapi import HTTPException
        from google.cloud.exceptions import NotFound

        v2, _bq = wired
        monkeypatch.setattr(v2, "get_bigquery_service",
                            lambda: FakeBQ(raises=NotFound("Not found: Table")))
        with pytest.raises(HTTPException) as exc:
            self._run(v2.list_sweeps(limit=10))
        assert exc.value.status_code == 503

    def test_missing_tables_are_503_on_the_detail_route(self, wired, monkeypatch):
        from fastapi import HTTPException
        from google.cloud.exceptions import NotFound

        v2, _bq = wired
        monkeypatch.setattr(v2, "get_bigquery_service",
                            lambda: FakeBQ(raises=NotFound("Not found: Table")))
        with pytest.raises(HTTPException) as exc:
            self._run(v2.get_sweep("abc123"))
        assert exc.value.status_code == 503

    def test_an_ordinary_bigquery_failure_is_not_disguised_as_503(
            self, wired, monkeypatch):
        """Only a MISSING TABLE gets the friendly message. A permissions error
        or a genuine outage must not be reported as "run the CLI sweep first"."""
        v2, _bq = wired
        monkeypatch.setattr(v2, "get_bigquery_service",
                            lambda: FakeBQ(raises=RuntimeError("quota exceeded")))
        with pytest.raises(RuntimeError, match="quota"):
            self._run(v2.list_sweeps(limit=10))

    # -- gates -------------------------------------------------------------
    def _blocked_detail(self, v2, monkeypatch, status):
        from fastapi import HTTPException

        stamp = datetime.now(timezone.utc).isoformat()
        live = FakeBQ(history=[{"run_id": "other", "status": status,
                                "written_at": stamp, "submitted_at": stamp}])
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: live)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 409
        return str(exc.value.detail)

    def test_the_409_names_the_right_clock_for_a_submitted_blocker(
            self, wired, monkeypatch):
        """The two states expire on different clocks, and a message quoting the
        wrong one told an operator to wait three hours for a lock that frees in
        ten minutes. "Wait three hours" is how a feature gets abandoned."""
        v2, _bq = wired
        detail = self._blocked_detail(v2, monkeypatch, "submitted")
        assert f"{S.STUCK_AFTER_MINUTES} minutes" in detail
        assert "task timeout" not in detail

    def test_the_409_names_the_right_clock_for_a_running_blocker(
            self, wired, monkeypatch):
        v2, _bq = wired
        detail = self._blocked_detail(v2, monkeypatch, "running")
        assert "task timeout" in detail
        assert f"{S.JOB_TASK_TIMEOUT_SECONDS // 3600}h" in detail
        assert f"{S.STUCK_AFTER_MINUTES} minutes" not in detail

    def test_a_running_sweep_is_409(self, wired, monkeypatch):
        from fastapi import HTTPException

        v2, _bq = wired
        live = FakeBQ(history=[{
            "run_id": "other", "status": "running",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "submitted_at": datetime.now(timezone.utc).isoformat()}])
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: live)
        with pytest.raises(HTTPException) as exc:
            self.submit(v2)
        assert exc.value.status_code == 409
        assert "other" in str(exc.value.detail)
        assert live.rows == []

    def test_a_prior_done_run_still_launches_and_is_only_a_hint(
            self, wired, monkeypatch):
        """THE API NEVER DEDUPLICATES (round-2 fix 1).

        It used to short-circuit here, binding the config predicate to the last
        `base_config_hash` any run on this commit had recorded. That is
        self-referential: after an operator flips `ROLLER_ENABLED` on the Job, a
        re-submitted spec matches the *pre-flip* run's own hash and dedups to it
        for ever — and because nothing is launched, the Job's exact check, the
        one that would have caught the flip, never runs.

        So it launches, every time, and returns the prior run as a HINT.
        """
        import httpx

        v2, _bq = wired
        cached = FakeBQ(prior={"run_id": "earlierrun000001"})
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: cached)
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")
        launched = []

        async def ok(self, url, **kwargs):
            launched.append(kwargs.get("json"))
            return httpx.Response(200, json={"metadata": {"name": "exec-1"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)

        out = self.submit(v2)
        assert out["status"] == "submitted"
        assert out["launched"] is True
        assert len(launched) == 1
        # The hint, and nothing that reads as a decision.
        assert out["prior_done_run_id"] == "earlierrun000001"
        assert out["deduplicated_to"] is None
        assert [r["status"] for r in cached.rows] == ["submitted", "submitted"]

    def test_the_api_never_writes_a_deduplicated_row(self, wired, monkeypatch):
        """Only the Job may write that status — it is the only side that can
        check the effective config."""
        import httpx

        v2, _bq = wired
        cached = FakeBQ(prior={"run_id": "earlierrun000001"})
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: cached)
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def ok(*_a, **_k):
            return httpx.Response(200, json={"metadata": {"name": "exec-1"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)
        self.submit(v2)
        assert all(r["status"] != "deduplicated" for r in cached.rows)

    def test_the_hint_is_not_bound_to_a_config_hash(self, wired, monkeypatch):
        """The self-referential bind is gone: the API passes no
        `base_config_hash`, so its answer cannot masquerade as a decision.

        Checked on the CALL, not on the source — the docstring necessarily
        mentions the parameter it no longer sends.
        """
        import httpx

        v2, _bq = wired
        recorded = []

        class Recording(FakeBQ):
            def find_done_sweep(self, key, base_config_hash=None,
                                engine_identity=None):
                recorded.append({"key": key, "hash": base_config_hash,
                                 "identity": engine_identity})
                return None

        monkeypatch.setattr(v2, "get_bigquery_service", lambda: Recording())
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def ok(*_a, **_k):
            return httpx.Response(200, json={"metadata": {"name": "e"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)
        self.submit(v2)

        assert len(recorded) == 1
        assert recorded[0]["hash"] is None
        # ...but the engine identity IS bound. It is not a configuration guess:
        # it is the image's own baked-in value, and binding it is what keeps a
        # pre-migration row (NULL column, commit-keyed) out of the hint.
        assert recorded[0]["identity"] == BAKED_IDENTITY

    def test_the_submit_carries_the_baked_identity_end_to_end(
            self, wired, monkeypatch):
        """The value reaches BOTH the response key and the stored row.

        A `sweep_key` on the row that the Job's own key does not match would
        make the run invisible to the dedup for ever, which is the failure the
        whole build-arg exists to prevent.
        """
        import httpx

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def ok(*_a, **_k):
            return httpx.Response(200, json={"metadata": {"name": "e"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)
        out = self.submit(v2)

        expected = S.compute_sweep_key(S.validate_spec(spec()), BAKED_IDENTITY)
        assert out["sweep_key"] == expected
        assert bq.rows[0]["sweep_key"] == expected
        assert bq.rows[0]["engine_identity"] == BAKED_IDENTITY

    def test_an_image_without_the_identity_still_launches_but_skips_the_hint(
            self, wired, monkeypatch):
        """The degraded posture, end to end.

        An image built without the build-arg cannot compute `sweep_key` at all.
        It must NOT key over a fallback — that key is a valid-looking 16 hex
        characters shared by every engine build, and a hit on it would point an
        operator at another engine's numbers. So: no hint lookup, a NULL
        `sweep_key` on the `submitted` row, and the submission still launches.
        The Job computes the real key from the tree it is running and does the
        dedup that decides anything.
        """
        import httpx

        v2, _bq = wired
        monkeypatch.delenv("ENGINE_IDENTITY", raising=False)
        S._reset_engine_identity_warning()

        looked_up = []

        class Recording(FakeBQ):
            def find_done_sweep(self, key, base_config_hash=None,
                                engine_identity=None):
                looked_up.append(key)
                return {"run_id": "earlierrun000001"}

        recording = Recording()
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: recording)
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        launched = []

        async def ok(self, url, **kwargs):
            launched.append(kwargs.get("json"))
            return httpx.Response(200, json={"metadata": {"name": "e"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)
        out = self.submit(v2)

        assert looked_up == [], (
            "the hint must not be looked up without an identity to bind — a "
            "query on a key computed over the empty string can HIT, and the hit "
            "would be another engine's run"
        )
        assert out["launched"] is True and len(launched) == 1
        assert out["sweep_key"] is None
        assert out["prior_done_run_id"] is None
        assert recording.rows[0]["sweep_key"] is None
        assert recording.rows[0]["engine_identity"] is None

    def test_the_self_referential_lookup_is_gone_entirely(self):
        """Dead code that encodes a rejected design is worse than none: the next
        reader would take it for the intended one."""
        from services import bigquery as bq_mod
        from services import sweeps as sweeps_mod

        assert not hasattr(bq_mod.BigQueryService, "latest_base_config_hash")
        assert not hasattr(sweeps_mod, "latest_base_config_hash_sql")

    def test_force_suppresses_even_the_hint(self, wired, monkeypatch):
        import httpx

        v2, _bq = wired
        cached = FakeBQ(prior={"run_id": "earlierrun000001"})
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: cached)
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")

        async def ok(*_a, **_k):
            return httpx.Response(200, json={"metadata": {"name": "exec-1"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)

        out = self.submit(v2, body=spec(force=True))
        assert out["launched"] is True
        assert out["forced"] is True
        assert out["deduplicated_to"] is None
        assert out["prior_done_run_id"] is None

    def test_a_422_writes_nothing(self, wired):
        from fastapi import HTTPException

        v2, bq = wired
        with pytest.raises(HTTPException) as exc:
            self.submit(v2, body=spec(scenarios=[
                {"name": "x", "overrides": {"strategy.put_target_dte": 99}}]))
        assert exc.value.status_code == 422
        assert "MAX_SWEEPABLE_DTE" in str(exc.value.detail)
        assert bq.rows == []

    # -- the happy path ----------------------------------------------------
    def test_a_successful_launch_records_the_execution_name(
            self, wired, monkeypatch):
        import httpx

        v2, bq = wired
        monkeypatch.setattr(v2, "_access_token", lambda: "tok")
        seen = {}

        async def ok(self, url, **kwargs):
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            return httpx.Response(200, json={"metadata": {"name": "exec-42"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)

        out = self.submit(v2)
        assert out["status"] == "submitted"
        assert out["execution_name"] == "exec-42"
        assert seen["url"].endswith("/jobs/backtest-sweep:run")
        env = seen["json"]["overrides"]["containerOverrides"][0]["env"]
        assert {e["name"] for e in env} == {
            "SWEEP_SPEC_JSON", "SWEEP_RUN_ID", "SWEEP_SUBMITTED_AT",
            "SWEEP_SUBMITTED_VIA"}
        # `args` is deliberately not overridden — the v2 API REPLACES args (it
        # merges env), so sending them would duplicate the Job's entry point.
        assert "args" not in seen["json"]["overrides"]["containerOverrides"][0]
        assert [r["status"] for r in bq.rows] == ["submitted", "submitted"]
        assert bq.rows[-1]["execution_name"] == "exec-42"


@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestTheAuthCallsDoNotBlockTheEventLoop:
    def test_the_token_is_fetched_in_a_threadpool(self):
        """`google.auth.default()` and `credentials.refresh()` both do blocking
        I/O (the metadata server on Cloud Run). Awaited inline in an `async def`
        they stall the whole event loop, so a metadata hiccup would freeze every
        other dashboard request, not just this one."""
        import inspect

        import routers.v2 as v2
        source = inspect.getsource(v2._launch_job)
        assert "run_in_threadpool(_access_token)" in source
        # ...and the blocking work really is in the sync helper, not inline.
        helper = inspect.getsource(v2._access_token)
        assert "google.auth.default" in helper
        assert "credentials.refresh" in helper
        assert "google.auth.default" not in source


class TestTheDashboardPathDoesNotShadowTheCli:
    """`import main` must still be the repo's CLI after this module loads.

    `sys.path` is process-global and outlives the module that edited it. Putting
    `dashboard/backend` at the FRONT (the first cut, copied from
    test_dashboard_pause_alert.py) makes `import main` resolve to the FastAPI app
    for every test module collected after this one — alphabetically, everything
    from `test_e*` onwards. It stayed invisible until `test_scenario_persist.py`
    imported `main`, and then presented as an AttributeError about a function
    that plainly exists.

    Asserted here rather than left to collection order, because the symptom
    appears in a different file from the cause.
    """

    def test_main_resolves_to_the_repo_cli_not_the_fastapi_app(self):
        """Asked of `sys.path`, not of `sys.modules`.

        `import main` would be answered from the module cache if anything
        earlier in the session already imported it — which makes the obvious
        version of this test pass even when the path is wrong. `find_spec`
        re-runs the search, so it fails exactly when a later module's `import
        main` would get the wrong file.
        """
        import importlib.util

        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.find_spec("main")
        assert spec is not None and spec.origin
        assert Path(spec.origin).resolve() == repo_root / "main.py", (
            f"`import main` would resolve to {spec.origin}; something put "
            f"dashboard/backend ahead of the repo root on sys.path"
        )

    def test_the_cli_entry_points_are_reachable(self):
        import main as main_mod
        assert hasattr(main_mod, "run_sweep_cmd")
        assert hasattr(main_mod, "SweepTerminated")

    def test_the_repo_root_precedes_the_backend_on_sys_path(self):
        repo_root = str(Path(__file__).resolve().parents[1])
        backend = str(Path(repo_root) / "dashboard" / "backend")
        assert backend in sys.path
        assert sys.path.index(repo_root) < sys.path.index(backend)

    def test_services_still_resolves_from_the_backend(self):
        import services.sweeps as reimported
        assert reimported is S


class TestTheAllowlistPayload:
    def test_serves_the_reasons_not_just_the_keys(self):
        payload = S.allowlist_payload()
        assert {a["key"] for a in payload["allowed"]} == set(ALLOWED_OVERRIDES)
        assert {r["key"] for r in payload["rejected"]} == set(REJECTED_OVERRIDES)
        for entry in payload["rejected"]:
            assert len(entry["reason"]) > 40, entry["key"]

    def test_caps_match_the_validator(self):
        caps = S.allowlist_payload()["caps"]
        assert caps["max_symbols"] == S.MAX_SYMBOLS
        assert caps["max_cells"] == S.MAX_CELLS
        assert caps["min_holdout_days"] == S.MIN_HOLDOUT_DAYS


# ==========================================================================
# FC-096 Phase A PR-2 — the DTE reach caveat, derived per run
# ==========================================================================
class TestTheDteReachCaveatIsNotAFork:
    """The CONSTANT is pinned byte-for-byte; the EMISSION is derived twice.

    The CLI reads `SweepResult.effective_max_dte`, which the runner computed
    from the base config plus every arm. The dashboard has no Config, so it
    derives the reach from the persisted spec instead. Two routes to the same
    condition is a deliberate design decision (the plan's M6), and it is safe
    only while both sides agree about the WORDS and about the threshold — hence
    these.
    """

    def test_the_caveat_text_matches_report_py(self):
        assert T.DTE_REACH_BIAS == engine_report.DTE_REACH_BIAS

    def test_the_threshold_matches_report_py(self):
        assert T.DTE_REACH_BIAS_THRESHOLD == engine_report.DTE_REACH_BIAS_THRESHOLD

    def test_the_dte_override_keys_are_imported_not_restated(self):
        """A key on one side and not the other means one side derives a reach
        the other does not — the footer would appear on `/sims` and not in
        `sweep.md`, or the reverse. `overrides.py` is stdlib-only and copied
        flat into the dashboard image, so both sides can import the SAME object
        rather than agreeing to keep two tuples equal."""
        from src.backtesting.scenarios import overrides, runner

        assert S.DTE_OVERRIDE_KEYS is overrides.DTE_OVERRIDE_KEYS
        assert runner.DTE_OVERRIDE_KEYS is overrides.DTE_OVERRIDE_KEYS
        assert set(overrides.DTE_OVERRIDE_KEYS) <= set(ALLOWED_OVERRIDES)


class TestSpecMaxDte:
    def test_a_spec_with_no_dte_arms_sits_at_the_threshold(self):
        assert S.spec_max_dte({"scenarios": [
            {"name": "a", "overrides": {"strategy.min_put_premium": 0.3}}]}) == 7

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_either_leg_raises_it(self, key):
        assert S.spec_max_dte({"scenarios": [
            {"name": "a", "overrides": {key: 14}}]}) == 14

    def test_the_maximum_wins(self):
        assert S.spec_max_dte({"scenarios": [
            {"name": "a", "overrides": {"strategy.put_target_dte": 3}},
            {"name": "b", "overrides": {"strategy.call_target_dte": 21}},
        ]}) == 21

    def test_a_shorter_arm_never_lowers_it(self):
        """Below the threshold the answer is the threshold: the caveat asks
        "did this run reach PAST 7", and a DTE-3 arm did not."""
        assert S.spec_max_dte({"scenarios": [
            {"name": "a", "overrides": {"strategy.put_target_dte": 3}}]}) == 7

    @pytest.mark.parametrize("spec", [
        {}, {"scenarios": None}, {"scenarios": []}, {"scenarios": ["not-a-dict"]},
        {"scenarios": [{"name": "a"}]},
        {"scenarios": [{"name": "a", "overrides": None}]},
        {"scenarios": [{"name": "a", "overrides": "nope"}]},
        {"scenarios": [{"name": "a", "overrides": {"strategy.put_target_dte": "14"}}]},
        {"scenarios": [{"name": "a", "overrides": {"strategy.put_target_dte": True}}]},
    ])
    def test_a_malformed_or_absent_spec_degrades_to_the_threshold(self, spec):
        """A `submitted` run has no cells and a run from before this field
        existed has no DTE keys; neither may 500 the results page, and neither
        may invent a caveat."""
        assert S.spec_max_dte(spec) == 7


class TestTheFooterIsPerRun:
    def _shaped(self, overrides):
        return S.shape_results({
            "run_id": "r", "status": "done", "in_sample_only": True,
            "spec_json": json.dumps({
                "symbols": ["AAPL"],
                "scenarios": [{"name": "x", "overrides": overrides}],
            }),
        }, [])

    def test_a_dte_7_run_carries_the_ordinary_footer_only(self):
        shaped = self._shaped({"strategy.min_put_premium": 0.3})
        assert shaped["effective_max_dte"] == 7
        titles = [b["title"] for b in shaped["known_biases"]]
        assert T.DTE_REACH_BIAS[0] not in titles
        assert shaped["known_biases"] == [
            {"title": t, "detail": d} for t, d in T.SWEEP_BIASES]

    @pytest.mark.parametrize("key", [
        "strategy.put_target_dte", "strategy.call_target_dte"])
    def test_a_long_dte_run_gains_the_caveat_and_nothing_else(self, key):
        shaped = self._shaped({key: 14})
        assert shaped["effective_max_dte"] == 14
        title, detail = T.DTE_REACH_BIAS
        assert shaped["known_biases"][-1] == {"title": title, "detail": detail}
        assert len(shaped["known_biases"]) == len(T.SWEEP_BIASES) + 1

    def test_the_two_renderings_agree_on_a_long_run(self):
        """The whole point of deriving the condition twice: `/sims` and
        `sweep.md` must warn the same reader in the same words about the same
        run."""
        sweep = _sample_sweep()
        sweep.effective_max_dte = 14
        rendered = json.loads(engine_report.render_json(sweep))
        shaped = self._shaped({"strategy.put_target_dte": 14})
        assert shaped["known_biases"] == rendered["known_biases"]
        assert shaped["effective_max_dte"] == rendered["effective_max_dte"]


class TestTheEarningsGapsAreStoredAndServed:
    """REVIEW: the PR claimed `earnings_symbols_without_data` reached the
    dashboard. It did not — the runner computed it and only the CLI report ever
    read it, so `/sims` was silent about a gate that never gated.

    It is PERSISTED rather than re-derived on read, because it is a property of
    the earnings table as it stood when the run replayed: the same spec, run
    again after the table is refreshed, gives a different answer. A derivation
    would report today's coverage against yesterday's numbers.
    """

    def test_the_done_row_carries_the_list_as_json(self):
        sweep = _sample_sweep()
        sweep.earnings_symbols_without_data = ["AAPL", "ZZZ"]
        row = store.status_row(
            run_id="r", status=store.STATUS_DONE,
            submitted_at="2026-08-29T12:00:00+00:00", result=sweep)
        assert json.loads(row["earnings_symbols_without_data"]) == ["AAPL", "ZZZ"]

    def test_no_gaps_stores_NULL_not_an_empty_array(self):
        """"No gaps" and "this run predates the column" are both absences here,
        and neither is a claim. A stored `"[]"` would assert that the run
        checked and found nothing — which a pre-column run did not do."""
        sweep = _sample_sweep()
        assert sweep.earnings_symbols_without_data == []
        row = store.status_row(
            run_id="r", status=store.STATUS_DONE,
            submitted_at="2026-08-29T12:00:00+00:00", result=sweep)
        assert row["earnings_symbols_without_data"] is None

    def test_every_status_row_carries_the_column(self):
        """The submitted/running rows must have it too, or the column set
        diverges by writer and `TestTheSubmittedRowMatchesTheJobsRowShape`
        stops meaning anything."""
        row = store.status_row(
            run_id="r", status=store.STATUS_RUNNING,
            submitted_at="2026-08-29T12:00:00+00:00")
        assert "earnings_symbols_without_data" in row
        assert row["earnings_symbols_without_data"] is None

    def test_the_column_is_in_the_sweeps_schema_as_a_nullable_string(self):
        field = next(
            f for f in store._sweeps_schema()
            if f.name == "earnings_symbols_without_data")
        assert field.field_type == "STRING"
        # NULLABLE: every pre-existing row has no value, and the additive
        # reconcile can only add a nullable column to a live table.
        assert (field.mode or "NULLABLE").upper() == "NULLABLE"

    def test_shape_results_serves_it(self):
        shaped = S.shape_results({
            "run_id": "r", "status": "done",
            "earnings_symbols_without_data": json.dumps(["AAPL", "ZZZ"]),
        }, [])
        assert shaped["earnings_symbols_without_data"] == ["AAPL", "ZZZ"]

    @pytest.mark.parametrize("stored", [
        pytest.param(None, id="pre-column run"),
        pytest.param("", id="empty string"),
        pytest.param("not json", id="unparseable"),
        pytest.param('{"a": 1}', id="json but not a list"),
    ])
    def test_an_absent_or_malformed_value_renders_as_empty_never_500s(self, stored):
        """A page that cannot render a sweep from before the column existed is
        worse than one that renders it without a caveat it never recorded."""
        shaped = S.shape_results(
            {"run_id": "r", "status": "done",
             "earnings_symbols_without_data": stored}, [])
        assert shaped["earnings_symbols_without_data"] == []

    def test_it_survives_the_real_round_trip(self, shaped_and_engine):
        """Through `rows_from_sweep`/`status_row` and out of `shape_results`,
        rather than through a hand-written row that could agree with a wrong
        implementation."""
        _shaped, sweep, _rendered = shaped_and_engine
        sweep.earnings_symbols_without_data = ["NVDA"]
        row = store.status_row(
            run_id="rid", status=store.STATUS_DONE,
            submitted_at="2026-08-29T12:00:00+00:00", result=sweep)
        shaped = S.shape_results(row, [])
        assert shaped["earnings_symbols_without_data"] == ["NVDA"]
        # ...and the CLI JSON says the same thing about the same run.
        assert json.loads(engine_report.render_json(sweep))[
            "earnings_symbols_without_data"] == ["NVDA"]
        sweep.earnings_symbols_without_data = []


class TestTheAllowlistProseDoesNotCiteARetiredRefusal:
    """REVIEW: both allowlist docstrings taught the reader with
    `put_target_dte` / `universe_dte=8` — a refusal that no longer exists. A
    doc example that the code contradicts is worse than none: it is the first
    thing a reader checks, and finding it false costs them trust in the rest."""

    @pytest.mark.parametrize("obj", [
        S.allowlist_payload,
        pytest.param(None, id="router"),
    ])
    def test_no_docstring_cites_the_pre_pr2_dte_refusal(self, obj):
        if obj is None:
            import routers.v2 as v2_mod
            doc = v2_mod.sweeps_allowlist.__doc__ or ""
        else:
            doc = obj.__doc__ or ""
        assert "universe_dte=8" not in doc
        assert "put_target_dte" not in doc
        # ...and it still teaches with a REAL refusal, not a generic one.
        assert "min_open_interest" in doc
