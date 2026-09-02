"""The interactive sim service — FC-096 Phase B PR-c.

What is asserted here, and why each one exists:

* the **budget** refuses a spec too big for an interactive request, at the
  boundary, with the numbers in the message;
* ``run_sensitivity`` is REFUSED rather than silently doubled;
* the **coverage pre-flight** names the missing symbol-days, tolerates weekends
  and holidays without a hardcoded calendar, and refuses an empty lake rather
  than declaring it complete;
* the **fetch guard** turns a lake miss into a loud failure instead of a vendor
  round-trip, and lets stock bars through;
* the **202 flow** writes `submitted` + `running` before it returns and a
  terminal row when the worker finishes;
* **SIGTERM** terminalises the in-flight run once, on the main thread, while the
  replay is still on the worker;
* the **thread contract** the tally actually has: entered on the worker thread it
  counts; entered on one thread and emitted on another it does not.

No GCP: every test injects a fake writer, a fake lake and a fake vendor, and the
artifact bucket is switched off, so nothing here constructs a GCS or BigQuery
client. ``tests/conftest.py`` blocks the ambient paths as well.
"""

import importlib.util
import json
import logging
import signal
import threading
import uuid
from datetime import date, datetime, timedelta

import pytest

import deploy.sim_service as sim
from src.backtesting.data.fetch_guard import (
    ChainFetchRefusingProvider, ColdChainFetchRefused, REFUSAL_EVENT,
)
from src.backtesting.data.lake_coverage import (
    MIN_SESSION_RATIO, check_coverage, weekdays_between,
)
from src.backtesting.scenarios import persist as store
from src.backtesting.scenarios.runner import ScenarioResult, SweepResult

# The endpoint-shaped tests import FastAPI (through `_simulate`'s JSONResponse),
# which is present in the dashboard image but NOT in the bot image where this
# suite runs. Skip is CLASS-scoped for the reason tests/test_dashboard_sweeps.py
# gives: a module-level importorskip aborts collection of the whole file and
# silently skips every pure test above it too, turning CI green while testing
# nothing.
_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


# ==========================================================================
# Fakes
# ==========================================================================
class FakeWriter:
    """A ``ScenarioRunWriter`` stand-in that records instead of inserting."""

    def __init__(self, *, prior=None, runs_ok=True, enabled=True):
        self.enabled = enabled
        self.prior = prior
        self.runs_ok = runs_ok
        self.status_rows = []
        self.run_rows = []
        self.dedup_calls = []

    def find_done_sweep(self, sweep_key, base_config_hash=None,
                        engine_identity=None):
        self.dedup_calls.append((sweep_key, base_config_hash, engine_identity))
        return self.prior

    def write_status(self, row):
        self.status_rows.append(row)
        return True

    def write_runs(self, rows):
        self.run_rows.extend(rows)
        return self.runs_ok

    def statuses(self):
        return [r["status"] for r in self.status_rows]

    def terminal(self):
        rows = [r for r in self.status_rows
                if r["status"] in store.TERMINAL_STATUSES]
        return rows[-1] if rows else None


class FakeLake:
    """``ChainLake.list_days`` only. Records what it was asked for."""

    def __init__(self, days_by_symbol):
        self._days = {k.upper(): set(v) for k, v in days_by_symbol.items()}
        self.asked = []

    def list_days(self, underlying):
        self.asked.append(underlying.upper())
        return set(self._days.get(underlying.upper(), set()))


class FakeStore:
    """``ChainStore`` stand-in: a lake and a summary, nothing else."""

    def __init__(self, lake=None):
        self.lake = lake

    def summary(self):
        return {}


class RecordingVendor:
    """Counts every provider call so a refusal can be proven to be a refusal."""

    def __init__(self, bars=None):
        self.calls = []
        self._bars = bars or []

    def get_contract_universe(self, *args, **kwargs):
        self.calls.append("get_contract_universe")
        return []

    def get_option_bars(self, *args, **kwargs):
        self.calls.append("get_option_bars")
        return {}

    def get_stock_bars(self, *args, **kwargs):
        self.calls.append("get_stock_bars")
        return list(self._bars)


def sessions(*days):
    return {date.fromisoformat(d) for d in days}


def spec(**over):
    base = {
        "symbols": ["AAPL"],
        "start": "2025-09-02",
        "end": "2025-10-31",
        "scenarios": [],
    }
    base.update(over)
    return base


def sweep_result(*, error=None, rows=1):
    result = SweepResult(
        scenarios=["base"], symbols=["AAPL"],
        windows=[("all", date(2025, 9, 2), date(2025, 10, 31))],
        base_config_hash="cfg", starting_cash=100_000.0)
    for i in range(rows):
        result.rows.append(ScenarioResult(
            scenario="base", symbol="AAPL", start=date(2025, 9, 2),
            end=date(2025, 10, 31), split="all", config_hash="cfg",
            scenario_hash="arm", verdict=None if error else "marginal",
            error=error, total_return=None if error else 0.01,
            replay_seconds=0.3))
    result.wall_seconds = 1.0
    return result


