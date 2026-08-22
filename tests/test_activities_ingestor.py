"""Tests for FC-012 ActivitiesIngestor.

Covers normalization, idempotency, pagination, and cursor derivation.
BigQuery is mocked throughout — no real cloud calls.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.data.activities_ingestor import ActivitiesIngestor


# --------------------------------------------------------------------------- #
# Sample activity payloads (shape observed from paper account 2026-04-23)
# --------------------------------------------------------------------------- #

SAMPLE_FILL_PUT_SELL = {
    "id": "20260423133023252::2537e3da-fafd-475b-b333-4a71ab0e625b",
    "activity_type": "FILL",
    "transaction_time": "2026-04-23T17:30:23.252125Z",
    "type": "fill",
    "price": "3.15",
    "qty": "1",
    "side": "sell_short",
    "symbol": "AMD260501P00277500",
    "leaves_qty": "0",
    "order_id": "d4b8fc96-874b-41a5-baf5-24c1cc7916b8",
    "cum_qty": "1",
    "order_status": "filled",
}

SAMPLE_FILL_PUT_CLOSE = {
    "id": "20260428000000000::bbbb2222-abcd-1111-2222-333344445555",
    "activity_type": "FILL",
    "transaction_time": "2026-04-28T17:30:23.252125Z",
    "type": "fill",
    "price": "0.50",
    "qty": "1",
    "side": "buy_to_close",
    "symbol": "AMD260501P00277500",
    "leaves_qty": "0",
    "order_id": "closeclose-1234-5678",
    "cum_qty": "1",
    "order_status": "filled",
}

SAMPLE_OPASN = {
    "id": "20260422000000000::eef503a1-a180-4ece-b6f3-f0463e28a539",
    "activity_type": "OPASN",
    "date": "2026-04-22",
    "created_at": "2026-04-23T08:23:24.973256Z",
    "net_amount": "0",
    "description": "Options Assignment",
    "symbol": "AMZN260422C00240000",
    "cusip": "AMZN260422C00240000",
    "qty": "1",
    "status": "executed",
    "group_id": "53054182-24d5-41df-9911-a534b061e1d8",
    "currency": "USD",
}

SAMPLE_OPEXP = {
    "id": "20251013000000000::ea9bdae8-dc0b-46fd-b622-450600cc6803",
    "activity_type": "OPEXP",
    "date": "2025-10-13",
    "created_at": "2025-10-14T08:20:21.45728Z",
    "net_amount": "0",
    "description": "Options Expiry",
    "symbol": "UNH251010P00347500",
    "cusip": "UNH251010P00347500",
    "qty": "1",
    "status": "executed",
    "group_id": "d5f8b869-1e35-44ba-893e-4399890add69",
    "currency": "USD",
}

SAMPLE_OPTRD = {
    "id": "20260422000000000::optrd-amzn-2026-04-22",
    "activity_type": "OPTRD",
    "date": "2026-04-22",
    "created_at": "2026-04-23T08:23:24.973256Z",
    "net_amount": "24000",
    "description": "Options Trade",
    "symbol": "AMZN",
    "qty": "-100",
    "price": "240",
    "status": "executed",
    "group_id": "53054182-24d5-41df-9911-a534b061e1d8",
    "currency": "USD",
}

SAMPLE_JNLC = {
    "id": "20251006000000000::jnlc-funding",
    "activity_type": "JNLC",
    "date": "2025-10-06",
    "created_at": "2025-10-06T13:00:00.000000Z",
    "net_amount": "100000",
    "description": "Initial deposit",
    "currency": "USD",
}

SAMPLE_STOCK_FILL = {
    "id": "20260420130000000::ffffffff-1111-2222-3333-444455556666",
    "activity_type": "FILL",
    "transaction_time": "2026-04-20T17:00:00.000000Z",
    "type": "fill",
    "price": "180.50",
    "qty": "100",
    "side": "buy",
    "symbol": "AMD",  # Not an OCC — underlying ticker
    "leaves_qty": "0",
    "order_id": "stockorder-1111",
    "cum_qty": "100",
    "order_status": "filled",
}


# --------------------------------------------------------------------------- #
# _normalize
# --------------------------------------------------------------------------- #


class TestNormalize:
    """Static normalization of raw Alpaca activities."""

    def test_fill_put_sell_short(self):
        row = ActivitiesIngestor._normalize(SAMPLE_FILL_PUT_SELL)
        assert row is not None
        assert row["activity_id"] == SAMPLE_FILL_PUT_SELL["id"]
        assert row["activity_type"] == "FILL"
        assert row["transaction_time"] == "2026-04-23T17:30:23.252125Z"
        assert row["symbol"] == "AMD260501P00277500"
        assert row["underlying"] == "AMD"
        assert row["side"] == "sell_short"
        assert row["qty"] == 1.0
        assert row["price"] == 3.15
        assert row["option_type"] == "put"
        assert row["strike_price"] == 277.5
        assert row["expiration"] == "2026-05-01"
        # premium = price * abs(qty) * 100
        assert row["premium_total"] == pytest.approx(315.0)
        # activity_date is absent on FILLs
        assert row["activity_date"] is None

    def test_fill_buy_to_close_computes_premium(self):
        row = ActivitiesIngestor._normalize(SAMPLE_FILL_PUT_CLOSE)
        assert row is not None
        assert row["side"] == "buy_to_close"
        assert row["premium_total"] == pytest.approx(50.0)

    def test_opasn_has_activity_date(self):
        row = ActivitiesIngestor._normalize(SAMPLE_OPASN)
        assert row is not None
        assert row["activity_type"] == "OPASN"
        assert row["activity_date"] == "2026-04-22"
        # transaction_time falls back to created_at
        assert row["transaction_time"] == "2026-04-23T08:23:24.973256Z"
        assert row["option_type"] == "call"
        assert row["strike_price"] == 240.0
        assert row["underlying"] == "AMZN"
        assert row["group_id"] == "53054182-24d5-41df-9911-a534b061e1d8"

    def test_opexp_parsing(self):
        row = ActivitiesIngestor._normalize(SAMPLE_OPEXP)
        assert row is not None
        assert row["activity_type"] == "OPEXP"
        assert row["activity_date"] == "2025-10-13"
        assert row["option_type"] == "put"
        assert row["strike_price"] == 347.5

    def test_stock_fill_no_option_parse(self):
        row = ActivitiesIngestor._normalize(SAMPLE_STOCK_FILL)
        assert row is not None
        assert row["symbol"] == "AMD"
        assert row["underlying"] == "AMD"
        assert row["option_type"] is None
        assert row["strike_price"] is None
        assert row["expiration"] is None
        # No option = no premium_total
        assert row["premium_total"] is None

    def test_missing_id_returns_none(self):
        assert ActivitiesIngestor._normalize({"activity_type": "FILL"}) is None

    def test_optrd_normalized_with_net_amount(self):
        row = ActivitiesIngestor._normalize(SAMPLE_OPTRD)
        assert row is not None
        assert row["activity_type"] == "OPTRD"
        assert row["symbol"] == "AMZN"
        assert row["underlying"] == "AMZN"
        assert row["option_type"] is None  # ticker, not OCC
        assert row["qty"] == -100.0
        assert row["price"] == 240.0
        assert row["net_amount"] == 24000.0
        assert row["activity_date"] == "2026-04-22"

    def test_jnlc_funding_event(self):
        row = ActivitiesIngestor._normalize(SAMPLE_JNLC)
        assert row is not None
        assert row["activity_type"] == "JNLC"
        assert row["net_amount"] == 100000.0
        assert row["activity_date"] == "2025-10-06"
        # No symbol on JNLC
        assert row["symbol"] == ""
        assert row["option_type"] is None
        # transaction_time falls back to created_at
        assert row["transaction_time"] == "2025-10-06T13:00:00.000000Z"

    def test_fee_event(self):
        """FC-031: FEE activities normalize through the JNLC-shaped path."""
        row = ActivitiesIngestor._normalize({
            "id": "20260103000000000::fee-ora",
            "activity_type": "FEE",
            "date": "2026-01-03",
            "net_amount": "-0.52",
            "description": "REG fee",
        })
        assert row is not None
        assert row["activity_type"] == "FEE"
        assert row["net_amount"] == -0.52
        assert row["symbol"] == ""
        assert row["option_type"] is None
        # transaction_time synthesized from date
        assert row["transaction_time"] == "2026-01-03T00:00:00Z"

    def test_synthesizes_timestamp_from_date_only(self):
        """An activity with only `date` (no transaction_time, no created_at)
        should synthesize a midnight UTC timestamp rather than be dropped.
        """
        activity = dict(SAMPLE_OPTRD)
        activity.pop("created_at")
        row = ActivitiesIngestor._normalize(activity)
        assert row is not None
        assert row["transaction_time"] == "2026-04-22T00:00:00Z"

    def test_blank_numeric_fields_stay_none(self):
        activity = dict(SAMPLE_FILL_PUT_SELL)
        activity["leaves_qty"] = ""
        activity["cum_qty"] = None
        row = ActivitiesIngestor._normalize(activity)
        assert row["leaves_qty"] is None
        assert row["cum_qty"] is None


# --------------------------------------------------------------------------- #
# run_once — pagination + idempotency
# --------------------------------------------------------------------------- #


def _build_ingestor_with_bq_mock(existing_ids=None, max_cursor=None):
    """Construct an ActivitiesIngestor bypassing BQ init, with mock client."""
    alpaca_mock = MagicMock()
    ingestor = ActivitiesIngestor.__new__(ActivitiesIngestor)
    ingestor.alpaca = alpaca_mock
    ingestor._project_id = "test-project"
    ingestor._dataset_id = "options_wheel"
    ingestor._strategy_id = "wheel"   # FC-075 Seam 4: stamped onto every row
    ingestor._table_ref = MagicMock()
    ingestor._client = MagicMock()
    ingestor._enabled = True

    # Cursor query: return MIN of fill_date / opevent_date
    cursor_row = MagicMock()
    cursor_row.__getitem__ = lambda self, k: max_cursor
    ingestor._client.query.return_value.result.return_value = iter(
        [cursor_row] if max_cursor else []
    )

    # Patch the helper methods directly for simpler assertions
    ingestor._read_cursor = MagicMock(return_value=(max_cursor, True))
    ingestor._existing_ids = MagicMock(return_value=set(existing_ids or []))

    return ingestor, alpaca_mock


class TestRunOnce:
    def test_no_activities_returned(self):
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        alpaca.get_account_activities.return_value = []

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["fetched"] == 0
        assert result["inserted"] == 0

    def test_happy_path_inserts_all_new(self):
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        alpaca.get_account_activities.return_value = [
            SAMPLE_FILL_PUT_SELL, SAMPLE_OPASN, SAMPLE_OPEXP,
        ]
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["fetched"] == 3
        assert result["inserted"] == 3
        assert result["skipped"] == 0
        ingestor._client.insert_rows_json.assert_called_once()
        inserted_rows = ingestor._client.insert_rows_json.call_args[0][1]
        assert len(inserted_rows) == 3
        assert {r["activity_id"] for r in inserted_rows} == {
            SAMPLE_FILL_PUT_SELL["id"],
            SAMPLE_OPASN["id"],
            SAMPLE_OPEXP["id"],
        }

    def test_dedups_existing_activity_ids(self):
        """Already-present activity_ids must not be re-inserted."""
        ingestor, alpaca = _build_ingestor_with_bq_mock(
            existing_ids={SAMPLE_FILL_PUT_SELL["id"]},
        )
        alpaca.get_account_activities.return_value = [
            SAMPLE_FILL_PUT_SELL, SAMPLE_OPASN,
        ]
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["inserted"] == 1
        assert result["skipped"] == 1
        inserted_rows = ingestor._client.insert_rows_json.call_args[0][1]
        assert len(inserted_rows) == 1
        assert inserted_rows[0]["activity_id"] == SAMPLE_OPASN["id"]

    def test_all_duplicate_no_insert_call(self):
        """If every activity is already present, we must not call insert."""
        ingestor, alpaca = _build_ingestor_with_bq_mock(
            existing_ids={SAMPLE_FILL_PUT_SELL["id"], SAMPLE_OPASN["id"]},
        )
        alpaca.get_account_activities.return_value = [
            SAMPLE_FILL_PUT_SELL, SAMPLE_OPASN,
        ]

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["inserted"] == 0
        assert result["skipped"] == 2
        ingestor._client.insert_rows_json.assert_not_called()

    def test_paginates_until_short_page(self):
        """First page full (100), second page short -> pagination ends."""
        ingestor, alpaca = _build_ingestor_with_bq_mock()

        # Fabricate 100 unique FILL activities + 3 on page 2
        page1 = []
        for i in range(100):
            a = dict(SAMPLE_FILL_PUT_SELL)
            a["id"] = f"page1-activity-{i:03d}"
            page1.append(a)
        page2 = []
        for i in range(3):
            a = dict(SAMPLE_OPEXP)
            a["id"] = f"page2-activity-{i:03d}"
            page2.append(a)

        alpaca.get_account_activities.side_effect = [page1, page2]
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert alpaca.get_account_activities.call_count == 2
        # page_token threaded on second call
        second_call_kwargs = alpaca.get_account_activities.call_args_list[1].kwargs
        assert second_call_kwargs["page_token"] == page1[-1]["id"]
        assert result["fetched"] == 103
        assert result["inserted"] == 103

    def test_pull_failure_returns_failed_status(self):
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        alpaca.get_account_activities.side_effect = RuntimeError("boom")

        result = ingestor.run_once()

        assert result["status"] == "failed"
        assert result["reason"] == "pull_error"

    def test_insert_errors_surface_as_partial(self):
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        alpaca.get_account_activities.return_value = [SAMPLE_FILL_PUT_SELL]
        ingestor._client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "invalid"}]}
        ]

        result = ingestor.run_once()

        assert result["status"] == "partial"
        assert result["inserted"] == 0

    def test_disabled_ingestor_returns_disabled(self):
        ingestor = ActivitiesIngestor.__new__(ActivitiesIngestor)
        ingestor._enabled = False
        ingestor._strategy_id = "wheel"
        result = ingestor.run_once()
        assert result["status"] == "disabled"

    def test_insert_passes_row_ids_for_idempotency(self):
        """insert_rows_json must receive row_ids= (activity_ids) for dedup."""
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        alpaca.get_account_activities.return_value = [
            SAMPLE_FILL_PUT_SELL, SAMPLE_OPASN,
        ]
        ingestor._client.insert_rows_json.return_value = []

        ingestor.run_once()

        call = ingestor._client.insert_rows_json.call_args
        assert "row_ids" in call.kwargs
        assert call.kwargs["row_ids"] == [
            SAMPLE_FILL_PUT_SELL["id"], SAMPLE_OPASN["id"],
        ]

    def test_cursor_failure_surfaces_cursor_ok_false(self):
        """When the cursor read fails, run_once must flag cursor_ok=False."""
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        ingestor._read_cursor = MagicMock(return_value=(None, False))
        alpaca.get_account_activities.return_value = []

        result = ingestor.run_once()

        assert result["status"] == "ok"
        assert result["cursor_ok"] is False

    def test_malformed_activity_without_timestamp_is_skipped(self):
        """Activity with no transaction_time or created_at is skipped."""
        ingestor, alpaca = _build_ingestor_with_bq_mock()
        malformed = dict(SAMPLE_FILL_PUT_SELL)
        malformed["id"] = "malformed-1"
        malformed.pop("transaction_time")
        # no `created_at` either — row cannot derive a partition ts
        alpaca.get_account_activities.return_value = [
            malformed, SAMPLE_OPASN,
        ]
        ingestor._client.insert_rows_json.return_value = []

        result = ingestor.run_once()

        assert result["malformed"] == 1
        assert result["fetched"] == 1   # only the OPASN normalized
        assert result["inserted"] == 1
        inserted_rows = ingestor._client.insert_rows_json.call_args[0][1]
        assert [r["activity_id"] for r in inserted_rows] == [SAMPLE_OPASN["id"]]


# --------------------------------------------------------------------------- #
# Option-symbol edge cases
# --------------------------------------------------------------------------- #


class TestOptionSymbolEdgeCases:
    def test_occ_with_long_underlying(self):
        activity = dict(SAMPLE_FILL_PUT_SELL)
        activity["id"] = "edge-long-underlying"
        activity["symbol"] = "GOOGL260619P00180000"
        row = ActivitiesIngestor._normalize(activity)
        assert row["underlying"] == "GOOGL"
        assert row["option_type"] == "put"
        assert row["strike_price"] == 180.0

    def test_occ_with_short_underlying_f(self):
        activity = dict(SAMPLE_FILL_PUT_SELL)
        activity["id"] = "edge-short-underlying"
        activity["symbol"] = "F260619P00010000"  # Ford, $10 strike
        row = ActivitiesIngestor._normalize(activity)
        assert row["underlying"] == "F"
        assert row["option_type"] == "put"
        assert row["strike_price"] == 10.0

    def test_blank_symbol(self):
        activity = dict(SAMPLE_FILL_PUT_SELL)
        activity["id"] = "edge-blank-symbol"
        activity["symbol"] = ""
        row = ActivitiesIngestor._normalize(activity)
        assert row["symbol"] == ""
        assert row["option_type"] is None
        assert row["underlying"] == ""
