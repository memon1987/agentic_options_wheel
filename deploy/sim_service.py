#!/usr/bin/env python3
"""The interactive simulation service — FC-096 Phase B B3.

A private, scale-to-zero Cloud Run **service** that runs the same scenario
replay the ``backtest-sweep`` Job runs, and answers in seconds instead of in a
three-minute container start. It is the same image and the same engine; only the
entry point and the posture differ.

    POST /simulate  -> 200 {run_id, deduplicated: true}   (already answered)
                    -> 202 {run_id}                        (accepted; poll)
                    -> 409 / 422                           (refused, with why)
    GET  /health    -> {engine_identity, engine_version, git_commit, busy}

**ASYNC-FIRST, and that is the architecture, not an optimisation.** Rev 1 of the
plan had ``/simulate`` return the results in the response. Two measured facts
killed it: a warm-lake materialisation costs 25-50 s per symbol (it dominates —
the replay itself is ~0.3 s/symbol-year), and this image running as a service
shows a 240 s+ cold-start tail (14 days of live probe data: ten startup-probe
DEADLINE_EXCEEDEDs, 503s at 241-246 s). A request budget bounded by cell count
bounds the wrong variable, and timeout-then-scale-down re-creates the
uncooperative-termination hole Phase A closed on the Job. So: this endpoint
persists a handle and returns, and the console polls the status machinery
FC-060 already shipped (``GET /api/v2/sweeps/{run_id}``).

**CONCURRENCY IS EXACTLY ONE, AND THE ENGINE DEPENDS ON IT.** The deploy pins
``--concurrency=1``, and this process runs at most one replay at a time on a
single worker thread (``_RUN_LOCK``). Both are load-bearing, because the replay
mutates process-global state that no worker could own:

  * ``ExecutionEngine._failed_symbols`` is a module-global set the day loop
    clears every simulated day;
  * the simulated clock (``src.utils.clock``) is frozen for the duration;
  * ``quiet_strategy_logs`` moves stdlib logger levels for the whole process;
  * the analytics writer is a configure-once singleton.

Two replays in one process would silently corrupt each other's results — not
crash, corrupt: the numbers would still come out. The lock is what makes the
process-global swaps safe, and the deploy flag is what stops a second request
arriving mid-replay from queueing behind it invisibly. A second request while
busy is a 409, never a queue.

**CPU allocation is instance-based** (``--no-cpu-throttling``). Under Cloud
Run's default request-based billing the CPU is throttled the moment a response
returns, and a 202-then-background-replay design starves: nothing else ever
supplies an in-flight request, because the console polls BigQuery through the
dashboard and never touches this service. ~$0.09/hr while an instance is alive,
$0 when fully idle. The residual is accepted and documented: an idle instance
can be scaled in mid-replay, in which case SIGTERM terminalises the run as
``failed`` (see ``_on_sigterm``) and the console's posture is resubmit.

**IT CAN NEVER REACH THE OPTIONS VENDOR.** Both halves of the guard the plan
requires are here: a pre-flight that lists the chain lake and refuses a spec it
cannot cover (``lake_coverage``), and — because a pre-flight is a prediction —
``ChainFetchRefusingProvider`` wrapped around the vendor client, so a chain
fetch that happens anyway fails the run loudly
(``sim_service_cold_fetch_refused``) instead of quietly spending minutes. Stock
bars remain permitted, on purpose; see ``fetch_guard``.

**``@require_account_match`` is deliberately absent.** That decorator pins a
request to a brokerage account because the endpoint it guards can place an
order. Nothing here can: this service replays historical data and writes rows to
BigQuery and objects to GCS. It holds Alpaca credentials only because ``Config``
requires them to construct, and because stock bars come from the same vendor.
The absence is stated rather than merely true, so a future reader does not
"restore" it and a future endpoint on this app does not inherit the silence.

**The service returns raw ids and status; the DASHBOARD shapes.**
``shape_results`` already exists there and is not duplicated into the engine
image — which is also why a dedup hit returns ``{run_id, deduplicated: true}``
and no rows.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import structlog

# `main.py` lives at the repo root and the image sets PYTHONPATH=/app, so this
# works both in the container and from a checkout. Prepending the repo root is
# deliberate reuse, not convenience: `main` owns the sweep spec's parser, its
# validators and its terminal-status writer, and a second copy of any of the
# three is how an API ends up accepting an arm the Job would refuse, or writing
# a `done` row under rules that have since changed on the other side.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Budget constants
# --------------------------------------------------------------------------- #

# Seconds to materialise ONE (symbol, window) from a warm lake, on this
# service's single vCPU.
#
# TODO(measure): rollout step 3 replaces this with the measured warm figure, and
# the boundary test that pins it moves in the same commit. 40 is the midpoint of
# the 25-50 s range measured during Phase A. Being wrong is asymmetric: too high
# refuses a spec that would have worked and sends the operator to the batch path
# (annoying); too low accepts one that pins the instance for half an hour, which
# is the outcome this bound exists to prevent.
MATERIALISE_SECONDS_PER_SYMBOL = 40.0

# Seconds per CELL of in-memory replay. The dashboard's own cell-cap message
# quotes "~1-2 s per cell on the Job's single vCPU"; 2.0 is the top of that
# range. Replay is a rounding error next to materialisation on any realistic
# spec — it is in the estimate so that twenty cheap arms cannot slip through on
# a materialise-only count.
REPLAY_SECONDS_PER_CELL = 2.0

# The wall clock a single interactive simulation may be ESTIMATED at before this
# service refuses it. Ten minutes: past that the operator is not waiting at a
# console, the instance is pinned for the duration (concurrency 1), and the
# `backtest-sweep` Job exists precisely for the big batch.
SIM_MAX_ESTIMATE_SECONDS = 600.0

# Secondary bound, mirroring the dashboard validator's `MAX_CELLS`. The estimate
# is the primary gate; this catches the shape the seconds model is least
# confident about — very many cheap arms over few symbols — without pretending
# the model is exact.
SIM_MAX_CELLS = 240

# What this service stamps on its non-terminal rows so a reader knows how long
# to wait before declaring it dead (`services/sweeps.row_liveness_seconds`).
# 900 s + the reader's 10-minute grace = a ~25-minute lock, against the 3 h 10 m
# a Job row buys. A scaled-in service instance dies in seconds; holding the
# one-at-a-time submit lock for three hours on it would take the feature offline
# for the rest of the afternoon.
LIVENESS_SECONDS = 900

#: ``submitted_via`` for every row this service writes. Free-form by schema; the
#: battery and trend queries filter on it.
SUBMITTED_VIA = "sim-service"

# --------------------------------------------------------------------------- #
# Process state. One replay at a time; see the module docstring.
# --------------------------------------------------------------------------- #
_RUN_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_CURRENT: Optional["_Run"] = None
_WORKER: Optional[threading.Thread] = None

_CONFIG: Any = None
_CONFIG_LOCK = threading.Lock()
_WRITER: Any = None
_WRITER_LOCK = threading.Lock()


class SpecRefused(Exception):
    """A spec this service will not run. ``status`` is the HTTP code."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


