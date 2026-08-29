"""Unit tests for dashboard/backend/services/lots.py (FC-031 FIFO open lots)."""

import sys
from pathlib import Path

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root
# kept ahead of it, so `import main` still resolves to the CLI rather than
# to dashboard/backend/main.py for every module collected after this one.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()

from services.lots import open_lot_basis, open_lots  # noqa: E402


def ev(t, qty, price):
    return {"transaction_time": t, "qty": qty, "price": price}


class TestOpenLots:
    def test_simple_buy_sell_leaves_nothing(self):
        lots, unpaired = open_lots([ev("2026-01-01", 100, 230.0), ev("2026-02-01", -100, 250.0)])
        assert lots == []
        assert unpaired == 0

    def test_amd_overlap_case(self):
        # FC-020's AMD anatomy: three buys, two sells → the Jan-31 lot stays open.
        events = [
            ev("2025-11-22", 100, 230.0),
            ev("2025-11-29", -100, 192.5),
            ev("2026-01-10", 100, 212.5),
            ev("2026-01-31", 100, 245.0),
            ev("2026-04-17", -100, 252.5),
        ]
        lots, _ = open_lots(events)
        assert len(lots) == 1
        assert lots[0]["qty"] == 100
        assert lots[0]["price"] == 245.0  # NOT the cumulative-average ~$242.50
        assert lots[0]["buy_time"] == "2026-01-31"

    def test_partial_sell_splits_lot(self):
        lots, _ = open_lots([ev("2026-01-01", 100, 50.0), ev("2026-02-01", -40, 55.0)])
        assert len(lots) == 1
        assert lots[0]["qty"] == 60
        assert lots[0]["price"] == 50.0

    def test_sell_spanning_two_lots(self):
        lots, _ = open_lots([
            ev("2026-01-01", 100, 50.0),
            ev("2026-01-15", 100, 60.0),
            ev("2026-02-01", -150, 65.0),
        ])
        assert len(lots) == 1
        assert lots[0]["qty"] == 50
        assert lots[0]["price"] == 60.0

    def test_unpaired_sell_flagged_not_crashed(self):
        lots, unpaired = open_lots([ev("2026-01-01", -100, 50.0), ev("2026-02-01", 100, 55.0)])
        assert len(lots) == 1
        assert unpaired == 1

    def test_out_of_order_events_sorted(self):
        lots, unpaired = open_lots([ev("2026-02-01", -100, 60.0), ev("2026-01-01", 100, 50.0)])
        assert lots == []
        assert unpaired == 0


class TestOpenLotBasis:
    def test_no_holdings(self):
        basis = open_lot_basis([ev("2026-01-01", 100, 50.0), ev("2026-02-01", -100, 55.0)])
        assert basis["shares"] == 0.0
        assert basis["basis_per_share"] is None

    def test_amd_open_lot_basis(self):
        events = [
            ev("2025-11-22", 100, 230.0),
            ev("2025-11-29", -100, 192.5),
            ev("2026-01-10", 100, 212.5),
            ev("2026-01-31", 100, 245.0),
            ev("2026-04-17", -100, 252.5),
        ]
        basis = open_lot_basis(events)
        assert basis["shares"] == 100
        assert basis["basis_per_share"] == pytest.approx(245.0)
        assert basis["acquired_at"] == "2026-01-31"

    def test_blended_basis_across_open_lots(self):
        basis = open_lot_basis([ev("2026-01-01", 100, 50.0), ev("2026-01-15", 100, 60.0)])
        assert basis["shares"] == 200
        assert basis["basis_per_share"] == pytest.approx(55.0)
