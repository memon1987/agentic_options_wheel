"""Tests for FC-012 Phase 2.5 PortfolioHistoryIngestor."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.data.portfolio_history_ingestor import PortfolioHistoryIngestor


SAMPLE_PAYLOAD = {
    # 2026-03-24, 2026-03-25, 2026-03-26 at 00:00 UTC
    "timestamp": [1774310400, 1774396800, 1774483200],
    "equity": [105429.53, 104531.42, 106754.42],
    "profit_loss": [1357.84, -898.11, 2223.00],
    "profit_loss_pct": [0.013, -0.0085, 0.0213],
    "base_value": 104071.69,
    "base_value_asof": "2026-03-24",
    "timeframe": "1D",
}


def _ingestor_with_mock_bq(existing_dates=None):
    ingestor = PortfolioHistoryIngestor.__new__(PortfolioHistoryIngestor)
    ingestor.alpaca = MagicMock()
    ingestor._project_id = "test-project"
    ingestor._dataset_id = "options_wheel"
    ingestor._strategy_id = "wheel"   # FC-075 Seam 4: stamped onto every row
    ingestor._table_ref = MagicMock()
    ingestor._client = MagicMock()
    ingestor._enabled = True
    ingestor._existing_dates = MagicMock(return_value=set(existing_dates or []))
    return ingestor


class TestResponseToRows:
    def test_converts_parallel_arrays_to_rows(self):
        rows = PortfolioHistoryIngestor._response_to_rows(SAMPLE_PAYLOAD)
        assert len(rows) == 3
        assert rows[0]["equity"] == pytest.approx(105429.53)
        assert rows[0]["profit_loss"] == pytest.approx(1357.84)
        assert rows[0]["base_value"] == pytest.approx(104071.69)
        assert rows[0]["base_value_asof"] == "2026-03-24"
        # Converted timestamp → ISO date (UTC)
        assert rows[0]["date"] == "2026-03-24"

    def test_sorted_ascending(self):
        rows = PortfolioHistoryIngestor._response_to_rows(SAMPLE_PAYLOAD)
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates)

    def test_drops_null_equity(self):
        payload = dict(SAMPLE_PAYLOAD)
        payload["equity"] = [None, 104531.42, None]
        rows = PortfolioHistoryIngestor._response_to_rows(payload)
        assert len(rows) == 1
        assert rows[0]["equity"] == pytest.approx(104531.42)

    def test_handles_empty_arrays(self):
        rows = PortfolioHistoryIngestor._response_to_rows({
            "timestamp": [], "equity": [], "profit_loss": [], "profit_loss_pct": [],
            "base_value": None, "base_value_asof": None,
        })
        assert rows == []


class TestRunOnce:
    def test_disabled_returns_disabled(self):
        ingestor = PortfolioHistoryIngestor.__new__(PortfolioHistoryIngestor)
        ingestor._enabled = False
        ingestor._strategy_id = "wheel"
        assert ingestor.run_once()["status"] == "disabled"

    def test_skips_today_row(self):
        """Today's row flaps intraday and must be skipped."""
        from datetime import datetime, timezone
        ingestor = _ingestor_with_mock_bq()
        today = date.today()
        yesterday = today - timedelta(days=1)

        def _epoch(d):
            return int(datetime(d.year, d.month, d.day,
                                tzinfo=timezone.utc).timestamp())

        ingestor._fetch = MagicMock(return_value={
            "timestamp": [_epoch(yesterday), _epoch(today)],
            "equity": [100.0, 200.0],
            "profit_loss": [0.0, 100.0],
            "profit_loss_pct": [0.0, 1.0],
            "base_value": 100.0,
            "base_value_asof": yesterday.isoformat(),
        })
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["skipped_today"] >= 1
        # Insert was called but only with the yesterday row
        call = ingestor._client.insert_rows_json.call_args
        inserted = call[0][1]
        assert all(r["date"] < today.isoformat() for r in inserted)
        assert len(inserted) == 1

    def test_happy_path_inserts_finalized(self):
        ingestor = _ingestor_with_mock_bq()
        ingestor._fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["inserted"] == 3  # SAMPLE_PAYLOAD dates are past
        call = ingestor._client.insert_rows_json.call_args
        # row_ids = dates (natural PK)
        assert call.kwargs["row_ids"] == ["2026-03-24", "2026-03-25", "2026-03-26"]

    def test_existing_dates_filtered(self):
        ingestor = _ingestor_with_mock_bq(existing_dates={"2026-03-24"})
        ingestor._fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["inserted"] == 2
        assert result["skipped_existing"] == 1

    def test_all_duplicate_no_insert(self):
        ingestor = _ingestor_with_mock_bq(
            existing_dates={"2026-03-24", "2026-03-25", "2026-03-26"},
        )
        ingestor._fetch = MagicMock(return_value=SAMPLE_PAYLOAD)

        result = ingestor.run_once()

        assert result["inserted"] == 0
        ingestor._client.insert_rows_json.assert_not_called()

    def test_fetch_error_surfaces(self):
        ingestor = _ingestor_with_mock_bq()
        ingestor._fetch = MagicMock(side_effect=RuntimeError("boom"))

        result = ingestor.run_once()

        assert result["status"] == "failed"
        assert result["reason"] == "fetch_error"

    def test_insert_errors_return_partial(self):
        ingestor = _ingestor_with_mock_bq()
        ingestor._fetch = MagicMock(return_value=SAMPLE_PAYLOAD)
        ingestor._client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "bad"}]}
        ]

        result = ingestor.run_once()

        assert result["status"] == "partial"
        assert result["inserted"] == 0