@pytest.fixture(autouse=True)
def _clean_service(monkeypatch):
    """A fresh process for every test, and no artifact bucket.

    ``SIM_ARTIFACT_BUCKET=""`` is the explicit off switch, so no test here can
    construct a GCS client even by accident; a separate assertion pins that the
    202 path notices and says so.
    """
    monkeypatch.setenv("SIM_ARTIFACT_BUCKET", "")
    sim.reset_for_tests()
    yield
    sim.reset_for_tests()


@pytest.fixture
def wired(monkeypatch):
    """A service wired to fakes: real Config, fake writer, covered lake."""
    from src.utils.config import Config

    monkeypatch.setattr(sim, "_CONFIG", Config())
    writer = FakeWriter()
    monkeypatch.setattr(sim, "_WRITER", writer)
    monkeypatch.setattr(sim, "check_coverage", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.backtesting.data.chain_store.ChainStore.from_env",
        classmethod(lambda cls, *a, **k: FakeStore()))
    return writer


# ==========================================================================
# (1) The budget
# ==========================================================================
class TestTheInteractiveBudget:
    """The estimate is the gate, and the cell count is only a backstop.

    Materialisation dominates every realistic spec (25-50 s per symbol-window
    against ~0.3 s of replay per symbol-year), so a cell count cannot tell a
    twenty-arm two-symbol request from a two-arm twelve-symbol one — and the
    second is the slow one.
    """

    def test_the_estimate_is_materialise_plus_replay(self):
        estimate = sim.estimate_seconds(sim.normalise_spec(spec())["spec"])
        assert estimate["materialise_seconds"] == sim.MATERIALISE_SECONDS_PER_SYMBOL
        assert estimate["replay_seconds"] == sim.REPLAY_SECONDS_PER_CELL
        assert estimate["total_seconds"] == (
            sim.MATERIALISE_SECONDS_PER_SYMBOL + sim.REPLAY_SECONDS_PER_CELL)

    def test_a_holdout_doubles_the_materialisation(self):
        one = sim.estimate_seconds(sim.normalise_spec(spec())["spec"])
        two = sim.estimate_seconds(sim.normalise_spec(
            spec(holdout_start="2025-10-01"))["spec"])
        assert two["materialise_seconds"] == 2 * one["materialise_seconds"]

    def _symbols(self, n):
        return [f"SYM{chr(65 + i)}" for i in range(n)]

    def test_just_under_the_budget_is_accepted(self):
        # symbols x 40s + cells x 2s, with one arm and one split:
        #   n * 40 + n * 2 = 42n  <= 600  =>  n <= 14
        payload = sim.normalise_spec(spec(symbols=self._symbols(14)))["spec"]
        estimate = sim.check_budget(payload)
        assert estimate["total_seconds"] <= sim.SIM_MAX_ESTIMATE_SECONDS

    def test_one_symbol_over_the_budget_is_refused_with_the_numbers(self):
        payload = sim.normalise_spec(spec(symbols=self._symbols(15)))["spec"]
        with pytest.raises(sim.SpecRefused) as exc:
            sim.check_budget(payload)
        assert exc.value.status == 409
        assert "use the batch path" in exc.value.detail.lower()
        # The NUMBERS, not just a verdict: an operator has to be able to see
        # which half of the estimate is the expensive one.
        assert f"{15 * sim.MATERIALISE_SECONDS_PER_SYMBOL:.0f}s materialising" \
            in exc.value.detail
        assert exc.value.extra["total_seconds"] > sim.SIM_MAX_ESTIMATE_SECONDS

    def test_the_boundary_constants_are_the_plans(self):
        """A silent constant change is a silent policy change.

        ``MATERIALISE_SECONDS_PER_SYMBOL`` is the one number rollout step 3 is
        required to replace with a measurement; it must stay inside the range
        the plan measured until it does.
        """
        assert sim.SIM_MAX_ESTIMATE_SECONDS == 600.0
        assert 25.0 <= sim.MATERIALISE_SECONDS_PER_SYMBOL <= 50.0
        assert sim.SIM_MAX_CELLS == 240
        assert sim.LIVENESS_SECONDS == 900

    def test_the_cell_cap_is_a_real_secondary_bound(self):
        """Many cheap arms over few symbols: cheap by the estimate, still capped."""
        arms = [{"name": f"arm{i}"} for i in range(250)]
        payload = sim.normalise_spec(spec(scenarios=arms))["spec"]
        assert sim.cell_count(payload) == 251
        assert sim.estimate_seconds(payload)["total_seconds"] <= (
            sim.SIM_MAX_ESTIMATE_SECONDS), (
            "this spec must be CHEAP by the estimate, or it is not testing "
            "the secondary bound")
        with pytest.raises(sim.SpecRefused) as exc:
            sim.check_budget(payload)
        assert exc.value.status == 409
        assert "cells exceeds" in exc.value.detail


