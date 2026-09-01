"""The covered-call profile must be able to price a stock.

`MarketDataManager.get_stock_metrics` hard-indexes `config.min_stock_price`,
`config.max_stock_price` and `config.min_avg_volume` on every symbol it prices,
and those `Config` properties index `strategy:` directly (no `.get` default).
`config/covered_call.yaml` shipped without the three keys, so the method raised
`KeyError` *after* fetching the quote, the broad `except` logged
`stock_metrics_error` and returned `{}`, and
`OptionsScanner._create_call_opportunity` then saw `current_price=0` and failed
the symbol closed (`call_scan_skipped_quote_unavailable`). Production logs show
that on 100% of covered-call symbol-scans since 2026-08-24 — the profile could
never build a single call opportunity.

Both tests go through the REAL `config/covered_call.yaml` on purpose: a fixture
dict would keep passing if the keys were deleted from the shipped file again.
"""

from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from src.api.market_data import MarketDataManager
from src.utils.config import Config

REPO = Path(__file__).resolve().parent.parent
CC_YAML = str(REPO / "config" / "covered_call.yaml")


@pytest.fixture
def cc_config():
    return Config(CC_YAML)


class TestCoveredCallScreeningKeysPresent:
    def test_price_and_volume_properties_resolve(self, cc_config):
        """Reading the three properties must not raise KeyError."""
        assert isinstance(cc_config.min_stock_price, (int, float))
        assert isinstance(cc_config.max_stock_price, (int, float))
        assert isinstance(cc_config.min_avg_volume, (int, float))
        assert cc_config.min_stock_price < cc_config.max_stock_price

    def test_wheel_profile_still_resolves_its_own_bounds(self):
        """Wheel-neutrality: the wheel's keys still resolve and are ordered.

        Deliberately does NOT pin the wheel's values — they are operator
        tunables (FC in FUTURE_CONSIDERATIONS proposes moving the $400
        ceiling) and a covered-call test must not veto that.
        """
        wheel = Config(str(REPO / "config" / "settings.yaml"))
        assert wheel.min_stock_price < wheel.max_stock_price
        assert wheel.min_avg_volume >= 0

    def test_profile_missing_screening_keys_fails_at_load(self, tmp_path):
        """The class-level guard: a profile without the three keys must fail
        validation at load on EVERY strategy_id, not at scan time inside
        `get_stock_metrics`'s broad except."""
        import yaml
        data = yaml.safe_load(open(CC_YAML))
        for key in ("min_stock_price", "max_stock_price", "min_avg_volume"):
            data["strategy"].pop(key, None)
        path = tmp_path / "cc_missing.yaml"
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match="min_stock_price is required"):
            Config(str(path))


class TestCoveredCallOpenInterestFloorSuspended:
    """`AlpacaClient.get_option_chain` builds contracts from the snapshot
    endpoint and hardcodes `open_interest: 0` (src/api/alpaca_client.py). Any
    `universe.min_open_interest` on this profile therefore rejects every strike
    that survives the other gates — which is exactly what happened in
    production 2026-08-24..31 (~114 GOOGL / ~86 UNH per scan, unlogged). The
    floor is suspended (null) until FC-097 sources real OI; this test pins the
    suspension to the hardcode so restoring one without the other fails CI.
    """

    def test_cc_oi_floor_is_unset_while_chain_source_reports_zero_oi(self, cc_config):
        src = (REPO / "src" / "api" / "alpaca_client.py").read_text()
        chain_reports_zero_oi = "'open_interest': 0," in src
        if chain_reports_zero_oi:
            assert cc_config.min_open_interest is None, (
                "universe.min_open_interest is set but the chain source "
                "hardcodes open_interest=0 — every qualifying strike would be "
                "rejected as open_interest_too_low (see FC-097)"
            )
        else:
            pytest.skip("chain source no longer hardcodes OI=0; FC-097 landed — "
                        "restore the CC OI floor and retire this test")

    def test_spread_gate_still_active(self, cc_config):
        """Only the OI half of the DD-3 validator is suspended."""
        assert cc_config.max_spread_pct == 0.10


class TestGetStockMetricsOnCoveredCallProfile:
    def _bars(self):
        return pd.DataFrame({
            "close": [98.0, 99.0, 100.0, 101.0, 100.5],
            "volume": [1000, 1200, 900, 1100, 1050],
        })

    def test_metrics_returns_current_price_not_empty_dict(self, cc_config):
        """The regression: this returned {} (KeyError swallowed) before the fix."""
        alpaca = Mock()
        alpaca.get_stock_quote.return_value = {"bid": 100.0, "ask": 101.0}
        alpaca.get_stock_bars.return_value = self._bars()

        md = MarketDataManager(alpaca, cc_config)
        metrics = md.get_stock_metrics("UNH")

        assert metrics != {}
        assert metrics["current_price"] == 100.5
        # The call path consumes only current_price; these are the put-path
        # fields, and on this profile's deliberately wide bounds no held symbol
        # is ever labelled unsuitable.
        assert bool(metrics["meets_price_criteria"]) is True
        assert bool(metrics["meets_volume_criteria"]) is True

    def test_no_stock_metrics_error_is_logged(self, cc_config, monkeypatch):
        """The exact production symptom: `stock_metrics_error` on every scan."""
        alpaca = Mock()
        alpaca.get_stock_quote.return_value = {"bid": 100.0, "ask": 101.0}
        alpaca.get_stock_bars.return_value = self._bars()

        errors = []
        fake_logger = Mock()
        fake_logger.error.side_effect = lambda *a, **kw: errors.append(kw)
        monkeypatch.setattr("src.api.market_data.logger", fake_logger)

        md = MarketDataManager(alpaca, cc_config)
        md.get_stock_metrics("GOOGL")

        assert not any(e.get("event_type") == "stock_metrics_error" for e in errors)
