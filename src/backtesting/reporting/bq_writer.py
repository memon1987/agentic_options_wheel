"""Persist screen-mode results to BigQuery (`options_wheel.backtest_runs`).

One row per symbol per screening run, so demotion candidates are queryable and
the dashboard can consume them later.

Two deliberate choices, both learned the hard way in this FC:

**A failed write is reported, never swallowed.** `AnalyticsWriter` is
fire-and-forget by design — losing an analytics row is cheaper than blocking a
trade. This is the opposite: a screen that silently drops rows produces a
partial table an operator will read as complete, and that table is the input to
a demotion decision. Writes here return success, and the caller surfaces it.

**Every row carries its config hash and the engine's known biases.** A verdict
read six months from now is meaningless without knowing which premium floor and
delta band produced it, or which fidelity gaps were open at the time. Dividends
were unmodeled before FC-042; rows from either side of that cutover are
distinguishable by ``benchmark_dividends`` being null (before) or present
(after), which is why the benchmark's price and dividend components are stored
separately rather than only as a total.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

from ..engine.rejections import stand_down_reasons

logger = structlog.get_logger(__name__)

try:  # pragma: no cover - exercised only where the dep is installed
    from google.cloud import bigquery
    _HAS_BIGQUERY = True
except ImportError:  # pragma: no cover
    _HAS_BIGQUERY = False

TABLE_NAME = "backtest_runs"


def _schema():
    """BigQuery schema for one screened symbol. Documented in docs/bigquery/."""
    f = bigquery.SchemaField
    return [
        # Run identity
        f("run_id", "STRING", mode="REQUIRED"),
        f("timestamp", "TIMESTAMP", mode="REQUIRED"),
        f("symbol", "STRING", mode="REQUIRED"),
        f("window_start", "DATE"),
        f("window_end", "DATE"),
        f("config_hash", "STRING"),
        f("engine_version", "STRING"),
        # Scope. Without these, "the latest run" (ORDER BY timestamp DESC) picks
        # whichever run happened last — so a 2-symbol ad-hoc probe hides a full
        # screen's demotion candidates and its failures. The endpoint's own 413
        # tells operators to make exactly those ad-hoc runs.
        f("run_kind", "STRING"),        # 'full' | 'adhoc'
        f("universe_size", "INTEGER"),  # symbols attempted in this run
        # Verdict — what a human acts on
        f("verdict", "STRING"),
        f("demote", "BOOL"),
        f("verdict_reasons", "STRING", mode="REPEATED"),
        f("binding_constraint", "STRING"),
        # Headline performance
        f("starting_cash", "FLOAT"),
        f("final_equity", "FLOAT"),
        f("total_return", "FLOAT"),
        f("annualized_return", "FLOAT"),
        f("annualized_return_on_collateral", "FLOAT"),
        f("benchmark_return", "FLOAT"),
        # Split out so a stored row stays interpretable across the FC-042 cutover:
        # benchmark_return now INCLUDES dividends, and rows written before that
        # have a null here rather than a silently different total.
        f("benchmark_price_return", "FLOAT"),
        f("benchmark_dividends", "FLOAT"),
        f("excess_return", "FLOAT"),
        # Attribution — never collapsed
        f("option_pnl", "FLOAT"),
        f("stock_pnl_realized", "FLOAT"),
        f("stock_pnl_unrealized", "FLOAT"),
        f("dividends", "FLOAT"),
        f("option_pnl_share", "FLOAT"),
        f("reconciliation_gap", "FLOAT"),
        # Activity and risk
        f("decision_days", "INTEGER"),
        f("days_in_position", "INTEGER"),
        f("days_in_position_fraction", "FLOAT"),
        f("cycles_completed", "INTEGER"),
        f("cycles_open", "INTEGER"),
        f("puts_sold", "INTEGER"),
        f("calls_sold", "INTEGER"),
        f("win_rate", "FLOAT"),
        f("assignment_rate", "FLOAT"),
        f("max_drawdown", "FLOAT"),
        f("days_underwater", "INTEGER"),
        f("avg_collateral", "FLOAT"),
        # Fill sensitivity — a verdict that flips is not a verdict
        f("bid_fill_return", "FLOAT"),
        f("verdict_flips_on_fill", "BOOL"),
        # Provenance
        f("known_biases", "STRING", mode="REPEATED"),
        f("error", "STRING"),
    ]


def config_hash(config) -> str:
    """Stable hash of the strategy parameters that shape a verdict.

    A verdict is only interpretable alongside the thresholds that produced it;
    without this, a re-run under a changed premium floor is indistinguishable
    from a genuine change in the symbol.
    """
    # HASH DISCONTINUITY (FC-069 S1, 2026-08-04): `gap_lookback_days`,
    # `max_gap_frequency` and `execution_gap_threshold` were dropped from this
    # list when item 5 deleted GapDetector and the `gap_risk_controls` knobs.
    # `config_hash` values therefore change for all `backtest_runs` rows written
    # at or after this boundary; rows either side are not hash-comparable even
    # when every surviving parameter is identical. This is the second
    # non-comparability marker on the table, beside FC-068's `engine_version`
    # boundary — one boundary, two markers.
    keys = [
        "put_target_dte", "call_target_dte", "put_delta_range", "call_delta_range",
        "min_put_premium", "min_call_premium", "max_position_size",
        "max_stock_price", "min_stock_price",
    ]
    # NOT `getattr(config, k, None)` (FC-096 Phase C, review round 1 B1).
    #
    # Four of these nine keys are PUT-side, and `Config`'s accessors for them are
    # properties that index `_config["strategy"]` directly — so a profile without
    # the key raises `KeyError` from INSIDE the property, and `getattr`'s default
    # only swallows `AttributeError`. `config/covered_call.yaml` declares
    # `call_target_dte` and no put keys at all, so the three-argument `getattr`
    # raised `KeyError: 'put_target_dte'` on EVERY covered-call sweep, at all
    # three call sites (`runner.run_sweep`, `main.run_sweep_cmd`,
    # `sim_service`) — before a single day was replayed.
    #
    # `runner.config_target_dte` already carried this lesson; this reader did
    # not. A profile that does not declare a knob hashes it as `None`, which is
    # the honest statement: the run had no such setting.
    payload = {}
    for key in keys:
        try:
            payload[key] = getattr(config, key)
        except Exception:  # noqa: BLE001 - an absent knob is not a hash failure
            payload[key] = None

    # The verdict is not computed from config alone: these live as module
    # constants and a default argument, and changing any of them flips symbols.
    # Omitting them made a threshold change byte-identical to a symbol change —
    # the exact confusion this hash exists to prevent.
    from ..evaluate import DEFAULT_FILL_HAIRCUT
    from ..metrics import fitness as _fit
    payload["_scoring"] = {
        "min_days_in_position": _fit.MIN_DAYS_IN_POSITION,
        "risk_free_rate": _fit.RISK_FREE_RATE,
        "max_drawdown_warn": _fit.MAX_DRAWDOWN_WARN,
        "fill_haircut": DEFAULT_FILL_HAIRCUT,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class BacktestRunWriter:
    """Writes screen-mode rows to `options_wheel.backtest_runs`."""

    def __init__(self, project_id: Optional[str] = None,
                 dataset_id: str = "options_wheel") -> None:
        self._enabled = False
        self._table = None

        if not _HAS_BIGQUERY:
            logger.warning("google-cloud-bigquery not installed — screen results "
                           "will NOT be persisted")
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — screen results will NOT be persisted")
            return

        self._dataset_id = dataset_id
        try:
            self._client = bigquery.Client(project=self._project_id)
            dataset_ref = bigquery.DatasetReference(self._project_id, dataset_id)
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = "us-central1"
            self._client.create_dataset(dataset, exists_ok=True)

            table_ref = dataset_ref.table(TABLE_NAME)
            table = bigquery.Table(table_ref, schema=_schema())
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field="timestamp",
            )
            existing = self._client.create_table(table, exists_ok=True)

            # create_table(exists_ok=True) returns the EXISTING table and never
            # reconciles its schema. Left alone, the first release that adds a
            # field makes every row carry an unknown key — and insert_rows_json
            # rejects the WHOLE request rather than the offending row, so a run
            # writes ZERO rows. Reconcile additive fields explicitly.
            have = {f.name for f in (existing.schema or [])}
            missing = [f for f in _schema() if f.name not in have]
            if missing:
                existing.schema = list(existing.schema) + missing
                self._client.update_table(existing, ["schema"])
                logger.info("Extended backtest_runs schema",
                            event_category="backtest",
                            event_type="screen_schema_extended",
                            added=[f.name for f in missing])
            self._table = table_ref
            self._enabled = True
        except Exception:
            logger.warning("BacktestRunWriter init failed — results will NOT be "
                           "persisted", exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def write(self, rows: List[Dict[str, Any]]) -> bool:
        """Insert rows. Returns True only if every row landed.

        Unlike the fire-and-forget analytics path, the caller is expected to act
        on a False: a partially-written screen table reads as a complete one.
        """
        if not rows:
            return True
        if not self._enabled:
            logger.error(
                "Screen results NOT persisted — writer disabled",
                event_category="backtest", event_type="screen_write_skipped",
                rows=len(rows),
            )
            return False
        try:
            errors = self._client.insert_rows_json(self._table, rows)
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            logger.error("Screen results write FAILED",
                         event_category="backtest",
                         event_type="screen_write_failed",
                         rows=len(rows), error=str(exc)[:200])
            return False
        if errors:
            logger.error("Screen results write returned errors",
                         event_category="backtest",
                         event_type="screen_write_errors",
                         errors=str(errors)[:400])
            return False
        logger.info("Screen results persisted",
                    event_category="backtest", event_type="screen_write_ok",
                    rows=len(rows), table=f"{self._dataset_id}.{TABLE_NAME}")
        return True


def build_row(*, run_id: str, symbol: str, report, sensitivity: Optional[dict],
              cfg_hash: str, engine_version: str,
              error: Optional[str] = None,
              run_kind: str = "full", universe_size: Optional[int] = None,
              window_start=None, window_end=None) -> Dict[str, Any]:
    """Flatten a FitnessReport into one BigQuery row.

    A symbol that failed still gets a row, carrying ``error`` and a null verdict.
    Dropping it would make the run look like it screened fewer symbols than it
    attempted — the difference between "KMI is fine" and "KMI was never checked".
    """
    now = datetime.now(timezone.utc).isoformat()
    if report is None:
        # A failed symbol keeps its window: a window-scoped query would
        # otherwise drop exactly the never-checked symbols the docs say to hunt.
        return {
            "run_id": run_id, "timestamp": now, "symbol": symbol,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
            "config_hash": cfg_hash, "engine_version": engine_version,
            "run_kind": run_kind, "universe_size": universe_size,
            "verdict": None, "demote": None, "error": (error or "unknown")[:500],
        }

    from .report import KNOWN_BIASES

    verdict = report.verdict()
    return {
        "run_id": run_id,
        "timestamp": now,
        "symbol": symbol,
        "window_start": report.start.isoformat(),
        "window_end": report.end.isoformat(),
        "config_hash": cfg_hash,
        "engine_version": engine_version,
        "run_kind": run_kind,
        "universe_size": universe_size,

        "verdict": verdict,
        # Demotion is a *recommendation*; the plan keeps the decision human.
        #
        # Two verdicts deliberately do NOT set it:
        #   'insufficient' — the window did not contain a completed cycle, which
        #     is a statement about the window, not the symbol;
        #   any verdict that flips between mid-fill and bid-fill — the report's
        #     own bias footer says such a verdict "is not a verdict", so acting
        #     on it would contradict the engine's own caveat.
        "demote": (
            verdict == "unfit"
            and not (sensitivity or {}).get("verdict_flips", False)
        ),
        "verdict_reasons": report.verdict_reasons(),
        "binding_constraint": _binding(report),

        "starting_cash": report.starting_cash,
        "final_equity": report.final_equity,
        "total_return": report.total_return,
        "annualized_return": report.annualized_return,
        "annualized_return_on_collateral": report.annualized_return_on_collateral,
        "benchmark_return": report.benchmark.total_return if report.benchmark else None,
        "benchmark_price_return": report.benchmark.price_return if report.benchmark else None,
        "benchmark_dividends": report.benchmark.dividends if report.benchmark else None,
        "excess_return": report.excess_return,

        "option_pnl": report.option_pnl,
        "stock_pnl_realized": report.stock_pnl,
        "stock_pnl_unrealized": report.unrealized_stock_pnl,
        "dividends": report.dividends,
        "option_pnl_share": report.option_pnl_share,
        "reconciliation_gap": report.reconciliation_gap,

        "decision_days": report.decision_days,
        "days_in_position": report.days_in_position,
        "days_in_position_fraction": report.days_in_position_fraction,
        "cycles_completed": len(report.closed_cycles),
        "cycles_open": len(report.cycles) - len(report.closed_cycles),
        "puts_sold": report.puts_sold,
        "calls_sold": report.calls_sold,
        "win_rate": report.win_rate,
        "assignment_rate": report.assignment_rate,
        "max_drawdown": report.max_drawdown,
        "days_underwater": report.days_underwater,
        "avg_collateral": report.avg_collateral,

        "bid_fill_return": (sensitivity or {}).get("bid_return"),
        "verdict_flips_on_fill": (sensitivity or {}).get("verdict_flips"),

        "known_biases": [title for title, _detail in KNOWN_BIASES],
        "error": None,
    }


def _binding(report) -> Optional[str]:
    """The stamped `binding_constraint` — the top *stand-down* reason, or None.

    FC-069 item 12 (review fix 1): `stand_down_reasons` drops the buckets that
    mean the strategy was deployed rather than blocked. Without it the
    "already holds this underlying" bucket — which on a healthy wheel is the
    most common day type there is — would be stamped as the binding constraint
    on nearly every row, and a demotion decision read off this column would be
    reading "the wheel held positions" as "the wheel could not trade".

    A run whose only bucket is a deployment bucket has no binding constraint:
    None is the honest answer, not the deployment label.
    """
    blocked = stand_down_reasons(report.data_quality.get("blocked_days_by_reason"))
    return next(iter(blocked), None) if blocked else None