# ==========================================================================
# (2) Spec validation
# ==========================================================================
class TestTheSpecIsTheJobs:
    """One parser. A spec this accepts is a spec the Job would accept."""

    def test_the_sensitivity_pass_is_refused_not_doubled(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(run_sensitivity=True))
        assert exc.value.status == 422
        assert "run_sensitivity" in exc.value.detail
        assert "backtest-sweep" in exc.value.detail

    def test_an_unknown_field_is_refused_with_the_known_set(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(holdout="2025-10-01"))
        assert exc.value.status == 422
        assert "holdout" in exc.value.detail

    def test_a_bad_override_carries_the_engines_own_reason(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(scenarios=[
                {"name": "nope", "overrides": {"universe.min_open_interest": 500}}]))
        assert exc.value.status == 422
        assert "nope" in exc.value.detail

    def test_base_is_reserved(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(scenarios=[{"name": "base"}]))
        assert exc.value.status == 422

    def test_a_duplicate_symbol_is_refused(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(symbols=["AAPL", "AAPL"]))
        assert exc.value.status == 422
        assert "twice" in exc.value.detail

    def test_an_unaddressable_symbol_is_refused_before_anything_is_written(self):
        """The artifact object name is `<scenario>__<symbol>__<split>`."""
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(symbols=["A__B"]))
        assert exc.value.status == 422

    def test_an_end_before_the_start_is_refused(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(start="2025-10-31", end="2025-09-02"))
        assert exc.value.status == 422

    def test_a_holdout_outside_the_window_is_refused(self):
        with pytest.raises(sim.SpecRefused) as exc:
            sim.normalise_spec(spec(holdout_start="2026-01-01"))
        assert exc.value.status == 422

    def test_the_normalised_spec_keys_like_the_dashboards(self):
        """The whole point of reusing the Job's parser.

        Two entry points that normalised differently would compute different
        `sweep_key`s for the same question and would never dedup against each
        other — a silent doubling of every cost this service exists to avoid.
        """
        from src.backtesting.scenarios.identity import sweep_key

        mine = sim.normalise_spec(spec(scenarios=[
            {"name": "tighter", "overrides": {"strategy.put_delta_range": [0.1, 0.2]}}
        ]))["spec"]
        # What the dashboard's `validate_spec` produces for the same input,
        # written out by hand so this test fails if either side moves.
        theirs = {
            "symbols": ["AAPL"],
            "start": "2025-09-02",
            "end": "2025-10-31",
            "holdout_start": None,
            "starting_cash": 100000.0,
            "run_sensitivity": False,
            "scenarios": [{"name": "tighter",
                           "overrides": {"strategy.put_delta_range": [0.1, 0.2]},
                           "fill_haircut": None}],
        }
        assert mine == theirs
        assert (sweep_key(mine, engine_version="v", engine_identity="i")
                == sweep_key(theirs, engine_version="v", engine_identity="i"))

    def test_an_absent_starting_cash_uses_the_shared_default(self):
        from src.backtesting.scenarios.identity import DEFAULT_STARTING_CASH

        payload = sim.normalise_spec(spec())["spec"]
        assert payload["starting_cash"] == DEFAULT_STARTING_CASH


