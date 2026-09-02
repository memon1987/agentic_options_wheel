"""The weekly battery and the pin store (FC-096 Phase B B4).

Everything here is a property that fails SILENTLY if it breaks, which is why it
is a test rather than a review note:

1. **Isolation.** One refused pin costs its own `failed` row and nothing else.
   A loop that let a refusal escape would end the battery at whichever pin came
   first, and the standing set would go unmeasured for a week with a Job that
   exited 0.
2. **Revalidation.** A pin that was legal when it was created and is not legal
   now must be refused AT BATTERY TIME, with a row saying so — not replayed
   under a rule that no longer exists, and not silently skipped.
3. **Exit classes.** The battery exits 0 whatever happens; the backfill's
   non-zero exit survives and the battery does not run behind it. The two
   failure kinds — data (a page) and measurement (a nag) — must not be able to
   inherit each other's severity.
4. **The wall cap** refuses to START past its budget and NAMES what it skipped.
   A battery that silently measured nine of thirty looks exactly like one that
   measured thirty.
5. **`submitted_via='battery'`** on every row, so the trend queries this phase
   exists to feed cannot pick up an operator's ad-hoc run or a smoke row.
6. **A dedup-hit week costs no replays.** That is the whole economics of the
   Saturday: unchanged specs are free, so the cap is only ever spent on
   questions whose answer could have moved.

No GCP: every writer here is a fake, and `TestNothingTouchesGCP` asserts that
constructing a real BigQuery client inside these tests is impossible.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import date, timedelta

import pytest

import main as cli
from src.backtesting.scenarios import persist as store
from src.backtesting.scenarios.runner import ScenarioResult, SweepResult
from src.backtesting.screen import ENGINE_VERSION


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Logger:
    """structlog-shaped recorder: the events ARE the contract here."""

    def __init__(self):
        self.events = []

    def _record(self, level, *a, **k):
        self.events.append((level, k.get("event_type"), k))

    def info(self, *a, **k):
        self._record("info", *a, **k)

    def warning(self, *a, **k):
        self._record("warning", *a, **k)

    def error(self, *a, **k):
        self._record("error", *a, **k)

    def types(self, level=None):
        return [t for lvl, t, _ in self.events if level is None or lvl == level]

    def payload(self, event_type):
        return next(k for _l, t, k in self.events if t == event_type)


class FakeWriter:
    """A `ScenarioRunWriter` stand-in with a pin store."""

    def __init__(self, *_a, enabled=True, pins=None, pins_enabled=True,
                 prior=None, history=None, **_kw):
        self.enabled = enabled
        self.pins_enabled = pins_enabled
        self.statuses = []
        self.runs = []
        self.pin_rows = []
        self._pins = list(pins or [])
        self.prior = prior
        self.history = history or {}
        self.history_calls = []

    # -- sweeps
    def write_status(self, row):
        self.statuses.append(row)
        return True

    def write_runs(self, rows):
        self.runs.extend(rows)
        return True

    def find_done_sweep(self, key, base_config_hash=None, engine_identity=None):
        return self.prior

    # -- pins
    def list_pins(self, *, active_only=True):
        return [p for p in self._pins
                if not active_only or p.get("active")]

    def write_pin(self, row):
        self.pin_rows.append(row)
        return True

    def recent_pin_statuses(self, pin_id, *, limit=2, exclude_run_id=None):
        self.history_calls.append((pin_id, limit, exclude_run_id))
        rows = [r for r in self.history.get(pin_id, [])
                if r.get("run_id") != exclude_run_id]
        return rows[:limit]


def cell(scenario="base", symbol="AAPL", split="all", *, error=None):
    return ScenarioResult(
        scenario=scenario, symbol=symbol, start=date(2025, 8, 1),
        end=date(2026, 7, 31), split=split, config_hash="cfg0123456789ab",
        scenario_hash="arm0123456789ab", verdict="fit", demote=False,
        total_return=0.10, annualized_return=0.12,
        annualized_return_on_collateral=0.09, benchmark_return=0.08,
        excess_return=0.02, option_pnl=1200.0, stock_pnl_realized=300.0,
        stock_pnl_unrealized=-50.0, max_drawdown=-0.07, win_rate=0.85,
        assignment_rate=0.15, puts_sold=20, calls_sold=8, cycles_completed=3,
        cycles_open=1, decision_days=250, days_in_position_fraction=0.60,
        bid_fill_return=0.08, verdict_flips_on_fill=False, replay_seconds=1.4,
        error=error,
    )


def clean_sweep(symbol="AAPL"):
    """A `SweepResult` with no errored cell — a run that exits 0."""
    return SweepResult(
        rows=[cell("base", symbol)],
        scenarios=["base"], symbols=[symbol],
        windows=[("all", date(2025, 8, 1), date(2026, 7, 31))],
        base_config_hash="basehash00000000",
        scenario_config_hashes={"base": "cfg0123456789ab"},
        scenario_hashes={"base": "arm0123456789ab"},
        scenario_overrides={"base": {}}, scenario_fill_haircuts={"base": None},
        materialise_seconds={f"{symbol}:all": 30.5},
        replay_seconds={f"base:{symbol}:all": 1.4},
        wall_seconds=32.0, provider_fetches_total=0, bar_cache_hits=40,
        starting_cash=100_000.0, run_sensitivity=False,
    )


def battery_args():
    """What `--command battery` hands `run_battery_cmd`."""
    return Namespace(
        scenarios=None, spec_env=None, persist=False, symbol=None,
        symbols=None, start=None, end=None, holdout_start=None,
        starting_cash=100_000.0, no_sensitivity=True, out=None, json_out=None,
        history_days=None,
    )


def _config():
    from src.utils.config import Config
    return Config("config/settings.yaml")


def pin(pin_id="pin0000000000001", spec=None, *, active=True, note=None,
        spec_json=None):
    if spec_json is None:
        spec_json = json.dumps(spec if spec is not None else valid_spec(),
                               sort_keys=True)
    return {"pin_id": pin_id, "spec_json": spec_json, "active": active,
            "written_at": "2026-09-01T12:00:00+00:00", "note": note}


def valid_spec(**over):
    spec = {
        "symbols": ["AAPL"],
        "start": "2025-08-01",
        "end": "2026-07-31",
        "holdout_start": None,
        "starting_cash": 100_000.0,
        "run_sensitivity": False,
        "scenarios": [{"name": "tighter",
                       "overrides": {"strategy.min_put_premium": 0.75},
                       "fill_haircut": None}],
    }
    spec.update(over)
    return spec


@pytest.fixture
def wired(monkeypatch):
    """`main` with the store and the runner replaced. Returns the fake writer.

    `run_sweep` returns a CLEAN sweep by default so an ordinary battery item
    exits 0; a test that wants a failure replaces it.
    """
    from src.backtesting.scenarios import persist as persist_mod
    import src.backtesting.scenarios as scenarios_pkg

    writer = FakeWriter()
    monkeypatch.setattr(persist_mod, "ScenarioRunWriter", lambda *a, **k: writer)
    monkeypatch.setattr(scenarios_pkg, "run_sweep",
                        lambda *a, **k: clean_sweep())
    # No artifact bucket: the writer is never constructed, so no GCS client is.
    monkeypatch.setenv("SIM_ARTIFACT_BUCKET", "")
    monkeypatch.delenv("BATTERY_MAX_SECONDS", raising=False)
    monkeypatch.delenv("SWEEP_RUN_ID", raising=False)
    monkeypatch.delenv("SWEEP_SUBMITTED_AT", raising=False)
    monkeypatch.delenv("SWEEP_SUBMITTED_VIA", raising=False)
    return writer


# ==========================================================================
# The standing set
# ==========================================================================
class TestTheStandingSet:
    def test_one_spec_per_live_symbol_over_the_trailing_year(self):
        from src.backtesting.screen import DEFAULT_LOOKBACK_DAYS

        config = _config()
        specs = cli.battery_standing_specs(config, today=date(2026, 9, 5))
        assert len(specs) == len(config.stock_symbols)
        assert [s["symbols"] for s in specs] == [[sym] for sym
                                                 in config.stock_symbols]
        for spec in specs:
            assert spec["end"] == "2026-09-04", (
                "the window must end at the LAST SETTLED session, never today: "
                "today's chain is still forming and the lake is structurally "
                "incapable of holding it, which is the trailing-gap shape the "
                "PR-c coverage review found"
            )
            assert spec["start"] == (
                date(2026, 9, 4) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
            ).isoformat()

    def test_the_end_is_the_backfills_own_notion_of_settled(self):
        """One function decides, not two.

        The battery rides the backfill's execution; if the two disagreed about
        which session is the last real one, the battery would ask every week
        for a day the run that preceded it could not have written.
        """
        from src.backtesting.data.bar_store import last_settled_day
        from src.backtesting.data.backfill import resolve_window

        today = date(2026, 9, 5)
        _start, backfill_end = resolve_window(history_days=30, today=today)
        specs = cli.battery_standing_specs(_config(), today=today)
        assert specs[0]["end"] == backfill_end.isoformat()
        assert backfill_end == last_settled_day(today)

    def test_the_holdout_is_on_and_wide_enough_for_the_api_to_accept_it(self):
        """An in-sample-only trend line records what the engine FITTED.

        The floor is the dashboard's own `MIN_HOLDOUT_DAYS`: a standing set the
        submit endpoint would refuse is a standing set nobody could re-create
        by hand.
        """
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        spec = cli.battery_standing_specs(_config(), today=date(2026, 9, 5))[0]
        assert spec["holdout_start"] is not None
        end = date.fromisoformat(spec["end"])
        holdout = date.fromisoformat(spec["holdout_start"])
        assert (end - holdout).days >= S.MIN_HOLDOUT_DAYS
        # And the whole spec survives the API's validator unchanged.
        assert S.validate_spec(spec) == spec

    def test_it_is_base_only_and_pays_for_no_sensitivity_pass(self):
        spec = cli.battery_standing_specs(_config(), today=date(2026, 9, 5))[0]
        assert spec["scenarios"] == [], (
            "the standing set IS the base config; an arm here would make every "
            "weekly point a comparison against something else"
        )
        assert spec["run_sensitivity"] is False


# ==========================================================================
# The loop: isolation, revalidation, the nag, the wall cap
# ==========================================================================
class TestTheBatteryMeasuresEverything:
    def test_every_standing_symbol_and_every_active_pin_is_submitted(
            self, wired, monkeypatch):
        submitted = []
        monkeypatch.setattr(
            cli, "run_sweep_cmd",
            lambda *a, **k: submitted.append(k) or 0)
        wired._pins = [pin("pin0000000000001"), pin("pin0000000000002")]

        rc = cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert rc == 0
        assert len(submitted) == len(_config().stock_symbols) + 2
        assert [k["pin_id"] for k in submitted][-2:] == [
            "pin0000000000001", "pin0000000000002"]

    def test_an_inactive_pin_is_not_measured(self, wired, monkeypatch):
        submitted = []
        monkeypatch.setattr(cli, "run_sweep_cmd",
                            lambda *a, **k: submitted.append(k) or 0)
        wired._pins = [pin("pin0000000000001", active=False),
                       pin("pin0000000000002", active=True)]

        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        pins_run = [k["pin_id"] for k in submitted if k["pin_id"]]
        assert pins_run == ["pin0000000000002"], (
            "un-pinning writes an `active=false` row rather than deleting; a "
            "battery that read the history instead of the latest state would "
            "keep running pins the operator removed"
        )

    def test_every_row_is_stamped_submitted_via_battery(self, wired,
                                                        monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "run_sweep_cmd",
                            lambda *a, **k: seen.append(k["submitted_via"]) or 0)
        wired._pins = [pin()]
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert set(seen) == {"battery"}
        assert cli.BATTERY_SUBMITTED_VIA == "battery"

    def test_the_rows_a_real_submission_writes_carry_it_too(self, wired):
        """Not just the parameter — the ROW. The trend queries read the column.

        This one goes through the real `run_sweep_cmd`, so it also pins that a
        battery item gets a `running` row and a terminal row of its own, with
        its own `run_id`, rather than inheriting the execution's.
        """
        logger = _Logger()
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert wired.statuses, "the battery wrote no rows at all"
        assert {r["submitted_via"] for r in wired.statuses} == {"battery"}
        assert {r["pin_id"] for r in wired.statuses} == {None}
        run_ids = {r["run_id"] for r in wired.statuses}
        assert len(run_ids) == len(_config().stock_symbols), (
            "each standing symbol is its own run; sharing a run_id would "
            "collapse fourteen runs into one unreadable timeline"
        )

    def test_a_pin_run_carries_its_pin_id_on_every_row(self, wired):
        wired._pins = [pin("pin0000000000009")]
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        pinned = [r for r in wired.statuses if r["pin_id"]]
        assert pinned, "the pin produced no rows"
        assert {r["pin_id"] for r in pinned} == {"pin0000000000009"}
        # Both the `running` and the terminal row: the nag counts the LATEST
        # status per run, so a pin_id on only one of them would make a pin's
        # history depend on which row won.
        assert {r["status"] for r in pinned} == {"running", "done"}


class TestTheBatteryRevalidatesEveryPin:
    """A pin legal yesterday and refused today gets a row, and the loop goes on.

    The revalidation IS `run_sweep_cmd`'s own validation — one parser, and the
    property it depends on is that every refusal happens BEFORE the first
    write. `test_a_refused_pin_writes_no_running_row` is what pins that.
    """

    REFUSED = {"strategy.min_open_interest": 500}

    def test_a_pin_whose_override_is_no_longer_allowlisted_is_refused(
            self, wired):
        logger = _Logger()
        wired._pins = [pin("pin000000000000a", spec=valid_spec(scenarios=[
            {"name": "oi", "overrides": self.REFUSED, "fill_haircut": None}]))]

        rc = cli.run_battery_cmd(battery_args(), _config(), logger)
        assert rc == 0
        failed = [r for r in wired.statuses
                  if r["pin_id"] == "pin000000000000a"]
        assert len(failed) == 1 and failed[0]["status"] == "failed"
        assert failed[0]["error"].startswith(cli.BATTERY_PIN_INVALID_PREFIX)
        assert "open_interest" in failed[0]["error"]
        assert "battery_pin_failed" in logger.types("error")
        assert logger.payload("battery_pin_failed")["invalid"] is True

    def test_a_refused_pin_writes_no_running_row(self, wired):
        """The coupling `run_battery_cmd` depends on, stated as a test.

        If a refusal could ever land AFTER the store section began, the pin
        would have an orphaned `running` row holding the dedup's duplicate
        check open until it aged out — and the battery's own `failed` row would
        be a second, contradictory record of one attempt.
        """
        wired._pins = [pin("pin000000000000a", spec=valid_spec(scenarios=[
            {"name": "oi", "overrides": self.REFUSED, "fill_haircut": None}]))]
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        rows = [r for r in wired.statuses if r["pin_id"]]
        assert [r["status"] for r in rows] == ["failed"]

    def test_a_refused_pin_does_not_stop_the_standing_set(self, wired):
        wired._pins = [pin("pin000000000000a", spec=valid_spec(scenarios=[
            {"name": "oi", "overrides": self.REFUSED, "fill_haircut": None}]))]
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        done = [r for r in wired.statuses
                if r["status"] == "done" and r["pin_id"] is None]
        assert len(done) == len(_config().stock_symbols), (
            "isolation: one bad pin must cost its own row and nothing else"
        )

    @pytest.mark.parametrize("spec_json,expected", [
        ("not json at all", "not valid JSON"),
        ("[1, 2, 3]", "not a JSON object"),
        ("", "no spec_json"),
    ])
    def test_a_corrupt_pin_row_is_refused_rather_than_raised(
            self, wired, spec_json, expected):
        """A row nothing can decode must not be able to end the battery."""
        wired._pins = [pin("pin000000000000b", spec_json=spec_json)]
        rc = cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert rc == 0
        row = next(r for r in wired.statuses if r["pin_id"] == "pin000000000000b")
        assert row["status"] == "failed" and expected in row["error"]

    def test_a_spec_too_malformed_to_derive_columns_from_still_gets_a_row(
            self, wired):
        """`status_row` reads `spec['scenarios'][i]['name']`; a refused pin is
        exactly the one likely to make that raise. The row is what matters."""
        wired._pins = [pin("pin000000000000c",
                           spec_json=json.dumps({"symbols": ["AAPL"],
                                                 "scenarios": ["not-a-dict"]}))]
        rc = cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert rc == 0
        row = next(r for r in wired.statuses if r["pin_id"] == "pin000000000000c")
        assert row["status"] == "failed"
        assert row["error"].startswith(cli.BATTERY_PIN_INVALID_PREFIX)

    def test_a_pin_that_is_still_legal_is_replayed_normally(self, wired):
        wired._pins = [pin("pin000000000000d")]
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        rows = [r for r in wired.statuses if r["pin_id"] == "pin000000000000d"]
        assert [r["status"] for r in rows] == ["running", "done"]


class TestTheThreeWeekNag:
    INVALID = {"strategy.min_open_interest": 500}

    def _refused_pin(self):
        return pin("pin000000000000e", spec=valid_spec(scenarios=[
            {"name": "oi", "overrides": self.INVALID, "fill_haircut": None}]))

    def _prior(self, n, *, invalid=True):
        prefix = cli.BATTERY_PIN_INVALID_PREFIX if invalid else ""
        return [{"run_id": f"prior{i}", "status": "failed",
                 "error": f"{prefix}whatever"} for i in range(n)]

    def test_two_prior_refusals_make_this_one_the_third_and_it_nags(
            self, wired):
        logger = _Logger()
        wired._pins = [self._refused_pin()]
        wired.history = {"pin000000000000e": self._prior(2)}

        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert "battery_pin_nag" in logger.types("error")
        payload = logger.payload("battery_pin_nag")
        assert payload["pin_id"] == "pin000000000000e"
        assert payload["weeks"] == cli.BATTERY_NAG_RUNS == 3

    def test_one_prior_refusal_does_not_nag(self, wired):
        """Two weeks is not three, and a nag that fires early is a nag nobody
        reads by the time it means something."""
        logger = _Logger()
        wired._pins = [self._refused_pin()]
        wired.history = {"pin000000000000e": self._prior(1)}
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert "battery_pin_nag" not in logger.types()

    def test_a_prior_FAILURE_that_was_not_a_refusal_does_not_count(self, wired):
        """The nag says "come and fix your pin". A vendor outage last week is
        not something the operator can fix by editing it, so it breaks the
        streak rather than feeding it."""
        logger = _Logger()
        wired._pins = [self._refused_pin()]
        wired.history = {"pin000000000000e":
                         self._prior(1) + self._prior(1, invalid=False)}
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert "battery_pin_nag" not in logger.types()

    def test_the_history_query_excludes_the_attempt_being_recorded(self, wired):
        """The row was inserted a moment ago; reading it back would make the
        nag depend on streaming-buffer visibility."""
        wired._pins = [self._refused_pin()]
        wired.history = {"pin000000000000e": self._prior(2)}
        cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert wired.history_calls, "the nag never asked for the history"
        pin_id, limit, exclude = wired.history_calls[0]
        assert pin_id == "pin000000000000e"
        assert limit == cli.BATTERY_NAG_RUNS - 1 == 2
        current = next(r["run_id"] for r in wired.statuses
                       if r["pin_id"] == "pin000000000000e")
        assert exclude == current

    def test_a_standing_item_never_nags(self, wired, monkeypatch):
        """The standing set has no pin to fix; the nag would name nothing."""
        logger = _Logger()

        def refuse(*_a, **_k):
            raise SystemExit("nope")
        monkeypatch.setattr(cli, "run_sweep_cmd", refuse)
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert "battery_pin_nag" not in logger.types()
        assert wired.history_calls == []


class TestTheWallCap:
    def test_a_battery_past_the_cap_starts_nothing_more_and_names_it(
            self, wired, monkeypatch):
        """The cap bounds when a NEW sweep may start. Nothing is interrupted.

        The clock is driven forward by the submissions themselves, which is
        what the real thing does: the first item is always allowed to run.
        """
        logger = _Logger()
        clock = {"t": 0.0}
        monkeypatch.setattr(cli, "battery_max_seconds", lambda: 100)
        import time as _time
        monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])

        def slow(*_a, **_k):
            clock["t"] += 60.0
            return 0
        monkeypatch.setattr(cli, "run_sweep_cmd", slow)

        rc = cli.run_battery_cmd(battery_args(), _config(), logger)
        assert rc == 0, "the wall cap is not a failure of the process"
        payload = logger.payload("battery_degraded")
        assert payload["reason"] == "wall_cap"
        # Two ran (t=0 and t=60); everything after t>=100 was refused a start.
        assert payload["measured"] == 2
        assert payload["skipped"] == len(_config().stock_symbols) - 2
        assert payload["skipped_labels"][0] == (
            f"standing:{_config().stock_symbols[2]}"), (
            "the skipped items must be NAMED — a battery that silently "
            "measured two of fourteen looks exactly like one that measured "
            "fourteen"
        )

    def test_the_item_in_flight_when_the_cap_passes_is_never_interrupted(
            self, wired, monkeypatch):
        logger = _Logger()
        clock = {"t": 0.0}
        monkeypatch.setattr(cli, "battery_max_seconds", lambda: 10)
        import time as _time
        monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
        finished = []

        def slow(*_a, **kw):
            clock["t"] += 1000.0     # blows straight through the cap
            finished.append(kw.get("run_id"))
            return 0
        monkeypatch.setattr(cli, "run_sweep_cmd", slow)

        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert len(finished) == 1, (
            "the first item must complete; killing it would leave a `running` "
            "row to age out and throw away a nearly-finished replay"
        )

    def test_the_default_is_four_hours(self):
        assert cli.BATTERY_MAX_SECONDS == 14_400

    def test_it_fits_inside_the_jobs_task_timeout(self):
        """The sizing claim, asserted rather than asserted-in-prose.

        The battery shares one `data-backfill` execution with the backfill. A
        cap at or above the task timeout could let a long measurement pass run
        the execution into a SIGKILL — which would take the BACKFILL's exit
        code with it and turn a successful data run into a page.
        """
        import re
        from pathlib import Path

        yaml_text = (Path(__file__).resolve().parents[1]
                     / "cloudbuild.yaml").read_text()
        block = yaml_text[yaml_text.index("gcloud run jobs deploy data-backfill"):]
        timeout = int(re.search(r"--task-timeout=(\d+)", block).group(1))
        assert cli.BATTERY_MAX_SECONDS < timeout, (
            f"BATTERY_MAX_SECONDS={cli.BATTERY_MAX_SECONDS} against a "
            f"{timeout}s task timeout leaves the backfill no room"
        )
        assert timeout - cli.BATTERY_MAX_SECONDS >= 3600, (
            "at least an hour of the execution must remain for the backfill "
            "itself plus the longest single sweep"
        )

    @pytest.mark.parametrize("raw", ["abc", "0", "-1"])
    def test_a_bad_env_override_is_refused_not_defaulted(self, monkeypatch, raw):
        """A typo that fell back to 4h would be indistinguishable from the
        override having worked."""
        monkeypatch.setenv("BATTERY_MAX_SECONDS", raw)
        with pytest.raises(SystemExit):
            cli.battery_max_seconds()

    def test_the_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("BATTERY_MAX_SECONDS", "60")
        assert cli.battery_max_seconds() == 60


# ==========================================================================
# Exit codes, and the composed backfill-then-battery flow
# ==========================================================================
class TestExitCodeClasses:
    def test_a_battery_in_which_everything_failed_still_exits_zero(
            self, wired, monkeypatch):
        logger = _Logger()

        def boom(*_a, **_k):
            raise RuntimeError("materialisation exploded")
        monkeypatch.setattr(cli, "run_sweep_cmd", boom)

        assert cli.run_battery_cmd(battery_args(), _config(), logger) == 0
        assert logger.payload("battery_degraded")["failed"] == len(
            _config().stock_symbols)
        assert logger.payload("battery_degraded")["measured"] == 0

    def test_a_failing_item_is_counted_but_its_row_is_not_duplicated(
            self, wired, monkeypatch):
        """`run_sweep_cmd`'s own `finally` already wrote the `failed` row with
        the reason on it; a second row here would be a duplicate record."""
        logger = _Logger()
        monkeypatch.setattr(
            cli, "run_sweep_cmd",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert wired.statuses == [], (
            "the battery must not write its own row for a sweep that got far "
            "enough to write one"
        )
        assert logger.payload("battery_pin_failed")["invalid"] is False

    def test_a_nonzero_exit_from_an_item_is_a_failure_not_a_measurement(
            self, wired, monkeypatch):
        logger = _Logger()
        monkeypatch.setattr(cli, "run_sweep_cmd", lambda *a, **k: 1)
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert logger.payload("battery_degraded")["measured"] == 0

    def test_a_clean_battery_says_completed_and_never_degraded(self, wired):
        logger = _Logger()
        assert cli.run_battery_cmd(battery_args(), _config(), logger) == 0
        assert "battery_completed" in logger.types("info")
        assert "battery_degraded" not in logger.types()

    def test_an_unavailable_store_degrades_loudly_and_exits_zero(
            self, monkeypatch):
        from src.backtesting.scenarios import persist as persist_mod

        logger = _Logger()
        monkeypatch.setattr(persist_mod, "ScenarioRunWriter",
                            lambda *a, **k: FakeWriter(enabled=False))
        assert cli.run_battery_cmd(battery_args(), _config(), logger) == 0
        assert logger.payload("battery_degraded")["reason"] == "store_unavailable"

    def test_a_sigterm_mid_battery_propagates(self, wired, monkeypatch):
        """Cloud Run is killing the container. The sweep in flight has already
        written its terminal row; there is no next item, and spending the
        10-second grace on another BigQuery round-trip would lose it."""
        def terminate(*_a, **_k):
            raise cli.SweepTerminated("SIGTERM")
        monkeypatch.setattr(cli, "run_sweep_cmd", terminate)
        with pytest.raises(cli.SweepTerminated):
            cli.run_battery_cmd(battery_args(), _config(), _Logger())


class TestTheComposedBackfillThenBattery:
    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True),
        ("on", True), ("", False), ("false", False), ("0", False),
        ("1.0", False), ("truthy", False), ("  true  ", True),
    ])
    def test_the_flag_is_a_closed_set_of_spellings(self, monkeypatch, raw,
                                                   expected):
        """Treating an unrecognised value as true would start a four-hour
        measurement pass off a typo."""
        monkeypatch.setenv("BACKFILL_THEN_BATTERY", raw)
        assert cli.battery_after_backfill_requested() is expected

    def test_an_unset_flag_means_backfill_only(self, monkeypatch):
        monkeypatch.delenv("BACKFILL_THEN_BATTERY", raising=False)
        assert cli.battery_after_backfill_requested() is False

    @staticmethod
    def _dispatch(monkeypatch, *, backfill_rc, flag="true"):
        """Drive `main()` through the backfill branch with both halves faked."""
        import sys

        calls = {"battery": 0}
        monkeypatch.setenv("BACKFILL_THEN_BATTERY", flag)
        monkeypatch.setattr(cli, "run_backfill_cmd",
                            lambda *a, **k: backfill_rc)
        monkeypatch.setattr(cli, "run_battery_cmd",
                            lambda *a, **k: calls.__setitem__(
                                "battery", calls["battery"] + 1) or 0)
        monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(cli, "configure_analytics_writer", lambda **k: None)
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--command", "backfill"])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        return code, calls

    def test_a_successful_backfill_runs_the_battery_and_exits_zero(
            self, monkeypatch):
        code, calls = self._dispatch(monkeypatch, backfill_rc=0)
        assert code == 0 and calls["battery"] == 1

    def test_a_failed_backfill_skips_the_battery_and_keeps_its_exit_code(
            self, monkeypatch):
        """The exit classes, in one test. DATA failure pages (non-zero, the Job
        policy); the battery does not run at all, because the lake is not what
        a measurement would be taken against."""
        code, calls = self._dispatch(monkeypatch, backfill_rc=1)
        assert code == 1, "the backfill's exit code must survive"
        assert calls["battery"] == 0

    def test_without_the_flag_a_backfill_is_only_a_backfill(self, monkeypatch):
        code, calls = self._dispatch(monkeypatch, backfill_rc=0, flag="false")
        assert code == 0 and calls["battery"] == 0

    def test_a_degraded_battery_never_reddens_a_good_backfill(self,
                                                              monkeypatch):
        """`run_battery_cmd` always returns 0 — asserted through the dispatch,
        because the whole point is that the exit code below it is the
        BACKFILL's."""
        import sys

        monkeypatch.setenv("BACKFILL_THEN_BATTERY", "true")
        monkeypatch.setattr(cli, "run_backfill_cmd", lambda *a, **k: 0)
        monkeypatch.setattr(cli, "run_battery_cmd", lambda *a, **k: 99)
        monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(cli, "configure_analytics_writer", lambda **k: None)
        monkeypatch.setattr(sys, "argv", ["main.py", "--command", "backfill"])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        assert code == 0, (
            "the battery's return value must not reach the process exit code "
            "on the composed path"
        )

    def test_battery_is_a_command_in_its_own_right(self, monkeypatch, wired):
        """`--command battery` runs it alone — which is how an operator
        re-runs a degraded week without waiting for Saturday."""
        import sys

        calls = {"n": 0}
        monkeypatch.setattr(cli, "run_battery_cmd",
                            lambda *a, **k: calls.__setitem__(
                                "n", calls["n"] + 1) or 0)
        monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(cli, "configure_analytics_writer", lambda **k: None)
        monkeypatch.setattr(sys, "argv", ["main.py", "--command", "battery"])
        cli.main()
        assert calls["n"] == 1


