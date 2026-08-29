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

    def __init__(self, *_a, prior=None, **_kw):
        self.statuses = []
        self.runs = []
        self.prior = prior
        self.enabled = True

    def write_status(self, row):
        self.statuses.append(row)
        return True

    def write_runs(self, rows):
        self.runs.extend(rows)
        return True

    def find_done_sweep(self, key):
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


class _Logger:
    """structlog-shaped no-op; `run_sweep_cmd` only ever calls `.info`."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass
