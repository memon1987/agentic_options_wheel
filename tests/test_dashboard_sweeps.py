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

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard" / "backend"))

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

    def test_an_hour_old_running_row_stops_blocking(self):
        """A Job killed before its `finally` leaves a `running` row nothing will
        ever terminalise. A permanent lock on a dead run takes the feature
        offline with no way back except a manual BigQuery insert."""
        stale = self.row("running", minutes_ago=61)
        assert S.blocking_sweep([stale], now=self.now()) is None
        fresh = self.row("running", minutes_ago=59)
        assert S.blocking_sweep([fresh], now=self.now()) is not None

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

    def test_a_running_row_is_never_stuck(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        row = {"status": "running",
               "submitted_at": (now - timedelta(hours=3)).isoformat()}
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
