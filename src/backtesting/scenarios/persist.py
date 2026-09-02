"""The scenario store — the tables a sweep may write, and none of them is the screen's.

FC-060 Layer 3. Modelled deliberately closely on
``src/backtesting/reporting/bq_writer.py`` (additive schema reconcile, streaming
insert, a write whose failure is *reported* rather than swallowed) so an operator
reading one understands the other. What is emphatically NOT shared is the table:

    **A sweep never writes to ``backtest_runs``.** That table's documented
    "current demotion candidates" query takes the latest ``run_kind='full'`` row
    (``docs/bigquery/backtest_runs.md``), so a persisted full-universe sweep with
    that shape would displace a real screen with a hypothetical. The tables here
    have no ``run_kind`` column at all, which makes the mistake unrepresentable
    rather than merely discouraged.

Two tables, because a sweep has two grains and collapsing them loses one of them:

* ``scenario_sweeps`` — one row per **status transition** of one submission.
  Insert-only: ``submitted`` (written by the dashboard), ``running`` /
  ``done`` / ``failed`` (written by the Job), ``deduplicated`` (written by
  whichever side notices). Readers take the latest row per ``run_id``. An
  UPDATE-in-place table would have been smaller and would have destroyed the
  timeline that answers "did the Job ever start?" — the single most useful
  question when a sweep looks stuck.
* ``scenario_runs`` — one row per **cell**: scenario x symbol x split. Every
  ``ScenarioResult`` field, plus the three partition flags (``insufficient`` /
  ``low_activity`` / ``measured``) computed here rather than left to each
  reader. They are properties on the dataclass, not fields, and a reader that
  re-derived ``low_activity`` from ``days_in_position_fraction`` would have to
  know ``MIN_DAYS_IN_POSITION`` and the ``insufficient``-wins precedence. One of
  them would eventually get it wrong, and a mis-classified cell is a fabricated
  return.

A third table joined them in FC-096 Phase B B4, at a different grain again:

* ``scenario_pins`` — one row per **state transition of a pin**: a combination
  an operator asked the weekly battery to keep re-measuring. Insert-only and
  latest-row-wins like ``scenario_sweeps``, so un-pinning is a row with
  ``active = FALSE`` rather than a delete, and "what was pinned last quarter"
  stays answerable. It is not a sweep and never blocks one: a dataset whose pin
  table cannot be created still persists every sweep (see ``__init__``).

**Ordering within a run_id is by ``written_at``, never ``submitted_at``.** Every
row of one submission carries the SAME ``submitted_at`` (it is the partition key
and the submission's identity), so ordering by it is a three-way tie and "latest
status wins" would resolve arbitrarily. ``written_at`` is stamped per row at
insert. ``STATUS_RANK`` is the deterministic tiebreak for the pathological case
of two rows sharing a microsecond.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

try:  # pragma: no cover - exercised only where the dep is installed
    from google.cloud import bigquery
    _HAS_BIGQUERY = True
except ImportError:  # pragma: no cover
    _HAS_BIGQUERY = False

SWEEPS_TABLE = "scenario_sweeps"
RUNS_TABLE = "scenario_runs"
# FC-096 Phase B B4. The third table: combinations an operator asked to have
# re-measured every week. Insert-only and latest-row-wins, exactly like
# `scenario_sweeps` — "delete" is a row with `active = FALSE`, so the history of
# what was pinned and when is never destroyed by an un-pin. The battery reads
# the latest row per `pin_id` and runs the ones that are still active.
PINS_TABLE = "scenario_pins"

# The status vocabulary. `submitted` is the dashboard's; the rest are the Job's,
# except `deduplicated`, which either side may write depending on which one
# noticed the prior `done` run first.
STATUS_SUBMITTED = "submitted"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DEDUPLICATED = "deduplicated"

STATUSES = (
    STATUS_SUBMITTED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED,
    STATUS_DEDUPLICATED,
)

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_DEDUPLICATED)

# Tiebreak for two rows of one run_id sharing a `written_at`. Higher wins. Only
# reachable if two writers stamp the same microsecond, which is why it exists at
# all: a reader that resolved such a tie by row order would report a finished
# sweep as still running roughly half the time.
#
# `done` outranks `failed` DELIBERATELY. The one realistic collision is the
# launch path: the API writes a `failed` row when `jobs.run` errors, and an
# execution that in fact started can write `done` in the same microsecond. A
# sweep whose cells are in the table is done, whatever a launch-side timeout
# thought. The reverse ranking would hide a completed run behind a spurious
# failure — and, worse, keep it out of the dedup so it would be replayed.
# How long a NON-TERMINAL row may go without an update before a reader may
# presume its writer is dead. Both numbers MIRROR `services/sweeps.py`
# (`JOB_TASK_TIMEOUT_SECONDS` / `STALE_GRACE_MINUTES`), and a test pins them
# equal: the dashboard decides when a row stops holding the submit lock and this
# module decides when it stops blocking a duplicate, and the two answering
# differently is how a sweep gets refused by one side after the other released
# it.
#
# `DEFAULT_LIVENESS_SECONDS` is what a row with no `liveness_seconds` MEANS —
# every row written before that column existed, and every row the sweep JOB
# writes, because Cloud Run kills the task at its `--task-timeout` and nothing
# will write its terminal row afterwards. Only a writer with a SHORTER life
# stamps a value; the sim service stamps 900.
DEFAULT_LIVENESS_SECONDS = 10_800   # 3h — the Job's --task-timeout
LIVENESS_GRACE_SECONDS = 600        # 10m — the terminal-write window

STATUS_RANK: Dict[str, int] = {
    STATUS_SUBMITTED: 0,
    STATUS_RUNNING: 1,
    STATUS_DEDUPLICATED: 2,
    STATUS_FAILED: 3,
    STATUS_DONE: 4,
}


# The ORDER BY that resolves "latest status wins" for one ``run_id``. Insert-only
# rows share ``submitted_at`` (the partition key), so ``written_at`` is the real
# clock and ``STATUS_RANK`` breaks a same-microsecond tie deterministically.
#
# The dashboard builds the same clause in ``services/sweeps.py``; it cannot
# import this module (that package's ``__init__`` pulls the whole engine), so the
# two are pinned equal by a test rather than shared. A silent divergence would
# make the API report a finished sweep as still running.
LATEST_STATUS_ORDER_BY = (
    "written_at DESC, "
    "CASE status "
    + " ".join(f"WHEN '{status}' THEN {rank}"
               for status, rank in sorted(STATUS_RANK.items()))
    + " ELSE -1 END DESC"
)


# FC-096 Phase B B4 — pins.
#
# How many pins may be ACTIVE at once (FC-096 D1: "capped ~20"). The cap is a
# WRITE-time rule the dashboard enforces; the battery does not silently drop
# pins beyond it, because dropping measurement quietly is worse than running
# long and the wall cap (`main.BATTERY_MAX_SECONDS`) already bounds the Saturday.
# `services/sweeps.MAX_ACTIVE_PINS` is pinned equal to this by a test.
MAX_ACTIVE_PINS = 20

# A pin's note is a reminder to its author ("the delta band Ed asked about"),
# not a document. Bounded here as well as at the API boundary because this
# module builds the row: a note is operator-typed text on a row a public
# dashboard renders, and an unbounded one is a paste of a whole spec.
PIN_NOTE_MAX_CHARS = 200

# "Latest row wins" for one ``pin_id``. ``written_at`` is the clock, and the
# tiebreak on a shared microsecond is `active ASC` — INACTIVE wins.
#
# That direction is deliberate: the only realistic collision is a create and a
# delete landing in the same microsecond, and resolving it towards "still
# pinned" would leave a pin the operator deleted running every Saturday for
# ever. Resolving it towards "deleted" costs one re-pin, which the operator will
# notice immediately because the pin is missing from the list.
#
# Mirrored in ``services/sweeps.PINS_LATEST_ORDER_BY`` (the dashboard cannot
# import this module) and pinned equal by a test, for `LATEST_STATUS_ORDER_BY`'s
# reason: two sides disagreeing about which row is current would make the API
# show a pin the battery does not run, or hide one it does.
PINS_LATEST_ORDER_BY = "written_at DESC, active ASC"


# BigQuery speaks TWO names for the same type, and the API answers in the older
# one whatever you asked for. A table created with `BOOL` reads back as
# `BOOLEAN`; `INT64` reads back as `INTEGER`; `FLOAT64` reads back as `FLOAT`.
#
# That cost a production execution. `_ensure_table`'s type guard compared the
# echoed name against the declared one, and the schemas below happen to declare
# `BOOL` (standard) alongside `INTEGER` and `FLOAT` (legacy) — so INTEGER and
# FLOAT matched by luck and `in_sample_only` did not. The first real
# `backtest-sweep` execution aborted at writer init on a table whose schema was
# in fact correct, and the Job then refused to replay. (The fail-closed posture
# was right; the guard was wrong.)
#
# Both sides are canonicalised to the standard name before comparison, so the
# guard fires only on a GENUINE retype. The declared strings are deliberately
# left as they are: the client translates them on the way out, `scenario_sweeps`
# already exists with the resulting types, and rewriting them would risk
# changing what `create_table` sends for `scenario_runs`, which does not exist
# yet and must come out compatible.
_CANONICAL_FIELD_TYPES: Dict[str, str] = {
    "BOOLEAN": "BOOL",
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "RECORD": "STRUCT",
}


def _canonical_type(field_type: Optional[str]) -> str:
    """The standard-SQL spelling of a BigQuery type name.

    Unknown names pass through upper-cased rather than raising: an unmapped type
    still compares equal to itself, so a future field cannot be waved through by
    accident — and `TestTheSchemaTypeNamesAreCovered` fails the build if the
    schemas ever declare one this map does not know.
    """
    name = (field_type or "").upper()
    return _CANONICAL_FIELD_TYPES.get(name, name)


def _canonical_mode(mode: Optional[str]) -> str:
    """``NULLABLE`` when unset, upper-cased otherwise."""
    return (mode or "NULLABLE").upper()


def _sweeps_schema():
    """One row per status transition of one submitted sweep. See D1."""
    f = bigquery.SchemaField
    return [
        # Identity
        f("run_id", "STRING", mode="REQUIRED"),
        f("sweep_key", "STRING"),
        f("status", "STRING", mode="REQUIRED"),
        f("deduplicated_to", "STRING"),
        # Timing. `submitted_at` is the PARTITION key and is identical on every
        # row of one run; `written_at` is what orders them.
        f("submitted_at", "TIMESTAMP", mode="REQUIRED"),
        f("written_at", "TIMESTAMP", mode="REQUIRED"),
        f("started_at", "TIMESTAMP"),
        f("finished_at", "TIMESTAMP"),
        # Provenance
        f("submitted_via", "STRING"),      # 'dashboard' | 'cli' | 'sim-service'
        f("execution_name", "STRING"),     # CLOUD_RUN_EXECUTION, for debugging
        # FC-096 Phase B B3. How long a NON-TERMINAL row of this run may go
        # without an update before a reader may declare it dead, in seconds.
        # Additive and NULLABLE; NULL means "use the Job's clock"
        # (`services/sweeps.JOB_TASK_TIMEOUT_SECONDS`, 3 h), which is what every
        # row written before this column existed means and what every Job row
        # still means.
        #
        # It exists because the sim service is NOT the Job. A Cloud Run service
        # instance that is scaled in mid-replay writes no terminal row, and the
        # Job's 3 h clock would hold the one-at-a-time submit lock for three
        # hours and ten minutes on a run whose process died in seconds. The
        # service stamps 900 s, so the lock releases in ~25 min
        # (900 s + the reader's existing 10-minute grace).
        #
        # Stamped on the row rather than inferred from `submitted_via` on
        # purpose: the reader must not have to know the deployment topology of
        # every writer, and a future writer with a different lifetime says so
        # in its rows instead of teaching the reader a fourth special case.
        f("liveness_seconds", "INTEGER"),
        f("git_commit", "STRING"),
        f("engine_version", "STRING"),
        # FC-096 Phase B: the content hash of `src/**` (`engine_identity.py`),
        # and HALF THE DEDUP KEY since it replaced `git_commit` there. Additive
        # and NULLABLE: `_ensure_table`'s reconcile adds it to the live table,
        # and the rows that predate it stay NULL. That is the correct migration
        # posture rather than a defect — a NULL row can never satisfy
        # `engine_identity = @engine_identity`, so legacy results are readable
        # by run_id and unreachable through the new key, which is exactly right:
        # they were keyed by a commit, and nothing can prove which source tree
        # produced them.
        f("engine_identity", "STRING"),
        f("base_config_hash", "STRING"),
        # The PAYLOAD, not just the hash: a hash proves two runs used the same
        # config, and tells a reader nothing about what that config was.
        f("base_config_json", "STRING"),
        f("spec_json", "STRING"),
        # Scope, denormalised out of spec_json so it is queryable without
        # JSON_VALUE on every row.
        f("symbols", "STRING", mode="REPEATED"),
        f("window_start", "DATE"),
        f("window_end", "DATE"),
        f("holdout_start", "DATE"),
        f("in_sample_only", "BOOL"),
        f("scenario_count", "INTEGER"),
        f("cell_count", "INTEGER"),
        # Cost
        f("wall_seconds", "FLOAT"),
        f("materialise_seconds", "FLOAT"),
        f("replay_seconds", "FLOAT"),
        f("provider_fetches", "INTEGER"),
        f("bar_cache_hits", "INTEGER"),
        f("lake_summary_json", "STRING"),
        f("error", "STRING"),
        # Completeness. `done` is not "the process exited" — it is "the cells are
        # in the table and every one of them was measured". Both numbers are on
        # the row because the DEDUP reads them: returning a prior run whose rows
        # never landed, or whose cells errored, would serve an absence as a
        # result. See `find_done_sweep`.
        f("rows_persisted", "INTEGER"),
        f("error_cells", "INTEGER"),
        # FC-096 Phase B B2. Whether EVERY non-errored cell of this run also
        # landed its detail artifact in GCS. Additive and NULLABLE, and the
        # three states are genuinely different: TRUE = the console can open any
        # cell of this run; FALSE = some cells have no evidence and the operator
        # should not read an empty ledger as "nothing happened"; NULL = this run
        # wrote no artifacts at all (a CLI run without `--persist`, or a row
        # written before the column existed), which is not a defect.
        #
        # An ERRORED cell is excluded from both sides of the comparison on
        # purpose: it produced no replay, so it has nothing to serialise, and
        # counting it would make every partially-failed sweep report incomplete
        # artifacts as well — two different problems reported as one.
        f("artifacts_complete", "BOOL"),
        # FC-096 A4. Symbols the FC-013 earnings gate could not gate because
        # they are absent from the committed table — JSON array text, NULLABLE,
        # NULL on every non-terminal row and on runs written before the column
        # existed. Stored rather than re-derived because it is a property of the
        # RUN's table snapshot, not of the spec: a symbol onboarded today and
        # refreshed tomorrow would have the same spec and a different answer.
        # Additive; `ensure_tables`' reconcile adds it to an existing table.
        f("earnings_symbols_without_data", "STRING"),
        # `bq_writer.config_hash` — nine strategy keys plus the scoring
        # constants. Kept SEPARATE from `base_config_hash` (which is the hash of
        # the whole EFFECTIVE snapshot) because the two answer different
        # questions: this one lines a sweep row up with a `backtest_runs` row;
        # that one decides whether two sweeps ran the same engine configuration.
        f("engine_config_hash", "STRING"),
        # FC-096 Phase B B4. Which PIN this run re-measured, or NULL — which is
        # what every non-battery row means, and what every row written before
        # this column existed means. Additive and NULLABLE.
        #
        # Stored rather than joined for two reasons. It is what makes a pin's
        # weekly history queryable at all (`recent_pin_statuses`), which is what
        # the 3-week nag counts; and a pin's SPEC can be re-created under a new
        # `pin_id`, so `sweep_key` is not a substitute — two pins can legally
        # ask the same question, and the operator who pinned one of them is the
        # one the nag is addressed to.
        f("pin_id", "STRING"),
    ]


def _pins_schema():
    """One row per state transition of one pin (FC-096 Phase B B4).

    Five columns and no more. A pin is a spec plus a note plus a bit saying
    whether the battery should still run it; everything else about what happened
    to it lives on the `scenario_sweeps` rows the battery writes, keyed by
    ``pin_id``. Putting a failure counter here instead would mean the battery
    UPDATES this table, and an insert-only table that one writer updates is the
    worst of both.

    ``spec_json`` is the DASHBOARD-normalised spec (``validate_spec``'s output,
    ``sort_keys=True``), which is what makes "two active pins with the same
    spec" answerable by string equality: the normaliser is the one canonical
    form in this system, so a pin submitted with the symbols in a different
    order is the same string here.
    """
    f = bigquery.SchemaField
    return [
        f("pin_id", "STRING", mode="REQUIRED"),
        f("spec_json", "STRING", mode="REQUIRED"),
        # REQUIRED, not "NULL means active": a pin whose current state is
        # unreadable must not default to "run it every week for ever".
        f("active", "BOOL", mode="REQUIRED"),
        f("written_at", "TIMESTAMP", mode="REQUIRED"),
        f("note", "STRING"),
    ]


def _runs_schema():
    """One row per cell: scenario x symbol x split. See D1."""
    f = bigquery.SchemaField
    return [
        f("run_id", "STRING", mode="REQUIRED"),
        f("submitted_at", "TIMESTAMP", mode="REQUIRED"),
        f("written_at", "TIMESTAMP"),
        # Arm
        f("scenario_name", "STRING"),
        f("scenario_hash", "STRING"),
        f("config_hash", "STRING"),
        f("overrides_json", "STRING"),
        f("fill_haircut", "FLOAT"),
        # Cell
        f("symbol", "STRING"),
        f("split", "STRING"),
        f("window_start", "DATE"),
        f("window_end", "DATE"),
        # Verdict
        f("verdict", "STRING"),
        f("demote", "BOOL"),
        # The cell-state partition, computed once here. `measured` is the only
        # state that carries a number worth ranking; the other three are counted
        # and never averaged (see report.py).
        f("insufficient", "BOOL"),
        f("low_activity", "BOOL"),
        f("measured", "BOOL"),
        # Performance
        f("total_return", "FLOAT"),
        f("annualized_return", "FLOAT"),
        f("annualized_return_on_collateral", "FLOAT"),
        f("benchmark_return", "FLOAT"),
        f("excess_return", "FLOAT"),
        f("option_pnl", "FLOAT"),
        f("stock_pnl_realized", "FLOAT"),
        f("stock_pnl_unrealized", "FLOAT"),
        f("max_drawdown", "FLOAT"),
        f("win_rate", "FLOAT"),
        f("assignment_rate", "FLOAT"),
        # Activity
        f("puts_sold", "INTEGER"),
        f("calls_sold", "INTEGER"),
        f("cycles_completed", "INTEGER"),
        f("cycles_open", "INTEGER"),
        f("decision_days", "INTEGER"),
        f("days_in_position_fraction", "FLOAT"),
        # Fill sensitivity — a verdict that flips is not a verdict
        f("bid_fill_return", "FLOAT"),
        f("verdict_flips_on_fill", "BOOL"),
        # Cost and failure
        f("replay_seconds", "FLOAT"),
        f("error", "STRING"),
        # Provenance, repeated on every cell so one row is self-describing
        f("engine_version", "STRING"),
        f("git_commit", "STRING"),
        f("engine_identity", "STRING"),
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_date(value) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class ScenarioRunWriter:
    """Writes ``scenario_sweeps`` / ``scenario_runs``. Insert-only, never updates.

    Construction is the same shape as ``BacktestRunWriter``: it never raises, it
    logs loudly when it disables itself, and ``enabled`` says which it is. A
    sweep whose store is unavailable must still produce its report — the report
    was the only artifact before Layer 3 and remains a complete one.
    """

    def __init__(self, dataset_id: str,
                 project_id: Optional[str] = None) -> None:
        # `dataset_id` is REQUIRED and has no default, per FC-075 Seam 4's lint
        # gate: a writer that defaults to `options_wheel` is a writer that
        # silently contaminates the wheel's dataset when a covered-call profile
        # runs it. `main.py` passes `config.bigquery_dataset`, so the profile
        # decides — the same fix DD-4 made to CostBasisResolver.
        self._enabled = False
        self._sweeps = None
        self._runs = None
        self._pins = None
        self._dataset_id = dataset_id

        if not _HAS_BIGQUERY:
            logger.warning("google-cloud-bigquery not installed — sweep results "
                           "will NOT be persisted")
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — sweep results will NOT be persisted")
            return

        try:
            self._client = bigquery.Client(project=self._project_id)
            dataset_ref = bigquery.DatasetReference(self._project_id, dataset_id)
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = "us-central1"
            self._client.create_dataset(dataset, exists_ok=True)

            self._sweeps = self._ensure_table(
                dataset_ref, SWEEPS_TABLE, _sweeps_schema(),
                partition_field="submitted_at", clustering=["run_id", "sweep_key"],
            )
            self._runs = self._ensure_table(
                dataset_ref, RUNS_TABLE, _runs_schema(),
                partition_field="submitted_at",
                clustering=["run_id", "scenario_name", "symbol"],
            )
            self._enabled = True
            # FC-096 Phase B B4. The pins table is reconciled by the SAME
            # writer — one schema owner, and it is the side that knows every
            # column — but in its OWN guard, and AFTER `_enabled` is set.
            #
            # That ordering is the point: pins are not on the sweep's critical
            # path. A dataset whose `scenario_pins` cannot be created (a
            # permission, a retype, a quota) must not disable sweep persistence
            # as a side effect — that would turn a pinning problem into "the Job
            # replayed for eight minutes and stored nothing". Pins simply
            # report themselves unavailable instead.
            try:
                self._pins = self._ensure_table(
                    dataset_ref, PINS_TABLE, _pins_schema(),
                    partition_field="written_at", clustering=["pin_id"],
                )
            except Exception:
                logger.warning(
                    "Pin store unavailable — sweeps are unaffected, but the "
                    "weekly battery will run the standing set only",
                    event_category="backtest",
                    event_type="pin_store_unavailable",
                    dataset=dataset_id, exc_info=True)
        except Exception:
            logger.warning("ScenarioRunWriter init failed — sweep results will "
                           "NOT be persisted", exc_info=True)

    def _ensure_table(self, dataset_ref, name, schema, *, partition_field,
                      clustering):
        """Create-or-reconcile, additively. Same hazard as ``BacktestRunWriter``.

        ``create_table(exists_ok=True)`` returns the EXISTING table and never
        reconciles its schema. Left alone, the first release that adds a field
        makes every row carry an unknown key — and ``insert_rows_json`` rejects
        the WHOLE request rather than the offending row, so a sweep would write
        ZERO rows and report a clean run.

        Only *additive* reconcile: a removed or retyped field is a table
        migration and must be a deliberate operator action, not a side effect of
        a deploy.
        """
        table_ref = dataset_ref.table(name)
        table = bigquery.Table(table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=partition_field,
        )
        if clustering:
            table.clustering_fields = clustering
        existing = self._client.create_table(table, exists_ok=True)

        # A column that EXISTS but with the wrong type is not an additive
        # change and must not be papered over. Left alone, every subsequent
        # `insert_rows_json` fails on that field and the sweep writes ZERO rows
        # while reporting a clean run — the exact silent-empty-table failure the
        # additive reconcile below exists to prevent, arriving through the one
        # door the reconcile does not watch. Retyping is an operator migration.
        # Compared on CANONICAL names (see `_canonical_type`): BigQuery echoes
        # `BOOLEAN`/`INTEGER`/`FLOAT` for a table created with
        # `BOOL`/`INT64`/`FLOAT64`, and comparing the raw strings failed the
        # first real production execution on a table that was perfectly correct.
        by_name = {f.name: f for f in (existing.schema or [])}
        conflicts = [
            f"{f.name}: table has {by_name[f.name].field_type}/"
            f"{by_name[f.name].mode}, code expects {f.field_type}/{f.mode}"
            for f in schema
            if f.name in by_name
            and (_canonical_type(by_name[f.name].field_type)
                 != _canonical_type(f.field_type)
                 or _canonical_mode(by_name[f.name].mode)
                 != _canonical_mode(f.mode))
        ]
        if conflicts:
            raise RuntimeError(
                f"`{self._dataset_id}.{name}` has column(s) whose type or mode "
                f"disagrees with this build's schema: {'; '.join(conflicts)}. "
                "This is a MIGRATION, not an additive change: every insert would "
                "fail on that field and the sweep would write zero rows while "
                "reporting success. Recreate the table (it is insert-only and "
                "cheap to rebuild) or ALTER the column deliberately."
            )

        have = set(by_name)
        missing = [f for f in schema if f.name not in have]
        if missing:
            existing.schema = list(existing.schema) + missing
            self._client.update_table(existing, ["schema"])
            logger.info("Extended sweep-store schema",
                        event_category="backtest",
                        event_type="sweep_schema_extended",
                        table=name, added=[f.name for f in missing])
        return table_ref

    @property
    def enabled(self) -> bool:
        return self._enabled

    def find_done_sweep(self, sweep_key: str,
                        base_config_hash: Optional[str] = None,
                        engine_identity: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
        """The most recent run that COMPLETED under ``sweep_key``, or None.

        A read on the write path, deliberately: the Job asks before it replays,
        so a re-submitted spec costs one query instead of eight minutes of
        1-vCPU compute. The dashboard asks the same question before it launches
        anything; this is the backstop for the CLI path and for the race where
        two submissions arrive together.

        **"Completed" is four conditions, not one status string** (D4, review
        round 1). ``status = 'done'`` alone would return:

        * a run whose ``write_runs`` failed — the cells are not in the table, so
          the "cached" answer is an empty grid;
        * a run every cell of which errored — a page of ``err`` served as a
          result, and the operator never learns the sweep is worth re-running;
        * a run under a different EFFECTIVE base config. ``sweep_key`` covers
          the spec, the engine version and the engine identity; it cannot see an
          operator flipping ``EARNINGS_ENABLED`` on the Job between two
          otherwise identical submissions, because that value is not in the yaml
          the key hashes. ``base_config_hash`` is the hash of the effective
          snapshot and closes exactly that hole.

        **Plus a fifth, from FC-096 Phase B: ``engine_identity`` equality.** The
        key already carries it, so this predicate is redundant for any row
        written after the re-key — and it is not redundant for the rows written
        BEFORE it, whose ``engine_identity`` is NULL and whose ``sweep_key`` was
        computed over a commit SHA. ``NULL = @engine_identity`` is never true, so
        those rows can never be served as a hit. That is the intended migration:
        a legacy row is readable by ``run_id`` for ever and is never a cache
        answer for a key it was not keyed by. Belt and braces on the one
        predicate whose failure mode is serving one engine's numbers as
        another's.

        A duplicate replay costs eight minutes. A wrong dedup hit serves one
        experiment's numbers as another's, and nothing in the UI says so.

        **A query failure returns None.** "We could not tell" must mean "run
        it", never "assume it is a duplicate".
        """
        if not self._enabled or not sweep_key:
            return None
        table = f"{self._project_id}.{self._dataset_id}.{SWEEPS_TABLE}"
        query = f"""
        WITH latest AS (
          SELECT run_id, status, submitted_at, finished_at, cell_count,
                 rows_persisted, error_cells, base_config_hash, engine_identity,
                 ROW_NUMBER() OVER (
                   PARTITION BY run_id ORDER BY {LATEST_STATUS_ORDER_BY}) AS rn
          FROM `{table}`
          WHERE sweep_key = @sweep_key
        )
        SELECT run_id, submitted_at, finished_at, cell_count, rows_persisted,
               error_cells, base_config_hash, engine_identity
        FROM latest
        WHERE rn = 1
          AND status = '{STATUS_DONE}'
          AND error_cells = 0
          AND rows_persisted IS NOT NULL
          AND rows_persisted = cell_count
          AND (@base_config_hash IS NULL OR base_config_hash = @base_config_hash)
          AND engine_identity = @engine_identity
        ORDER BY submitted_at DESC
        LIMIT 1
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("sweep_key", "STRING", sweep_key),
                bigquery.ScalarQueryParameter("base_config_hash", "STRING",
                                              base_config_hash),
                bigquery.ScalarQueryParameter("engine_identity", "STRING",
                                              engine_identity),
            ])
            rows = list(self._client.query(query, job_config=job_config).result(
                timeout=60))
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not dedup
            logger.warning("Sweep dedup lookup failed — replaying rather than "
                           "assuming a duplicate",
                           event_category="backtest",
                           event_type="sweep_dedup_lookup_failed",
                           sweep_key=sweep_key, error=str(exc)[:200])
            return None
        return dict(rows[0].items()) if rows else None

    def find_running_sweep(self, sweep_key: str, *,
                           exclude_run_id: Optional[str] = None
                           ) -> Optional[Dict[str, Any]]:
        """A live, non-stale ``running`` run under ``sweep_key``, or None.

        FC-096 Phase B PR-c, from the service review. The sim service holds a
        per-PROCESS lock so one instance never replays two specs at once — and
        ``--max-instances=2`` means a second CONTAINER can accept the same spec
        a second later and replay it in parallel. The plan's "they would contend
        on one chain cache" rationale is false across containers (each has its
        own tmpfs), but the waste is real and so is the confusion: two `running`
        rows for one question, two sets of cells, and a dedup that can serve
        either.

        So this is the cross-instance half of the same gate. It is advisory by
        construction — two requests inside one query round-trip still race — and
        that is acceptable: the cost of losing the race is a duplicate replay,
        which is what happens today, while the cost of being wrong in the other
        direction would be refusing a legitimate submission for ever.

        **The age bound is PER ROW, off that row's own ``liveness_seconds``** —
        the same rule ``services/sweeps.row_liveness_seconds`` applies, mirrored
        into SQL so the two sides cannot answer differently. A caller-supplied
        single bound was the review's finding and it was wrong in the direction
        that matters: bounding every row at the SIM service's ~25 minutes made a
        **Job** legitimately 30+ minutes into replaying the same spec invisible,
        so the check waved through a concurrent duplicate — the exact confusion
        it exists to prevent — while the 409 it did not raise promised a release
        window that was never true for Job rows.

        So: a row stamped 900 (the sim service) stops blocking at 1500 s; a row
        with NULL — every Job row, and every row written before the column
        existed — stops at 11400 s (3 h + the 10-minute grace). A stamped value
        that is zero or negative is treated as absence, exactly as the Python
        reader treats it, because shortening the bound is the dangerous
        direction: it releases under a run that is still going.

        Without an upper bound at all this would be a permanent lock on a dead
        run, which is the failure ``blocking_sweep``'s expiry exists to prevent.

        A query failure returns None: "we could not tell" must mean "run it".
        """
        if not self._enabled or not sweep_key:
            return None
        table = f"{self._project_id}.{self._dataset_id}.{SWEEPS_TABLE}"
        query = f"""
        WITH latest AS (
          SELECT run_id, status, submitted_at, written_at, submitted_via,
                 liveness_seconds,
                 ROW_NUMBER() OVER (
                   PARTITION BY run_id ORDER BY {LATEST_STATUS_ORDER_BY}) AS rn
          FROM `{table}`
          WHERE sweep_key = @sweep_key
        )
        SELECT run_id, submitted_at, written_at, submitted_via,
               liveness_seconds
        FROM latest
        WHERE rn = 1
          AND status = '{STATUS_RUNNING}'
          AND written_at > TIMESTAMP_SUB(
                CURRENT_TIMESTAMP(),
                INTERVAL CAST(
                  IF(COALESCE(liveness_seconds, 0) > 0,
                     liveness_seconds, @default_liveness) + @grace
                  AS INT64) SECOND)
          AND (@exclude IS NULL OR run_id != @exclude)
        ORDER BY written_at DESC
        LIMIT 1
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("sweep_key", "STRING", sweep_key),
                bigquery.ScalarQueryParameter("default_liveness", "INT64",
                                              DEFAULT_LIVENESS_SECONDS),
                bigquery.ScalarQueryParameter("grace", "INT64",
                                              LIVENESS_GRACE_SECONDS),
                bigquery.ScalarQueryParameter("exclude", "STRING",
                                              exclude_run_id),
            ])
            rows = list(self._client.query(query, job_config=job_config).result(
                timeout=30))
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not block
            logger.warning("Live-sweep lookup failed — accepting rather than "
                           "assuming a duplicate is in flight",
                           event_category="backtest",
                           event_type="sweep_running_lookup_failed",
                           sweep_key=sweep_key, error=str(exc)[:200])
            return None
        return dict(rows[0].items()) if rows else None

    # ---------------------------------------------------------------- pins --
    @property
    def pins_enabled(self) -> bool:
        """Whether the pin table is usable. Separate from ``enabled``.

        A writer can be perfectly able to store sweep results and unable to
        store pins (see the guarded reconcile in ``__init__``), and the battery
        has to be able to tell those apart: "no pins are configured" and "the
        pin table could not be read" send an operator to different places, and
        neither is a reason to skip the standing set.
        """
        return bool(self._enabled and self._pins is not None)

    def write_pin(self, row: Dict[str, Any]) -> bool:
        """Insert one ``scenario_pins`` row (a create, or a deactivation)."""
        if not self.pins_enabled:
            logger.error("Pin NOT persisted — pin store unavailable",
                         event_category="backtest",
                         event_type="pin_write_skipped",
                         pin_id=row.get("pin_id"))
            return False
        return self._insert(self._pins, PINS_TABLE, [row])

    def list_pins(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """The CURRENT state of every pin — latest row per ``pin_id``.

        ``active_only`` is what the battery asks for; the dashboard's list view
        wants the deactivated ones too, so it is a parameter rather than two
        queries.

        **A query failure returns an empty list, loudly.** The battery's
        alternative would be to abort, which trades "the pins were not
        re-measured this week" for "nothing was re-measured this week" —
        strictly worse, because the standing set is what the trend charts are
        built on. The caller logs the degradation; see ``main.run_battery_cmd``.
        """
        if not self.pins_enabled:
            return []
        table = f"{self._project_id}.{self._dataset_id}.{PINS_TABLE}"
        query = f"""
        WITH latest AS (
          SELECT pin_id, spec_json, active, written_at, note,
                 ROW_NUMBER() OVER (
                   PARTITION BY pin_id ORDER BY {PINS_LATEST_ORDER_BY}) AS rn
          FROM `{table}`
        )
        SELECT pin_id, spec_json, active, written_at, note
        FROM latest
        WHERE rn = 1
          AND (@active_only = FALSE OR active = TRUE)
        ORDER BY written_at ASC
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("active_only", "BOOL",
                                              bool(active_only)),
            ])
            rows = list(self._client.query(query, job_config=job_config).result(
                timeout=60))
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            logger.error("Pin lookup failed — the battery will run the "
                         "standing set only",
                         event_category="backtest",
                         event_type="pin_lookup_failed",
                         error=str(exc)[:200])
            return []
        return [dict(row.items()) for row in rows]

    def recent_pin_statuses(self, pin_id: str, *, limit: int = 2,
                            exclude_run_id: Optional[str] = None
                            ) -> List[Dict[str, Any]]:
        """The last ``limit`` battery attempts at ``pin_id``, newest first.

        One row per run: the LATEST status of each, by the same clause every
        other reader of this table uses. It is what the 3-week nag counts, and
        it exists as a query rather than as a counter column on the pin because
        a counter would mean the battery UPDATES the pin table — and an
        insert-only table with one updating writer is the worst of both.

        ``exclude_run_id`` skips the attempt currently being recorded. The
        caller has just inserted that row and knows its outcome; asking
        BigQuery to hand it back would make the nag depend on how fast a
        streamed row becomes visible to a query, which is not a property
        anything here should be sensitive to.

        **A query failure returns an empty list.** The nag then does not fire —
        the right direction: a nag is a convenience, and inventing one from a
        failed lookup would teach the operator to ignore it.
        """
        if not self._enabled or not pin_id:
            return []
        table = f"{self._project_id}.{self._dataset_id}.{SWEEPS_TABLE}"
        query = f"""
        WITH latest AS (
          SELECT run_id, status, error, submitted_at, written_at, pin_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY run_id ORDER BY {LATEST_STATUS_ORDER_BY}) AS rn
          FROM `{table}`
          WHERE pin_id = @pin_id
        )
        SELECT run_id, status, error, submitted_at, written_at
        FROM latest
        WHERE rn = 1
          AND (@exclude IS NULL OR run_id != @exclude)
        ORDER BY submitted_at DESC
        LIMIT @limit
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("pin_id", "STRING", pin_id),
                bigquery.ScalarQueryParameter("exclude", "STRING",
                                              exclude_run_id),
                bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
            ])
            rows = list(self._client.query(query, job_config=job_config).result(
                timeout=60))
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not nag
            logger.warning("Pin history lookup failed — no nag will be emitted "
                           "for this pin",
                           event_category="backtest",
                           event_type="pin_history_lookup_failed",
                           pin_id=pin_id, error=str(exc)[:200])
            return []
        return [dict(row.items()) for row in rows]

    def write_status(self, row: Dict[str, Any]) -> bool:
        """Insert one ``scenario_sweeps`` row."""
        return self._insert(self._sweeps, SWEEPS_TABLE, [row])

    def write_runs(self, rows: List[Dict[str, Any]]) -> bool:
        """Insert the cell rows."""
        return self._insert(self._runs, RUNS_TABLE, rows)

    def _insert(self, table_ref, name: str, rows: List[Dict[str, Any]]) -> bool:
        if not rows:
            return True
        if not self._enabled:
            logger.error(
                "Sweep results NOT persisted — writer disabled",
                event_category="backtest", event_type="sweep_write_skipped",
                table=name, rows=len(rows),
            )
            return False
        try:
            errors = self._client.insert_rows_json(table_ref, rows)
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            logger.error("Sweep results write FAILED",
                         event_category="backtest",
                         event_type="sweep_write_failed",
                         table=name, rows=len(rows), error=str(exc)[:200])
            return False
        if errors:
            logger.error("Sweep results write returned errors",
                         event_category="backtest",
                         event_type="sweep_write_errors",
                         table=name, errors=str(errors)[:400])
            return False
        logger.info("Sweep results persisted",
                    event_category="backtest", event_type="sweep_write_ok",
                    table=f"{self._dataset_id}.{name}", rows=len(rows))
        return True


# --------------------------------------------------------------------------- #
# Row derivation — pure, so it is tested without BigQuery.
# --------------------------------------------------------------------------- #
def status_row(
    *,
    run_id: str,
    status: str,
    submitted_at: str,
    sweep_key: Optional[str] = None,
    submitted_via: str = "cli",
    engine_version: Optional[str] = None,
    git_commit: Optional[str] = None,
    engine_identity: Optional[str] = None,
    execution_name: Optional[str] = None,
    spec: Optional[Dict[str, Any]] = None,
    base_config: Optional[Dict[str, Any]] = None,
    base_config_hash: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    deduplicated_to: Optional[str] = None,
    result: Optional[Any] = None,
    lake_summary: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    rows_persisted: Optional[int] = None,
    engine_config_hash: Optional[str] = None,
    artifacts_complete: Optional[bool] = None,
    liveness_seconds: Optional[int] = None,
    pin_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One ``scenario_sweeps`` row.

    Every row of a submission repeats the identity and scope columns. That is
    deliberate denormalisation: a reader takes the latest row per ``run_id`` and
    must not have to join back to the ``submitted`` row to learn what was asked
    for — and the ``submitted`` row does not exist at all when the sweep was
    launched from the CLI.

    ``result`` is an optional ``SweepResult``; when present its timing and
    provider counters are flattened in. It is duck-typed rather than imported
    for a type check so this module stays importable without the engine.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown sweep status {status!r}; expected one of {STATUSES}")

    spec = spec or {}
    scenarios = spec.get("scenarios") or []
    row: Dict[str, Any] = {
        "run_id": run_id,
        "sweep_key": sweep_key,
        "status": status,
        "deduplicated_to": deduplicated_to,
        "submitted_at": submitted_at,
        "written_at": _now(),
        "started_at": started_at,
        "finished_at": finished_at,
        "submitted_via": submitted_via,
        "execution_name": execution_name,
        # FC-096 Phase B B3. NULL means "this writer runs under the Job's
        # clock"; the sim service stamps 900. Present on EVERY row, like every
        # other column here, so the two writers cannot diverge by whichever one
        # happened to know a field.
        "liveness_seconds": (None if liveness_seconds is None
                             else int(liveness_seconds)),
        "git_commit": git_commit,
        "engine_version": engine_version,
        "engine_identity": engine_identity,
        "base_config_hash": base_config_hash,
        "base_config_json": (json.dumps(base_config, sort_keys=True, default=str)
                             if base_config is not None else None),
        "spec_json": json.dumps(spec, sort_keys=True, default=str) if spec else None,
        # DECLARATION order, de-duplicated in place — never sorted. The grid's
        # columns are read in the order the operator typed their universe, and
        # the API shapes the grid from this same list, so sorting here and not
        # there would silently transpose the columns of a dashboard-launched
        # sweep relative to the spec that produced it.
        "symbols": _ordered_unique(
            str(sym).upper() for sym in (spec.get("symbols") or [])),
        "window_start": _iso_date(spec.get("start")),
        "window_end": _iso_date(spec.get("end")),
        "holdout_start": _iso_date(spec.get("holdout_start")),
        # An explicitly-declared `base` arm is not an extra arm — the runner
        # folds it into the implicit one — so the count is "declared arms that
        # are not base, plus base".
        "scenario_count": len(
            {str(s.get("name")) for s in scenarios} | {"base"}
        ) if scenarios else None,
        "in_sample_only": (not spec.get("holdout_start")) if spec else None,
        "error": (error[:1000] if error else None),
        "lake_summary_json": (json.dumps(lake_summary, sort_keys=True)
                              if lake_summary else None),
        # Always present, NULL until a result exists. Every status row of a run
        # therefore carries the same column set — which is what lets the
        # dashboard's `submitted` row and the Job's rows be compared for shape
        # (TestTheSubmittedRowMatchesTheJobsRowShape) instead of diverging by
        # whichever writer happened to know a field.
        "cell_count": None,
        "wall_seconds": None,
        "materialise_seconds": None,
        "replay_seconds": None,
        "provider_fetches": None,
        "bar_cache_hits": None,
        "rows_persisted": rows_persisted,
        "error_cells": None,
        # FC-096 Phase B B2. NULL on every non-terminal row and on every run
        # that wrote no artifacts; the terminal row of an artifact-writing run
        # carries the answer. Present on EVERY row for the reason the block
        # above gives: one column set per run, whichever writer produced it.
        "artifacts_complete": (None if artifacts_complete is None
                               else bool(artifacts_complete)),
        "engine_config_hash": engine_config_hash,
        "earnings_symbols_without_data": None,
        # FC-096 Phase B B4. The pin this run re-measured, or NULL on every run
        # that is not a pin's — which is every dashboard, CLI, Job and
        # sim-service run. Present on EVERY row for the reason the block above
        # gives: one column set per run, whichever writer produced it.
        "pin_id": pin_id,
    }

    if result is not None:
        row.update({
            "cell_count": len(getattr(result, "rows", []) or []),
            "wall_seconds": getattr(result, "wall_seconds", None),
            "materialise_seconds": round(
                sum((getattr(result, "materialise_seconds", None) or {}).values()), 3),
            "replay_seconds": round(
                sum((getattr(result, "replay_seconds", None) or {}).values()), 3),
            "provider_fetches": getattr(result, "provider_fetches_total", None),
            "bar_cache_hits": getattr(result, "bar_cache_hits", None),
            "base_config_hash": (base_config_hash
                                 or getattr(result, "base_config_hash", None)),
            "in_sample_only": getattr(result, "in_sample_only", row["in_sample_only"]),
            "scenario_count": len(getattr(result, "scenarios", []) or [])
                              or row["scenario_count"],
            # Counted from the cells themselves, not inferred from the exit code.
            # A sweep in which every arm errored exits 1 and is still `done` as a
            # process; it is emphatically not a result, and this is the number
            # the dedup reads to know that.
            "error_cells": sum(
                1 for cell in (getattr(result, "rows", []) or [])
                if getattr(cell, "error", None)),
        })
        # FC-096 A4. `None` when the list is empty, not `"[]"`: "no gaps" and
        # "this run predates the column" must not read alike, and a JSON `[]`
        # in the column would claim the run checked and found nothing.
        gaps = list(getattr(result, "earnings_symbols_without_data", None) or [])
        row["earnings_symbols_without_data"] = (
            json.dumps(gaps) if gaps else None)
    return row


def rows_from_sweep(
    result: Any,
    *,
    run_id: str,
    submitted_at: str,
    engine_version: str,
    git_commit: Optional[str] = None,
    engine_identity: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """One ``scenario_runs`` row per cell of ``result``.

    Every cell gets a row, including errored ones. Dropping them would make a
    half-run sweep read as a complete one — the same rule ``build_row`` follows
    for a failed screen symbol, and for the same reason: "this arm was not
    measured" and "this arm was fine" must not look alike.
    """
    written = _now()
    out: List[Dict[str, Any]] = []
    overrides = getattr(result, "scenario_overrides", {}) or {}
    haircuts = getattr(result, "scenario_fill_haircuts", {}) or {}

    for cell in getattr(result, "rows", []) or []:
        out.append(_finite({
            "run_id": run_id,
            "submitted_at": submitted_at,
            "written_at": written,

            "scenario_name": cell.scenario,
            "scenario_hash": cell.scenario_hash,
            "config_hash": cell.config_hash,
            "overrides_json": json.dumps(overrides.get(cell.scenario) or {},
                                         sort_keys=True, default=str),
            "fill_haircut": haircuts.get(cell.scenario),

            "symbol": cell.symbol,
            "split": cell.split,
            "window_start": _iso_date(cell.start),
            "window_end": _iso_date(cell.end),

            "verdict": cell.verdict,
            "demote": cell.demote,
            # Read off the dataclass properties, never re-derived downstream.
            "insufficient": cell.insufficient,
            "low_activity": cell.low_activity,
            "measured": cell.measured,

            "total_return": cell.total_return,
            "annualized_return": cell.annualized_return,
            "annualized_return_on_collateral": cell.annualized_return_on_collateral,
            "benchmark_return": cell.benchmark_return,
            "excess_return": cell.excess_return,
            "option_pnl": cell.option_pnl,
            "stock_pnl_realized": cell.stock_pnl_realized,
            "stock_pnl_unrealized": cell.stock_pnl_unrealized,
            "max_drawdown": cell.max_drawdown,
            "win_rate": cell.win_rate,
            "assignment_rate": cell.assignment_rate,

            "puts_sold": cell.puts_sold,
            "calls_sold": cell.calls_sold,
            "cycles_completed": cell.cycles_completed,
            "cycles_open": cell.cycles_open,
            "decision_days": cell.decision_days,
            "days_in_position_fraction": cell.days_in_position_fraction,

            "bid_fill_return": cell.bid_fill_return,
            "verdict_flips_on_fill": cell.verdict_flips_on_fill,

            "replay_seconds": cell.replay_seconds,
            "error": (cell.error[:1000] if cell.error else None),

            "engine_version": engine_version,
            "git_commit": git_commit,
            "engine_identity": engine_identity,
        }))
    return out


def _finite(row: Dict[str, Any]) -> Dict[str, Any]:
    """NaN / +-inf -> NULL.

    A zero-day window or a zero-collateral cycle can produce a NaN or an inf in
    an annualised ratio. `insert_rows_json` serialises those as the bare JSON
    tokens `NaN` / `Infinity`, which BigQuery rejects — and it rejects the WHOLE
    request, so one pathological cell would silently cost the sweep all of its
    rows. NULL is also the honest value: "this ratio is not defined here" is not
    a number, and rendering it as one is the FC-057 dishonest-metric class.
    """
    for key, value in row.items():
        if isinstance(value, float) and not math.isfinite(value):
            row[key] = None
    return row


def _ordered_unique(values) -> List[str]:
    """De-duplicate while preserving first-seen order."""
    seen = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


# --------------------------------------------------------------------------- #
# Pins (FC-096 Phase B B4) — pure, so they are tested without BigQuery.
# --------------------------------------------------------------------------- #
def new_pin_id() -> str:
    """16 hex characters, the same shape as a ``run_id``.

    Deliberately not derived from the spec. Two operators may pin the same
    question for different reasons, with different notes, and un-pinning one of
    them must not un-pin the other — which is exactly what a content-addressed
    id would do. The "no two ACTIVE pins with the same spec" rule is enforced at
    write time by the API, where it can say WHICH pin already asks it.
    """
    import uuid

    return uuid.uuid4().hex[:16]


def pin_row(*, pin_id: str, spec: Optional[Dict[str, Any]] = None,
            spec_json: Optional[str] = None, active: bool,
            note: Optional[str] = None,
            written_at: Optional[str] = None) -> Dict[str, Any]:
    """One ``scenario_pins`` row — a create, or a deactivation.

    Exactly one of ``spec`` and ``spec_json`` is given. ``spec_json`` is the
    form the API already holds (it validated and canonicalised the spec, and
    that STRING is what the duplicate check compares), and re-encoding a decoded
    copy of it would risk a byte the comparison then misses. ``spec`` is the
    convenience for a caller holding a dict.

    A deactivation carries the SAME ``spec_json`` as the create it retires. The
    row is a state transition of the pin, not a tombstone: a reader that took
    the latest row must be able to see what was un-pinned without walking back
    through the history, exactly as a ``failed`` sweep row still carries the
    spec that failed.
    """
    if (spec is None) == (spec_json is None):
        raise ValueError("pin_row takes exactly one of `spec` / `spec_json`")
    if not pin_id:
        raise ValueError("pin_row requires a pin_id")
    encoded = (spec_json if spec_json is not None
               else json.dumps(spec, sort_keys=True, default=str))
    return {
        "pin_id": pin_id,
        "spec_json": encoded,
        "active": bool(active),
        "written_at": written_at or _now(),
        "note": (note[:PIN_NOTE_MAX_CHARS] if note else None),
    }


# Every knob a scenario may override, plus the three env-shadowed switches,
# mapped to the ``Config`` ACCESSOR that yields its effective value. The yaml is
# not the configuration: ``EARNINGS_ENABLED`` / ``ROLLER_ENABLED`` /
# ``ROLLER_DRY_RUN`` win over their keys at runtime (FC-013 DD-7, FC-078 DD-7),
# so a snapshot taken from ``_config`` alone records a gate as ON that the run
# had OFF. Two sweeps either side of a kill switch would then be
# indistinguishable in the store — and, before the dedup started reading
# ``base_config_hash``, one would have been served as the other's answer.
_EFFECTIVE_KEYS: Dict[str, str] = {
    "strategy.put_target_dte": "put_target_dte",
    "strategy.call_target_dte": "call_target_dte",
    "strategy.put_delta_range": "put_delta_range",
    "strategy.call_delta_range": "call_delta_range",
    "strategy.min_put_premium": "min_put_premium",
    "strategy.min_call_premium": "min_call_premium",
    "strategy.min_stock_price": "min_stock_price",
    "strategy.max_stock_price": "max_stock_price",
    "strategy.min_avg_volume": "min_avg_volume",
    "risk.max_position_size": "max_position_size",
    "earnings.enabled": "earnings_enabled",
    "earnings.blackout_days": "earnings_blackout_days",
    "rolling.enabled": "rolling_enabled",
    "rolling.dry_run": "roller_dry_run",
    "rolling.itm_trigger_ratio": "rolling_itm_trigger_ratio",
    "rolling.max_extension_days": "rolling_max_extension_days",
    "rolling.max_replacement_delta": "rolling_max_replacement_delta",
    "rolling.min_net_credit_per_contract": "rolling_min_net_credit_per_contract",
    "rolling.imminence_extrinsic_threshold": "rolling_imminence_extrinsic_threshold",
    "universe.excluded_symbols": "excluded_symbols",
    "universe.max_spread_pct": "max_spread_pct",
}


def base_config_snapshot(config: Any) -> Dict[str, Any]:
    """The EFFECTIVE base config a sweep row carries (D1, review round 1).

    Two layers, and the order matters:

    1. the raw ``strategy`` / ``risk`` / ``earnings`` / ``rolling`` /
       ``universe`` sections, so the payload stays complete and readable a year
       later — a hash proves two runs matched and says nothing about what they
       matched on;
    2. an ``effective`` block read through ``Config``'s ACCESSORS, which is what
       the run actually used. Where the two disagree, (2) is the truth and (1)
       is the file.

    ``alpaca:`` is excluded on purpose: it holds credentials and an account
    number, and this store is read by a public dashboard.
    """
    raw = getattr(config, "_config", None) or {}
    sections = ("strategy", "risk", "earnings", "rolling", "universe")
    snapshot: Dict[str, Any] = {
        name: raw.get(name) for name in sections if raw.get(name) is not None
    }
    effective: Dict[str, Any] = {}
    for key, accessor in _EFFECTIVE_KEYS.items():
        try:
            value = getattr(config, accessor)
        except Exception:  # noqa: BLE001 - a missing knob is not a sweep failure
            continue
        # `excluded_symbols` is a set; JSON needs a stable list.
        effective[key] = sorted(value) if isinstance(value, (set, frozenset)) else value
    snapshot["effective"] = effective
    return snapshot


def base_config_hash(snapshot: Dict[str, Any]) -> str:
    """sha256[:16] of the EFFECTIVE snapshot — the dedup's configuration guard.

    Deliberately not ``bq_writer.config_hash``: that hashes nine strategy keys
    plus the scoring constants, so every ``rolling.*`` and ``earnings.*`` knob —
    including the two the environment can flip out from under the yaml — is
    invisible to it. Two sweeps that differed only in ``EARNINGS_ENABLED`` would
    share it, and the dedup would serve one as the other. ``config_hash`` is
    still stored, as ``engine_config_hash``, because it is what lines a sweep row
    up with a ``backtest_runs`` row; the two answer different questions.
    """
    blob = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