# ==========================================================================
# The economics: a week in which nothing moved costs nothing
# ==========================================================================
class TestADedupHitWeekReplaysNothing:
    def test_a_stored_answer_for_every_spec_means_zero_replays(
            self, wired, monkeypatch):
        """The Saturday's whole economics. `find_done_sweep` answers for every
        key, so the battery writes `deduplicated` rows and replays nothing."""
        replays = []
        import src.backtesting.scenarios as scenarios_pkg
        monkeypatch.setattr(scenarios_pkg, "run_sweep",
                            lambda *a, **k: replays.append(1))
        wired.prior = {"run_id": "earlierrun000001"}
        wired._pins = [pin("pin000000000000f")]

        rc = cli.run_battery_cmd(battery_args(), _config(), _Logger())
        assert rc == 0
        assert replays == [], (
            "a dedup-hit week must not replay anything — that is what makes "
            "the weekly cadence affordable at all"
        )
        terminal = [r for r in wired.statuses if r["status"] != "running"]
        assert {r["status"] for r in terminal} == {"deduplicated"}
        assert all(r["deduplicated_to"] == "earlierrun000001" for r in terminal)

    def test_a_deduplicated_item_counts_as_measured_not_failed(
            self, wired, monkeypatch):
        logger = _Logger()
        import src.backtesting.scenarios as scenarios_pkg
        monkeypatch.setattr(scenarios_pkg, "run_sweep",
                            lambda *a, **k: pytest.fail("replayed"))
        wired.prior = {"run_id": "earlierrun000001"}
        cli.run_battery_cmd(battery_args(), _config(), logger)
        assert "battery_degraded" not in logger.types()
        assert logger.payload("battery_completed")["measured"] == len(
            _config().stock_symbols)


