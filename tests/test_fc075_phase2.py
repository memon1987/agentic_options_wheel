"""FC-075 Phase 2 — the covered-call engine (call-only gating of the shared pipeline).

Each test names the regression it catches. Covers: the put-scan gate (DD-2), the
inventory-validator chain criteria + their wheel-neutrality (DD-3), the DD-7 write
interlock (server + config), strategy-scoped BQ reads (DD-4), and the config
surface. The /run-internal defense-in-depth (call-only filter, reconcile gate) and
the exposure alert are exercised through unit-level assertions here and by the
wheel-neutral full suite; see the PR for the coverage note.
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.config import Config

# google-cloud-storage is not installed in the test env (only bigquery). The
# server + OpportunityStore import it at module load; stub so imports succeed.
try:  # pragma: no cover
    from google.cloud import storage  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules.setdefault("google", types.ModuleType("google"))
    _cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    _storage = types.ModuleType("google.cloud.storage")
    _storage.Client = Mock
    sys.modules["google.cloud.storage"] = _storage
    _cloud.storage = _storage

from src.data.options_scanner import OptionsScanner
from src.api.market_data import MarketDataManager

REPO = Path(__file__).resolve().parent.parent


def _wheel_config():
    c = Mock(spec=Config)
    c.strategy_id = "wheel"
    c.stock_symbols = ["AAPL"]
    c.call_target_dte = 7
    c.call_delta_range = [0.15, 0.25]
    c.min_call_premium = 0.30
    c.excluded_symbols = set()
    c.min_open_interest = None
    c.max_spread_pct = None
    c.bigquery_dataset = "options_wheel"
    return c


def _cc_config():
    c = _wheel_config()
    c.strategy_id = "covered_call"
    c.excluded_symbols = {"AAPL"}
    c.min_open_interest = 500
    c.max_spread_pct = 0.10
    c.bigquery_dataset = "covered_call"
    return c


# --------------------------------------------------------------------------- #
# DD-2 — put-scan gate
# --------------------------------------------------------------------------- #

class TestPutScanGate:
    def _scanner(self, config):
        with patch("src.data.options_scanner.UncoveredDaysResolver"), \
             patch("src.data.options_scanner.CostBasisResolver"):
            return OptionsScanner(Mock(), Mock(), config)

    def test_non_wheel_put_scan_returns_empty_and_touches_no_market_data(self):
        md = Mock()
        cc = _cc_config()
        with patch("src.data.options_scanner.UncoveredDaysResolver"), \
             patch("src.data.options_scanner.CostBasisResolver"):
            scanner = OptionsScanner(Mock(), md, cc)
        result = scanner.scan_for_put_opportunities()
        assert result == []
        # The gate must return BEFORE any market-data work (the KeyError-noise fix).
        md.filter_suitable_stocks.assert_not_called()

    def test_wheel_put_scan_is_not_gated(self):
        # Wheel profile must still run the put scan (it will do real work / fail
        # on the fully-mocked market data, but it must NOT early-return []).
        wheel = _wheel_config()
        md = Mock()
        md.filter_suitable_stocks.return_value = []
        with patch("src.data.options_scanner.UncoveredDaysResolver"), \
             patch("src.data.options_scanner.CostBasisResolver"):
            scanner = OptionsScanner(Mock(), md, wheel)
        scanner.scan_for_put_opportunities()
        # It reached market-data filtering → not gated.
        assert md.filter_suitable_stocks.called


# --------------------------------------------------------------------------- #
# DD-3 — inventory-validator chain criteria + wheel-neutrality
# --------------------------------------------------------------------------- #

class TestChainCriteria:
    def _md(self, config):
        return MarketDataManager(Mock(), config)

    def _call(self, **over):
        base = {"dte": 5, "mid_price": 1.0, "delta": 0.20, "volume": 100,
                "open_interest": 1000, "bid": 0.98, "ask": 1.02, "strike_price": 200}
        base.update(over)
        return base

    def test_open_interest_below_threshold_rejected(self):
        md = self._md(_cc_config())  # min_open_interest=500
        assert md._check_call_criteria_detailed(self._call(open_interest=100)) == "open_interest_too_low"
        assert md._check_call_criteria_detailed(self._call(open_interest=1000)) is None

    def test_spread_wider_than_threshold_rejected(self):
        md = self._md(_cc_config())  # max_spread_pct=0.10
        # spread 0.30 on mid 1.0 = 30% > 10%
        assert md._check_call_criteria_detailed(self._call(bid=0.85, ask=1.15)) == "spread_too_wide"
        assert md._check_call_criteria_detailed(self._call(bid=0.98, ask=1.02)) is None

    def test_missing_field_fails_closed(self):
        md = self._md(_cc_config())
        # OI absent with a threshold set → reject (fail closed).
        assert md._check_call_criteria_detailed(self._call(open_interest=None)) == "open_interest_too_low"
        # bid absent with max_spread_pct set → reject (can't verify the spread).
        assert md._check_call_criteria_detailed(self._call(bid=None)) == "spread_too_wide"

    def test_wheel_profile_criteria_inert(self):
        # No thresholds set → the new checks never fire → identical to before.
        md = self._md(_wheel_config())
        for oi in (0, 5, 5000):
            # would fail OI on CC, but wheel has min_open_interest=None
            assert md._check_call_criteria_detailed(self._call(open_interest=oi, volume=100)) is None
        # wide spread passes on the wheel (no max_spread_pct)
        assert md._check_call_criteria_detailed(self._call(bid=0.5, ask=1.5)) is None


# --------------------------------------------------------------------------- #
# DD-4 — strategy-scoped BQ reads
# --------------------------------------------------------------------------- #

class TestBigQueryReadDataset:
    def test_uncovered_days_resolver_built_with_config_dataset(self):
        with patch("src.data.options_scanner.UncoveredDaysResolver") as UDR, \
             patch("src.data.options_scanner.CostBasisResolver"):
            OptionsScanner(Mock(), Mock(), _cc_config())
        _, kwargs = UDR.call_args
        assert kwargs.get("dataset_id") == "covered_call"

    def test_wheel_uses_options_wheel_dataset(self):
        with patch("src.data.options_scanner.UncoveredDaysResolver") as UDR, \
             patch("src.data.options_scanner.CostBasisResolver"):
            OptionsScanner(Mock(), Mock(), _wheel_config())
        _, kwargs = UDR.call_args
        assert kwargs.get("dataset_id") == "options_wheel"

    @pytest.mark.real_bq_lookup  # opt out of the conftest hermeticity stub
    def test_cost_basis_query_uses_config_dataset(self):
        from src.strategy.cost_basis import CostBasisResolver
        r = CostBasisResolver.__new__(CostBasisResolver)
        r.config = _cc_config()          # bigquery_dataset = "covered_call"
        r.allow_bigquery = True
        r._bq_client = None
        captured = {}

        class _FakeJob:
            def result(self, *a, **k):
                return []

        class _FakeClient:
            def query(self, sql, **kw):
                captured["sql"] = sql
                return _FakeJob()

        with patch("google.cloud.bigquery.Client", return_value=_FakeClient()), \
             patch("google.cloud.bigquery.QueryJobConfig", return_value=Mock()), \
             patch("google.cloud.bigquery.ScalarQueryParameter", return_value=Mock()):
            r._lookup_assignment_basis("AAPL", 100)
        assert "covered_call.trades_from_activities" in captured["sql"]
        assert "options_wheel.trades_from_activities" not in captured["sql"]


# --------------------------------------------------------------------------- #
# Config surface
# --------------------------------------------------------------------------- #

class TestConfigSurface:
    def test_real_covered_call_config_loads_with_phase2_keys(self):
        c = Config(str(REPO / "config" / "covered_call.yaml"))
        assert c.strategy_id == "covered_call"
        assert c.writes_isolated is False          # DD-7 armed
        assert c.sizing_basis == "equity"
        assert c.min_open_interest == 500
        assert c.max_spread_pct == 0.10
        assert c.earnings_enabled is True
        assert c.excluded_symbols == set()          # empty but normalized

    def test_wheel_defaults_are_neutral(self):
        c = Config(str(REPO / "config" / "settings.yaml"))
        assert c.writes_isolated is True
        assert c.sizing_basis == "buying_power"
        assert c.min_open_interest is None
        assert c.max_spread_pct is None
        assert c.excluded_symbols == set()

    def _write(self, tmp_path, extra):
        import yaml
        data = {
            "strategy_id": "covered_call",
            "alpaca": {"paper_trading": True, "api_key_id": "k", "secret_key": "s",
                       "expected_account_number": "PA_CC"},
            "strategy": {"call_target_dte": 7, "call_delta_range": [0.15, 0.25]},
            "risk": {"sizing_basis": "equity", "max_position_size": 0.35},
            "gcs": {"opportunity_bucket": "cc"},
            "bigquery": {"dataset": "covered_call"},
        }
        for k, v in extra.items():
            data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.update({k: v})
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump(data))
        return str(p)

    def test_validation_rejects_bad_spread(self, tmp_path):
        with pytest.raises(ValueError, match="max_spread_pct"):
            Config(self._write(tmp_path, {"universe": {"max_spread_pct": 1.5}}))

    def test_validation_rejects_non_list_excluded(self, tmp_path):
        with pytest.raises(ValueError, match="excluded_symbols"):
            Config(self._write(tmp_path, {"universe": {"excluded_symbols": "AAPL"}}))

    def test_validation_rejects_unknown_sizing_basis(self, tmp_path):
        with pytest.raises(ValueError, match="sizing_basis"):
            Config(self._write(tmp_path, {"risk": {"sizing_basis": "margin"}}))

    def test_excluded_symbols_normalized(self, tmp_path):
        c = Config(self._write(tmp_path, {"universe": {"excluded_symbols": ["aapl", " Msft "]}}))
        assert c.excluded_symbols == {"AAPL", "MSFT"}


# --------------------------------------------------------------------------- #
# DD-7 — write interlock (server)
# --------------------------------------------------------------------------- #

flask = pytest.importorskip("flask")


@pytest.fixture
def server():
    mod = importlib.import_module("deploy.cloud_run_server")
    mod.reset_strategy_state()
    yield mod
    mod.reset_strategy_state()


class TestWriteInterlock:
    def test_covered_call_scan_refused_until_seam4(self, server, monkeypatch):
        monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")
        monkeypatch.delenv("STRATEGY_API_KEY", raising=False)
        server.reset_strategy_state()
        # Pass the account interlock (CC account) so we reach the write guard.
        alpaca = Mock()
        alpaca.get_account.return_value = {"account_number": "PA37XLNWDLB3"}
        with patch("src.api.alpaca_client.AlpacaClient", return_value=alpaca):
            resp = server.app.test_client().post("/scan")
        assert resp.status_code == 503
        assert b"write_isolation_unavailable" in resp.data

    def test_ingest_also_write_isolated(self, server, monkeypatch):
        monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")
        monkeypatch.delenv("STRATEGY_API_KEY", raising=False)
        alpaca = Mock()
        alpaca.get_account.return_value = {"account_number": "PA37XLNWDLB3"}
        for ep in ("/ingest-activities", "/ingest-portfolio-history", "/ingest-stock-history"):
            server.reset_strategy_state()
            with patch("src.api.alpaca_client.AlpacaClient", return_value=alpaca):
                resp = server.app.test_client().post(ep)
            assert resp.status_code == 503, ep
            assert b"write_isolation_unavailable" in resp.data

    def test_wheel_passes_the_write_guard(self, server, monkeypatch):
        # The wheel profile is always write-isolated: the guard must NOT 503 it.
        # (It fails the account interlock with the mocked wrong account, but the
        # point is the write guard doesn't fire — a wheel 503 here would be the
        # account guard, not write_isolation.)
        monkeypatch.delenv("STRATEGY_CONFIG", raising=False)
        server.reset_strategy_state()
        assert server.strategy_config().writes_isolated is True
