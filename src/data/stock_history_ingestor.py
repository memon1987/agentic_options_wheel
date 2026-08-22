"""Alpaca stock daily bars → BigQuery ingest (FC-018 Phase B).

Pulls daily OHLC bars for the symbols we've actually traded and appends
them to ``options_wheel.stock_history_from_alpaca``. Used by FC-018's
vs-buy-and-hold view to compare wheel performance against a synthetic
hold of the same stock.

Universe is derived from `trades_with_outcomes` rather than the static
config — we only need bars for symbols we've actually traded. Append-only,
idempotent by (date, symbol).

See ``docs/plans/fc-018.md`` §"Data layer additions".
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


TABLE_NAME = "stock_history_from_alpaca"
DEFAULT_BACKFILL_DAYS = 365

# FC-031: always ingest benchmark bars alongside traded symbols. The
# benchmark powers the Overview comparison curve and doubles as the
# independent trading-day calendar for bot-health anomaly detection (a day
# with a benchmark bar and zero scans is a missed trading day even if the
# scheduler died completely). Same env var as the dashboard backend
# (dashboard/backend/services/bigquery.py BENCHMARK_SYMBOL) — one setting,
# two deployables, no drift.
BENCHMARK_SYMBOLS = [os.getenv("BENCHMARK_SYMBOL", "SPY")]


if _HAS_BIGQUERY:
    _SCHEMA = [
        bigquery.SchemaField("date", "DATE", mode="REQUIRED",
                             description="Trading date (ET)"),
        bigquery.SchemaField("symbol", "STRING", mode="REQUIRED",
                             description="Stock ticker"),
        bigquery.SchemaField("open", "FLOAT"),
        bigquery.SchemaField("high", "FLOAT"),
        bigquery.SchemaField("low", "FLOAT"),
        bigquery.SchemaField("close", "FLOAT"),
        bigquery.SchemaField("volume", "INTEGER"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP"),
        # FC-075 Seam 4 (DD-3). NULLABLE and additive: pre-Seam-4 rows have no
        # value, so readers segmenting by strategy use IFNULL(strategy_id, 'wheel').
        bigquery.SchemaField("strategy_id", "STRING",
                             description="Strategy profile that wrote this row; NULL = wheel (pre-Seam-4 rows)"),
    ]
else:
    _SCHEMA = []


class StockHistoryIngestor:
    """Pulls daily Alpaca stock bars and appends to BigQuery.

    The universe is derived from `trades_with_outcomes.underlying` so we
    only fetch for symbols that have actually been traded.
    """

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
            logger.warning("google-cloud-bigquery not available — StockHistoryIngestor disabled")
            return

        self._project_id = (
            project_id
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        if not self._project_id:
            logger.warning("No GCP project ID — StockHistoryIngestor disabled")
            return

        try:
            self._client = bigquery.Client(project=self._project_id)
            self._ensure_table()
            self._enabled = True
            logger.info(
                "StockHistoryIngestor initialised",
                event_category="system",
                event_type="stock_history_ingestor_initialized",
                project=self._project_id,
                dataset=self._dataset_id,
                strategy_id=self._strategy_id,
                table=TABLE_NAME,
            )
        except Exception:
            logger.warning("Failed to initialise StockHistoryIngestor — disabled",
                           exc_info=True)

    def _ensure_table(self) -> None:
        """Create the stock-history table if it doesn't exist.

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
        table.clustering_fields = ["symbol"]
        self._client.create_table(table, exists_ok=True)
        self._table_ref = table_ref

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Universe + cursor
    # ------------------------------------------------------------------

    def _traded_universe(self) -> List[str]:
        """Distinct underlyings we've actually traded, plus benchmark symbols."""
        if not self._enabled:
            return []
        query = f"""
            SELECT DISTINCT underlying
            FROM `{self._project_id}.{self._dataset_id}.trades_with_outcomes`
            WHERE underlying IS NOT NULL AND underlying != ''
        """
        try:
            traded = [row["underlying"] for row in self._client.query(query).result()]
        except Exception:
            logger.warning("Universe query failed",
                           event_category="error",
                           event_type="stock_history_ingest_universe_failed",
                           exc_info=True)
            traded = []
        # Benchmarks ride along even when the traded query fails — the
        # anomaly calendar must not depend on trade history being readable.
        return sorted(set(traded) | set(BENCHMARK_SYMBOLS))

    def _max_date_per_symbol(self, symbols: List[str]) -> Dict[str, date]:
        """For each symbol, return the most recent date we've already ingested."""
        if not self._enabled or not symbols:
            return {}
        query = f"""
            SELECT symbol, MAX(date) AS max_date
            FROM `{self._project_id}.{self._dataset_id}.{TABLE_NAME}`
            WHERE symbol IN UNNEST(@symbols)
            GROUP BY symbol
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("symbols", "STRING", symbols)]
        )
        try:
            return {row["symbol"]: row["max_date"]
                    for row in self._client.query(query, job_config=job_config).result()}
        except Exception:
            logger.warning("Max-date query failed",
                           event_category="error",
                           event_type="stock_history_ingest_cursor_failed",
                           exc_info=True)
            return {}

    # ------------------------------------------------------------------
    # Alpaca pull
    # ------------------------------------------------------------------

    def _fetch_bars(self, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
        """Call ``/v2/stocks/{symbol}/bars`` for daily bars in [start, end]."""
        if self.alpaca.config.paper_trading:
            base_url = 'https://data.alpaca.markets'
        else:
            base_url = 'https://data.alpaca.markets'  # market data uses same host
        url = f"{base_url}/v2/stocks/{symbol}/bars"
        headers = {
            'APCA-API-KEY-ID': self.alpaca.config.alpaca_api_key,
            'APCA-API-SECRET-KEY': self.alpaca.config.alpaca_secret_key,
            'Accept': 'application/json',
        }
        params = {
            'timeframe': '1Day',
            'start': start.isoformat(),
            'end': end.isoformat(),
            'limit': 10000,
            'adjustment': 'all',
            'feed': 'iex',
        }

        all_bars: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        pages = 0
        while True:
            if page_token:
                params['page_token'] = page_token
            elif 'page_token' in params:
                del params['page_token']
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            bars = payload.get('bars') or []
            all_bars.extend(bars)
            pages += 1
            page_token = payload.get('next_page_token')
            if not page_token or pages >= 20:
                break
        return all_bars

    @staticmethod
    def _bar_to_row(symbol: str, bar: Dict[str, Any], ingested_at: str) -> Optional[Dict[str, Any]]:
        ts = bar.get('t')
        if not ts:
            return None
        # Alpaca returns timestamps as ISO 8601 — normalize to ET date.
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None
        # Alpaca daily bars are stamped at 04:00 UTC for the previous trading day.
        # Use the date part of the UTC timestamp; Alpaca's convention is that
        # `t` already represents the trading session date.
        return {
            'date': dt.date().isoformat(),
            'symbol': symbol,
            'open': bar.get('o'),
            'high': bar.get('h'),
            'low': bar.get('l'),
            'close': bar.get('c'),
            'volume': bar.get('v'),
            'ingested_at': ingested_at,
        }

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    def run_once(self, backfill_days: int = DEFAULT_BACKFILL_DAYS) -> Dict[str, Any]:
        """Pull bars for the traded universe.

        For each symbol:
          - If we have prior data: fetch from `max_date + 1` to today.
          - Otherwise: fetch the last `backfill_days` days.

        Returns a per-symbol summary.
        """
        if not self._enabled:
            return {"status": "disabled", "reason": "BigQuery unavailable"}

        universe = self._traded_universe()
        if not universe:
            logger.info("StockHistoryIngestor empty universe",
                        event_category="system",
                        event_type="stock_history_ingest_empty_universe")
            return {"status": "ok", "symbols_processed": 0, "rows_inserted": 0}

        cursors = self._max_date_per_symbol(universe)
        today = date.today()
        ingested_at = datetime.now(timezone.utc).isoformat()

        per_symbol: Dict[str, Dict[str, Any]] = {}
        total_inserted = 0
        had_failure = False

        for symbol in universe:
            last_date = cursors.get(symbol)
            if last_date:
                start = last_date + timedelta(days=1)
            else:
                start = today - timedelta(days=backfill_days)
            if start >= today:
                per_symbol[symbol] = {"status": "up_to_date", "inserted": 0}
                continue

            try:
                bars = self._fetch_bars(symbol, start, today)
            except Exception as exc:
                logger.warning("StockHistoryIngestor fetch failed",
                               event_category="error",
                               event_type="stock_history_ingest_fetch_failed",
                               symbol=symbol, error=str(exc))
                per_symbol[symbol] = {"status": "fetch_error", "error": str(exc)[:200]}
                had_failure = True
                continue

            rows = [r for r in (self._bar_to_row(symbol, b, ingested_at) for b in bars) if r]
            # Stamped here rather than inside the static transform: this is the
            # single row-construction path feeding insert_rows_json. row_ids
            # below stay (date|symbol) — the dedup key must not change meaning.
            for row in rows:
                row["strategy_id"] = self._strategy_id
            # Drop today's bar — daily bar may not be finalized intraday.
            today_iso = today.isoformat()
            rows = [r for r in rows if r['date'] < today_iso]

            if not rows:
                per_symbol[symbol] = {"status": "no_new_bars", "inserted": 0}
                continue

            row_ids = [f"{r['date']}|{r['symbol']}" for r in rows]
            try:
                errors = self._client.insert_rows_json(self._table_ref, rows, row_ids=row_ids)
                if errors:
                    logger.warning("StockHistoryIngestor insert errors",
                                   event_category="error",
                                   event_type="stock_history_ingest_insert_errors",
                                   symbol=symbol, errors=str(errors)[:300])
                    per_symbol[symbol] = {"status": "insert_errors",
                                          "inserted": 0,
                                          "errors": str(errors)[:300]}
                    had_failure = True
                    continue
            except Exception as exc:
                logger.warning("StockHistoryIngestor insert failed",
                               event_category="error",
                               event_type="stock_history_ingest_insert_failed",
                               symbol=symbol, error=str(exc))
                per_symbol[symbol] = {"status": "insert_failed", "error": str(exc)[:200]}
                had_failure = True
                continue

            per_symbol[symbol] = {"status": "ok", "inserted": len(rows)}
            total_inserted += len(rows)

        logger.info("StockHistoryIngestor completed",
                    event_category="system",
                    event_type="stock_history_ingest_completed",
                    symbols=len(universe),
                    rows_inserted=total_inserted,
                    had_failure=had_failure)

        return {
            "status": "partial" if had_failure else "ok",
            "symbols_processed": len(universe),
            "rows_inserted": total_inserted,
            "per_symbol": per_symbol,
        }
