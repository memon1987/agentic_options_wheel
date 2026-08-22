"""Alpaca Account Activities → BigQuery ingest (FC-012 Phase 2.1).

Pulls FILL, OPASN, and OPEXP activities from Alpaca's
``/v2/account/activities`` endpoint and appends them to the
``options_wheel.trades_from_activities`` table. Append-only; idempotent by
``activity_id``. Outcome projection happens read-side via the
``trades_with_outcomes`` view — this module does not UPDATE rows.

Cursor strategy:
  - FILL rows use ``transaction_time`` (ISO 8601 with ms).
  - OPASN/OPEXP rows use ``date`` (day-granular) + ``created_at``.
  - ``MAX(transaction_time) FROM trades_from_activities`` is the cursor for
    the next pull. Separately, ``MAX(activity_date)`` is tracked for
    OPASN/OPEXP (which arrive the morning after and carry only ``date``).

See ``docs/plans/fc-012.md`` §2.1.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import structlog

from src.api.alpaca_client import AlpacaClient
from src.utils.option_symbols import parse_option_symbol

logger = structlog.get_logger(__name__)

try:
    from google.cloud import bigquery
    _HAS_BIGQUERY = True
except ImportError:
    _HAS_BIGQUERY = False
    bigquery = None  # type: ignore


# FC-031 added FEE: regulatory/options fees, JNLC-shaped (no symbol/qty,
# net_amount negative). ~$24 lifetime on paper but a first-class
# reconciliation component so nothing breaks at live go-live.
ACTIVITY_TYPES = "FILL,OPASN,OPEXP,OPTRD,JNLC,FEE"
TABLE_NAME = "trades_from_activities"


if _HAS_BIGQUERY:
    _SCHEMA = [
        bigquery.SchemaField("activity_id", "STRING", mode="REQUIRED",
                             description="Unique Alpaca activity ID (idempotency key)"),
        bigquery.SchemaField("activity_type", "STRING",
                             description="FILL, OPASN, OPEXP, OPTRD, JNLC"),
        bigquery.SchemaField("transaction_time", "TIMESTAMP",
                             description="FILL: transaction_time; others: created_at"),
        bigquery.SchemaField("activity_date", "DATE",
                             description="ET calendar date of the activity"),
        bigquery.SchemaField("order_id", "STRING"),
        bigquery.SchemaField("symbol", "STRING",
                             description="OCC for option FILLs, ticker for stock OPTRD, blank for JNLC"),
        bigquery.SchemaField("underlying", "STRING"),
        bigquery.SchemaField("side", "STRING",
                             description="sell_short, buy_to_close, buy, sell, etc."),
        bigquery.SchemaField("qty", "FLOAT"),
        bigquery.SchemaField("price", "FLOAT"),
        bigquery.SchemaField("leaves_qty", "FLOAT"),
        bigquery.SchemaField("cum_qty", "FLOAT"),
        bigquery.SchemaField("order_status", "STRING"),
        bigquery.SchemaField("group_id", "STRING",
                             description="Join key for OPASN/OPEXP back to originating order"),
        bigquery.SchemaField("option_type", "STRING",
                             description="call, put, or null for stock"),
        bigquery.SchemaField("strike_price", "FLOAT"),
        bigquery.SchemaField("expiration", "DATE"),
        bigquery.SchemaField("dte_at_event", "INTEGER"),
        bigquery.SchemaField("premium_total", "FLOAT",
                             description="price * qty * 100 for option FILLs"),
        bigquery.SchemaField("net_amount", "FLOAT",
                             description="Cash flow from this activity. Critical for OPTRD "
                                         "(share-side cash flow on assignment/called-away) and "
                                         "JNLC (account deposits/withdrawals). Null for FILL/OPASN/OPEXP."),
        bigquery.SchemaField("ingested_at", "TIMESTAMP",
                             description="When this row was written to BQ"),
        # FC-075 Seam 4 (DD-3). NULLABLE and additive: pre-Seam-4 rows have no
        # value, so readers segmenting by strategy use IFNULL(strategy_id, 'wheel').
        bigquery.SchemaField("strategy_id", "STRING",
                             description="Strategy profile that wrote this row; NULL = wheel (pre-Seam-4 rows)"),
    ]
else:
    _SCHEMA = []


class ActivitiesIngestor:
    """Pulls Alpaca account activities and appends to BigQuery (append-only)."""

    def __init__(self, alpaca: AlpacaClient, project_id: Optional[str] = None,
                 *, dataset_id: str, strategy_id: str) -> None:
        # dataset_id/strategy_id are required and keyword-only (FC-075 Seam 4):
        # a defaulted dataset is how a second strategy profile silently writes
        # the wheel's tables. Assigned before any early return so a disabled
        # ingestor still reports the identity it was constructed with.
        self.alpaca = alpaca
        self._enabled = False
        self._client = None
        self._table_ref = None
        self._dataset_id = dataset_id
        self._strategy_id = strategy_id

        if not _HAS_BIGQUERY:
            logger.warning("google-cloud-bigquery not available — ActivitiesIngestor disabled")
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — ActivitiesIngestor disabled")
            return

        try:
            self._client = bigquery.Client(project=self._project_id)
            self._ensure_table()
            self._enabled = True
            logger.info(
                "ActivitiesIngestor initialised",
                event_category="system",
                event_type="activities_ingestor_initialized",
                project=self._project_id,
                dataset=self._dataset_id,
                strategy_id=self._strategy_id,
                table=TABLE_NAME,
            )
        except Exception:
            logger.warning("Failed to initialise ActivitiesIngestor — disabled",
                           exc_info=True)

    def _ensure_table(self) -> None:
        """Create the trades_from_activities table if it doesn't exist.

        The dataset itself is NEVER created here (FC-075 Seam 4): datasets are
        an operator provisioning step, so a missing dataset raises NotFound and
        leaves this ingestor disabled.
        """
        dataset_ref = bigquery.DatasetReference(self._project_id, self._dataset_id)

        table_ref = dataset_ref.table(TABLE_NAME)
        table = bigquery.Table(table_ref, schema=_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="transaction_time",
        )
        table.clustering_fields = ["symbol", "activity_type"]
        self._client.create_table(table, exists_ok=True)
        self._table_ref = table_ref

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Cursor helpers
    # ------------------------------------------------------------------

    def _read_cursor(self) -> tuple[Optional[str], bool]:
        """Return the earliest 'after' date that covers every ingested activity type.

        FILL rows use ``transaction_time``; everything else (OPASN, OPEXP,
        OPTRD, JNLC) uses ``activity_date``. We take the MIN of all maxes as
        the cursor so no type is missed. If a type has no rows yet (e.g.,
        first run after adding JNLC/OPTRD to ACTIVITY_TYPES), its date is
        NULL and excluded from the MIN; the dedup check on activity_id then
        prevents re-ingesting any rows the existing types overlap.

        For a true clean-slate backfill of newly-added activity types when the
        existing types already have rows past the historical window of the
        new types, callers should pass ``after_override`` to ``run_once``.
        """
        if not self._enabled:
            return None, True

        query = f"""
            SELECT
              MAX(CASE WHEN activity_type = 'FILL' THEN DATE(transaction_time, 'America/New_York') END) AS max_fill_date,
              MAX(CASE WHEN activity_type IN ('OPASN','OPEXP') THEN activity_date END) AS max_opevent_date,
              MAX(CASE WHEN activity_type IN ('OPTRD','JNLC','FEE') THEN activity_date END) AS max_cash_date
            FROM `{self._project_id}.{self._dataset_id}.{TABLE_NAME}`
        """
        try:
            row = next(iter(self._client.query(query).result()), None)
            if row is None:
                return None, True
            candidates = [d for d in (row["max_fill_date"],
                                      row["max_opevent_date"],
                                      row["max_cash_date"]) if d is not None]
            if not candidates:
                return None, True
            cursor_date = min(candidates)
            return cursor_date.isoformat(), True
        except Exception:
            logger.warning("Cursor read failed — falling back to full backfill",
                           event_category="error",
                           event_type="activities_ingest_cursor_failed",
                           exc_info=True)
            return None, False

    def _existing_ids(self, activity_ids: Iterable[str]) -> set:
        """Return the subset of ``activity_ids`` that already exist in the table."""
        if not self._enabled:
            return set()
        ids = [i for i in activity_ids if i]
        if not ids:
            return set()

        # Parametrized IN clause
        query = f"""
            SELECT activity_id
            FROM `{self._project_id}.{self._dataset_id}.{TABLE_NAME}`
            WHERE activity_id IN UNNEST(@ids)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        )
        try:
            result = self._client.query(query, job_config=job_config).result()
            return {row["activity_id"] for row in result}
        except Exception:
            logger.warning("Existing-id check failed — may double-insert",
                           exc_info=True)
            return set()

    # ------------------------------------------------------------------
    # Pull + normalize
    # ------------------------------------------------------------------

    def _pull_all(self, after: Optional[str]) -> List[Dict[str, Any]]:
        """Paginate through Alpaca activities starting from ``after``.

        Uses ascending order so the ``page_token`` walk produces a stable
        forward-only stream (newest activities last).
        """
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        pages = 0
        while True:
            activities = self.alpaca.get_account_activities(
                activity_types=ACTIVITY_TYPES,
                after=after,
                page_size=100,
                page_token=page_token,
                direction="asc",
            )
            if not activities:
                break
            results.extend(activities)
            pages += 1
            if len(activities) < 100:
                break
            page_token = activities[-1].get("id")
            if pages >= 100:
                logger.warning("ActivitiesIngestor: hit 100-page cap",
                               event_category="system",
                               event_type="activities_ingest_page_cap",
                               collected=len(results))
                break
        return results

    @staticmethod
    def _normalize(activity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert a raw Alpaca activity dict into a table row.

        Handles five activity types with subtly different shapes:
          - FILL: per-contract execution. OCC symbol, transaction_time (ms),
            side, qty, price.
          - OPASN: assignment/called-away. OCC symbol, date + created_at,
            qty, no price (net_amount=0).
          - OPEXP: expiration. OCC symbol, date + created_at, qty.
          - OPTRD: daily share-side rollup per underlying. Symbol = ticker
            (NOT OCC), date + created_at, qty (signed: + buy, − sell), price,
            net_amount (real cash flow on share movement).
          - JNLC: cash journal (deposits/withdrawals). No symbol, no qty,
            date + created_at, net_amount.

        Returns ``None`` if the activity is malformed (no ID, no usable
        timestamp). Activities with no ``transaction_time`` and no
        ``created_at`` and no ``date`` are dropped.
        """
        activity_id = activity.get("id")
        if not activity_id:
            return None

        activity_type = activity.get("activity_type", "")
        symbol = activity.get("symbol", "") or ""

        # Timestamp normalization. BQ partition field must be non-null.
        # Preference order: transaction_time (FILL) → created_at (OP*)
        # → synthesized from date (oldest fallback for malformed payloads).
        tx_time = activity.get("transaction_time") or activity.get("created_at")
        activity_date = activity.get("date")
        if not tx_time and activity_date:
            tx_time = f"{activity_date}T00:00:00Z"
        if not tx_time:
            return None

        def _f(key: str) -> Optional[float]:
            v = activity.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        qty = _f("qty")
        price = _f("price")
        net_amount = _f("net_amount")

        # OCC symbol parsing — only for FILL on option contracts.
        # OPTRD symbol is the underlying ticker (e.g., 'AMZN'), not OCC.
        # OPASN/OPEXP symbol is OCC (e.g., 'AMZN260422C00240000').
        # JNLC has no symbol.
        option_type: Optional[str] = None
        strike_price: Optional[float] = None
        expiration: Optional[str] = None
        dte_at_event: Optional[int] = None
        underlying = symbol

        if symbol and len(symbol) >= 15 and any(c.isdigit() for c in symbol):
            parsed = parse_option_symbol(symbol)
            if parsed.get("option_type") in ("put", "call"):
                option_type = parsed["option_type"]
                strike_price = parsed.get("strike_price") or None
                expiration = parsed.get("expiration_date")
                dte_at_event = parsed.get("dte")
                underlying = parsed.get("underlying") or symbol

        premium_total: Optional[float] = None
        if option_type and qty is not None and price is not None:
            premium_total = price * abs(qty) * 100

        return {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "transaction_time": tx_time,
            "activity_date": activity_date,
            "order_id": activity.get("order_id", ""),
            "symbol": symbol,
            "underlying": underlying,
            "side": activity.get("side", ""),
            "qty": qty,
            "price": price,
            "leaves_qty": _f("leaves_qty"),
            "cum_qty": _f("cum_qty"),
            "order_status": activity.get("order_status", ""),
            "group_id": activity.get("group_id", ""),
            "option_type": option_type,
            "strike_price": strike_price,
            "expiration": expiration,
            "dte_at_event": dte_at_event,
            "premium_total": premium_total,
            "net_amount": net_amount,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    def run_once(self, after_override: Optional[str] = None) -> Dict[str, Any]:
        """Pull new activities and append them to BigQuery. Idempotent.

        Args:
            after_override: ISO date string. When provided, bypasses the
                normal cursor logic and starts the pull from this date.
                Used for backfills when newly-added activity types
                (e.g., JNLC, OPTRD added in FC-019) have no rows in the
                table yet.

        Returns a summary dict for the endpoint response.
        """
        if not self._enabled:
            return {"status": "disabled", "reason": "BigQuery unavailable"}

        if after_override:
            after, cursor_ok = after_override, True
        else:
            after, cursor_ok = self._read_cursor()
        logger.info("ActivitiesIngestor run_once starting",
                    event_category="system",
                    event_type="activities_ingest_started",
                    after=after,
                    cursor_ok=cursor_ok,
                    override=bool(after_override))

        try:
            raw_activities = self._pull_all(after)
        except Exception as exc:
            logger.error("ActivitiesIngestor pull failed",
                         event_category="error",
                         event_type="activities_ingest_pull_failed",
                         error=str(exc),
                         exc_info=True)
            return {"status": "failed", "reason": "pull_error",
                    "error": str(exc), "cursor_ok": cursor_ok}

        if not raw_activities:
            logger.info("ActivitiesIngestor no new activities",
                        event_category="system",
                        event_type="activities_ingest_no_new")
            return {"status": "ok", "fetched": 0, "inserted": 0,
                    "skipped": 0, "cursor_ok": cursor_ok}

        rows: List[Dict[str, Any]] = []
        malformed = 0
        for a in raw_activities:
            row = self._normalize(a)
            if row is None:
                malformed += 1
            else:
                # Stamped here rather than inside the static normalizer: this is
                # the single row-construction path feeding insert_rows_json.
                row["strategy_id"] = self._strategy_id
                rows.append(row)

        # Dedup against existing
        candidate_ids = [r["activity_id"] for r in rows]
        existing = self._existing_ids(candidate_ids)
        new_rows = [r for r in rows if r["activity_id"] not in existing]

        if not new_rows:
            logger.info("ActivitiesIngestor all activities already present",
                        event_category="system",
                        event_type="activities_ingest_all_duplicate",
                        fetched=len(rows),
                        malformed=malformed)
            return {"status": "ok", "fetched": len(rows),
                    "inserted": 0, "skipped": len(rows),
                    "malformed": malformed, "cursor_ok": cursor_ok}

        # Pass row_ids= for streaming-insert dedup (BigQuery best-effort).
        row_ids = [r["activity_id"] for r in new_rows]
        try:
            errors = self._client.insert_rows_json(
                self._table_ref, new_rows, row_ids=row_ids,
            )
            if errors:
                logger.error("ActivitiesIngestor BQ insert errors",
                             event_category="error",
                             event_type="activities_ingest_insert_errors",
                             errors=str(errors)[:500])
                # Per-row errors are not a retry-worthy failure; the scheduler
                # should not storm-retry. Surface as partial with 200 status.
                return {"status": "partial", "fetched": len(rows),
                        "inserted": 0, "skipped": len(existing),
                        "errors": str(errors)[:500],
                        "malformed": malformed, "cursor_ok": cursor_ok}
        except Exception as exc:
            logger.error("ActivitiesIngestor BQ insert failed",
                         event_category="error",
                         event_type="activities_ingest_insert_failed",
                         error=str(exc),
                         exc_info=True)
            return {"status": "failed", "reason": "insert_error",
                    "error": str(exc), "cursor_ok": cursor_ok}

        logger.info("ActivitiesIngestor inserted rows",
                    event_category="system",
                    event_type="activities_ingest_completed",
                    fetched=len(rows),
                    inserted=len(new_rows),
                    skipped=len(existing),
                    malformed=malformed)

        return {
            "status": "ok",
            "fetched": len(rows),
            "inserted": len(new_rows),
            "skipped": len(existing),
            "malformed": malformed,
            "cursor_ok": cursor_ok,
        }
