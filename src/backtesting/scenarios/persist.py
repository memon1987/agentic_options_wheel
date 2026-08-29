"""The scenario store — two tables a sweep may write, and neither is the screen's.

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

**Ordering within a run_id is by ``written_at``, never ``submitted_at``.** Every
row of one submission carries the SAME ``submitted_at`` (it is the partition key
and the submission's identity), so ordering by it is a three-way tie and "latest
status wins" would resolve arbitrarily. ``written_at`` is stamped per row at
insert. ``STATUS_RANK`` is the deterministic tiebreak for the pathological case
of two rows sharing a microsecond.
"""

from __future__ import annotations

import json
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
STATUS_RANK: Dict[str, int] = {
    STATUS_SUBMITTED: 0,
    STATUS_RUNNING: 1,
    STATUS_DEDUPLICATED: 2,
    STATUS_FAILED: 3,
    STATUS_DONE: 3,
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
        f("submitted_via", "STRING"),      # 'dashboard' | 'cli'
        f("execution_name", "STRING"),     # CLOUD_RUN_EXECUTION, for debugging
        f("git_commit", "STRING"),
        f("engine_version", "STRING"),
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

        have = {f.name for f in (existing.schema or [])}
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

    def find_done_sweep(self, sweep_key: str) -> Optional[Dict[str, Any]]:
        """The most recent run that reached ``done`` under ``sweep_key``, or None.

        A read on the write path, deliberately: the Job asks before it replays,
        so a re-submitted spec costs one query instead of eight minutes of
        1-vCPU compute. The dashboard asks the same question before it launches
        anything; this is the backstop for the CLI path and for the race where
        two submissions arrive together.

        **A query failure returns None.** "We could not tell" must mean "run
        it", never "assume it is a duplicate" — the cost of the first is a
        redundant replay, the cost of the second is silently serving one
        experiment's numbers as another's.
        """
        if not self._enabled or not sweep_key:
            return None
        table = f"{self._project_id}.{self._dataset_id}.{SWEEPS_TABLE}"
        query = f"""
        WITH latest AS (
          SELECT run_id, status, submitted_at, finished_at,
                 ROW_NUMBER() OVER (
                   PARTITION BY run_id ORDER BY {LATEST_STATUS_ORDER_BY}) AS rn
          FROM `{table}`
          WHERE sweep_key = @sweep_key
        )
        SELECT run_id, submitted_at, finished_at
        FROM latest
        WHERE rn = 1 AND status = '{STATUS_DONE}'
        ORDER BY submitted_at DESC
        LIMIT 1
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("sweep_key", "STRING", sweep_key),
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
        "git_commit": git_commit,
        "engine_version": engine_version,
        "base_config_hash": base_config_hash,
        "base_config_json": (json.dumps(base_config, sort_keys=True, default=str)
                             if base_config is not None else None),
        "spec_json": json.dumps(spec, sort_keys=True, default=str) if spec else None,
        "symbols": sorted({str(s).upper() for s in (spec.get("symbols") or [])}),
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
        })
    return row


def rows_from_sweep(
    result: Any,
    *,
    run_id: str,
    submitted_at: str,
    engine_version: str,
    git_commit: Optional[str] = None,
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
        out.append({
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
        })
    return out


def base_config_snapshot(config: Any) -> Dict[str, Any]:
    """The sections of the effective base config a sweep row carries (D1).

    Only the strategy-shaping sections, and only as they are *after* env
    substitution — this is the payload that makes a stored verdict readable a
    year later. ``alpaca:`` is excluded on purpose: it holds credentials and an
    account number, and the store is read by a public dashboard.
    """
    raw = getattr(config, "_config", None) or {}
    sections = ("strategy", "risk", "earnings", "rolling", "universe")
    return {name: raw.get(name) for name in sections if raw.get(name) is not None}