# --------------------------------------------------------------------------- #
# Lazily-built process singletons
# --------------------------------------------------------------------------- #
def get_config():
    """The service's ``Config``, built once.

    Once, because ``Config()`` reads and validates a yaml and the account
    interlock, and because every request must see the same effective
    configuration: the dedup's ``base_config_hash`` is computed from it, and a
    config that changed between two requests of one process would make two
    identical specs key differently.
    """
    global _CONFIG
    with _CONFIG_LOCK:
        if _CONFIG is None:
            from src.utils.config import Config
            _CONFIG = Config()
        return _CONFIG


def get_writer():
    """The ``ScenarioRunWriter``, built once. May be a DISABLED writer.

    Construction reconciles the two sweep tables' schemas additively, which is
    also how ``liveness_seconds`` reaches a live table that predates it. Cached
    because that reconcile is API round-trips and must not be paid per request.

    A disabled writer is returned rather than raised on, and the caller refuses
    the request with a 503: a service that cannot persist has nothing to hand
    back, since its whole answer is "poll this run_id".
    """
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is None:
            from src.backtesting.scenarios import persist as sweep_store
            _WRITER = sweep_store.ScenarioRunWriter(
                dataset_id=get_config().bigquery_dataset)
        return _WRITER


def reset_for_tests() -> None:
    """Drop the process singletons and the run slot. Never called in production."""
    global _CONFIG, _WRITER, _CURRENT, _WORKER
    with _CONFIG_LOCK:
        _CONFIG = None
    with _WRITER_LOCK:
        _WRITER = None
    with _STATE_LOCK:
        _CURRENT = None
    _WORKER = None
    if _RUN_LOCK.locked():
        try:
            _RUN_LOCK.release()
        except RuntimeError:  # pragma: no cover - defensive
            pass


