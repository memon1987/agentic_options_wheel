"""FC-031: falsifiable reconciliation-identity tests.

Reconstructs NLV from a synthetic activity stream and asserts
`get_reconciliation` composes the identity correctly:

    NLV − deposits = closed net cash P&L + open-option premium + fees
                     + live market value (stocks + options, signed)

This is the test the adversarial review demanded (F1/F15): a broken
convention (e.g. double-subtracting held-share cost) produces a non-zero
residual here, where a tautological "invariant" could not fail.
"""

import sys
from pathlib import Path

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root
# kept ahead of it, so `import main` still resolves to the CLI rather than
# to dashboard/backend/main.py for every module collected after this one.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()


def _service(view_rows, deposits=100_000.0, fees=-2.0):
    """BigQueryService with all BQ I/O stubbed.

    ``view_rows``: per-symbol scorecard rows — the single merged query now
    supplies both the ledger sums and the share counts (review E3).
    """
    from services.bigquery import BigQueryService

    svc = BigQueryService.__new__(BigQueryService)
    svc.dataset = "test.options_wheel"

    def fake_run_query(query, params=None):
        if "FROM `test.options_wheel.fc018_per_symbol_scorecard`" in query:
            return view_rows
        raise AssertionError(f"unexpected query: {query[:80]}")

    svc._run_query = fake_run_query
    svc.get_account_baseline = lambda: {"starting_capital": deposits, "source": "test"}
    svc.get_fees_total = lambda: fees
    return svc


# Synthetic stream: $100k deposit; put sold for $300 then assigned
# (OPTRD −$25,000 for 100sh @ $250, put realized_pnl = +$300); covered call
# open with $200 premium; $2 of fees.
#   cash = 100,000 + 300 − 25,000 + 200 − 2 = 75,498
#   MV   = 100sh @ $255 = 25,500 ; short call mark −150
#   NLV  = 75,498 + 25,500 − 150 = 100,848
def rows(realized_cash=300.0 - 25_000.0, open_premium=200.0, symbol="XYZ", shares=100.0):
    return [{
        "underlying": symbol,
        "current_shares": shares,
        "total_realized_pnl": realized_cash,
        "open_option_premium": open_premium,
    }]


LIVE = [
    {"symbol": "XYZ", "qty": "100", "market_value": "25500"},
    {"symbol": "XYZ260117C00260000", "qty": "-1", "market_value": "-150"},
]
NLV = 100_848.0


class TestIdentity:
    def test_residual_zero_on_consistent_stream(self):
        svc = _service(rows())
        rec = svc.get_reconciliation(NLV, LIVE)
        assert rec["residual"] == pytest.approx(0.0, abs=1e-6)
        # No known gaps on file (AMD's retired 2026-07-18) → clean books = ok.
        assert rec["status"] == "ok"

    def test_stale_known_gap_trips_warn(self):
        # Regression cover for the stale-allowance detector: a documented gap
        # whose symbol no longer mismatches must WARN (the allowance is rot,
        # not slack) — this is what surfaced AMD's resolution in production.
        svc = _service(rows())
        svc.KNOWN_RECONCILIATION_GAPS = [
            {"symbol": "AMD", "amount": -1594.0, "as_of": "2026-05-05",
             "reason": "test fixture"}]
        rec = svc.get_reconciliation(NLV, LIVE)
        assert rec["residual"] == pytest.approx(0.0, abs=1e-6)
        assert rec["status"] == "warn"

    def test_missing_share_cash_breaks_identity(self):
        # Drop the OPTRD outflow from the ledger (ingest gap): the residual
        # must expose the full $25,000, and status must warn.
        svc = _service(rows(realized_cash=300.0))
        rec = svc.get_reconciliation(NLV, LIVE)
        assert rec["residual"] == pytest.approx(-25_000.0, abs=1e-6)
        assert rec["status"] == "warn"

    def test_double_subtracted_basis_is_caught(self):
        # The exact F1 bug: treating (price − basis) × shares as the
        # unrealized add-back after the cash ledger already expensed the
        # basis. That understates the ledger side by basis × shares and the
        # residual exposes it.
        f1_cash = (300.0 - 25_000.0) + (255.0 - 250.0) * 100 - 25_500.0
        svc = _service(rows(realized_cash=f1_cash))
        rec = svc.get_reconciliation(NLV, LIVE)
        assert abs(rec["residual"]) == pytest.approx(25_000.0, abs=1e-6)
        assert rec["status"] == "warn"

    def test_share_count_mismatch_detected(self):
        # View says AMD holds 100 shares, broker says 0 (the AMD anomaly
        # shape); broker holds XYZ the view doesn't know about.
        svc = _service(rows(symbol="AMD"))
        rec = svc.get_reconciliation(NLV, LIVE)
        symbols = {m["symbol"] for m in rec["share_count_mismatches"]}
        assert "AMD" in symbols          # view-only shares
        assert "XYZ" in symbols          # live-only shares

    def test_no_live_data_is_unknown_not_ok(self):
        svc = _service(rows())
        rec = svc.get_reconciliation(None, None)
        assert rec["residual"] is None
        assert rec["status"] == "unknown"
