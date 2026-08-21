"""Dedicated BigQuery analytics writer with explicit schemas.

Replaces the fragile Cloud Logging → BQ log sink approach with direct
writes to purpose-built, time-partitioned tables.  Each table has a
code-defined schema — no auto-detection, no wildcard query conflicts.

Manages these tables in the dataset its constructor is given (FC-075 Seam 4 —
``config.bigquery_dataset``; every row also carries ``strategy_id``):
  - errors          (error events)
  - executions      (endpoint run summaries)
  - decision_events (FC-065 Phase 4 — one covered-call decision per held
                     symbol per cycle, keyed (run_id, symbol, stage))

Post-FC-012 (2026-04-24) removals:
  - write_scan_result / write_scan_results_batch (never called)
  - write_position_snapshot / write_position_snapshots_batch (migrated
    to equity_history_from_alpaca; per-symbol snapshots dropped)

Post-FC-035 (2026-07-29) removals:
  - write_order_status + the order_statuses table. Never populated: the
    poll that fed it ran every /run cycle but its effectful branches
    never fired (0 events, 0 rows, in ~490 production invocations).
    Fills/expirations come from the activities ingestor ->
    trades_from_activities, which is authoritative and idempotent.

Post-FC-069 (2026-08-04) removals:
  - write_wheel_cycle + the wheel_cycles table. Its two callers in
    ``reconcile_positions`` wrote unkeyed rows whose ``capital_gain``
    always resolved to 0 — it was derived from wheel-state cost basis,
    and that state does not survive a cold start (``_save_state`` is a
    no-op, FC-039) — and the same assignments were re-detected from a
    rolling 7-day activity window on every cold start, so the rows also
    duplicated: 78 rows when the writer was stopped, 72 of them
    zero-gain writer artifacts (74 total zero-gain, of which two are
    hand-backfilled breakeven cycles that are legitimately $0). Completed cycles come from the
    ``wheel_cycles_from_activities`` VIEW (FC-012 / FC-018), derived
    from ``trades_from_activities`` and untouched by this removal; the
    raw table had no readers at all.

Design principles:
  - Graceful no-op if BigQuery is unavailable
  - ``insert_rows_json`` for simple, low-latency writes
  - All tables time-partitioned by ``timestamp``
  - Schema changes = add NULLABLE columns (safe in BQ)
"""

import os
import threading
import structlog
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = structlog.get_logger(__name__)

try:
    from google.cloud import bigquery
    _HAS_BIGQUERY = True
except ImportError:
    _HAS_BIGQUERY = False
    bigquery = None  # type: ignore

# --------------------------------------------------------------------------- #
# Schema definitions
# --------------------------------------------------------------------------- #

# FC-075 Seam 4 (DD-3). NULLABLE and additive: pre-Seam-4 rows have no value,
# so readers segmenting by strategy use IFNULL(strategy_id, 'wheel').
_STRATEGY_ID_DESC = "Strategy profile that wrote this row; NULL = wheel (pre-Seam-4 rows)"

_SCHEMAS: Dict[str, list] = {}

