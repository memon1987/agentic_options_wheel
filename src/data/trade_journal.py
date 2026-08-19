"""Persistent trade journal backed by BigQuery.

Records every executed trade to a BigQuery table for P&L tracking,
audit trail, and historical analysis. Designed to be **optional** --
if the BigQuery client is unavailable or misconfigured the journal
degrades to a no-op so the trading system is never disrupted.
"""

import os
import time
import structlog
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ..utils.option_symbols import strict_option_type

logger = structlog.get_logger(__name__)

try:
    from google.cloud import bigquery  # type: ignore
    _HAS_BIGQUERY = True
except ImportError:
    _HAS_BIGQUERY = False
    logger.warning("google-cloud-bigquery not installed -- TradeJournal will be a no-op")

# --------------------------------------------------------------------------- #
# Schema definition
# --------------------------------------------------------------------------- #

_TABLE_SCHEMA = [
    bigquery.SchemaField("order_id", "STRING", description="Broker order ID"),
    bigquery.SchemaField("client_order_id", "STRING", description="Client-generated order ID"),
    bigquery.SchemaField("symbol", "STRING", description="Option symbol (OCC format)"),
    bigquery.SchemaField("underlying", "STRING", description="Underlying stock ticker"),
    bigquery.SchemaField("option_type", "STRING", description="put or call"),
    bigquery.SchemaField("side", "STRING", description="buy or sell"),
    bigquery.SchemaField("qty", "INTEGER", description="Number of contracts"),
    bigquery.SchemaField("strike_price", "FLOAT", description="Option strike price"),
    bigquery.SchemaField("premium", "FLOAT", description="Per-contract premium (mid price)"),
    bigquery.SchemaField("limit_price", "FLOAT", description="Limit price on the order"),
    bigquery.SchemaField("fill_price", "FLOAT", description="Actual fill price (if filled)"),
    bigquery.SchemaField("bid", "FLOAT", description="Best bid at order time"),
    bigquery.SchemaField("ask", "FLOAT", description="Best ask at order time"),
    bigquery.SchemaField("mid_price", "FLOAT", description="Mid-price at order time"),
    bigquery.SchemaField("total_premium", "FLOAT", description="Total premium collected (premium * qty * 100)"),
    bigquery.SchemaField("collateral", "FLOAT", description="Collateral required (strike * qty * 100)"),
    bigquery.SchemaField("status", "STRING", description="Order status (submitted, filled, etc.)"),
    bigquery.SchemaField("strategy", "STRING", description="Strategy name (sell_put, sell_call, etc.)"),
    bigquery.SchemaField("expiration", "STRING", description="Option expiration date"),
    bigquery.SchemaField("dte", "INTEGER", description="Days to expiration at entry"),
    bigquery.SchemaField("roi", "FLOAT", description="Return on investment at entry"),
    bigquery.SchemaField("outcome", "STRING", description="Terminal outcome (expired, assigned, closed, active)"),
    bigquery.SchemaField("outcome_price", "FLOAT", description="Price at outcome (assignment/close price)"),
    bigquery.SchemaField("realized_pnl", "FLOAT", description="Realized P&L if position closed"),
    bigquery.SchemaField("filled_at", "TIMESTAMP", description="Timestamp when order was actually filled"),
    bigquery.SchemaField("timestamp", "TIMESTAMP", description="UTC timestamp of the record"),
] if _HAS_BIGQUERY else []