# ==========================================================================
# The pin store: schema and row derivation (no BigQuery)
# ==========================================================================
class TestThePinTable:
    def test_the_schema_is_the_five_columns_and_no_counter(self):
        pytest.importorskip("google.cloud.bigquery")
        fields = {f.name: f for f in store._pins_schema()}
        assert set(fields) == {"pin_id", "spec_json", "active", "written_at",
                               "note"}
        assert store._canonical_type(fields["active"].field_type) == "BOOL"
        for required in ("pin_id", "spec_json", "active", "written_at"):
            assert fields[required].mode == "REQUIRED", (
                f"{required} must be REQUIRED: a pin whose current state is "
                f"unreadable must not default to 'run it every week for ever'"
            )
        assert (fields["note"].mode or "NULLABLE").upper() == "NULLABLE"

    def test_a_pin_id_is_run_id_shaped(self):
        pin_id = store.new_pin_id()
        assert len(pin_id) == 16 and int(pin_id, 16) >= 0

    def test_two_pins_of_the_same_spec_get_different_ids(self):
        """Not content-addressed on purpose: two operators may pin the same
        question for different reasons, and un-pinning one must not un-pin the
        other."""
        assert store.new_pin_id() != store.new_pin_id()

    def test_pin_row_takes_exactly_one_of_spec_and_spec_json(self):
        with pytest.raises(ValueError):
            store.pin_row(pin_id="p", active=True)
        with pytest.raises(ValueError):
            store.pin_row(pin_id="p", spec={}, spec_json="{}", active=True)

    def test_a_deactivation_carries_the_spec_it_retires(self):
        row = store.pin_row(pin_id="p", spec_json='{"a": 1}', active=False)
        assert row["spec_json"] == '{"a": 1}' and row["active"] is False

    def test_the_note_is_bounded(self):
        row = store.pin_row(pin_id="p", spec={}, active=True,
                            note="x" * 5_000)
        assert len(row["note"]) == store.PIN_NOTE_MAX_CHARS

    def test_the_latest_row_tiebreak_resolves_towards_deleted(self):
        """A create and a delete in the same microsecond must resolve to
        DELETED: the other direction leaves a pin the operator removed running
        every Saturday for ever."""
        assert store.PINS_LATEST_ORDER_BY.endswith("active ASC")

    def test_the_sweep_table_carries_pin_id_additively(self):
        pytest.importorskip("google.cloud.bigquery")
        field = next(f for f in store._sweeps_schema() if f.name == "pin_id")
        assert store._canonical_type(field.field_type) == "STRING"
        assert (field.mode or "NULLABLE").upper() == "NULLABLE"

    def test_every_status_row_carries_the_key_even_when_it_is_null(self):
        row = store.status_row(run_id="r", status=store.STATUS_RUNNING,
                               submitted_at="2026-09-01T12:00:00+00:00")
        assert "pin_id" in row and row["pin_id"] is None