if _HAS_BIGQUERY:
    _SCHEMAS = {
        "errors": [
            bigquery.SchemaField("timestamp", "TIMESTAMP"),
            bigquery.SchemaField("event_type", "STRING"),
            bigquery.SchemaField("error_type", "STRING"),
            bigquery.SchemaField("error_message", "STRING"),
            bigquery.SchemaField("symbol", "STRING"),
            bigquery.SchemaField("underlying", "STRING"),
            bigquery.SchemaField("component", "STRING"),
            bigquery.SchemaField("recoverable", "BOOLEAN"),
            bigquery.SchemaField("request_id", "STRING"),
            bigquery.SchemaField("stack_trace", "STRING"),
            bigquery.SchemaField("strategy_id", "STRING", description=_STRATEGY_ID_DESC),
        ],
        "executions": [
            bigquery.SchemaField("timestamp", "TIMESTAMP"),
            bigquery.SchemaField("endpoint", "STRING", description="/scan, /run, /monitor"),
            bigquery.SchemaField("status", "STRING"),
            bigquery.SchemaField("duration_seconds", "FLOAT"),
            bigquery.SchemaField("scan_count", "INTEGER"),
            bigquery.SchemaField("opportunities_found", "INTEGER"),
            bigquery.SchemaField("trades_executed", "INTEGER"),
            bigquery.SchemaField("trades_failed", "INTEGER"),
            bigquery.SchemaField("errors", "INTEGER"),
            bigquery.SchemaField("buying_power_before", "FLOAT"),
            bigquery.SchemaField("buying_power_after", "FLOAT"),
            bigquery.SchemaField("portfolio_value", "FLOAT"),
            bigquery.SchemaField("request_id", "STRING"),
            bigquery.SchemaField("strategy_id", "STRING", description=_STRATEGY_ID_DESC),
        ],
        # scans table: populated by the Cloud Logging sink (filtering events).
        # AnalyticsWriter never wrote it — dead schema + methods removed in FC-012.
        #
        # wheel_cycles: removed in FC-069 (item 10). The schema entry is
        # deliberately absent — _ensure_all_tables() auto-creates a table for
        # every key here, so re-adding it would resurrect the dropped table on
        # the next cold start. Completed cycles come from the
        # wheel_cycles_from_activities VIEW.
        #
        # position_snapshots: removed in FC-012. PORTFOLIO rows migrated to
        # equity_history_from_alpaca; non-PORTFOLIO rows dropped entirely
        # (frontend never consumed /metrics/stock-snapshots).
        #
        # FC-065 Phase 4. Adopts FC-044 Phase 1's `decision_events` design,
        # with its "metrics JSON" column deliberately NOT adopted: the metrics
        # are typed scalars here and the chain-rejection breakdown is a
        # REPEATED RECORD. String-ifying arrays or structs into a BigQuery
        # column is the 2026-04-07 schema lesson (it produced a dataset whose
        # wildcard queries could not be run at all).
        "decision_events": [
            bigquery.SchemaField("timestamp", "TIMESTAMP",
                                 description="Write time (partition field)"),
            bigquery.SchemaField("run_id", "STRING",
                                 description="Minted at /scan, threaded to /run"),
            bigquery.SchemaField("run_ts", "TIMESTAMP",
                                 description="Cycle start — stable across stages"),
            bigquery.SchemaField("stage", "STRING", description="scan | run"),
            bigquery.SchemaField("endpoint", "STRING", description="/scan | /run"),
            bigquery.SchemaField("symbol", "STRING", description="Underlying"),
            bigquery.SchemaField(
                "outcome", "STRING",
                description=("Closed enum: sold | no_candidates | dropped | "
                             "blocked | not_eligible")),
            bigquery.SchemaField("reason", "STRING",
                                 description="Sub-reason scoped to outcome"),
            bigquery.SchemaField("shares", "INTEGER"),
            bigquery.SchemaField("cost_basis_per_share", "FLOAT",
                                 description="Resolved floor (avg_entry_price)"),
            bigquery.SchemaField("current_price", "FLOAT"),
            bigquery.SchemaField(
                "underwater_pct", "FLOAT",
                description="(price - floor) / floor; NEGATIVE is underwater"),
            bigquery.SchemaField(
                "uncovered_days", "INTEGER",
                description="Trading days since the last covered call; NULL = unknown"),
            bigquery.SchemaField("candidates", "INTEGER",
                                 description="Qualifying call contracts found"),
            bigquery.SchemaField("option_symbol", "STRING"),
            bigquery.SchemaField("strike_price", "FLOAT"),
            bigquery.SchemaField("premium", "FLOAT"),
            bigquery.SchemaField("contracts", "INTEGER"),
            bigquery.SchemaField("order_id", "STRING"),
            bigquery.SchemaField(
                "reason_counts", "RECORD", mode="REPEATED",
                description="Chain-rejection breakdown — REPEATED, never a JSON string",
                fields=[
                    bigquery.SchemaField("reason", "STRING"),
                    bigquery.SchemaField("count", "INTEGER"),
                ]),
            bigquery.SchemaField(
                "dedup_key", "STRING",
                description="run_id|symbol|stage — also the streaming insertId"),
            bigquery.SchemaField("strategy_id", "STRING", description=_STRATEGY_ID_DESC),
        ],
    }