# ==========================================================================
# (3) The coverage pre-flight
# ==========================================================================
class TestTheCoverageGuard:
    """Half (a) of the no-vendor contract: refuse before accepting.

    The session calendar is DERIVED from the lake — the union of the spec's
    symbols' stored days — rather than from a hardcoded holiday table, which
    would be wrong the first time the market closes for a funeral and would then
    refuse every spec spanning that day for ever.
    """

    WEEK = ("2025-09-02", "2025-09-03", "2025-09-04", "2025-09-05",
            "2025-09-08", "2025-09-09", "2025-09-10", "2025-09-11",
            "2025-09-12")

    def test_a_fully_covered_window_is_complete(self):
        lake = FakeLake({"AAPL": sessions(*self.WEEK),
                         "NVDA": sessions(*self.WEEK)})
        report = check_coverage(lake, ["AAPL", "NVDA"],
                               date(2025, 9, 2), date(2025, 9, 12))
        assert report.complete
        assert report.missing == {}
        assert lake.asked == ["AAPL", "NVDA"]

    def test_a_hole_in_the_middle_names_the_symbol_and_the_day(self):
        hole = [d for d in self.WEEK if d != "2025-09-08"]
        lake = FakeLake({"AAPL": sessions(*hole),
                         "NVDA": sessions(*self.WEEK)})
        report = check_coverage(lake, ["AAPL", "NVDA"],
                               date(2025, 9, 2), date(2025, 9, 12))
        assert not report.complete
        assert report.missing == {"AAPL": [date(2025, 9, 8)]}
        detail = report.describe()
        assert "AAPL: 2025-09-08" in detail
        assert "data-backfill" in detail, (
            "a 409 that names the gap and not the remedy makes the operator go "
            "and look up the command"
        )

    def test_weekends_and_holidays_are_not_demanded(self):
        """No symbol has them, so the derived calendar does not contain them.

        The window below spans two weekends AND Labor Day (2025-09-01), and the
        lake holds nothing for any of the three. A hardcoded weekday calendar
        would report every symbol as missing three days.
        """
        lake = FakeLake({"AAPL": sessions(*self.WEEK),
                         "NVDA": sessions(*self.WEEK)})
        report = check_coverage(lake, ["AAPL", "NVDA"],
                               date(2025, 9, 1), date(2025, 9, 14))
        assert report.complete
        assert date(2025, 9, 1) not in {d for days in report.missing.values()
                                        for d in days}

    def test_an_empty_lake_is_never_complete(self):
        """The guard's one catastrophic failure mode, closed.

        With nothing stored the union is empty and every symbol is trivially
        "missing nothing" — which would wave through exactly the spec that is
        guaranteed to hit the fetch guard mid-replay.
        """
        lake = FakeLake({})
        report = check_coverage(lake, ["AAPL"], date(2025, 9, 2),
                               date(2025, 9, 12))
        assert not report.complete
        assert report.thin_window
        assert "has not been backfilled" in report.describe()

    def test_a_barely_seeded_window_is_refused_by_the_ratio(self):
        lake = FakeLake({"AAPL": sessions("2025-09-02", "2025-09-03")})
        report = check_coverage(lake, ["AAPL"], date(2025, 9, 2),
                               date(2025, 9, 12))
        assert not report.complete and report.thin_window
        assert report.sessions == 2
        assert report.sessions < MIN_SESSION_RATIO * report.weekdays

    def test_no_lake_configured_is_incomplete_not_complete(self):
        report = check_coverage(None, ["AAPL"], date(2025, 9, 2),
                                date(2025, 9, 12))
        assert not report.complete and report.thin_window

    def test_a_lake_failure_propagates_rather_than_reading_as_absence(self):
        """"We could not tell" must never render as "there is nothing there"."""
        class Angry:
            def list_days(self, symbol):
                raise PermissionError("403")

        with pytest.raises(PermissionError):
            check_coverage(Angry(), ["AAPL"], date(2025, 9, 2), date(2025, 9, 12))

    def test_the_reported_day_list_is_bounded(self):
        many = {date(2025, 9, 2) + timedelta(days=i) for i in range(60)}
        lake = FakeLake({"AAPL": set(), "NVDA": many})
        report = check_coverage(lake, ["AAPL", "NVDA"], date(2025, 9, 2),
                               date(2025, 10, 31))
        assert not report.complete
        assert "more)" in report.describe()

    def test_weekdays_between_counts_only_mon_to_fri(self):
        assert weekdays_between(date(2025, 9, 1), date(2025, 9, 7)) == 5
        assert weekdays_between(date(2025, 9, 7), date(2025, 9, 1)) == 0

    def test_list_days_parses_the_lake_layout_and_skips_strays(self):
        """One `list_blobs` per symbol; an unparseable object is not a claim."""
        from src.backtesting.data.chain_store import ChainLake

        class Blob:
            def __init__(self, name):
                self.name = name

        lake = ChainLake("bucket", "chains/v1")
        listed = []

        class Client:
            def bucket(self, name):
                return name

            def list_blobs(self, bucket, prefix=None, **kwargs):
                listed.append(prefix)
                return [
                    Blob("chains/v1/AAPL/2025-09-02.parquet"),
                    Blob("chains/v1/AAPL/2025-09-03.parquet"),
                    Blob("chains/v1/AAPL/README.txt"),
                    Blob("chains/v1/AAPL/not-a-date.parquet"),
                    Blob("chains/v1/AAPL/nested/2025-09-04.parquet"),
                ]

        lake._client = Client()
        lake._probed = True
        assert lake.list_days("aapl") == sessions("2025-09-02", "2025-09-03")
        assert listed == ["chains/v1/AAPL/"]