class TradeJournal:
    """Write trade records to BigQuery for persistent P&L tracking.

    If BigQuery is not available (missing library, no project ID, or table
    creation fails) the instance becomes a silent no-op.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: str = "options_wheel",
        table_id: str = "trades",
    ):
        self._enabled = False

        if not _HAS_BIGQUERY:
            return

        self._project_id = project_id or os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not self._project_id:
            logger.warning("No GCP project ID configured -- TradeJournal disabled")
            return

        self._dataset_id = dataset_id
        self._table_id = table_id

        try:
            self._client = bigquery.Client(project=self._project_id)
            self._ensure_dataset_and_table()
            self._enabled = True
            logger.info(
                "TradeJournal initialised",
                project=self._project_id,
                dataset=self._dataset_id,
                table=self._table_id,
            )
        except Exception:
            logger.warning("Failed to initialise BigQuery client -- TradeJournal disabled", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dataset_and_table(self) -> None:
        """Create dataset and table if they do not already exist."""
        dataset_ref = bigquery.DatasetReference(self._project_id, self._dataset_id)

        # Dataset
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "us-central1"
        self._client.create_dataset(dataset, exists_ok=True)

        # Table
        table_ref = dataset_ref.table(self._table_id)
        table = bigquery.Table(table_ref, schema=_TABLE_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="timestamp",
        )
        self._client.create_table(table, exists_ok=True)
        self._table_ref = table_ref

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_trade(self, trade_data: dict) -> None:
        """Insert a single trade row into BigQuery.

        ``trade_data`` is a flat dict whose keys should align with the
        table schema.  Unknown keys are silently dropped; missing keys
        default to ``None``.

        Args:
            trade_data: Dictionary of trade fields.
        """
        if not self._enabled:
            return

        # Resolve the option leg (FC-067). The audit row must record the leg that
        # was ACTUALLY TRADED, so resolve the OCC ``option_symbol`` FIRST — that
        # is what ``execute_batch`` routes on, and when a producer's declared
        # ``type`` contradicts the symbol the engine trades by the symbol and logs
        # ``opportunity_type_mismatch`` (FC-048). Labelling from the declared key
        # would journal a leg the engine did not trade, which is the very
        # label-vs-contract drift FC-067 exists to end. Resolution order:
        #   OCC symbol → explicit option_type → scanner 'type' → infer from strategy.
        # Old code read 'option_type'/'strategy' directly; the scanner supplies
        # neither (it uses 'type'), so EVERY row defaulted to put/sell_put —
        # covered calls were silently mislabeled. Write null rather than guess a
        # leg when nothing resolves (the silent 'put' default was the bug).
        option_type = (
            strict_option_type(trade_data.get("option_symbol"))
            or trade_data.get("option_type")
            or trade_data.get("type")
            or ("call" if trade_data.get("strategy") == "sell_call"
                else "put" if trade_data.get("strategy") == "sell_put"
                else None)
        )
        # Strategy follows the resolved leg. This system only ever *sells* the
        # leg (side='sell'), so a declared strategy that contradicts the leg
        # actually traded is wrong by construction — the contract wins (FC-048).
        # Only when the leg is unresolved do we keep whatever strategy was declared.
        strategy = (
            "sell_call" if option_type == "call"
            else "sell_put" if option_type == "put"
            else trade_data.get("strategy")
        )

        # Build a row that matches the schema exactly.
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "order_id": _str_or_none(trade_data.get("order_id")),
            "client_order_id": _str_or_none(trade_data.get("client_order_id")),
            "symbol": trade_data.get("option_symbol") or trade_data.get("symbol"),
            "underlying": trade_data.get("underlying") or trade_data.get("symbol"),
            "option_type": option_type,
            "side": trade_data.get("side", "sell"),
            "qty": trade_data.get("contracts") or trade_data.get("qty"),
            "strike_price": trade_data.get("strike_price"),
            "premium": trade_data.get("premium"),
            "limit_price": trade_data.get("limit_price"),
            "fill_price": trade_data.get("fill_price"),
            "bid": trade_data.get("bid"),
            "ask": trade_data.get("ask"),
            "mid_price": trade_data.get("mid_price"),
            "total_premium": _calc_total_premium(trade_data),
            "collateral": _calc_collateral(trade_data),
            "status": trade_data.get("status", "submitted"),
            "strategy": strategy,
            "expiration": trade_data.get("expiration"),
            "dte": trade_data.get("dte"),
            "roi": trade_data.get("roi"),
            "outcome": trade_data.get("outcome"),
            "outcome_price": trade_data.get("outcome_price"),
            "realized_pnl": trade_data.get("realized_pnl"),
            "filled_at": trade_data.get("filled_at"),
            "timestamp": trade_data.get("timestamp") or now,
        }

        try:
            errors = self._client.insert_rows_json(self._table_ref, [row])
            if errors:
                logger.error("BigQuery insert errors", errors=errors)
        except Exception:
            logger.error("Failed to record trade to BigQuery", exc_info=True)

    def get_trades(self, days: int = 30) -> list:
        """Return recent trades from the journal.

        Args:
            days: Number of days to look back (default 30).

        Returns:
            List of dicts, one per trade row.
        """
        if not self._enabled:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        query = f"""
            SELECT *
            FROM `{self._project_id}.{self._dataset_id}.{self._table_id}`
            WHERE DATE(timestamp) >= '{cutoff}'
            ORDER BY timestamp DESC
        """

        try:
            result = self._client.query(query).result()
            return [dict(row) for row in result]
        except Exception:
            logger.error("Failed to query trades from BigQuery", exc_info=True)
            return []

    def get_pnl_summary(self) -> dict:
        """Aggregate P&L by symbol and strategy.

        Returns:
            Dict with ``by_symbol`` and ``by_strategy`` keys, each
            mapping to a list of summary rows.
        """
        if not self._enabled:
            return {"by_symbol": [], "by_strategy": []}

        base_table = f"`{self._project_id}.{self._dataset_id}.{self._table_id}`"

        by_symbol_query = f"""
            SELECT
                underlying,
                COUNT(*) AS trade_count,
                SUM(total_premium) AS total_premium,
                SUM(collateral) AS total_collateral,
                SAFE_DIVIDE(SUM(total_premium), SUM(collateral)) AS avg_roi,
                MIN(timestamp) AS first_trade,
                MAX(timestamp) AS last_trade
            FROM {base_table}
            GROUP BY underlying
            ORDER BY total_premium DESC
        """

        by_strategy_query = f"""
            SELECT
                strategy,
                COUNT(*) AS trade_count,
                SUM(total_premium) AS total_premium,
                SUM(collateral) AS total_collateral,
                SAFE_DIVIDE(SUM(total_premium), SUM(collateral)) AS avg_roi
            FROM {base_table}
            GROUP BY strategy
            ORDER BY total_premium DESC
        """

        summary: Dict[str, list] = {"by_symbol": [], "by_strategy": []}

        try:
            result = self._client.query(by_symbol_query).result()
            summary["by_symbol"] = [dict(row) for row in result]
        except Exception:
            logger.error("Failed to query P&L by symbol", exc_info=True)

        try:
            result = self._client.query(by_strategy_query).result()
            summary["by_strategy"] = [dict(row) for row in result]
        except Exception:
            logger.error("Failed to query P&L by strategy", exc_info=True)

        return summary


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #

def _str_or_none(value: Any) -> Optional[str]:
    """Convert a value to string, or return None."""
    return str(value) if value is not None else None


def _calc_total_premium(data: dict) -> Optional[float]:
    premium = data.get("premium")
    qty = data.get("contracts") or data.get("qty")
    if premium is not None and qty is not None:
        return float(premium) * 100 * int(qty)
    return None


def _calc_collateral(data: dict) -> Optional[float]:
    strike = data.get("strike_price")
    qty = data.get("contracts") or data.get("qty")
    if strike is not None and qty is not None:
        return float(strike) * 100 * int(qty)
    return None