# --------------------------------------------------------------------------- #
# Spec handling — the JOB's parser, reused
# --------------------------------------------------------------------------- #
def normalise_spec(spec: Any) -> Dict[str, Any]:
    """Validate ``spec`` with the Job's own validators; return the canonical form.

    The ``spec`` key of the result is identical in shape to the ``spec_payload``
    ``main.run_sweep_cmd`` builds, which is what makes a sweep submitted here and
    the same sweep submitted through the dashboard produce the SAME ``sweep_key``
    and dedup against each other. That is the plan's "``sweep_key`` computed over
    the API-normalised spec": there is one canonical form in this system, and a
    third one would simply never hit either side's cache.

    ``main``'s validators raise ``SystemExit`` because their first home was a
    CLI. That is caught here and turned into a 422 carrying the same text — the
    message is the useful part ("scenario 'x' has unknown field(s) [...]") and
    re-implementing the checks to get a nicer exception type would be two
    parsers, which is the failure this reuse exists to prevent.
    """
    import json

    import main as cli
    from src.backtesting.scenarios import BASE_SCENARIO_NAME
    from src.backtesting.scenarios.identity import (
        DEFAULT_STARTING_CASH, validate_symbol,
    )
    from src.backtesting.scenarios.overrides import (
        OverrideError, validate_overrides,
    )
    from src.backtesting.screen import DEFAULT_LOOKBACK_DAYS

    if not isinstance(spec, dict):
        raise SpecRefused(
            422, f"expected a JSON object; got {type(spec).__name__}")
    unknown = set(spec) - cli.SPEC_FIELDS
    if unknown:
        raise SpecRefused(
            422,
            f"unknown field(s) {sorted(unknown)}. A misspelled field would "
            f"silently do nothing. Known fields: {sorted(cli.SPEC_FIELDS)}.")

    encoded = json.dumps(spec, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > cli.MAX_SPEC_BYTES:
        raise SpecRefused(
            422,
            f"spec is {len(encoded.encode('utf-8'))} bytes, over the "
            f"{cli.MAX_SPEC_BYTES}-byte limit")

    try:
        scenarios = cli.scenarios_from_spec(spec)
        end = cli._spec_date(spec, "end") or date.today()
        start = (cli._spec_date(spec, "start")
                 or end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        holdout_start = cli._spec_date(spec, "holdout_start")
    except SystemExit as exc:
        raise SpecRefused(422, str(exc))

    if end <= start:
        raise SpecRefused(422, f"'end' ({end}) must be after 'start' ({start})")
    if holdout_start is not None and not (start < holdout_start <= end):
        raise SpecRefused(
            422,
            f"'holdout_start' {holdout_start} must fall inside "
            f"({start}, {end}]; otherwise one of the two windows is empty")

    symbols = [str(x).strip().upper() for x in (spec.get("symbols") or [])
               if str(x).strip()]
    if not symbols:
        raise SpecRefused(422, "'symbols' must be a non-empty list")
    seen = set()
    for symbol in symbols:
        try:
            validate_symbol(symbol, "symbols")
        except ValueError as exc:
            raise SpecRefused(422, str(exc))
        if symbol in seen:
            raise SpecRefused(
                422,
                f"symbol {symbol} appears twice; the universe is a set — a "
                f"repeat would materialise once and render as one column anyway")
        seen.add(symbol)

    for scenario in scenarios:
        if scenario.name == BASE_SCENARIO_NAME:
            # `base` is the implicit comparator every delta is measured against
            # and `_with_base_first` folds a declared one in. Refused here for
            # the dashboard's reason: an arm that redefined it would move every
            # other number in the table while looking ordinary.
            raise SpecRefused(
                422,
                f"'{BASE_SCENARIO_NAME}' is reserved: it is the comparator "
                f"every delta is measured against, and it is added "
                f"automatically")
        try:
            validate_overrides(scenario.overrides)
        except OverrideError as exc:
            raise SpecRefused(422, f"scenario '{scenario.name}': {exc}")

    # `identity.DEFAULT_STARTING_CASH`, the value `canonical_spec` itself
    # substitutes for an absent one. Any other default here would make an
    # unspecified spec key differently from the same spec submitted through the
    # dashboard, and the two would never dedup against each other.
    raw_cash = spec.get("starting_cash")
    if raw_cash in (None, ""):
        starting_cash = float(DEFAULT_STARTING_CASH)
    else:
        try:
            starting_cash = float(raw_cash)
        except (TypeError, ValueError):
            raise SpecRefused(
                422, f"'starting_cash' must be a number; got {raw_cash!r}")

    run_sensitivity = spec.get("run_sensitivity", False)
    if not isinstance(run_sensitivity, bool):
        raise SpecRefused(422, "'run_sensitivity' must be true or false")
    if run_sensitivity:
        # REFUSED on this path, never silently doubled. The sensitivity pass
        # replays every cell a second time at the bid for one extra scalar, so
        # it doubles the expensive half of an interactive request to answer a
        # question that matters on the arm you finally CHOOSE — which is CLI and
        # Job territory, where nobody is watching a spinner.
        raise SpecRefused(
            422,
            "'run_sensitivity' is not available on the interactive sim "
            "service: it replays every cell a second time at the bid, doubling "
            "the cost of the request for a scalar that matters on the arm you "
            "finally choose. Run it through the `backtest-sweep` Job (or the "
            "CLI) on that arm.")

    force = spec.get("force", False)
    if not isinstance(force, bool):
        raise SpecRefused(422, "'force' must be true or false")

    normalised: Dict[str, Any] = {
        "symbols": symbols,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "holdout_start": holdout_start.isoformat() if holdout_start else None,
        "starting_cash": starting_cash,
        "run_sensitivity": run_sensitivity,
        "scenarios": [
            {"name": s.name, "overrides": dict(s.overrides),
             "fill_haircut": s.fill_haircut}
            for s in scenarios
        ],
    }
    return {"spec": normalised, "scenarios": scenarios, "start": start,
            "end": end, "holdout_start": holdout_start, "force": force}


def cell_count(spec: Dict[str, Any]) -> int:
    """Cells this spec produces: arms (incl. the implicit base) x symbols x splits."""
    from src.backtesting.scenarios import BASE_SCENARIO_NAME

    names = {str(s.get("name")) for s in (spec.get("scenarios") or [])}
    arms = len(names | {BASE_SCENARIO_NAME})
    splits = 2 if spec.get("holdout_start") else 1
    return arms * len(spec.get("symbols") or []) * splits


def estimate_seconds(spec: Dict[str, Any]) -> Dict[str, float]:
    """Pre-flight wall-clock estimate, and its two components.

    ``symbols x splits x MATERIALISE_SECONDS_PER_SYMBOL`` plus
    ``cells x REPLAY_SECONDS_PER_CELL``. The materialisation term dominates on
    every realistic spec, which is exactly why the ESTIMATE is the gate rather
    than the cell count: twenty cheap arms over two symbols is a fast request
    and two arms over twelve symbols is not, and a cell count cannot tell them
    apart.
    """
    symbols = len(spec.get("symbols") or [])
    splits = 2 if spec.get("holdout_start") else 1
    materialise = symbols * splits * MATERIALISE_SECONDS_PER_SYMBOL
    replay = cell_count(spec) * REPLAY_SECONDS_PER_CELL
    return {"materialise_seconds": round(materialise, 1),
            "replay_seconds": round(replay, 1),
            "total_seconds": round(materialise + replay, 1)}


def check_budget(spec: Dict[str, Any]) -> Dict[str, float]:
    """Refuse a spec too big for an interactive request. Returns the estimate."""
    cells = cell_count(spec)
    if cells > SIM_MAX_CELLS:
        raise SpecRefused(
            409,
            f"{cells} cells exceeds this service's cap of {SIM_MAX_CELLS}. Use "
            f"the batch path: submit it to the `backtest-sweep` Job "
            f"(POST /api/v2/sweeps), which has a 3-hour task timeout.",
            cells=cells, max_cells=SIM_MAX_CELLS)
    estimate = estimate_seconds(spec)
    if estimate["total_seconds"] > SIM_MAX_ESTIMATE_SECONDS:
        splits = 2 if spec.get("holdout_start") else 1
        raise SpecRefused(
            409,
            f"estimated {estimate['total_seconds']:.0f}s "
            f"({estimate['materialise_seconds']:.0f}s materialising "
            f"{len(spec.get('symbols') or [])} symbol(s) x {splits} split(s) at "
            f"{MATERIALISE_SECONDS_PER_SYMBOL:.0f}s each, plus "
            f"{estimate['replay_seconds']:.0f}s replaying {cells} cell(s)), "
            f"over this service's {SIM_MAX_ESTIMATE_SECONDS:.0f}s interactive "
            f"budget. Use the batch path: submit it to the `backtest-sweep` Job "
            f"(POST /api/v2/sweeps), which has a 3-hour task timeout.",
            cells=cells, **estimate)
    return estimate


def check_coverage(chain_store, spec: Dict[str, Any], start: date,
                   end: date) -> None:
    """Refuse a spec the chain lake cannot answer without a vendor fetch."""
    from src.backtesting.data.lake_coverage import check_coverage as _check

    report = _check(getattr(chain_store, "lake", None),
                    spec.get("symbols") or [], start, end)
    if not report.complete:
        raise SpecRefused(
            409, report.describe(),
            sessions=report.sessions, weekdays=report.weekdays,
            missing_symbol_days=report.missing_symbol_days,
            missing={sym: [d.isoformat() for d in days]
                     for sym, days in report.missing.items()})


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #
class _Run:
    """One accepted simulation, and everything its terminal row needs.

    Held in ``_CURRENT`` for exactly as long as the replay is in flight, so the
    SIGTERM handler — which runs on the MAIN thread while the replay is on the
    worker — can terminalise it. ``finalise`` is idempotent under
    ``_terminal_lock``: the worker's ``finally`` and the signal handler race by
    construction, and two terminal rows for one run would let a ``failed``
    written after a ``done`` mask it, because readers take the latest row by
    ``written_at``.
    """

    def __init__(self, *, run_id: str, provenance: Dict[str, Any],
                 writer: Any, artifact_writer: Any, started_at: str,
                 spec: Dict[str, Any], scenarios, start: date, end: date,
                 holdout_start: Optional[date], identity: Optional[str],
                 git_commit: Optional[str], submitted_at: str) -> None:
        self.run_id = run_id
        self.provenance = provenance
        self.writer = writer
        self.artifact_writer = artifact_writer
        self.started_at = started_at
        self.submitted_at = submitted_at
        self.spec = spec
        self.scenarios = scenarios
        self.start = start
        self.end = end
        self.holdout_start = holdout_start
        self.identity = identity
        self.git_commit = git_commit
        self.result = None
        self.chain_store = None
        self.failure: Optional[str] = None
        self._terminal_lock = threading.Lock()
        self.terminalised = False

    def finalise(self, *, failure: Optional[str] = None) -> None:
        """Write the cells and the terminal status row. At most once.

        Delegates to ``main._finalise_sweep_status`` — the Job's own terminal
        writer, SIG_IGN window included — rather than restating its rules. Those
        rules are subtle and have been fixed twice: ``done`` requires the cell
        rows to have LANDED, the lake summary and the cell write are guarded
        independently, and ``artifacts_complete`` distinguishes three states. A
        second implementation would drift from the one the dashboard reads.
        """
        import main as cli
        from src.backtesting.screen import ENGINE_VERSION

        with self._terminal_lock:
            if self.terminalised:
                return
            self.terminalised = True
        cli._finalise_sweep_status(
            writer=self.writer, logger=logger, result=self.result,
            failure=failure or self.failure, chain_store=self.chain_store,
            started_at=self.started_at, run_id=self.run_id,
            submitted_at=self.submitted_at, engine_version=ENGINE_VERSION,
            git_commit=self.git_commit, engine_identity=self.identity,
            provenance=self.provenance, deduplicated_to=None,
            artifact_writer=self.artifact_writer,
        )


def _replay(run: "_Run") -> None:
    """The worker-thread body. Never raises out; always terminalises.

    Runs on ONE thread for the life of a run, and the rejection tally is entered
    on that same thread by ``Simulator.replay`` — so the tally's contextvar
    binding and the events it counts share a context. A tally entered on one
    thread is invisible to another by design (``utils.logger._ACTIVE_TALLY`` is
    a ``ContextVar``); that detachment is the right answer for a service and is
    asserted, both directions, by ``tests/test_sim_service.py``.
    """
    from src.backtesting.data.chain_store import ChainStore
    from src.backtesting.data.fetch_guard import ChainFetchRefusingProvider
    from src.backtesting.scenarios import run_sweep

    began = time.perf_counter()
    try:
        run.chain_store = ChainStore.from_env()
        run.result = run_sweep(
            get_config(), run.scenarios, run.spec["symbols"],
            run.start, run.end,
            holdout_start=run.holdout_start,
            starting_cash=run.spec["starting_cash"],
            run_sensitivity=False,
            chain_store=run.chain_store,
            artifact_sink=(run.artifact_writer.write
                           if run.artifact_writer is not None
                           and run.artifact_writer.enabled else None),
            run_id=run.run_id,
            engine_identity=run.identity,
            vendor_guard=lambda provider: ChainFetchRefusingProvider(
                provider, run_id=run.run_id),
            quiet_exempt=(__name__,),
        )
    except BaseException as exc:  # noqa: BLE001 - recorded, never propagated
        # Nothing above this frame can report it: the request that started this
        # replay returned its 202 long ago, and an exception escaping a worker
        # thread prints a traceback to stderr and is otherwise lost. The row is
        # the only channel the operator has.
        run.failure = f"{type(exc).__name__}: {exc}"
        logger.error(
            "Simulation FAILED",
            event_category="backtest", event_type="sim_run_failed",
            run_id=run.run_id, error=run.failure[:300])
    finally:
        try:
            run.finalise()
        except Exception:  # noqa: BLE001 - nothing left to fall back to
            logger.error(
                "Terminal sim status row could not be written — this run will "
                "read as `running` until its liveness bound expires",
                event_category="backtest",
                event_type="sim_status_write_failed",
                run_id=run.run_id, exc_info=True)
        _release(run)
        logger.info(
            "Simulation finished",
            event_category="backtest", event_type="sim_run_finished",
            run_id=run.run_id, seconds=round(time.perf_counter() - began, 2),
            failed=run.failure is not None)


def _release(run: Optional["_Run"]) -> None:
    """Clear the in-flight run and free the worker slot. Idempotent."""
    global _CURRENT
    with _STATE_LOCK:
        if run is None or _CURRENT is run:
            _CURRENT = None
    if _RUN_LOCK.locked():
        try:
            _RUN_LOCK.release()
        except RuntimeError:  # pragma: no cover - already released
            pass


# --------------------------------------------------------------------------- #
# SIGTERM: the Job's discipline, adapted to a service
# --------------------------------------------------------------------------- #
_PREVIOUS_SIGTERM: Any = None


def _on_sigterm(signum, frame):
    """Terminalise the in-flight run before this instance disappears.

    Cloud Run sends SIGTERM and SIGKILL ten seconds later — on a scale-in, on a
    new revision taking traffic, on an eviction. The Job turns that signal into
    an exception so its ``finally`` runs; **a service cannot**, because the
    signal is delivered on the main thread while the replay is on the worker,
    and Python has no supported way to raise into another thread. So the handler
    does the ``finally``'s job itself: it writes the terminal row here, on the
    main thread, inside a SIG_IGN window so a second signal cannot kill the
    process mid-insert.

    Then it chains to whatever handler was installed before it — uvicorn's, in
    production — so the graceful shutdown this replaces none of still runs.

    ``_Run.finalise`` is idempotent, so the worker's own ``finally`` losing this
    race writes nothing further. The row says ``failed``, which is the truth: the
    replay did not finish and its cells are not in the table.
    """
    import main as cli

    with _STATE_LOCK:
        run = _CURRENT
    if run is not None:
        logger.warning(
            "SIGTERM received — recording a terminal sim status before the "
            "instance is killed",
            event_category="backtest", event_type="sim_sigterm",
            signal=signum, run_id=run.run_id)
        with cli.ignore_sigterm_while_finalising(logger, what="sim"):
            try:
                run.finalise(failure=(
                    f"the instance received signal {signum} (Cloud Run sends "
                    f"SIGTERM then SIGKILL 10s later): scale-in, a new revision "
                    f"taking traffic, or an eviction. The replay did not "
                    f"finish; resubmit."))
            except Exception:  # noqa: BLE001 - nothing left to fall back to
                logger.error(
                    "Terminal sim status row could not be written on SIGTERM",
                    event_category="backtest",
                    event_type="sim_status_write_failed",
                    run_id=run.run_id, exc_info=True)
    previous = _PREVIOUS_SIGTERM
    if callable(previous):
        previous(signum, frame)


def install_sigterm_handler() -> bool:
    """Arm ``_on_sigterm``, chaining to whatever is already installed.

    Called from the app's startup hook — uvicorn installs its own handlers
    before startup runs, so the previous handler captured here is uvicorn's and
    the graceful shutdown is preserved rather than replaced.

    No-ops off the main thread, or on a platform without SIGTERM
    (``signal.signal`` raises there), exactly as ``main.terminate_on_sigterm``
    does, so tests and library callers are unaffected. Returns whether it armed.
    """
    global _PREVIOUS_SIGTERM
    try:
        _PREVIOUS_SIGTERM = signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, AttributeError):
        _PREVIOUS_SIGTERM = None
        return False
    return True


def restore_sigterm_handler() -> None:
    """Put back whatever was there before ``install_sigterm_handler``."""
    global _PREVIOUS_SIGTERM
    previous = _PREVIOUS_SIGTERM
    _PREVIOUS_SIGTERM = None
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
def create_app():
    """Build the FastAPI app. A function, so importing this module is cheap."""
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI(title="options-wheel sim service", docs_url=None,
                  redoc_url=None)

    @app.on_event("startup")
    def _startup() -> None:
        install_sigterm_handler()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        restore_sigterm_handler()

    @app.get("/health")
    def health() -> Dict[str, Any]:
        """Identity and liveness. Never touches BigQuery, GCS or the vendor.

        ``engine_identity`` is the value the deploy smoke test and the rollout
        parity check read: it must equal the ``ENGINE_IDENTITY`` baked into the
        dashboard image from the same commit, or the two sides are keying
        different trees and the dedup silently stops firing.

        A sync ``def``, like every handler here, so a health check answers while
        the worker thread is mid-replay.
        """
        from src.backtesting.scenarios.engine_identity import engine_identity
        from src.backtesting.screen import ENGINE_VERSION

        with _STATE_LOCK:
            current = _CURRENT
        return {
            "status": "ok",
            "engine_identity": engine_identity(),
            "engine_version": ENGINE_VERSION,
            "git_commit": os.environ.get("GIT_COMMIT") or None,
            "busy": current is not None,
            "run_id": current.run_id if current is not None else None,
        }

    @app.post("/simulate")
    def simulate(spec: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Validate, guard, dedup, and either answer or accept.

        * **422** — a spec the engine would refuse, with the engine's own reason.
        * **409** — over the interactive budget, or the chain lake cannot cover
          the window, or this instance is already replaying.
        * **503** — the sweep store is unavailable, so there is nowhere to put
          the answer this endpoint promises to persist.
        * **200** ``{run_id, deduplicated: true}`` — an identical spec already
          completed under this engine identity. Nothing is replayed and NO rows
          are written; ``run_id`` is the PRIOR run's, which is what the console
          then GETs through the dashboard.
        * **202** ``{run_id}`` — accepted. The replay is running on this
          instance's worker thread; poll ``GET /api/v2/sweeps/{run_id}``.

        The order is deliberate: everything that can refuse runs BEFORE any row
        is written, so a refused spec leaves nothing behind to terminalise and
        nothing holding a lock.
        """
        try:
            return _simulate(spec)
        except SpecRefused as exc:
            return JSONResponse(status_code=exc.status,
                                content={"detail": exc.detail, **exc.extra})
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - never a bare 500
            logger.error("sim submit failed unexpectedly",
                         event_category="backtest",
                         event_type="sim_submit_failed", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"detail": f"the sim service failed to accept this "
                                   f"spec: {type(exc).__name__}: {exc}"})

    return app


def _simulate(raw_spec: Dict[str, Any]):
    """``POST /simulate``'s body, outside the app so it is directly testable."""
    from fastapi.responses import JSONResponse

    from src.backtesting.data.chain_store import ChainStore
    from src.backtesting.reporting.artifact_store import ArtifactWriter
    from src.backtesting.reporting.bq_writer import config_hash
    from src.backtesting.scenarios import persist as sweep_store
    from src.backtesting.scenarios.engine_identity import engine_identity
    from src.backtesting.scenarios.identity import sweep_key as compute_sweep_key
    from src.backtesting.screen import ENGINE_VERSION

    global _CURRENT, _WORKER

    parsed = normalise_spec(raw_spec)
    spec = parsed["spec"]
    check_budget(spec)

    # The lake, for the pre-flight only. `ChainStore.from_env()` builds a GCS
    # client and probes the bucket, so it is inside the request rather than at
    # import time: a broken lake must be a 409 with a reason, not a service that
    # fails to start.
    store = ChainStore.from_env()
    check_coverage(store, spec, parsed["start"], parsed["end"])

    writer = get_writer()
    if not writer.enabled:
        raise SpecRefused(
            503,
            "the scenario store is unavailable (no BigQuery client, no GCP "
            "project, or the tables could not be reconciled). Refusing: this "
            "endpoint's whole answer is a run_id to poll, and there would be "
            "nothing to poll.")

    identity = engine_identity()
    key = compute_sweep_key(spec, engine_version=ENGINE_VERSION,
                            engine_identity=identity)
    snapshot = sweep_store.base_config_snapshot(get_config())
    effective_hash = sweep_store.base_config_hash(snapshot)

    if not parsed["force"]:
        prior = writer.find_done_sweep(
            key, base_config_hash=effective_hash, engine_identity=identity)
        if prior is not None:
            # NO ROWS. A dedup hit is not a new run: writing a `deduplicated`
            # row here would put a second submission in the store for a question
            # already answered. The console reads the PRIOR run through the
            # dashboard, which shapes it — which is also what keeps the shaper
            # from being duplicated into the engine image.
            logger.info(
                "Simulation deduplicated against a completed run",
                event_category="backtest", event_type="sim_deduplicated",
                sweep_key=key, run_id=prior["run_id"])
            return JSONResponse(
                status_code=200,
                content={"run_id": prior["run_id"], "deduplicated": True,
                         "sweep_key": key})

    # THE SLOT, taken before any row is written so a `running` row can never
    # exist for a replay that was refused for being second. Released by
    # `_replay`'s `finally`, whatever happens inside it.
    if not _RUN_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            busy = _CURRENT
        busy_id = busy.run_id if busy is not None else None
        raise SpecRefused(
            409,
            f"this instance is already replaying {busy_id or 'a simulation'}. "
            f"The engine mutates process-global state during a replay, so "
            f"exactly one runs at a time; wait for it (poll "
            f"GET /api/v2/sweeps/{busy_id or '<run_id>'}) or submit to the "
            f"`backtest-sweep` Job.",
            run_id=busy_id)

    run = None
    try:
        run_id = uuid.uuid4().hex[:16]
        submitted_at = datetime.now(timezone.utc).isoformat()
        started_at = submitted_at
        git_commit = os.environ.get("GIT_COMMIT") or None
        provenance = dict(
            run_id=run_id,
            submitted_at=submitted_at,
            sweep_key=key,
            submitted_via=SUBMITTED_VIA,
            engine_version=ENGINE_VERSION,
            git_commit=git_commit,
            engine_identity=identity,
            execution_name=os.environ.get("K_REVISION"),
            spec=spec,
            base_config=snapshot,
            base_config_hash=effective_hash,
            engine_config_hash=config_hash(get_config()),
            # Read by `services/sweeps.row_liveness_seconds`. It rides in the
            # provenance dict so EVERY row of this run carries it: `submitted`,
            # `running` and the terminal one all go through
            # `status_row(**provenance)`.
            liveness_seconds=LIVENESS_SECONDS,
        )
        artifact_writer = ArtifactWriter(run_id)
        if not artifact_writer.enabled:
            logger.warning(
                "Detail artifacts are switched off for this run (no artifact "
                "bucket configured); results are unaffected",
                event_category="backtest", event_type="sim_artifacts_disabled",
                run_id=run_id)

        run = _Run(
            run_id=run_id, provenance=provenance, writer=writer,
            artifact_writer=artifact_writer, started_at=started_at,
            spec=spec, scenarios=parsed["scenarios"], start=parsed["start"],
            end=parsed["end"], holdout_start=parsed["holdout_start"],
            identity=identity, git_commit=git_commit,
            submitted_at=submitted_at)

        # `submitted` THEN `running`, both before the worker starts. The pair is
        # what the dashboard's history reads: a `submitted` row with no
        # `running` row after ten minutes is its "stuck" signal, and a writer
        # that produced only one of the two would be a shape the readers have
        # never seen from the other writer.
        writer.write_status(sweep_store.status_row(
            status=sweep_store.STATUS_SUBMITTED, **provenance))
        writer.write_status(sweep_store.status_row(
            status=sweep_store.STATUS_RUNNING, started_at=started_at,
            **provenance))

        with _STATE_LOCK:
            _CURRENT = run
        worker = threading.Thread(target=_replay, args=(run,),
                                  name=f"sim-replay-{run_id}", daemon=True)
        _WORKER = worker
        worker.start()
    except BaseException:
        # Anything between taking the slot and starting the thread: give the
        # slot back, or this instance refuses every later request with a 409
        # naming a run that is not running.
        _release(run)
        raise

    logger.info(
        "Simulation accepted",
        event_category="backtest", event_type="sim_accepted",
        run_id=run_id, sweep_key=key, symbols=spec["symbols"],
        cells=cell_count(spec), **estimate_seconds(spec))
    return JSONResponse(
        status_code=202,
        content={"run_id": run_id, "status": sweep_store.STATUS_RUNNING,
                 "sweep_key": key, "deduplicated": False,
                 "cell_count": cell_count(spec),
                 "liveness_seconds": LIVENESS_SECONDS,
                 **estimate_seconds(spec)})


def main() -> None:  # pragma: no cover - container entry point
    """Serve. Port from ``PORT`` (Cloud Run); one worker process by construction."""
    import uvicorn

    from src.utils.logger import setup_logging

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), log_to_file=False)
    uvicorn.run(create_app(), host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()