# ==========================================================================
# (4) The fetch guard
# ==========================================================================
class TestTheFetchGuard:
    """Half (b): the contract is ENFORCED, not assumed.

    A pre-flight is a prediction. This is what happens when the prediction is
    wrong — and it is a loud failure rather than minutes of silent vendor
    latency on a request the operator was told would be interactive.
    """

    def test_a_chain_universe_fetch_raises_and_never_reaches_the_vendor(self):
        vendor = RecordingVendor()
        guard = ChainFetchRefusingProvider(vendor, run_id="r1")
        with pytest.raises(ColdChainFetchRefused) as exc:
            guard.get_contract_universe("AAPL", date(2025, 9, 8), 22)
        assert vendor.calls == []
        assert "AAPL" in str(exc.value)
        assert "data-backfill" in str(exc.value)
        assert guard.refusals == 1

    def test_an_option_bar_fetch_is_refused_too(self):
        vendor = RecordingVendor()
        guard = ChainFetchRefusingProvider(vendor)
        with pytest.raises(ColdChainFetchRefused):
            guard.get_option_bars(["AAPL251003C00230000"], date(2025, 9, 8),
                                  date(2025, 9, 8))
        assert vendor.calls == []

    def test_stock_bars_are_PERMITTED(self):
        """Deliberate, and stated in the module rather than left to omission.

        Daily stock bars are one small cached request per symbol-window and are
        rate-benign next to a chain (a contract-universe listing plus a bar
        request per contract, per session). Refusing them would leave the
        service unable to derive its own decision calendar and would buy
        nothing.
        """
        vendor = RecordingVendor(bars=["bar"])
        guard = ChainFetchRefusingProvider(vendor)
        assert guard.get_stock_bars("AAPL", date(2025, 9, 2),
                                    date(2025, 9, 8)) == ["bar"]
        assert vendor.calls == ["get_stock_bars"]

    def test_everything_else_falls_through(self):
        class Vendor:
            marker = "kept"

        guard = ChainFetchRefusingProvider(Vendor())
        assert guard.marker == "kept"

    def test_a_lake_miss_mid_window_refuses_instead_of_fetching(self):
        """The hole-in-the-middle case, through the REAL ChainBuilder.

        The store has no object for this day, so `build` falls through to
        `_build_uncached` — which is the exact path a coverage gap takes at
        replay time. With the guard in place it raises; the vendor sees nothing.
        """
        from src.backtesting.data.chain_builder import ChainBuilder

        vendor = RecordingVendor()
        guard = ChainFetchRefusingProvider(vendor, run_id="r2")
        builder = ChainBuilder(guard, store=None)
        with pytest.raises(ColdChainFetchRefused):
            builder.build("AAPL", date(2025, 9, 8), 7, underlying_price=230.0)
        assert vendor.calls == [], (
            "the guard let a chain fetch through — the service can reach the "
            "options vendor, which is the whole thing this prevents"
        )

    def test_the_refusal_event_name_is_the_one_monitoring_greps_for(self):
        assert REFUSAL_EVENT == "sim_service_cold_fetch_refused"

    def test_run_sweep_puts_the_guard_innermost(self, monkeypatch):
        """Between the vendor and the counter, so a refusal is not a "fetch".

        And above nothing: the bar cache cannot be used to route around it.
        """
        from src.backtesting.scenarios import runner

        seen = {}

        def fake_guard(provider):
            seen["wrapped"] = provider
            return ChainFetchRefusingProvider(provider)

        vendor = RecordingVendor()
        # Raise at the first step AFTER the provider chain is built, so the
        # wiring is what the test observes and nothing is replayed.
        monkeypatch.setattr(runner, "effective_max_dte",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("stop after wiring")))
        with pytest.raises(RuntimeError):
            runner.run_sweep(object(), [], ["AAPL"], date(2025, 9, 2),
                             date(2025, 9, 30), bar_provider=vendor,
                             chain_store=object(), vendor_guard=fake_guard)
        assert seen["wrapped"] is vendor