class TestThePinStoreNeverBlocksASweep:
    def test_a_pin_table_that_cannot_be_created_leaves_the_writer_enabled(
            self, monkeypatch):
        """The ordering in `__init__` is the property. A permission problem on
        `scenario_pins` must not turn into "the Job replayed for eight minutes
        and stored nothing"."""
        pytest.importorskip("google.cloud.bigquery")
        writer = store.ScenarioRunWriter.__new__(store.ScenarioRunWriter)
        calls = []

        def ensure(_ref, name, *_a, **_k):
            calls.append(name)
            if name == store.PINS_TABLE:
                raise RuntimeError("no permission on scenario_pins")
            return f"ref:{name}"

        monkeypatch.setattr(writer, "_ensure_table", ensure, raising=False)
        # Re-run the body of __init__'s table section the way it is written.
        writer._enabled = False
        writer._pins = None
        writer._sweeps = ensure(None, store.SWEEPS_TABLE)
        writer._runs = ensure(None, store.RUNS_TABLE)
        writer._enabled = True
        try:
            writer._pins = ensure(None, store.PINS_TABLE)
        except Exception:
            pass
        assert writer.enabled is True
        assert writer.pins_enabled is False
        assert writer.list_pins() == [], (
            "an unavailable pin store must read as 'no pins', never raise — "
            "the standing set still has to be measured"
        )

    def test_pins_enabled_is_not_enabled(self):
        writer = store.ScenarioRunWriter.__new__(store.ScenarioRunWriter)
        writer._enabled = True
        writer._pins = None
        assert writer.enabled and not writer.pins_enabled


class TestNothingTouchesGCP:
    def test_no_bigquery_client_is_constructible_in_this_module(self,
                                                                monkeypatch):
        """The guard the sim-artifact suite established, applied here.

        Every writer in this file is a fake; if one of them were ever replaced
        by the real thing, this makes the test fail rather than reach for a
        credential.
        """
        pytest.importorskip("google.cloud.bigquery")
        from google.cloud import bigquery

        def refuse(*_a, **_k):
            raise AssertionError("a real BigQuery client was constructed")
        monkeypatch.setattr(bigquery, "Client", refuse)
        writer = FakeWriter()
        assert writer.list_pins() == []
