"""Alpaca portfolio history → BigQuery ingest (FC-012 Phase 2.5).

Pulls daily equity / P&L history from
``/v2/account/portfolio/history?period=1M&timeframe=1D`` and appends to
``options_wheel.equity_history_from_alpaca``. Idempotent by ``date``;
finalized-days-only (skips today, since today's row would flap intraday).

Replaces the PORTFOLIO rows of ``AnalyticsWriter.write_position_snapshot``.
Alpaca's endpoint does not return ``cash`` or ``buying_power`` historically —
only ``equity``. That's acceptable for the dashboard's current equity-curve
view; if cash/buying-power history is needed later, it must be written
separately.

See ``docs/plans/fc-012.md`` §2.5.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, date, timedelta
from typing import Any, Dict, List, Optional

import requests
import structlog

from src.api.alpaca_client import AlpacaClient

logger = structlog.get_logger(__name__)

try:
    from google.cloud import bigquery
    _HAS_BIGQUERY = True
except ImportError:
    _HAS_BIGQUERY = False
    bigquery = None  # type: ignore


TABLE_NAME = "equity_history_from_alpaca"
DEFAULT_PERIOD = "1M"


if _HAS_BIGQUERY:
    _SCHEMA = [
        bigquery.SchemaField("date", "DATE", mode="REQUIRED",
                             description="ET calendar date"),
        bigquery.SchemaField("timestamp_unix", "INTEGER",
                             description="Unix epoch seconds from Alpaca"),
        bigquery.SchemaField("equity", "FLOAT",
                             description="Portfolio equity (stock + option market value + cash)"),
        bigquery.SchemaField("profit_loss", "FLOAT"),
        bigquery.SchemaField("profit_loss_pct", "FLOAT"),
        bigquery.SchemaField("base_value", "FLOAT",
                             description="Baseline equity for P&L computation"),
        bigquery.SchemaField("base_value_asof", "DATE"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP"),
        # FC-075 Seam 4 (DD-3). NULLABLE and additive: pre-Seam-4 rows have no
        # value, so readers segmenting by strategy use IFNULL(strategy_id, 'wheel').
        bigquery.SchemaField("strategy_id", "STRING",
                             description="Strategy profile that wrote this row; NULL = wheel (pre-Seam-4 rows)"),
    ]
else:
    _SCHEMA = []


class PortfolioHistoryIngestor:
    """Pulls Alpaca daily equity history and appends to BigQuery."""

    def __init__(self, alpaca: AlpacaClient,
                 project_id: Optional[str] = None,
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
            logger.warning("google-cloud-bigquery not available — PortfolioHistoryIngestor disabled")
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — PortfolioHistoryIngestor disabled")
            return

        try:
            self._client = bigquery.Client(project=self._project_id)
            self._ensure_table()
            self._enabled = True
            logger.info(
                "PortfolioHistoryIngestor initialised",
                event_category="system",
                event_type="portfolio_history_ingestor_initialized",
                project=self._project_id,
                dataset=self._dataset_id,
                strategy_id=self._strategy_id,
                table=TABLE_NAME,
            )
        except Exception:
            logger.warning("Failed to initialise PortfolioHistoryIngestor — disabled",
                           exc_info=True)

    def _ensure_table(self) -> None:
        """Create the equity-history table if it doesn't exist.

        The dataset itself is NEVER created here (FC-075 Seam 4): datasets are
        an operator provisioning step, so a missing dataset raises NotFound and
        leaves this ingestor disabled.
        """
        dataset_ref = bigquery.DatasetReference(self._project_id, self._dataset_id)

        table_ref = dataset_ref.table(TABLE_NAME)
        table = bigquery.Table(table_ref, schema=_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="date",
        )
        self._client.create_table(table, exists_ok=True)
        self._table_ref = table_ref

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Alpaca pull
    # ------------------------------------------------------------------

    def _fetch(self, period: str = DEFAULT_PERIOD) -> Dict[str, Any]:
        """Call `/v2/account/portfolio/history`.

        The TradingClient SDK does not expose this endpoint, so we call REST
        directly, matching the pattern in ``AlpacaClient.get_account_activities``.
        """
        if self.alpaca.config.paper_trading:
            base_url = 'https://paper-api.alpaca.markets'
        else:
            base_url = 'https://api.alpaca.markets'
        url = f"{base_url}/v2/account/portfolio/history"
        headers = {
            'APCA-API-KEY-ID': self.alpaca.config.alpaca_api_key,
            'APCA-API-SECRET-KEY': self.alpaca.config.alpaca_secret_key,
            'Accept': 'application/json',
        }
        params = {'period': period, 'timeframe': '1D'}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------

    @staticmethod
    def _response_to_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert the Alpaca parallel-arrays response to row dicts.

        Alpaca returns the response as four parallel arrays
        (``timestamp``, ``equity``, ``profit_loss``, ``profit_loss_pct``)
        with metadata (``base_value``, ``base_value_asof``). Each ``timestamp``
        is a unix-epoch-seconds value at 00:00 ET for the trading day.

        Drops rows where ``equity`` is null (non-trading days sometimes appear
        with null values). Returns rows sorted ascending by date.
        """
        timestamps = payload.get("timestamp") or []
        equity = payload.get("equity") or []
        pnl = payload.get("profit_loss") or []
        pnl_pct = payload.get("profit_loss_pct") or []
        base_value = payload.get("base_value")
        base_value_asof = payload.get("base_value_asof")

        n = min(len(timestamps), len(equity), len(pnl), len(pnl_pct))
        ingested_at = datetime.now(timezone.utc).isoformat()
        rows: List[Dict[str, Any]] = []
        for i in range(n):
            eq = equity[i]
            ts = timestamps[i]
            if eq is None or ts is None:
                continue
            row_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            rows.append({
                "date": row_date.isoformat(),
                "timestamp_unix": int(ts),
                "equity": float(eq),
                "profit_loss": float(pnl[i]) if pnl[i] is not None else None,
                "profit_loss_pct": float(pnl_pct[i]) if pnl_pct[i] is not None else None,
                "base_value": float(base_value) if base_value is not None else None,
                "base_value_asof": base_value_asof,
                "ingested_at": ingested_at,
            })
        rows.sort(key=lambda r: r["date"])
        return rows

    # ------------------------------------------------------------------
    # BQ helpers
    # ------------------------------------------------------------------

    def _existing_dates(self, dates: List[str]) -> set:
        if not self._enabled or not dates:
            return set()
        query = f"""
            SELECT date
            FROM `{self._project_id}.{self._dataset_id}.{TABLE_NAME}`
            WHERE date IN UNNEST(@dates)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("dates", "DATE", dates)
            ]
        )
        try:
            result = self._client.query(query, job_config=job_config).result()
            return {r["date"].isoformat() for r in result}
        except Exception:
            logger.warning("Existing-date check failed — may double-insert",
                           exc_info=True)
            return set()

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    def run_once(self, period: str = DEFAULT_PERIOD) -> Dict[str, Any]:
        """Fetch portfolio history and append finalized days to BigQuery.

        Skips today's row (flaps intraday). Idempotent: existing dates are
        filtered out before insert.
        """
        if not self._enabled:
            return {"status": "disabled", "reason": "BigQuery unavailable"}

        logger.info("PortfolioHistoryIngestor run_once starting",
                    event_category="system",
                    event_type="portfolio_history_ingest_started",
                    period=period)

        try:
            payload = self._fetch(period=period)
        except Exception as exc:
            logger.error("PortfolioHistoryIngestor fetch failed",
                         event_category="error",
                         event_type="portfolio_history_ingest_fetch_failed",
                         error=str(exc),
                         exc_info=True)
            return {"status": "failed", "reason": "fetch_error", "error": str(exc)}

        rows = self._response_to_rows(payload)
        # Stamped here rather than inside the static transform: this is the
        # single row-construction path feeding insert_rows_json.
        for row in rows:
            row["strategy_id"] = self._strategy_id

        # Drop today's and future rows — only write finalized days.
        today_iso = date.today().isoformat()
        finalized = [r for r in rows if r["date"] < today_iso]
        skipped_today = len(rows) - len(finalized)

        if not finalized:
            logger.info("PortfolioHistoryIngestor no finalized rows",
                        event_category="system",
                        event_type="portfolio_history_ingest_empty")
            return {"status": "ok", "fetched": len(rows),
                    "inserted": 0, "skipped_existing": 0,
                    "skipped_today": skipped_today}

        # Idempotency: drop dates already in the table
        existing = self._existing_dates([r["date"] for r in finalized])
        new_rows = [r for r in finalized if r["date"] not in existing]

        if not new_rows:
            logger.info("PortfolioHistoryIngestor all rows already present",
                        event_category="system",
                        event_type="portfolio_history_ingest_all_duplicate",
                        fetched=len(finalized))
            return {"status": "ok", "fetched": len(finalized),
                    "inserted": 0, "skipped_existing": len(existing),
                    "skipped_today": skipped_today}

        row_ids = [r["date"] for r in new_rows]  # date is a natural PK
        try:
            errors = self._client.insert_rows_json(
                self._table_ref, new_rows, row_ids=row_ids,
            )
            if errors:
                logger.error("PortfolioHistoryIngestor insert errors",
                             event_category="error",
                             event_type="portfolio_history_ingest_insert_errors",
                             errors=str(errors)[:500])
                return {"status": "partial",
                        "fetched": len(finalized), "inserted": 0,
                        "skipped_existing": len(existing),
                        "skipped_today": skipped_today,
                        "errors": str(errors)[:500]}
        except Exception as exc:
            logger.error("PortfolioHistoryIngestor insert failed",
                         event_category="error",
                         event_type="portfolio_history_ingest_insert_failed",
                         error=str(exc),
                         exc_info=True)
            return {"status": "failed", "reason": "insert_error", "error": str(exc)}

        logger.info("PortfolioHistoryIngestor inserted rows",
                    event_category="system",
                    event_type="portfolio_history_ingest_completed",
                    fetched=len(finalized),
                    inserted=len(new_rows),
                    skipped_existing=len(existing),
                    skipped_today=skipped_today)

        return {
            "status": "ok",
            "fetched": len(finalized),
            "inserted": len(new_rows),
            "skipped_existing": len(existing),
            "skipped_today": skipped_today,
        }