# ==========================================================================
# (5) The 202 flow
# ==========================================================================
@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestTheAcceptedFlow:
    """Accept fast, persist a handle, replay on the worker, terminalise."""

    def _run(self, monkeypatch, result=None, block=None):
        def fake_run_sweep(*args, **kwargs):
            if block is not None:
                block.wait(timeout=5)
            return result if result is not None else sweep_result()

        monkeypatch.setattr("src.backtesting.scenarios.run_sweep",
                            fake_run_sweep)
        return sim._simulate(spec())

    def test_it_returns_202_with_a_run_id_before_the_replay_finishes(
            self, wired, monkeypatch):
        gate = threading.Event()
        response = self._run(monkeypatch, block=gate)
        try:
            assert response.status_code == 202
            body = json.loads(response.body)
            assert body["run_id"] and body["deduplicated"] is False
            assert body["liveness_seconds"] == sim.LIVENESS_SECONDS
            # The rows are already there, and the replay is not finished.
            assert wired.statuses() == [store.STATUS_SUBMITTED,
                                        store.STATUS_RUNNING]
            assert wired.terminal() is None
        finally:
            gate.set()
            sim._WORKER.join(timeout=5)

    def test_every_row_carries_the_service_marks(self, wired, monkeypatch):
        self._run(monkeypatch)
        sim._WORKER.join(timeout=5)
        assert wired.status_rows, "no rows were written at all"
        for row in wired.status_rows:
            assert row["submitted_via"] == "sim-service"
            assert row["liveness_seconds"] == sim.LIVENESS_SECONDS, (
                "a row without the liveness stamp falls back to the JOB's 3h "
                "clock, and a killed instance would hold the submit lock for "
                "3h10m instead of ~25 minutes"
            )
            assert row["engine_identity"]

    def test_the_worker_writes_the_cells_then_a_done_row(self, wired,
                                                         monkeypatch):
        self._run(monkeypatch)
        sim._WORKER.join(timeout=5)
        assert len(wired.run_rows) == 1
        terminal = wired.terminal()
        assert terminal["status"] == store.STATUS_DONE
        assert terminal["rows_persisted"] == 1
        assert terminal["error_cells"] == 0

    def test_a_replay_that_raises_becomes_a_failed_row_not_a_lost_thread(
            self, wired, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("materialise exploded")

        monkeypatch.setattr("src.backtesting.scenarios.run_sweep", boom)
        sim._simulate(spec())
        sim._WORKER.join(timeout=5)
        terminal = wired.terminal()
        assert terminal["status"] == store.STATUS_FAILED
        assert "materialise exploded" in terminal["error"]

    def test_a_second_request_while_busy_is_409_never_a_queue(
            self, wired, monkeypatch):
        gate = threading.Event()
        first = self._run(monkeypatch, block=gate)
        try:
            with pytest.raises(sim.SpecRefused) as exc:
                sim._simulate(spec(symbols=["NVDA"]))
            assert exc.value.status == 409
            assert json.loads(first.body)["run_id"] in exc.value.detail
        finally:
            gate.set()
            sim._WORKER.join(timeout=5)

    def test_the_slot_is_released_when_the_replay_ends(self, wired,
                                                       monkeypatch):
        self._run(monkeypatch)
        sim._WORKER.join(timeout=5)
        assert not sim._RUN_LOCK.locked()
        assert sim._CURRENT is None
        # ...and a second submission is accepted rather than 409'd for ever.
        second = self._run(monkeypatch)
        sim._WORKER.join(timeout=5)
        assert second.status_code == 202

    def test_a_disabled_store_is_a_503_before_anything_is_written(
            self, monkeypatch):
        from src.utils.config import Config

        monkeypatch.setattr(sim, "_CONFIG", Config())
        writer = FakeWriter(enabled=False)
        monkeypatch.setattr(sim, "_WRITER", writer)
        monkeypatch.setattr(sim, "check_coverage", lambda *a, **k: None)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, *a, **k: FakeStore()))
        with pytest.raises(sim.SpecRefused) as exc:
            sim._simulate(spec())
        assert exc.value.status == 503
        assert writer.status_rows == []

    def test_the_coverage_guard_refuses_before_any_row_is_written(
            self, monkeypatch):
        from src.utils.config import Config

        monkeypatch.setattr(sim, "_CONFIG", Config())
        writer = FakeWriter()
        monkeypatch.setattr(sim, "_WRITER", writer)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, *a, **k: FakeStore(FakeLake({}))))
        with pytest.raises(sim.SpecRefused) as exc:
            sim._simulate(spec())
        assert exc.value.status == 409
        assert writer.status_rows == []
        assert not sim._RUN_LOCK.locked(), (
            "a refused spec must not leave the instance's one slot taken"
        )


# ==========================================================================
# (6) Dedup
# ==========================================================================
@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestTheDedupHit:
    def test_it_returns_ids_only_and_writes_nothing(self, monkeypatch):
        from src.utils.config import Config

        monkeypatch.setattr(sim, "_CONFIG", Config())
        writer = FakeWriter(prior={"run_id": "prior123", "cell_count": 1})
        monkeypatch.setattr(sim, "_WRITER", writer)
        monkeypatch.setattr(sim, "check_coverage", lambda *a, **k: None)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, *a, **k: FakeStore()))
        monkeypatch.setattr(
            "src.backtesting.scenarios.run_sweep",
            lambda *a, **k: pytest.fail("a dedup hit must not replay"))

        response = sim._simulate(spec())
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {"run_id": "prior123", "deduplicated": True,
                        "sweep_key": body["sweep_key"]}
        # NO ROWS. The dashboard shapes the prior run; the engine image does not
        # carry `shape_results` and must not start.
        assert writer.status_rows == []
        assert set(body) == {"run_id", "deduplicated", "sweep_key"}

    def test_force_skips_the_lookup_entirely(self, wired, monkeypatch):
        wired.prior = {"run_id": "prior123"}
        monkeypatch.setattr("src.backtesting.scenarios.run_sweep",
                            lambda *a, **k: sweep_result())
        response = sim._simulate(spec(force=True))
        sim._WORKER.join(timeout=5)
        assert response.status_code == 202
        assert wired.dedup_calls == []

    def test_the_lookup_binds_the_effective_config_hash(self, wired,
                                                        monkeypatch):
        wired.prior = None
        monkeypatch.setattr("src.backtesting.scenarios.run_sweep",
                            lambda *a, **k: sweep_result())
        sim._simulate(spec())
        sim._WORKER.join(timeout=5)
        (key, base_hash, identity), = wired.dedup_calls
        assert key and base_hash and identity, (
            "the service holds a real Config, so unlike the dashboard it MUST "
            "bind base_config_hash — that predicate is what catches a kill "
            "switch flipped between two otherwise identical submissions"
        )