class AnalyticsWriter:
    """Write structured analytics data to dedicated BigQuery tables.

    Manages table creation and row insertion for 3 analytics tables
    (trades table is handled by the existing TradeJournal).

    If BigQuery is unavailable, all write methods are silent no-ops.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        *,
        dataset_id: str,
        strategy_id: str,
    ):
        # Both required and keyword-only (FC-075 Seam 4): a defaulted dataset is
        # how a second strategy profile silently writes the wheel's tables.
        # Assigned before any early return so a disabled writer still reports
        # the identity it was constructed with.
        self._enabled = False
        self._tables: Dict[str, Any] = {}
        self._dataset_id = dataset_id
        self._strategy_id = strategy_id
        self._project_id: Optional[str] = None

        if not _HAS_BIGQUERY:
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — AnalyticsWriter disabled")
            return

        try:
            self._client = bigquery.Client(project=self._project_id)
            self._ensure_all_tables()
            self._enabled = True
            logger.info(
                "AnalyticsWriter initialised",
                event_category="system",
                event_type="analytics_writer_initialized",
                project=self._project_id,
                dataset=self._dataset_id,
                strategy_id=self._strategy_id,
                tables=list(_SCHEMAS.keys()),
            )
        except Exception:
            logger.warning("Failed to initialise AnalyticsWriter — disabled",
                          exc_info=True)

    def _ensure_all_tables(self) -> None:
        """Create the analytics tables if they don't exist.

        The dataset itself is NEVER created here (FC-075 Seam 4): datasets are
        an operator provisioning step. Under dataset-create-capable credentials
        a mis-pointed profile would otherwise conjure its own dataset and
        side-step provisioning/IAM entirely; a missing dataset must instead
        raise NotFound and leave this writer disabled.
        """
        dataset_ref = bigquery.DatasetReference(self._project_id, self._dataset_id)

        for table_name, schema in _SCHEMAS.items():
            table_ref = dataset_ref.table(table_name)
            table = bigquery.Table(table_ref, schema=schema)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="timestamp",
            )
            self._client.create_table(table, exists_ok=True)
            self._tables[table_name] = table_ref

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Generic write
    # ------------------------------------------------------------------

    def _write(self, table_name: str, row: dict) -> None:
        """Insert a single row into a named table."""
        if not self._enabled or table_name not in self._tables:
            return
        if "timestamp" not in row:
            row["timestamp"] = datetime.now(timezone.utc).isoformat()
        # Stamped at the chokepoint, not at each producer: two lines cover
        # write_error / write_execution / write_decision_events and every
        # future writer method (FC-075 Seam 4, DD-3).
        row["strategy_id"] = self._strategy_id
        try:
            errors = self._client.insert_rows_json(self._tables[table_name], [row])
            if errors:
                logger.error("AnalyticsWriter insert errors",
                            table=table_name, errors=str(errors)[:200])
        except Exception:
            logger.debug("AnalyticsWriter insert failed",
                        table=table_name, exc_info=True)

    def _write_batch(self, table_name: str, rows: List[dict],
                     row_ids: Optional[List[str]] = None) -> None:
        """Insert multiple rows into a named table.

        ``row_ids`` are passed to BigQuery as streaming ``insertId``s, which
        gives best-effort de-duplication of an identical row re-sent inside
        the streaming window — the first line of defence against a Cloud
        Scheduler retry.  It is only the first line: the window is short, so
        any table written with row_ids must ALSO be read through a
        dedup-on-key query.  See ``write_decision_events``.
        """
        if not self._enabled or not rows or table_name not in self._tables:
            return
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            if "timestamp" not in row:
                row["timestamp"] = now
            row["strategy_id"] = self._strategy_id
        try:
            kwargs = {"row_ids": row_ids} if row_ids else {}
            errors = self._client.insert_rows_json(
                self._tables[table_name], rows, **kwargs)
            if errors:
                logger.error("AnalyticsWriter batch insert errors",
                            table=table_name, count=len(rows),
                            errors=str(errors)[:200])
        except Exception:
            logger.debug("AnalyticsWriter batch insert failed",
                        table=table_name, exc_info=True)

    # ------------------------------------------------------------------
    # Typed write methods
    # ------------------------------------------------------------------

    def write_error(self, *, event_type: str, error_type: str = "",
                    error_message: str = "", symbol: str = "",
                    underlying: str = "", component: str = "",
                    recoverable: bool = True, request_id: str = "",
                    stack_trace: str = "", **extra) -> None:
        self._write("errors", {
            "event_type": event_type,
            "error_type": error_type,
            "error_message": error_message[:1000],
            "symbol": symbol,
            "underlying": underlying,
            "component": component,
            "recoverable": recoverable,
            "request_id": request_id,
            "stack_trace": stack_trace[:2000],
        })

    def write_execution(self, *, endpoint: str, status: str,
                        duration_seconds: float = 0,
                        scan_count: int = 0, opportunities_found: int = 0,
                        trades_executed: int = 0, trades_failed: int = 0,
                        errors: int = 0, buying_power_before: float = 0,
                        buying_power_after: float = 0,
                        portfolio_value: float = 0,
                        request_id: str = "", **extra) -> None:
        self._write("executions", {
            "endpoint": endpoint,
            "status": status,
            "duration_seconds": duration_seconds,
            "scan_count": scan_count,
            "opportunities_found": opportunities_found,
            "trades_executed": trades_executed,
            "trades_failed": trades_failed,
            "errors": errors,
            "buying_power_before": buying_power_before,
            "buying_power_after": buying_power_after,
            "portfolio_value": portfolio_value,
            "request_id": request_id,
        })

    def write_decision_events(self, rows: List[dict]) -> None:
        """Write FC-065 Phase 4 decision records, keyed for idempotency.

        Each row carries ``dedup_key = run_id|symbol|stage``; that string is
        handed to BigQuery as the streaming ``insertId``.  Rows arriving
        without one are refused rather than written unkeyed — an unkeyed
        fire-and-forget writer is exactly what produced 36 duplicate
        ``wheel_cycles`` rows per assignment (that writer and its table were
        retired in FC-069; the duplication lesson is why this one is keyed),
        and a decision table that double-counts is worse than no decision
        table.

        Readers must still dedup on ``dedup_key`` (insertId dedup is
        best-effort over a short window; a scheduler retry minutes later
        slips through it).  The canonical reading pattern lives in
        ``dashboard/backend/services/bigquery.py`` and the runbook.
        """
        if not rows:
            return
        keyed = [r for r in rows if r.get("dedup_key")]
        if len(keyed) != len(rows):
            logger.error("Refusing unkeyed decision rows",
                         table="decision_events",
                         refused=len(rows) - len(keyed))
        if not keyed:
            return
        self._write_batch("decision_events", keyed,
                          row_ids=[r["dedup_key"] for r in keyed])

    def query(self, table_name: str, query_sql: str) -> List[dict]:
        """Run a query and return results as list of dicts."""
        if not self._enabled:
            return []
        try:
            result = self._client.query(query_sql).result(timeout=60)
            return [dict(row) for row in result]
        except Exception:
            logger.debug("AnalyticsWriter query failed",
                        table=table_name, exc_info=True)
            return []

    @property
    def project_id(self) -> str:
        return self._project_id or ""

    @property
    def dataset_id(self) -> str:
        return self._dataset_id

    @property
    def strategy_id(self) -> str:
        return self._strategy_id


class _DisabledAnalyticsWriter(AnalyticsWriter):
    """The unconfigured-singleton sentinel: structurally incapable of writing.

    Returned by ``get_analytics_writer()`` when no process entry point has
    called ``configure_analytics_writer``. It deliberately does NOT call
    ``AnalyticsWriter.__init__``: constructing the real class with a
    placeholder dataset would build a BigQuery client and create tables under
    whatever ambient credentials exist — the fail-open this sentinel exists to
    make impossible. No client, no ensure, no env reads.
    """

    def __init__(self) -> None:
        self._enabled = False
        self._tables: Dict[str, Any] = {}
        # Identity attributes only, so the properties above stay answerable.
        self._project_id = None
        self._dataset_id = None
        self._strategy_id = None


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #

_instance: Optional[AnalyticsWriter] = None
_instance_lock = threading.Lock()

# Set by configure_analytics_writer() at the process entry point (the Flask
# strategy_config() cache, or main.py after it parses --config). Until then the
# singleton is unconfigured and every fetch returns the disabled sentinel: one
# process runs one strategy profile, and a profile that never announced itself
# must not inherit the wheel's dataset by default.
_configured_dataset_id: Optional[str] = None
_configured_strategy_id: Optional[str] = None
_warned_unconfigured = False

_DISABLED_WRITER = _DisabledAnalyticsWriter()

# THREAD-LOCAL override, for the same reason src/utils/clock.py is thread-local.
# cloud_run_server runs Flask with threaded=True at containerConcurrency 10 and
# maxScale 1 — one process, ten concurrent requests. A backtest replay swapping
# a PROCESS-GLOBAL singleton silently routed a concurrently-running live
# trading cycle's analytics into the no-op writer, discarding its rows
# (including the write_error that warns of re-execution risk). The dashboard
# then shows a clean run over a trade whose telemetry was thrown away.
_override = threading.local()


def configure_analytics_writer(*, dataset_id: str, strategy_id: str) -> None:
    """Declare which dataset/strategy this process's singleton writes.

    Called once per process at the entry point that knows the profile — the
    server's ``strategy_config()`` cache and ``main.py`` after it parses
    ``--config``. The CLI selects its profile with ``--config`` rather than
    ``STRATEGY_CONFIG``, which is why the singleton cannot self-resolve from the
    environment.

    Re-declaring DIFFERENT values after the singleton has been constructed
    raises: one process, one profile, no silent re-pointing of live telemetry.
    Before first construction a re-declaration is accepted silently — nothing
    has been written under the old values, and in production there is only ever
    one call.
    """
    global _configured_dataset_id, _configured_strategy_id
    with _instance_lock:
        if _instance is not None and (
            (dataset_id, strategy_id)
            != (_configured_dataset_id, _configured_strategy_id)
        ):
            raise RuntimeError(
                "AnalyticsWriter already constructed for "
                f"({_configured_dataset_id!r}, {_configured_strategy_id!r}); "
                f"refusing to re-point it at ({dataset_id!r}, {strategy_id!r})"
            )
        _configured_dataset_id = dataset_id
        _configured_strategy_id = strategy_id


def get_analytics_writer() -> AnalyticsWriter:
    """Get the writer for THIS thread: a replay override, else the singleton.

    Fails CLOSED when unconfigured: returns the disabled sentinel and warns
    once, rather than constructing a writer against a default dataset. The
    sentinel is not cached, so a call after ``configure_analytics_writer``
    still gets the real writer.
    """
    override = getattr(_override, "writer", None)
    if override is not None:
        return override
    global _instance, _warned_unconfigured
    if _instance is not None:
        return _instance
    # Double-checked: Flask runs threaded=True at containerConcurrency 10, and
    # the one-process-one-profile invariant now hangs off _instance identity.
    with _instance_lock:
        if _instance is not None:
            return _instance
        if _configured_dataset_id is None:
            if not _warned_unconfigured:
                _warned_unconfigured = True
                logger.warning(
                    "AnalyticsWriter unconfigured — analytics writes are no-ops",
                    event_category="system",
                    event_type="analytics_writer_unconfigured",
                )
            return _DISABLED_WRITER
        _instance = AnalyticsWriter(
            dataset_id=_configured_dataset_id,
            strategy_id=_configured_strategy_id,
        )
    return _instance


def _reset_for_tests() -> None:
    """Clear the singleton and its configuration. TESTS ONLY.

    The suite flips ``STRATEGY_CONFIG`` between profiles inside one pytest
    process; without this, the first test to construct the singleton would make
    every later profile flip raise. Never call it from production code — a
    re-pointing seam is precisely what ``configure_analytics_writer`` refuses.
    """
    global _instance, _configured_dataset_id, _configured_strategy_id
    global _warned_unconfigured
    with _instance_lock:
        _instance = None
        _configured_dataset_id = None
        _configured_strategy_id = None
        _warned_unconfigured = False


def set_analytics_writer(writer: Optional[AnalyticsWriter]) -> Optional[AnalyticsWriter]:
    """Override the writer FOR THIS THREAD; returns the previous override.

    Strategy code reaches for this writer from inside its methods rather than
    taking it by constructor injection, so a backtest has no seam to use. It
    installs a no-op writer here for the duration of a replay — otherwise a
    simulated trade would write rows into the production analytics tables.

    Thread-local by construction: a replay must never redirect a concurrently
    running live cycle's telemetry. Passing ``None`` clears this thread's
    override and restores the shared singleton.
    """
    previous = getattr(_override, "writer", None)
    _override.writer = writer
    return previous
