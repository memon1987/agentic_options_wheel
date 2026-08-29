"""The scenario store: row derivation, status sequence, dedup identity (FC-060 L3).

Four properties, each of which fails silently if broken — which is why they are
tested rather than reviewed:

1. **Every cell gets a row, and its state is the STORED state.** A sweep that
   dropped its errored cells would read as a complete run. A row whose
   `measured` flag disagreed with `runner.ScenarioResult.measured` would let an
   `insuf` cell be averaged into a ranking downstream.
2. **The status sequence is `running` -> `done`, or `running` -> `failed` via the
   `finally`.** A sweep that raised and wrote nothing would sit as `running`
   forever and block the next submission for an hour.
3. **`sweep_key` means "the same question", not "the same JSON".** Reordering
   symbols must not change it; changing ANY override, haircut, window bound or
   commit must.
4. **`--spec-env` refuses what it cannot run**, in milliseconds, before a
   container has spent three minutes starting.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import date

import pytest

from src.backtesting.scenarios import persist as store
from src.backtesting.scenarios.identity import (
    canonical_spec, scenario_arm_hash, sweep_key,
)
from src.backtesting.scenarios.runner import (
    BASE_SCENARIO_NAME, Scenario, ScenarioResult, SweepResult,
)
from src.backtesting.screen import ENGINE_VERSION


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def cell(scenario="base", symbol="AAPL", split="all", *, verdict="fit",
         annualized=0.12, days_fraction=0.60, error=None) -> ScenarioResult:
    return ScenarioResult(
        scenario=scenario, symbol=symbol, start=date(2025, 8, 1),
        end=date(2026, 7, 31), split=split, config_hash="cfg0123456789ab",
        scenario_hash="arm0123456789ab", verdict=verdict, demote=False,
        total_return=0.10, annualized_return=annualized,
        annualized_return_on_collateral=0.09, benchmark_return=0.08,
        excess_return=0.02, option_pnl=1200.0, stock_pnl_realized=300.0,
        stock_pnl_unrealized=-50.0, max_drawdown=-0.07, win_rate=0.85,
        assignment_rate=0.15, puts_sold=20, calls_sold=8, cycles_completed=3,
        cycles_open=1, decision_days=250, days_in_position_fraction=days_fraction,
        bid_fill_return=0.08, verdict_flips_on_fill=False, replay_seconds=1.4,
        error=error,
    )


@pytest.fixture
def sweep() -> SweepResult:
    rows = [
        cell("base", "AAPL"),
        cell("base", "NVDA", verdict="insufficient", annualized=None),
        cell("tighter", "AAPL", annualized=0.15),
        # low activity: measured-looking, but the wheel was barely deployed.
        cell("tighter", "NVDA", days_fraction=0.04),
        cell("broken", "AAPL", error="RuntimeError: boom"),
        cell("broken", "NVDA", error="RuntimeError: boom"),
    ]
    return SweepResult(
        rows=rows,
        scenarios=["base", "tighter", "broken"],
        symbols=["AAPL", "NVDA"],
        windows=[("all", date(2025, 8, 1), date(2026, 7, 31))],
        base_config_hash="basehash00000000",
        scenario_config_hashes={"base": "cfg0123456789ab"},
        scenario_hashes={"base": "arm0123456789ab"},
        scenario_overrides={"base": {}, "tighter": {"strategy.min_put_premium": 0.75},
                            "broken": {}},
        scenario_fill_haircuts={"base": None, "tighter": None, "broken": 1.0},
        materialise_seconds={"AAPL:all": 30.5, "NVDA:all": 28.0},
        replay_seconds={"base:AAPL:all": 1.4},
        wall_seconds=95.2, provider_fetches_total=7, bar_cache_hits=402,
        starting_cash=100_000.0, run_sensitivity=False,
    )


SPEC = {
    "symbols": ["AAPL", "NVDA"],
    "start": "2025-08-01",
    "end": "2026-07-31",
    "holdout_start": None,
    "starting_cash": 100_000.0,
    "run_sensitivity": False,
    "scenarios": [
        {"name": "tighter", "overrides": {"strategy.min_put_premium": 0.75},
         "fill_haircut": None},
    ],
}


# --------------------------------------------------------------------------
# (1) Row derivation
# --------------------------------------------------------------------------
class TestRowsFromSweep:
    def test_one_row_per_cell_including_errors(self, sweep):
        rows = store.rows_from_sweep(
            sweep, run_id="abc123", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION, git_commit="deadbeef")
        assert len(rows) == len(sweep.rows) == 6
        # The errored cells are the whole point: a sweep that dropped them would
        # report four measured cells out of four and read as a complete run.
        assert sum(1 for r in rows if r["error"]) == 2

    def test_partition_flags_come_from_the_dataclass_not_a_reimplementation(self, sweep):
        rows = {(r["scenario_name"], r["symbol"]): r for r in store.rows_from_sweep(
            sweep, run_id="abc123", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION)}

        assert rows[("base", "AAPL")]["measured"] is True
        assert rows[("base", "AAPL")]["insufficient"] is False
        assert rows[("base", "AAPL")]["low_activity"] is False

        # `insufficient` wins over `low_activity` — a window with no completed
        # cycle also has a tiny days-in-position fraction, and counting it twice
        # is how a summary row stops adding up.
        assert rows[("base", "NVDA")]["insufficient"] is True
        assert rows[("base", "NVDA")]["low_activity"] is False
        assert rows[("base", "NVDA")]["measured"] is False

        assert rows[("tighter", "NVDA")]["low_activity"] is True
        assert rows[("tighter", "NVDA")]["measured"] is False

        # An errored cell is none of the three.
        broken = rows[("broken", "AAPL")]
        assert not any(broken[k] for k in ("measured", "insufficient", "low_activity"))

    def test_exactly_one_state_per_cell(self, sweep):
        """The four states partition every cell. Pinned here as well as in the
        runner, because this is where they become stored data."""
        for row in store.rows_from_sweep(
                sweep, run_id="r", submitted_at="2026-08-29T12:00:00+00:00",
                engine_version=ENGINE_VERSION):
            states = [bool(row["error"]), row["insufficient"],
                      row["low_activity"], row["measured"]]
            assert sum(states) == 1, row

    def test_provenance_and_arm_metadata_are_on_every_row(self, sweep):
        rows = store.rows_from_sweep(
            sweep, run_id="abc123", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION, git_commit="deadbeef")
        tighter = next(r for r in rows if r["scenario_name"] == "tighter")
        assert tighter["run_id"] == "abc123"
        assert tighter["engine_version"] == ENGINE_VERSION
        assert tighter["git_commit"] == "deadbeef"
        # Overrides travel WITH the cell, so one row is self-describing without
        # a join back to the sweep row.
        assert json.loads(tighter["overrides_json"]) == {
            "strategy.min_put_premium": 0.75}
        assert next(r for r in rows if r["scenario_name"] == "broken")[
            "fill_haircut"] == 1.0
        assert tighter["window_start"] == "2025-08-01"

    def test_every_row_key_is_a_declared_schema_column(self, sweep):
        """`insert_rows_json` rejects the WHOLE request on one unknown key.

        So a typo in a row key does not lose one field — it writes ZERO rows,
        and the sweep reports a clean run with nothing in the table.
        """
        pytest.importorskip("google.cloud.bigquery")
        declared = {f.name for f in store._runs_schema()}
        for row in store.rows_from_sweep(
                sweep, run_id="r", submitted_at="2026-08-29T12:00:00+00:00",
                engine_version=ENGINE_VERSION):
            assert set(row) <= declared, set(row) - declared

    def test_status_row_keys_are_declared_columns(self):
        pytest.importorskip("google.cloud.bigquery")
        declared = {f.name for f in store._sweeps_schema()}
        row = store.status_row(
            run_id="r", status=store.STATUS_RUNNING,
            submitted_at="2026-08-29T12:00:00+00:00", spec=SPEC)
        assert set(row) <= declared, set(row) - declared

    def test_status_row_refuses_an_unknown_status(self):
        with pytest.raises(ValueError, match="unknown sweep status"):
            store.status_row(run_id="r", status="finished-ish",
                             submitted_at="2026-08-29T12:00:00+00:00")

    def test_terminal_row_flattens_the_result(self, sweep):
        row = store.status_row(
            run_id="r", status=store.STATUS_DONE,
            submitted_at="2026-08-29T12:00:00+00:00", spec=SPEC, result=sweep)
        assert row["cell_count"] == 6
        assert row["wall_seconds"] == 95.2
        assert row["materialise_seconds"] == pytest.approx(58.5)
        assert row["provider_fetches"] == 7
        assert row["bar_cache_hits"] == 402
        assert row["symbols"] == ["AAPL", "NVDA"]

    def test_base_config_snapshot_excludes_credentials(self):
        """The store is read by a PUBLIC dashboard. `alpaca:` never goes in."""
        class FakeConfig:
            _config = {
                "strategy": {"put_target_dte": 7},
                "risk": {"max_position_size": 0.35},
                "alpaca": {"api_key": "SECRET", "expected_account_number": "123"},
                "stocks": {"symbols": ["AAPL"]},
            }
        snap = store.base_config_snapshot(FakeConfig())
        assert "alpaca" not in snap
        assert snap["strategy"] == {"put_target_dte": 7}
        assert "SECRET" not in json.dumps(snap)


class TestTheSchemaGuards:
    def test_only_identity_columns_are_required(self):
        """Everything else must be NULLABLE.

        A REQUIRED column is a column every writer must know about, and the two
        writers here are in different images with different knowledge — the API
        has no Config and cannot fill `base_config_hash`, the Job has no
        `submitted` row to copy from. A REQUIRED field nobody can supply makes
        `insert_rows_json` reject the WHOLE request, so one row's gap costs the
        run all of its rows.
        """
        pytest.importorskip("google.cloud.bigquery")
        required = {f.name for f in store._sweeps_schema() if f.mode == "REQUIRED"}
        assert required == {"run_id", "status", "submitted_at", "written_at"}

        required_runs = {f.name for f in store._runs_schema() if f.mode == "REQUIRED"}
        assert required_runs == {"run_id", "submitted_at"}

    def test_a_retyped_column_raises_rather_than_being_ignored(self):
        """Additive reconcile cannot fix a type change, and silently leaving one
        means every insert fails on that field — the sweep writes ZERO rows and
        reports a clean run, which is exactly the failure the reconcile exists
        to prevent, arriving through the one door it did not watch."""
        bigquery = pytest.importorskip("google.cloud.bigquery")

        good = store._sweeps_schema()
        drifted = [
            bigquery.SchemaField("wall_seconds", "STRING")
            if f.name == "wall_seconds" else f
            for f in good
        ]

        class FakeClient:
            def create_table(self, table, exists_ok=False):
                existing = bigquery.Table(table.reference, schema=drifted)
                return existing

        writer = store.ScenarioRunWriter.__new__(store.ScenarioRunWriter)
        writer._client = FakeClient()
        writer._dataset_id = "options_wheel"
        ref = bigquery.DatasetReference("p", "options_wheel")
        with pytest.raises(RuntimeError, match="MIGRATION"):
            writer._ensure_table(ref, store.SWEEPS_TABLE, good,
                                 partition_field="submitted_at", clustering=None)

    def test_a_missing_column_is_added_not_refused(self):
        bigquery = pytest.importorskip("google.cloud.bigquery")

        good = store._sweeps_schema()
        trimmed = [f for f in good if f.name != "error_cells"]
        updated = {}

        class FakeClient:
            def create_table(self, table, exists_ok=False):
                return bigquery.Table(table.reference, schema=trimmed)

            def update_table(self, table, fields):
                updated["names"] = {f.name for f in table.schema}

        writer = store.ScenarioRunWriter.__new__(store.ScenarioRunWriter)
        writer._client = FakeClient()
        writer._dataset_id = "options_wheel"
        ref = bigquery.DatasetReference("p", "options_wheel")
        writer._ensure_table(ref, store.SWEEPS_TABLE, good,
                             partition_field="submitted_at", clustering=None)
        assert "error_cells" in updated["names"]


class TestNonFiniteValuesAreScrubbed:
    """NaN / inf serialise as the bare JSON tokens `NaN` / `Infinity`, which
    BigQuery rejects — and it rejects the WHOLE request, so one pathological
    cell costs the sweep every row it was going to write. NULL is also the
    honest value: an undefined ratio is not a number."""

    def test_nan_and_inf_become_null(self, sweep):
        sweep.rows[0].annualized_return = float("nan")
        sweep.rows[0].total_return = float("inf")
        sweep.rows[1].max_drawdown = float("-inf")
        rows = store.rows_from_sweep(
            sweep, run_id="r", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION)
        assert rows[0]["annualized_return"] is None
        assert rows[0]["total_return"] is None
        assert rows[1]["max_drawdown"] is None

    def test_ordinary_numbers_survive(self, sweep):
        rows = store.rows_from_sweep(
            sweep, run_id="r", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION)
        assert rows[0]["annualized_return"] == pytest.approx(0.12)
        assert rows[0]["win_rate"] == pytest.approx(0.85)

    def test_every_written_row_is_json_serialisable_by_bigquery(self, sweep):
        """`json.dumps(..., allow_nan=False)` is the same rule BigQuery applies."""
        sweep.rows[0].annualized_return = float("nan")
        rows = store.rows_from_sweep(
            sweep, run_id="r", submitted_at="2026-08-29T12:00:00+00:00",
            engine_version=ENGINE_VERSION)
        json.dumps(rows, allow_nan=False)


class TestTheLatestStatusOrderBy:
    def test_orders_by_written_at_not_submitted_at(self):
        """Every row of a submission shares `submitted_at` — it is the partition
        key. Ordering by it is a three-way tie, and "latest status wins" would
        then resolve arbitrarily: a finished sweep would render as still running
        roughly whenever BigQuery felt like it."""
        assert store.LATEST_STATUS_ORDER_BY.startswith("written_at DESC")
        assert "submitted_at" not in store.LATEST_STATUS_ORDER_BY

    def test_terminal_statuses_outrank_running_in_the_tiebreak(self):
        assert store.STATUS_RANK[store.STATUS_DONE] > store.STATUS_RANK[store.STATUS_RUNNING]
        assert store.STATUS_RANK[store.STATUS_FAILED] > store.STATUS_RANK[store.STATUS_RUNNING]
        assert store.STATUS_RANK[store.STATUS_RUNNING] > store.STATUS_RANK[store.STATUS_SUBMITTED]

    def test_done_outranks_failed(self):
        """The one realistic same-microsecond collision is the launch path: the
        API writes `failed` when `jobs.run` errors while an execution that in
        fact started writes `done`. A sweep whose cells are in the table is
        done, whatever a launch-side timeout thought — and ranking it below
        `failed` would additionally keep it out of the dedup, so it would be
        replayed."""
        assert store.STATUS_RANK[store.STATUS_DONE] > store.STATUS_RANK[store.STATUS_FAILED]


# --------------------------------------------------------------------------
# (3) Dedup identity
# --------------------------------------------------------------------------
class TestSweepKey:
    def key(self, spec, commit="abc123", engine=ENGINE_VERSION):
        return sweep_key(spec, engine_version=engine, git_commit=commit)

    def test_symbol_and_scenario_order_do_not_matter(self):
        a = dict(SPEC, symbols=["AAPL", "NVDA"], scenarios=[
            {"name": "x", "overrides": {"strategy.min_put_premium": 0.5}},
            {"name": "y", "overrides": {"strategy.min_call_premium": 0.2}}])
        b = dict(SPEC, symbols=["NVDA", "AAPL"], scenarios=[
            {"name": "y", "overrides": {"strategy.min_call_premium": 0.2}},
            {"name": "x", "overrides": {"strategy.min_put_premium": 0.5}}])
        assert self.key(a) == self.key(b)

    def test_override_key_order_inside_an_arm_does_not_matter(self):
        a = dict(SPEC, scenarios=[{"name": "x", "overrides": {
            "strategy.min_put_premium": 0.5, "strategy.min_call_premium": 0.2}}])
        b = dict(SPEC, scenarios=[{"name": "x", "overrides": {
            "strategy.min_call_premium": 0.2, "strategy.min_put_premium": 0.5}}])
        assert self.key(a) == self.key(b)

    def test_symbol_case_does_not_matter(self):
        assert self.key(dict(SPEC, symbols=["aapl", "nvda"])) == self.key(SPEC)

    def test_an_explicit_empty_base_arm_is_the_implicit_one(self):
        """The runner prepends `base` either way, so the two specs describe the
        identical run and must not key as two different experiments."""
        explicit = dict(SPEC, scenarios=[
            {"name": BASE_SCENARIO_NAME, "overrides": {}}] + SPEC["scenarios"])
        assert self.key(explicit) == self.key(SPEC)

    @pytest.mark.parametrize("mutate", [
        pytest.param(lambda s: dict(s, scenarios=[
            {"name": "tighter", "overrides": {"strategy.min_put_premium": 0.76}}]),
            id="override value"),
        pytest.param(lambda s: dict(s, scenarios=[
            {"name": "tighter", "overrides": {"strategy.min_call_premium": 0.75}}]),
            id="override key"),
        pytest.param(lambda s: dict(s, scenarios=[
            {"name": "renamed", "overrides": {"strategy.min_put_premium": 0.75}}]),
            id="arm name"),
        pytest.param(lambda s: dict(s, scenarios=[
            {"name": "tighter", "overrides": {"strategy.min_put_premium": 0.75},
             "fill_haircut": 1.0}]), id="fill haircut"),
        pytest.param(lambda s: dict(s, scenarios=[]), id="arm dropped"),
        pytest.param(lambda s: dict(s, start="2025-08-02"), id="window start"),
        pytest.param(lambda s: dict(s, end="2026-07-30"), id="window end"),
        pytest.param(lambda s: dict(s, holdout_start="2026-05-01"), id="holdout"),
        pytest.param(lambda s: dict(s, symbols=["AAPL"]), id="symbol dropped"),
        pytest.param(lambda s: dict(s, starting_cash=50_000.0), id="starting cash"),
        pytest.param(lambda s: dict(s, run_sensitivity=True), id="sensitivity"),
    ])
    def test_any_material_change_changes_the_key(self, mutate):
        """The mutation guard for D4. If the key ignored overrides — or any of
        these — a dedup hit would serve one experiment's numbers as another's,
        which is the one cache miss that is worse than no cache at all."""
        assert self.key(mutate(SPEC)) != self.key(SPEC)

    def test_an_integer_and_a_float_threshold_are_the_same_sweep(self):
        """A price ceiling typed as an integer in YAML and as a float by a JSON
        form is the same threshold — the engine coerces both before it compares
        anything. Without the fold the CLI and the dashboard never dedup against
        each other, which is precisely the pair D4 exists to connect."""
        as_int = dict(SPEC, scenarios=[
            {"name": "c", "overrides": {"strategy.max_stock_price": 1000}}])
        as_float = dict(SPEC, scenarios=[
            {"name": "c", "overrides": {"strategy.max_stock_price": 1000.0}}])
        assert self.key(as_int) == self.key(as_float)

    def test_a_band_of_ints_matches_a_band_of_floats(self):
        a = dict(SPEC, scenarios=[
            {"name": "b", "overrides": {"strategy.call_delta_range": [0, 1]}}])
        b = dict(SPEC, scenarios=[
            {"name": "b", "overrides": {"strategy.call_delta_range": [0.0, 1.0]}}])
        assert self.key(a) == self.key(b)

    def test_a_bool_does_not_fold_into_a_number(self):
        """`True` is not `1.0` here; folding it would make
        `earnings.enabled: true` collide with an integer 1 elsewhere."""
        as_bool = dict(SPEC, scenarios=[
            {"name": "e", "overrides": {"earnings.enabled": True}}])
        as_one = dict(SPEC, scenarios=[
            {"name": "e", "overrides": {"earnings.enabled": 1}}])
        assert self.key(as_bool) != self.key(as_one)

    def test_the_default_haircut_spelled_out_is_the_default_haircut(self):
        """The runner substitutes DEFAULT_FILL_HAIRCUT for None, so an arm that
        spells it out and one that omits it run identically."""
        from src.backtesting.evaluate import DEFAULT_FILL_HAIRCUT
        spelled = dict(SPEC, scenarios=[
            {"name": "d", "overrides": {}, "fill_haircut": DEFAULT_FILL_HAIRCUT}])
        omitted = dict(SPEC, scenarios=[{"name": "d", "overrides": {}}])
        assert self.key(spelled) == self.key(omitted)

    def test_the_identity_haircut_constant_matches_the_engine(self):
        from src.backtesting.evaluate import DEFAULT_FILL_HAIRCUT
        from src.backtesting.scenarios import identity
        assert identity.DEFAULT_FILL_HAIRCUT == DEFAULT_FILL_HAIRCUT

    def test_a_non_default_haircut_still_changes_the_key(self):
        other = dict(SPEC, scenarios=[
            {"name": "d", "overrides": {}, "fill_haircut": 1.0}])
        omitted = dict(SPEC, scenarios=[{"name": "d", "overrides": {}}])
        assert self.key(other) != self.key(omitted)

    def test_force_is_not_part_of_the_key(self):
        """A forced re-run must key identically to the run it deliberately
        reproduces, or the two are never comparable."""
        assert self.key(dict(SPEC, force=True)) == self.key(SPEC)

    def test_engine_version_and_commit_are_in_the_key(self):
        assert self.key(SPEC, engine="fc-999-other") != self.key(SPEC)
        assert self.key(SPEC, commit="feedface") != self.key(SPEC)

    def test_a_missing_commit_hashes_rather_than_raises(self):
        """A local sweep has no commit stamp; refusing to key it would mean
        refusing to persist it."""
        assert len(self.key(SPEC, commit=None)) == 16

    def test_arm_hash_is_the_runners_scenario_hash(self):
        """One definition of "the same arm", used by both sides."""
        arm = Scenario("x", {"strategy.min_put_premium": 0.5}, 0.4)
        assert arm.scenario_hash() == scenario_arm_hash(arm.overrides, arm.fill_haircut)

    def test_canonical_spec_carries_no_raw_overrides(self):
        """Only the arm hash, so the key cannot become order-sensitive again."""
        canon = canonical_spec(SPEC)
        assert set(canon["scenarios"][0]) == {"name", "hash"}


# --------------------------------------------------------------------------
# (2) + (4) The Job entry point: status sequence and --spec-env
# --------------------------------------------------------------------------
class FakeWriter:
    """Records what a real ScenarioRunWriter would have inserted."""

    def __init__(self, *_a, prior=None, enabled=True, runs_ok=True, **_kw):
        self.statuses = []
        self.runs = []
        self.prior = prior
        self.enabled = enabled
        self.runs_ok = runs_ok
        self.dedup_calls = []

    def write_status(self, row):
        self.statuses.append(row)
        return True

    def write_runs(self, rows):
        if not self.runs_ok:
            return False
        self.runs.extend(rows)
        return True

    def find_done_sweep(self, key, base_config_hash=None):
        self.dedup_calls.append((key, base_config_hash))
        return self.prior


@pytest.fixture
def cli_args():
    return Namespace(
        scenarios=None, spec_env="SWEEP_SPEC_JSON", persist=False,
        symbol=None, symbols=None, start=None, end=None, holdout_start=None,
        starting_cash=100_000.0, no_sensitivity=True, out=None, json_out=None,
    )


@pytest.fixture
def job_env(monkeypatch):
    spec = {
        "symbols": ["AAPL"],
        "start": "2025-08-01", "end": "2026-07-31",
        "scenarios": [{"name": "tighter",
                       "overrides": {"strategy.min_put_premium": 0.75}}],
    }
    monkeypatch.setenv("SWEEP_SPEC_JSON", json.dumps(spec))
    monkeypatch.setenv("SWEEP_RUN_ID", "run0123456789ab")
    monkeypatch.setenv("SWEEP_SUBMITTED_AT", "2026-08-29T12:00:00+00:00")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "backtest-sweep-abcde")
    monkeypatch.setenv("GIT_COMMIT", "cafebabe")
    return spec


@pytest.fixture
def wired(monkeypatch, sweep):
    """`main` with the writer and the runner replaced. Returns the fake writer."""
    import main as main_mod
    from src.backtesting.scenarios import persist as persist_mod
    import src.backtesting.scenarios as scenarios_pkg

    writer = FakeWriter()
    monkeypatch.setattr(persist_mod, "ScenarioRunWriter",
                        lambda *a, **k: writer)
    monkeypatch.setattr(scenarios_pkg, "run_sweep", lambda *a, **k: sweep)
    return main_mod, writer


def _config():
    from src.utils.config import Config
    return Config("config/settings.yaml")


class TestTheJobStatusSequence:
    def test_running_then_done_and_rows_before_the_terminal_row(
            self, wired, cli_args, job_env, capsys):
        main_mod, writer = wired
        rc = main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert rc == 1  # the fixture sweep has two errored cells
        assert [r["status"] for r in writer.statuses] == ["running", "done"]
        assert len(writer.runs) == 6
        # Provenance from the environment, not invented.
        assert writer.statuses[0]["run_id"] == "run0123456789ab"
        assert writer.statuses[0]["execution_name"] == "backtest-sweep-abcde"
        assert writer.statuses[0]["git_commit"] == "cafebabe"
        assert writer.statuses[0]["submitted_at"] == "2026-08-29T12:00:00+00:00"
        assert writer.statuses[-1]["error"] is None

    def test_a_raising_sweep_still_writes_failed(
            self, monkeypatch, wired, cli_args, job_env):
        main_mod, writer = wired
        import src.backtesting.scenarios as scenarios_pkg

        def boom(*_a, **_k):
            raise RuntimeError("materialisation exploded")
        monkeypatch.setattr(scenarios_pkg, "run_sweep", boom)

        with pytest.raises(RuntimeError):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        # Without the `finally` this row never lands and the sweep stays
        # `running` forever, blocking the next submission for an hour.
        assert [r["status"] for r in writer.statuses] == ["running", "failed"]
        assert "materialisation exploded" in writer.statuses[-1]["error"]
        assert writer.runs == []

    def test_a_prior_done_run_short_circuits_the_replay(
            self, monkeypatch, wired, cli_args, job_env):
        main_mod, writer = wired
        writer.prior = {"run_id": "earlierrun000001"}
        called = []
        import src.backtesting.scenarios as scenarios_pkg
        monkeypatch.setattr(scenarios_pkg, "run_sweep",
                            lambda *a, **k: called.append(1))

        rc = main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert rc == 0
        assert called == [], "a deduplicated sweep must not replay anything"
        assert [r["status"] for r in writer.statuses] == ["running", "deduplicated"]
        assert writer.statuses[-1]["deduplicated_to"] == "earlierrun000001"

    def test_the_cli_path_does_not_persist_without_persist(
            self, monkeypatch, wired, cli_args, tmp_path):
        """The Layer-2 behaviour contract: `--command sweep` with a file spec is
        unchanged, and unchanged includes writing nothing."""
        main_mod, writer = wired
        spec_file = tmp_path / "s.yaml"
        spec_file.write_text(
            "scenarios:\n  - name: tighter\n    overrides:\n"
            "      strategy.min_put_premium: 0.75\n")
        cli_args.spec_env = None
        cli_args.scenarios = str(spec_file)
        cli_args.symbols = "AAPL"
        cli_args.start, cli_args.end = "2025-08-01", "2026-07-31"

        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert writer.statuses == [] and writer.runs == []


class TestDoneMeansTheRowsAreThere:
    """`done` is not "the process exited" (review round 1).

    It used to be chosen from `failure is None` alone, so a `write_runs` that
    returned False still produced a `done` row — and the dedup would then serve
    that run as a cached answer whose grid is empty. `done` now means "the
    process finished AND its rows are in the table".
    """

    def test_a_failed_cell_write_produces_failed_not_done(
            self, monkeypatch, wired, cli_args, job_env):
        main_mod, writer = wired
        writer.runs_ok = False

        rc = main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert [r["status"] for r in writer.statuses] == ["running", "failed"]
        assert "cell rows not persisted" in writer.statuses[-1]["error"]
        # ...and the CLI says so rather than printing a "Stored as" line that
        # points at rows nobody can read.
        assert rc == 1

    def test_a_done_row_records_how_many_rows_landed(
            self, wired, cli_args, job_env):
        main_mod, writer = wired
        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        done = writer.statuses[-1]
        assert done["status"] == "done"
        assert done["rows_persisted"] == 6 == done["cell_count"]

    def test_a_done_row_counts_its_errored_cells(self, wired, cli_args, job_env):
        """The fixture sweep has two errored cells, so this run is `done` and
        must NOT be dedup-eligible. `error_cells` is what says so."""
        main_mod, writer = wired
        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert writer.statuses[-1]["error_cells"] == 2

    def test_a_failed_row_never_claims_rows_persisted(
            self, monkeypatch, wired, cli_args, job_env):
        main_mod, writer = wired
        import src.backtesting.scenarios as scenarios_pkg
        monkeypatch.setattr(scenarios_pkg, "run_sweep",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert writer.statuses[-1]["rows_persisted"] is None


class TestTerminationIsRecorded:
    """Cloud Run sends SIGTERM then SIGKILL 10 s later.

    Python's default SIGTERM handler exits immediately: no `finally`, no
    terminal row. The sweep would sit as `running` for ever, hold the
    one-at-a-time lock until the stale cutoff, and tell an operator nothing.
    Ten seconds is comfortably enough for two streaming inserts.
    """

    def test_the_handler_raises_so_the_finally_runs(
            self, monkeypatch, wired, cli_args, job_env):
        import signal

        main_mod, writer = wired
        import src.backtesting.scenarios as scenarios_pkg

        def terminated_mid_replay(*_a, **_k):
            # Exactly what Cloud Run does, from inside the replay.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        monkeypatch.setattr(scenarios_pkg, "run_sweep", terminated_mid_replay)
        with pytest.raises(main_mod.SweepTerminated):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())

        assert [r["status"] for r in writer.statuses] == ["running", "failed"]
        assert "SIGKILL" in writer.statuses[-1]["error"]

    def test_a_cancel_during_the_dedup_lookup_still_terminalises(
            self, monkeypatch, wired, cli_args, job_env):
        """SIGTERM in the first seconds, before a single day is replayed.

        The `running` insert, the dedup query (up to 60 s) and
        `ChainStore.from_env()`'s bucket probe all happen before the replay
        starts, and until round-2 fix 2 the handler was not installed until
        after them. A cancel in that window — the minutes when a cancel is most
        likely — killed the process outright and left exactly the orphaned
        `running` row the mechanism exists to prevent.
        """
        import os
        import signal

        main_mod, writer = wired

        def cancelled_during_lookup(key, base_config_hash=None):
            # A real signal, delivered to this process, not a hand-called
            # handler: that is what proves the handler is INSTALLED by now.
            os.kill(os.getpid(), signal.SIGTERM)
            return None

        writer.find_done_sweep = cancelled_during_lookup

        with pytest.raises(main_mod.SweepTerminated):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())

        assert [r["status"] for r in writer.statuses] == ["running", "failed"]
        assert "SIGKILL" in writer.statuses[-1]["error"]

    def test_a_cancel_before_the_running_row_still_terminalises(
            self, monkeypatch, wired, cli_args, job_env):
        """Earlier still: the signal arrives as the `running` row is written."""
        import os
        import signal

        main_mod, writer = wired
        real_write = writer.write_status

        def cancel_on_first_write(row):
            real_write(row)
            if row["status"] == "running":
                os.kill(os.getpid(), signal.SIGTERM)
            return True

        writer.write_status = cancel_on_first_write

        with pytest.raises(main_mod.SweepTerminated):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert [r["status"] for r in writer.statuses] == ["running", "failed"]

    def test_sigterm_during_the_finalise_does_not_kill_the_writes(
            self, monkeypatch, wired, cli_args, job_env):
        """`terminate_on_sigterm` restores the previous handler exactly when the
        finalise begins, so a signal landing there used to hit SIG_DFL and kill
        the process mid-insert — with no terminal row. Round-2 fix 3 ignores
        SIGTERM for the duration of the two writes, which finish well inside
        Cloud Run's 10-second grace.
        """
        import os
        import signal

        main_mod, writer = wired
        fired = {"n": 0}
        real_write_runs = writer.write_runs

        def cancel_mid_finalise(rows):
            out = real_write_runs(rows)
            fired["n"] += 1
            os.kill(os.getpid(), signal.SIGTERM)   # would be fatal under SIG_DFL
            return out

        writer.write_runs = cancel_mid_finalise
        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())

        assert fired["n"] == 1
        # The process survived the signal AND the terminal row landed.
        assert [r["status"] for r in writer.statuses] == ["running", "done"]
        assert len(writer.runs) == 6

    def test_the_finalise_restores_the_handler_afterwards(self):
        import signal

        import main as main_mod
        before = signal.getsignal(signal.SIGTERM)
        with main_mod.ignore_sigterm_while_finalising(_Logger()):
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is before

    def test_a_sweep_terminated_is_not_an_ordinary_exception(self):
        """`run_sweep` catches `Exception` per scenario so one bad arm does not
        cost the others their results. A termination caught THERE would be
        recorded as "this arm errored" while the container died around it."""
        import main as main_mod
        assert not issubclass(main_mod.SweepTerminated, Exception)
        assert issubclass(main_mod.SweepTerminated, BaseException)

    def test_the_previous_handler_is_restored(self):
        import signal

        import main as main_mod
        before = signal.getsignal(signal.SIGTERM)
        with main_mod.terminate_on_sigterm(_Logger()):
            assert signal.getsignal(signal.SIGTERM) is not before
        assert signal.getsignal(signal.SIGTERM) is before

    def test_the_status_row_is_attempted_even_when_the_lake_summary_raises(
            self, monkeypatch, wired, cli_args, job_env):
        """Every step of the `finally` can itself raise, and a raise inside a
        `finally` skips whatever it had not reached. The one thing that must
        survive is the terminal row."""
        main_mod, writer = wired
        from src.backtesting.data.chain_store import ChainStore

        def boom(self):
            raise RuntimeError("GCS exploded")

        monkeypatch.setattr(ChainStore, "summary", boom)
        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert writer.statuses[-1]["status"] == "done"
        assert writer.statuses[-1]["lake_summary_json"] is None


class TestTheJobRefusesToReplayWithNowhereToStore:
    def test_spec_env_mode_exits_non_zero_before_replaying(
            self, monkeypatch, cli_args, job_env, sweep):
        """An execution launched from the dashboard exists solely to put rows in
        the store. Eight minutes of 1-vCPU compute whose output goes to a log
        nobody reads is worse than an immediate, loud failure — and the
        submitter would sit on `submitted` until the lock expired with no
        explanation anywhere."""
        import main as main_mod
        from src.backtesting.scenarios import persist as persist_mod
        import src.backtesting.scenarios as scenarios_pkg

        dead = FakeWriter(enabled=False)
        monkeypatch.setattr(persist_mod, "ScenarioRunWriter", lambda *a, **k: dead)
        replayed = []
        monkeypatch.setattr(scenarios_pkg, "run_sweep",
                            lambda *a, **k: replayed.append(1) or sweep)

        rc = main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert rc == 2
        assert replayed == [], "nothing may be replayed with nowhere to store it"
        assert dead.statuses == []

    def test_the_cli_still_degrades_to_report_only(
            self, monkeypatch, cli_args, tmp_path, sweep):
        """`--persist` from a terminal is a human watching the report come out;
        losing the store there costs a warning, not the run."""
        import main as main_mod
        from src.backtesting.scenarios import persist as persist_mod
        import src.backtesting.scenarios as scenarios_pkg

        dead = FakeWriter(enabled=False)
        monkeypatch.setattr(persist_mod, "ScenarioRunWriter", lambda *a, **k: dead)
        monkeypatch.setattr(scenarios_pkg, "run_sweep", lambda *a, **k: sweep)

        spec_file = tmp_path / "s.yaml"
        spec_file.write_text("scenarios:\n  - name: t\n")
        cli_args.spec_env = None
        cli_args.scenarios = str(spec_file)
        cli_args.persist = True
        cli_args.symbols = "AAPL"
        cli_args.start, cli_args.end = "2025-08-01", "2026-07-31"

        rc = main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert rc == 1   # warned, not crashed


class TestForceSkipsTheDedup:
    def test_force_true_never_queries_for_a_prior_run(
            self, monkeypatch, wired, cli_args, job_env):
        main_mod, writer = wired
        writer.prior = {"run_id": "earlierrun000001"}
        monkeypatch.setenv("SWEEP_SPEC_JSON", json.dumps(dict(job_env, force=True)))

        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert writer.dedup_calls == []
        assert [r["status"] for r in writer.statuses] == ["running", "done"]

    def test_without_force_the_lookup_carries_the_effective_config_hash(
            self, wired, cli_args, job_env):
        main_mod, writer = wired
        main_mod.run_sweep_cmd(cli_args, _config(), _Logger())
        assert len(writer.dedup_calls) == 1
        key, cfg_hash = writer.dedup_calls[0]
        assert cfg_hash and len(cfg_hash) == 16
        # ...and it is the hash of the EFFECTIVE snapshot, not config_hash.
        from src.backtesting.scenarios import persist as store_mod
        assert cfg_hash == store_mod.base_config_hash(
            store_mod.base_config_snapshot(_config()))


class TestTheEffectiveConfigSnapshot:
    """The yaml is not the configuration.

    ``EARNINGS_ENABLED`` / ``ROLLER_ENABLED`` / ``ROLLER_DRY_RUN`` win over their
    keys at runtime (FC-013 DD-7, FC-078 DD-7), so a snapshot read from
    ``_config`` alone records a gate as ON that the run had OFF — and, since the
    dedup reads ``base_config_hash``, two sweeps either side of a kill switch
    would have been served as one another's answer.
    """

    def test_env_shadowed_switches_are_reflected(self, monkeypatch):
        from src.backtesting.scenarios import persist as store_mod

        monkeypatch.setenv("EARNINGS_ENABLED", "false")
        monkeypatch.setenv("ROLLER_ENABLED", "false")
        off = store_mod.base_config_snapshot(_config())
        assert off["effective"]["earnings.enabled"] is False
        assert off["effective"]["rolling.enabled"] is False

        monkeypatch.setenv("EARNINGS_ENABLED", "true")
        monkeypatch.setenv("ROLLER_ENABLED", "true")
        on = store_mod.base_config_snapshot(_config())
        assert on["effective"]["earnings.enabled"] is True
        assert store_mod.base_config_hash(on) != store_mod.base_config_hash(off)

    def test_the_raw_sections_are_still_carried(self):
        """A hash proves two runs matched and says nothing about what they
        matched on."""
        from src.backtesting.scenarios import persist as store_mod
        snap = store_mod.base_config_snapshot(_config())
        assert "strategy" in snap and "effective" in snap

    def test_every_allowlisted_key_has_an_effective_reading(self):
        from src.backtesting.scenarios import persist as store_mod
        from src.backtesting.scenarios.overrides import ALLOWED_OVERRIDES
        snap = store_mod.base_config_snapshot(_config())
        missing = set(ALLOWED_OVERRIDES) - set(snap["effective"])
        assert missing == set(), missing

    def test_credentials_never_reach_the_snapshot(self):
        from src.backtesting.scenarios import persist as store_mod
        blob = json.dumps(store_mod.base_config_snapshot(_config()), default=str)
        assert "alpaca" not in blob.lower()


class TestSpecEnvParsing:
    def test_missing_variable_fails_fast(self, monkeypatch):
        import main as main_mod
        monkeypatch.delenv("NOPE", raising=False)
        with pytest.raises(SystemExit, match="unset or empty"):
            main_mod.load_spec_from_env("NOPE")

    def test_invalid_json_fails_fast(self, monkeypatch):
        import main as main_mod
        monkeypatch.setenv("S", "{not json")
        with pytest.raises(SystemExit, match="not valid JSON"):
            main_mod.load_spec_from_env("S")

    def test_a_json_array_is_refused(self, monkeypatch):
        import main as main_mod
        monkeypatch.setenv("S", "[1,2,3]")
        with pytest.raises(SystemExit, match="expected a JSON object"):
            main_mod.load_spec_from_env("S")

    def test_an_oversized_spec_is_refused(self, monkeypatch):
        import main as main_mod
        monkeypatch.setenv("S", json.dumps({"symbols": ["A" * 30_000]}))
        with pytest.raises(SystemExit, match="over the"):
            main_mod.load_spec_from_env("S")

    def test_a_misspelled_field_is_refused_not_ignored(self, monkeypatch):
        """`holdout` instead of `holdout_start` would run IN-SAMPLE and then
        report itself as validated. That is the expensive typo."""
        import main as main_mod
        monkeypatch.setenv("S", json.dumps({"symbols": ["AAPL"], "holdout": "x"}))
        with pytest.raises(SystemExit, match="unknown field"):
            main_mod.load_spec_from_env("S")

    def test_a_disallowed_override_is_refused_with_the_runners_reason(
            self, wired, cli_args, monkeypatch):
        main_mod, _writer = wired
        from src.backtesting.scenarios.overrides import OverrideError

        monkeypatch.setenv("SWEEP_SPEC_JSON", json.dumps({
            "symbols": ["AAPL"], "start": "2025-08-01", "end": "2026-07-31",
            "scenarios": [{"name": "x",
                           "overrides": {"strategy.put_target_dte": 14}}]}))
        with pytest.raises(OverrideError, match="universe_dte"):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())

    def test_both_sources_at_once_is_refused(self, cli_args, job_env):
        import main as main_mod
        cli_args.scenarios = "some.yaml"
        with pytest.raises(SystemExit, match="not both"):
            main_mod.run_sweep_cmd(cli_args, _config(), _Logger())

    def test_a_bad_date_names_the_field(self, monkeypatch):
        import main as main_mod
        with pytest.raises(SystemExit, match="'end' must be YYYY-MM-DD"):
            main_mod._spec_date({"end": "31/07/2026"}, "end")


class TestTheReportTellsTheTruthAboutPersistence:
    """`render_markdown` asserted "this report is the only record of the run"
    unconditionally, and `render_json` hardcoded `"persisted": false`. Both
    became false statements the moment `--persist` existed — in the two places a
    reader trusts most."""

    def persistence(self, persisted=True):
        import main as main_mod
        return main_mod.SweepPersistence(
            persisted=persisted, run_id="run0123456789ab",
            sweep_key="key0123456789ab", dataset="options_wheel")

    def test_a_stored_run_says_where_it_is(self, sweep):
        from src.backtesting.scenarios.report import render_markdown
        text = render_markdown(sweep, self.persistence())
        assert "Results are persisted" in text
        assert "options_wheel.scenario_sweeps" in text
        assert "run0123456789ab" in text
        assert "this report is the only record" not in text

    def test_an_unstored_run_still_says_so(self, sweep):
        from src.backtesting.scenarios.report import render_markdown
        text = render_markdown(sweep)
        assert "Results are not persisted" in text
        assert "this report is the only record of it" in text

    def test_both_forms_keep_the_backtest_runs_prohibition(self, sweep):
        from src.backtesting.scenarios.report import render_markdown
        for persistence in (None, self.persistence()):
            assert "backtest_runs" in render_markdown(sweep, persistence)

    def test_the_json_carries_the_run_id_when_stored(self, sweep):
        from src.backtesting.scenarios.report import render_json
        payload = json.loads(render_json(sweep, self.persistence()))
        assert payload["persisted"] is True
        assert payload["run_id"] == "run0123456789ab"
        assert payload["sweep_key"] == "key0123456789ab"
        assert payload["dataset"] == "options_wheel"

    def test_the_json_defaults_to_not_persisted(self, sweep):
        from src.backtesting.scenarios.report import render_json
        payload = json.loads(render_json(sweep))
        assert payload["persisted"] is False
        assert payload["run_id"] is None

    def test_a_run_whose_rows_did_not_land_is_not_reported_as_stored(self, sweep):
        from src.backtesting.scenarios.report import render_markdown
        text = render_markdown(sweep, self.persistence(persisted=False))
        assert "Results are not persisted" in text


class _Logger:
    """structlog-shaped no-op; `run_sweep_cmd` only ever calls `.info`."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass
