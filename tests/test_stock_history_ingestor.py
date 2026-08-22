"""Tests for FC-018 PR B StockHistoryIngestor."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.data.stock_history_ingestor import StockHistoryIngestor


SAMPLE_BAR = {
    "t": "2026-04-01T04:00:00Z",
    "o": 150.0,
    "h": 155.5,
    "l": 149.0,
    "c": 154.25,
    "v": 1_234_567,
}


def _ingestor(universe=None, cursors=None):
    """Build an ingestor with BQ + alpaca mocked."""
    ingestor = StockHistoryIngestor.__new__(StockHistoryIngestor)
    ingestor.alpaca = MagicMock()
    ingestor.alpaca.config.paper_trading = True
    ingestor.alpaca.config.alpaca_api_key = "key"
    ingestor.alpaca.config.alpaca_secret_key = "secret"
    ingestor._project_id = "test"
    ingestor._dataset_id = "options_wheel"
    ingestor._strategy_id = "wheel"   # FC-075 Seam 4: stamped onto every row
    ingestor._table_ref = MagicMock()
    ingestor._client = MagicMock()
    ingestor._enabled = True
    ingestor._traded_universe = MagicMock(return_value=universe or [])
    ingestor._max_date_per_symbol = MagicMock(return_value=cursors or {})
    return ingestor


class TestBarToRow:
    def test_basic_bar_converted(self):
        row = StockHistoryIngestor._bar_to_row("AMD", SAMPLE_BAR, "2026-04-24T07:00:00Z")
        assert row is not None
        assert row["symbol"] == "AMD"
        assert row["date"] == "2026-04-01"
        assert row["open"] == 150.0
        assert row["close"] == 154.25
        assert row["volume"] == 1_234_567
        assert row["ingested_at"] == "2026-04-24T07:00:00Z"

    def test_missing_timestamp_returns_none(self):
        bar = {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}
        assert StockHistoryIngestor._bar_to_row("X", bar, "now") is None

    def test_malformed_timestamp_returns_none(self):
        bar = {"t": "not-a-date", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}
        assert StockHistoryIngestor._bar_to_row("X", bar, "now") is None


class TestRunOnce:
    def test_disabled_returns_disabled(self):
        ingestor = StockHistoryIngestor.__new__(StockHistoryIngestor)
        ingestor._enabled = False
        assert ingestor.run_once()["status"] == "disabled"

    def test_empty_universe(self):
        ingestor = _ingestor(universe=[])
        result = ingestor.run_once()
        assert result["status"] == "ok"
        assert result["symbols_processed"] == 0
        assert result["rows_inserted"] == 0

    def test_happy_path_inserts_for_each_symbol(self):
        ingestor = _ingestor(universe=["AMD", "NVDA"], cursors={})
        ingestor._fetch_bars = MagicMock(return_value=[SAMPLE_BAR])
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once(backfill_days=30)

        assert result["status"] == "ok"
        assert result["symbols_processed"] == 2
        assert result["rows_inserted"] == 2  # one bar per symbol
        assert ingestor._client.insert_rows_json.call_count == 2

    def test_skips_today_bar(self):
        ingestor = _ingestor(universe=["AMD"], cursors={})
        from datetime import datetime, timezone
        today_bar = {
            **SAMPLE_BAR,
            "t": datetime.combine(date.today(),
                                  datetime.min.time(),
                                  tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z'),
        }
        ingestor._fetch_bars = MagicMock(return_value=[SAMPLE_BAR, today_bar])
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once(backfill_days=30)

        # SAMPLE_BAR is 2026-04-01, today_bar is today — only SAMPLE_BAR should insert
        assert result["status"] == "ok"
        assert result["rows_inserted"] == 1
        inserted = ingestor._client.insert_rows_json.call_args[0][1]
        assert all(r["date"] != date.today().isoformat() for r in inserted)

    def test_uses_cursor_when_available(self):
        cursors = {"AMD": date(2026, 3, 15)}
        ingestor = _ingestor(universe=["AMD"], cursors=cursors)
        ingestor._fetch_bars = MagicMock(return_value=[SAMPLE_BAR])
        ingestor._client.insert_rows_json.return_value = []

        ingestor.run_once(backfill_days=30)

        # Should fetch from cursor + 1 day, not from today - 30
        call = ingestor._fetch_bars.call_args
        assert call[0][0] == "AMD"
        assert call[0][1] == date(2026, 3, 16)  # cursor + 1

    def test_cursor_after_today_skips(self):
        """If we already have today's data, skip without fetching."""
        cursors = {"AMD": date.today()}
        ingestor = _ingestor(universe=["AMD"], cursors=cursors)
        ingestor._fetch_bars = MagicMock()

        result = ingestor.run_once()

        assert result["per_symbol"]["AMD"]["status"] == "up_to_date"
        ingestor._fetch_bars.assert_not_called()

    def test_fetch_failure_partial_status(self):
        ingestor = _ingestor(universe=["AMD", "NVDA"], cursors={})
        ingestor._fetch_bars = MagicMock(
            side_effect=[RuntimeError("network"), [SAMPLE_BAR]]
        )
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once(backfill_days=30)

        assert result["status"] == "partial"
        assert result["per_symbol"]["AMD"]["status"] == "fetch_error"
        assert result["per_symbol"]["NVDA"]["status"] == "ok"

    def test_insert_errors_partial_status(self):
        ingestor = _ingestor(universe=["AMD"], cursors={})
        ingestor._fetch_bars = MagicMock(return_value=[SAMPLE_BAR])
        ingestor._client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "bad"}]}
        ]

        result = ingestor.run_once(backfill_days=30)

        assert result["status"] == "partial"
        assert result["per_symbol"]["AMD"]["status"] == "insert_errors"

    def test_row_ids_passed_for_idempotency(self):
        ingestor = _ingestor(universe=["AMD"], cursors={})
        ingestor._fetch_bars = MagicMock(return_value=[SAMPLE_BAR])
        ingestor._client.insert_rows_json.return_value = []

        ingestor.run_once(backfill_days=30)

        call = ingestor._client.insert_rows_json.call_args
        assert "row_ids" in call.kwargs
        assert call.kwargs["row_ids"] == ["2026-04-01|AMD"]


class TestBenchmarkUniverse:
    """FC-031: SPY benchmark bars ride the traded-symbol universe."""

    def test_benchmark_included_with_traded(self):
        ingestor = _ingestor()
        mock_result = [{"underlying": "AMD"}, {"underlying": "NVDA"}]
        ingestor._client.query.return_value.result.return_value = iter(mock_result)
        universe = StockHistoryIngestor._traded_universe(ingestor)
        assert "SPY" in universe
        assert "AMD" in universe and "NVDA" in universe

    def test_benchmark_included_when_query_fails(self):
        ingestor = _ingestor()
        ingestor._client.query.side_effect = RuntimeError("bq down")
        universe = StockHistoryIngestor._traded_universe(ingestor)
        assert universe == ["SPY"]
