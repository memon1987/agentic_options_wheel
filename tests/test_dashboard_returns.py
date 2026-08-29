"""Unit tests for dashboard/backend/services/returns.py (FC-031).

The dashboard backend is not an installed package; import it by path so the
pure math stays testable from the main suite.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root
# kept ahead of it, so `import main` still resolves to the CLI rather than
# to dashboard/backend/main.py for every module collected after this one.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()

from services.returns import (  # noqa: E402
    annualize,
    dollar_drawdown,
    indexed_curve,
    max_drawdown,
    twr,
    twr_series,
    xirr,
)


class TestXirr:
    def test_single_deposit_matches_cagr(self):
        # $100k in, $120k two years later → (1.2)^(1/2) − 1 ≈ 9.545%
        rate = xirr([(date(2024, 1, 1), 100_000.0)], (date(2026, 1, 1), 120_000.0))
        assert rate == pytest.approx(1.2 ** (365.0 / 731.0) - 1, abs=1e-4)

    def test_known_spreadsheet_fixture(self):
        # Excel XIRR fixture: −10000 @ 2008-01-01, 2750 @ 2008-03-01,
        # 4250 @ 2008-10-30, 3250 @ 2009-02-15, 2750 @ 2009-04-01 → 37.34%.
        # Flows here use JNLC convention (deposit positive), receipts modeled
        # via negative deposits except the terminal.
        flows = [
            (date(2008, 1, 1), 10_000.0),
            (date(2008, 3, 1), -2_750.0),
            (date(2008, 10, 30), -4_250.0),
            (date(2009, 2, 15), -3_250.0),
        ]
        rate = xirr(flows, (date(2009, 4, 1), 2_750.0))
        assert rate == pytest.approx(0.3734, abs=2e-3)

    def test_multiple_deposits(self):
        flows = [(date(2025, 1, 1), 50_000.0), (date(2025, 7, 1), 50_000.0)]
        rate = xirr(flows, (date(2026, 1, 1), 110_000.0))
        # Second deposit compounds for half the time → rate above simple 10%/yr blend
        assert rate is not None
        assert 0.10 < rate < 0.20

    def test_negative_return(self):
        rate = xirr([(date(2025, 1, 1), 100_000.0)], (date(2026, 1, 1), 80_000.0))
        assert rate == pytest.approx(0.8 ** (365.0 / 365.0) - 1, abs=1e-3)

    def test_withdrawal_flow(self):
        flows = [(date(2025, 1, 1), 100_000.0), (date(2025, 7, 1), -20_000.0)]
        rate = xirr(flows, (date(2026, 1, 1), 85_000.0))
        assert rate is not None
        assert rate > 0  # 100k → 20k out + 85k left is a gain

    def test_degenerate_cases(self):
        assert xirr([], (date(2026, 1, 1), 100.0)) is None
        # zero-day span
        assert xirr([(date(2026, 1, 1), 100.0)], (date(2026, 1, 1), 100.0)) is None
        # account went to zero → no positive flow from investor perspective
        assert xirr([(date(2025, 1, 1), 100.0)], (date(2026, 1, 1), 0.0)) is None


class TestTwr:
    def test_no_flows_equals_simple_return(self):
        pts = [(date(2025, 1, 1), 100.0), (date(2025, 6, 1), 110.0), (date(2026, 1, 1), 121.0)]
        assert twr(pts, []) == pytest.approx(0.21)

    def test_deposit_not_counted_as_gain(self):
        # Flat performance, one $50k deposit mid-way: TWR must be 0.
        pts = [(date(2025, 1, 1), 100_000.0), (date(2025, 6, 1), 150_000.0), (date(2026, 1, 1), 150_000.0)]
        assert twr(pts, [(date(2025, 6, 1), 50_000.0)]) == pytest.approx(0.0)

    def test_weekend_deposit_attributed_to_next_observation(self):
        # Deposit dated Saturday; next equity row Monday. The Monday sub-period
        # return must strip the flow.
        pts = [(date(2025, 1, 3), 100_000.0), (date(2025, 1, 6), 111_000.0)]
        flows = [(date(2025, 1, 4), 10_000.0)]
        assert twr(pts, flows) == pytest.approx(0.01)

    def test_withdrawal(self):
        # 100 → 90 after withdrawing 20: performance is (90 − (−20) − 100)/100 = +10%
        pts = [(date(2025, 1, 1), 100.0), (date(2025, 2, 1), 90.0)]
        assert twr(pts, [(date(2025, 2, 1), -20.0)]) == pytest.approx(0.10)

    def test_short_series(self):
        assert twr([(date(2025, 1, 1), 100.0)], []) is None
        assert twr([], []) is None

    def test_series_base_100(self):
        pts = [(date(2025, 1, 1), 200.0), (date(2025, 1, 2), 220.0)]
        series = twr_series(pts, [])
        assert series[0][1] == pytest.approx(100.0)
        assert series[-1][1] == pytest.approx(110.0)


class TestAnnualize:
    def test_suppressed_below_90_days(self):
        assert annualize(0.05, 89) is None

    def test_one_year_identity(self):
        assert annualize(0.10, 365) == pytest.approx(0.10)

    def test_none_passthrough(self):
        assert annualize(None, 365) is None


class TestMaxDrawdown:
    def test_simple_drawdown(self):
        pts = [
            (date(2025, 1, 1), 100.0),
            (date(2025, 2, 1), 120.0),
            (date(2025, 3, 1), 90.0),
            (date(2025, 4, 1), 130.0),
        ]
        dd = max_drawdown(pts, [])
        assert dd["max_dd"] == pytest.approx(-0.25)  # 120 → 90
        assert dd["max_dd_peak"] == date(2025, 2, 1)
        assert dd["max_dd_trough"] == date(2025, 3, 1)
        assert dd["current_dd"] == pytest.approx(0.0)  # new high at the end

    def test_deposit_does_not_mask_drawdown(self):
        # Equity falls 100k → 90k, then a 30k deposit lifts raw equity to a
        # "new high" 118k — but performance is still −2% from peak
        # (90k → 88k post-deposit organic move).
        pts = [
            (date(2025, 1, 1), 100_000.0),
            (date(2025, 2, 1), 90_000.0),
            (date(2025, 3, 1), 118_000.0),
        ]
        dd = max_drawdown(pts, [(date(2025, 3, 1), 30_000.0)])
        assert dd["max_dd"] == pytest.approx(-0.12, abs=1e-9)  # trough at 90k
        assert dd["current_dd"] == pytest.approx((88_000.0 / 90_000.0 * 0.9) - 1, abs=1e-6)

    def test_monotonic_up_has_zero_dd(self):
        pts = [(date(2025, 1, 1), 100.0), (date(2025, 2, 1), 110.0)]
        dd = max_drawdown(pts, [])
        assert dd["max_dd"] == 0.0

    def test_insufficient_data(self):
        assert max_drawdown([], [])["max_dd"] is None


class TestDollarDrawdown:
    def test_simple_dollar_dd(self):
        pts = [
            (date(2025, 1, 1), 100_000.0),
            (date(2025, 2, 1), 120_000.0),
            (date(2025, 3, 1), 111_000.0),
        ]
        dd = dollar_drawdown(pts, [])
        assert dd["max_dd_dollars"] == pytest.approx(-9_000.0)
        assert dd["current_dd_dollars"] == pytest.approx(-9_000.0)

    def test_deposit_does_not_erase_dollar_dd(self):
        # 120k → 110k organic loss, then a 30k deposit → raw equity 140k
        # looks like a new high, but flow-adjusted the account is still
        # $10k below its peak.
        pts = [
            (date(2025, 1, 1), 120_000.0),
            (date(2025, 2, 1), 110_000.0),
            (date(2025, 3, 1), 140_000.0),
        ]
        dd = dollar_drawdown(pts, [(date(2025, 3, 1), 30_000.0)])
        assert dd["max_dd_dollars"] == pytest.approx(-10_000.0)
        assert dd["current_dd_dollars"] == pytest.approx(-10_000.0)

    def test_insufficient(self):
        assert dollar_drawdown([], [])["max_dd_dollars"] is None


class TestIndexedCurve:
    def test_chart_shape(self):
        pts = [(date(2025, 1, 1), 100.0), (date(2025, 1, 2), 105.0)]
        curve = indexed_curve(pts, [])
        assert curve == [
            {"date": "2025-01-01", "index": 100.0},
            {"date": "2025-01-02", "index": 105.0},
        ]
