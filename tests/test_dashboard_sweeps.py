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

    def test_call_dte_upward_is_refused_downward_is_allowed(self):
        """The one value-dependent rule, and the API honours it because it
        calls the runner's validator rather than checking key membership."""
        with pytest.raises(S.SweepValidationError, match="not in the cached files"):
            S.validate_spec(spec(scenarios=[
                {"name": "x", "overrides": {"strategy.call_target_dte": 14}}]))
        S.validate_spec(spec(scenarios=[
            {"name": "x", "overrides": {"strategy.call_target_dte": 5}}]))


def _plausible_value(key):
    if key.endswith("_range"):
        return [0.10, 0.20]
    if key == "strategy.call_target_dte":
        return 5
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
                                    {"strategy.put_target_dte": 14}}]})
    raise SystemExit("a rejected override was accepted")
except S.SweepValidationError as exc:
    assert "universe_dte" in str(exc), str(exc)
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

    def __init__(self, *, prior=None, history=None, raises=None,
                 config_hash="cfg0123456789ab"):
        self.rows = []
        self.prior = prior
        self.history = history or []
        self.raises = raises
        self.config_hash = config_hash

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def latest_base_config_hash(self, git_commit):
        self._maybe_raise()
        return self.config_hash

    def find_done_sweep(self, key, base_config_hash=None):
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

    def test_a_prior_done_run_returns_200_and_launches_nothing(
            self, wired, monkeypatch):
        v2, _bq = wired
        cached = FakeBQ(prior={"run_id": "earlierrun000001"})
        monkeypatch.setattr(v2, "get_bigquery_service", lambda: cached)
        launched = []
        monkeypatch.setattr(v2, "_launch_job",
                            lambda body: launched.append(body))

        out = self.submit(v2)
        assert out["status"] == "deduplicated"
        assert out["deduplicated_to"] == "earlierrun000001"
        assert out["launched"] is False
        assert launched == []

    def test_force_skips_the_dedup_lookup(self, wired, monkeypatch):
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

    def test_a_422_writes_nothing(self, wired):
        from fastapi import HTTPException

        v2, bq = wired
        with pytest.raises(HTTPException) as exc:
            self.submit(v2, body=spec(scenarios=[
                {"name": "x", "overrides": {"strategy.put_target_dte": 14}}]))
        assert exc.value.status_code == 422
        assert "universe_dte" in str(exc.value.detail)
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