# ==========================================================================
# (7) SIGTERM
# ==========================================================================
@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestSigterm:
    """A scaled-in instance must not leave a `running` row nobody terminalises.

    The Job turns SIGTERM into an exception so its `finally` runs. A service
    cannot: the signal lands on the main thread and the replay is on the worker,
    and Python has no supported way to raise into another thread. So the handler
    does the `finally`'s job itself.
    """

    def test_it_terminalises_the_in_flight_run_as_failed(self, wired,
                                                         monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(
            "src.backtesting.scenarios.run_sweep",
            lambda *a, **k: (gate.wait(timeout=5), sweep_result())[1])
        sim._simulate(spec())
        try:
            sim._on_sigterm(signal.SIGTERM, None)
            terminal = wired.terminal()
            assert terminal is not None, (
                "SIGTERM left no terminal row — the run reads as `running` "
                "until its liveness bound expires and holds the submit lock"
            )
            assert terminal["status"] == store.STATUS_FAILED
            assert "SIGTERM" in terminal["error"]
        finally:
            gate.set()
            sim._WORKER.join(timeout=5)

    def test_the_worker_does_not_write_a_second_terminal_row(self, wired,
                                                             monkeypatch):
        """Insert-only table: a `failed` after a `done` would MASK the done.

        Readers take the latest row per run_id by `written_at`, so the race
        between the handler and the worker's `finally` has to be resolved once,
        not tolerated twice.
        """
        gate = threading.Event()
        monkeypatch.setattr(
            "src.backtesting.scenarios.run_sweep",
            lambda *a, **k: (gate.wait(timeout=5), sweep_result())[1])
        sim._simulate(spec())
        sim._on_sigterm(signal.SIGTERM, None)
        gate.set()
        sim._WORKER.join(timeout=5)
        terminals = [r for r in wired.status_rows
                     if r["status"] in store.TERMINAL_STATUSES]
        assert len(terminals) == 1
        assert terminals[0]["status"] == store.STATUS_FAILED

    def test_with_nothing_in_flight_it_is_a_no_op(self, wired):
        sim._on_sigterm(signal.SIGTERM, None)
        assert wired.status_rows == []

    def test_it_chains_to_the_handler_it_replaced(self, monkeypatch):
        """uvicorn's graceful shutdown must survive this being installed."""
        called = []
        previous = signal.signal(signal.SIGTERM,
                                 lambda *a: called.append("previous"))
        try:
            assert sim.install_sigterm_handler() is True
            assert signal.getsignal(signal.SIGTERM) is sim._on_sigterm
            sim._on_sigterm(signal.SIGTERM, None)
            assert called == ["previous"]
        finally:
            sim.restore_sigterm_handler()
            signal.signal(signal.SIGTERM, previous)

    def test_installing_off_the_main_thread_is_a_no_op_not_a_crash(self):
        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(sim.install_sigterm_handler()))
        thread.start()
        thread.join(timeout=5)
        assert outcome == [False]


# ==========================================================================
# (8) The thread contract of the rejection tally  (debt owed by PR-c)
# ==========================================================================
class TestTheTallysThreadContract:
    """PR-a's logging review deferred these to PR-c, where a thread exists.

    The FC-092 fix binds the active tally in a ``contextvars.ContextVar``. That
    has a threading consequence nobody had executed: a thread starts with a
    FRESH context, so a tally entered on one thread is invisible to another.

    For this service that is exactly right and is the reason the design works:
    ``Simulator.replay`` enters the tally on whatever thread runs it, which is
    the worker thread, so the binding and the events share a context. It would
    be exactly wrong if the tally were entered on the request thread and the
    replay ran elsewhere — which is why both directions are pinned here rather
    than only the one that passes.
    """

    @staticmethod
    def _cli_structlog():
        from contextlib import contextmanager

        import structlog

        @contextmanager
        def _cfg():
            previous = structlog.get_config()
            try:
                structlog.configure(
                    processors=[
                        structlog.contextvars.merge_contextvars,
                        structlog.stdlib.filter_by_level,
                        structlog.stdlib.add_log_level,
                        structlog.processors.JSONRenderer(),
                    ],
                    logger_factory=structlog.stdlib.LoggerFactory(),
                    wrapper_class=structlog.stdlib.BoundLogger,
                    cache_logger_on_first_use=False,
                )
                yield
            finally:
                structlog.configure(**previous)

        return _cfg()

    @staticmethod
    def _emit(name):
        import structlog

        structlog.get_logger(name).info(
            "blocked", event_type="stage_7_complete_not_found")

    def test_a_tally_entered_on_the_worker_thread_counts(self):
        """The service's actual shape, executed."""
        import structlog  # noqa: F401 - the config context needs it imported

        from src.backtesting.engine.rejections import RejectionTally
        from src.utils import clock

        name = f"src.fc096c_probe_{uuid.uuid4().hex}"
        probe = logging.getLogger(name)
        probe.setLevel(logging.WARNING)
        summaries = {}

        def worker():
            # The clock seam is thread-local, so the freeze has to happen on
            # THIS thread — the same property being asserted about the tally.
            clock.set_now(datetime(2025, 9, 8, 16, 0))
            try:
                tally = RejectionTally()
                with tally:
                    self._emit(name)
                summaries["worker"] = tally.summary()
            finally:
                clock.set_now(None)

        try:
            with self._cli_structlog():
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=5)
        finally:
            probe.setLevel(logging.NOTSET)
            logging.Logger.manager.loggerDict.pop(name, None)

        assert summaries["worker"] == {
            "no put cleared delta/DTE/premium (stage 7)": 1
        }, (
            "a tally entered on the worker thread counted nothing — the sim "
            "service's every run would report zero blocked days, which every "
            "consumer reads as 'the strategy was never blocked' (the FC-057 "
            "class, arrived at by silence)"
        )

    def test_a_tally_entered_on_one_thread_does_not_see_another_thread(self):
        """The detachment, asserted rather than assumed.

        If this ever starts failing, the tally has stopped being context-local
        and two concurrent replays would silently merge their counts — which is
        precisely why `--concurrency=1` and `_RUN_LOCK` exist.
        """
        from src.backtesting.engine.rejections import RejectionTally
        from src.utils import clock

        name = f"src.fc096c_probe_{uuid.uuid4().hex}"
        probe = logging.getLogger(name)
        probe.setLevel(logging.WARNING)

        def emit_elsewhere():
            clock.set_now(datetime(2025, 9, 8, 16, 0))
            try:
                self._emit(name)
            finally:
                clock.set_now(None)

        clock.set_now(datetime(2025, 9, 8, 16, 0))
        try:
            with self._cli_structlog():
                tally = RejectionTally()
                with tally:
                    other = threading.Thread(target=emit_elsewhere)
                    other.start()
                    other.join(timeout=5)
                summary = tally.summary()
        finally:
            clock.set_now(None)
            probe.setLevel(logging.NOTSET)
            logging.Logger.manager.loggerDict.pop(name, None)

        assert summary == {}, (
            "an event emitted on ANOTHER thread reached a tally bound on this "
            "one. `_ACTIVE_TALLY` is a ContextVar precisely so that cannot "
            "happen; if it can, two replays could merge their counts."
        )


# ==========================================================================
# (9) Quiet-log exemption (plan L14)
# ==========================================================================
class TestTheServiceLoggerIsNotSilenced:
    """`_QUIET_LOGGERS` is ("src", "deploy") and this service lives under it.

    The replay is the only stretch of a sim request during which anything
    interesting happens, and it is the only log an operator has while waiting on
    a 202. Silencing the service's own logger for exactly that stretch would
    leave the request looking like nothing happened.
    """

    def test_an_exempt_logger_stays_at_info_through_the_quieting(self):
        from src.backtesting.scenarios.runner import quiet_strategy_logs

        service = logging.getLogger("deploy.sim_service")
        quieted = logging.getLogger("src")
        before = service.level
        try:
            with quiet_strategy_logs(True, exempt=("deploy.sim_service",)):
                assert quieted.level == logging.WARNING
                assert service.level == logging.INFO
            assert service.level == before
        finally:
            service.setLevel(before)

    def test_without_the_exemption_it_is_silenced(self):
        """The mutation this test exists to catch, spelled out."""
        from src.backtesting.scenarios.runner import quiet_strategy_logs

        service = logging.getLogger("deploy.sim_service")
        before = service.level
        try:
            with quiet_strategy_logs(True):
                assert logging.getLogger("deploy").level == logging.WARNING
        finally:
            service.setLevel(before)

    def test_the_service_asks_for_its_own_exemption(self, monkeypatch, wired):
        seen = {}

        def fake_run_sweep(*args, **kwargs):
            seen.update(kwargs)
            return sweep_result()

        monkeypatch.setattr("src.backtesting.scenarios.run_sweep",
                            fake_run_sweep)
        pytest.importorskip("fastapi")
        sim._simulate(spec())
        sim._WORKER.join(timeout=5)
        assert seen["quiet_exempt"] == (sim.__name__,)
        assert seen["run_sensitivity"] is False
        assert callable(seen["vendor_guard"])
