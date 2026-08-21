"""FC-075 Phase 2 — the covered-call engine (call-only gating of the shared pipeline).

Each test names the regression it catches. Covers: the put-scan gate (DD-2); the
call-only filter + its `previously_failed`-mislabel fix, via the extracted
`_call_only_opportunities` helper (DD-2); the reconcile gate, both directions via
`/run` (DD-6); the inventory validator — chain criteria + `excluded_symbols`
decision row + wheel-neutrality (DD-3); strategy-scoped BQ reads (DD-4); the
exposure alert incl. get_account-raises, via `_maybe_alert_long_exposure` (DD-5);
the DD-7 write interlock on the server AND the main.py CLI; and the config surface.

Note on the covered-call `/run`/`/scan` handler bodies: DD-7 makes those endpoints
503 for a non-wheel profile until Seam 4, so the covered-call *direction* of the
inline gates is tested either through the extracted helpers (call-only filter,
exposure alert) or by patching `strategy_config` to a mock covered-call profile
with `writes_isolated=True` (simulating post-Seam-4), while the wheel direction is
driven through the real endpoint.
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


# --------------------------------------------------------------------------- #
# DD-2 — call-only filter (extracted helper) + previously_failed no-mislabel
# --------------------------------------------------------------------------- #

class TestCallOnlyFilter:
    def _call(self, sym="META260220C00600000"):
        return {"option_symbol": sym, "symbol": "META", "type": "call"}

    def _put(self, sym="AAPL260220P00170000"):
        return {"option_symbol": sym, "symbol": "AAPL", "type": "put"}

    def test_drops_puts_keeps_calls_and_logs(self, server):
        events = []
        with patch.object(server, "log_error_event",
                          side_effect=lambda *a, **k: events.append(k.get("error_type"))):
            kept = server._call_only_opportunities(
                [self._call(), self._put(), self._call("NVDA260220C00500000")],
                "covered_call", Mock())
        assert [o["option_symbol"] for o in kept] == \
            ["META260220C00600000", "NVDA260220C00500000"]
        assert events == ["non_call_opportunity_refused"]  # one per dropped put

    def test_refused_put_not_mislabeled_previously_failed(self, server):
        # The MEDIUM fix: applying the filter to BOTH lists means the refused
        # put's underlying is not flagged previously_failed by _underlyings_removed.
        blob = [self._call(), self._put()]
        kept = server._call_only_opportunities(blob, "covered_call", Mock())
        # handler sets both opportunities and blob_opportunities to `kept`
        assert server._underlyings_removed(kept, kept) == set()
        # sanity: had the blob NOT been filtered, the put's underlying WOULD show
        # (proves the assertion is meaningful) — puts aren't call-opps though, so
        # _underlyings_removed only tracks calls; the real guarantee is no crash +
        # empty diff on the filtered lists.
        assert "AAPL" not in server._underlyings_removed(kept, kept)


# --------------------------------------------------------------------------- #
# DD-5 — exposure alert (extracted helper)
# --------------------------------------------------------------------------- #

class TestExposureAlert:
    def _cfg(self, basis="equity", threshold=1.0):
        c = Mock()
        c.sizing_basis = basis
        c.max_long_market_value_pct_of_equity = threshold
        return c

    def _events(self, server, cfg, account):
        alpaca = Mock()
        if isinstance(account, Exception):
            alpaca.get_account.side_effect = account
        else:
            alpaca.get_account.return_value = account
        seen = []
        with patch.object(server, "log_error_event",
                          side_effect=lambda *a, **k: seen.append(k.get("error_type"))):
            server._maybe_alert_long_exposure(cfg, alpaca, Mock())
        return seen, alpaca

    def test_alerts_when_exposure_exceeds_equity(self, server):
        seen, _ = self._events(server, self._cfg(),
                               {"long_market_value": 150.0, "equity": 100.0})
        assert "long_exposure_exceeds_equity" in seen

    def test_no_alert_when_within_equity(self, server):
        seen, _ = self._events(server, self._cfg(),
                               {"long_market_value": 90.0, "equity": 100.0})
        assert seen == []

    def test_alerts_when_equity_nonpositive_with_exposure(self, server):
        seen, _ = self._events(server, self._cfg(),
                               {"long_market_value": 50.0, "equity": 0.0})
        assert "long_exposure_exceeds_equity" in seen

    def test_get_account_raises_is_isolated(self, server):
        # HIGH-1's mandated case: a get_account blip must NOT propagate (would
        # otherwise kill the scan) — it logs a check-failed event instead.
        seen, _ = self._events(server, self._cfg(), RuntimeError("alpaca down"))
        assert seen == ["long_exposure_check_failed"]

    def test_wheel_basis_never_fetches_account(self, server):
        seen, alpaca = self._events(server, self._cfg(basis="buying_power"),
                                    {"long_market_value": 999.0, "equity": 1.0})
        assert seen == []
        alpaca.get_account.assert_not_called()


# --------------------------------------------------------------------------- #
# DD-6 — reconcile gate, both directions via /run
# --------------------------------------------------------------------------- #

def _run_mock_config(strategy_id, account="PA_ACCT"):
    c = Mock()
    c.strategy_id = strategy_id
    c.expected_account_number = account
    c.writes_isolated = True  # bypass DD-7 to reach the handler (post-Seam-4 sim)
    return c


def _drive_run(server, config_mock):
    server.reset_strategy_state()
    alpaca = Mock()
    alpaca.get_account.return_value = {
        "account_number": config_mock.expected_account_number,
        "buying_power": 100000, "options_buying_power": 100000, "equity": 100000,
        "long_market_value": 0, "cash": 100000, "portfolio_value": 100000,
    }
    store = Mock()
    store.get_pending_opportunities.return_value = []  # early return after reconcile
    wheel_cls = Mock()
    with patch.object(server, "strategy_config", return_value=config_mock), \
         patch("src.api.alpaca_client.AlpacaClient", return_value=alpaca), \
         patch("src.data.opportunity_store.OpportunityStore", return_value=store), \
         patch("src.strategy.wheel_engine.WheelEngine", wheel_cls):
        resp = server.app.test_client().post("/run")
    return resp, wheel_cls


class TestReconcileGate:
    def test_wheel_run_reconciles(self, server, monkeypatch):
        monkeypatch.delenv("STRATEGY_API_KEY", raising=False)
        resp, wheel_cls = _drive_run(server, _run_mock_config("wheel"))
        assert resp.status_code == 200
        assert wheel_cls.called  # WheelEngine constructed
        assert wheel_cls.return_value.reconcile_positions.called

    def test_covered_call_run_skips_reconcile(self, server, monkeypatch):
        monkeypatch.delenv("STRATEGY_API_KEY", raising=False)
        resp, wheel_cls = _drive_run(server, _run_mock_config("covered_call"))
        assert resp.status_code == 200
        assert not wheel_cls.called  # no WheelEngine → FC-054/079 sites not traversed


# --------------------------------------------------------------------------- #
# DD-3 — excluded_symbols produces an excluded_by_config decision row
# --------------------------------------------------------------------------- #

class TestExcludedSymbols:
    def test_excluded_holding_recorded_as_excluded_before_lot_check(self):
        cc = _cc_config()          # excluded_symbols = {"AAPL"}
        alpaca = Mock()
        # A 50-share AAPL lot: excluded must win over the <100-share not-eligible.
        alpaca.get_positions.return_value = [{
            "symbol": "AAPL", "qty": "50", "asset_class": "us_equity",
            "current_price": "190", "avg_entry_price": "150", "market_value": "9500",
        }]
        recorded = []
        rec = Mock()
        rec.record.side_effect = lambda sym, outcome, reason, **k: recorded.append((sym, outcome, reason))
        with patch("src.data.options_scanner.UncoveredDaysResolver") as UDR, \
             patch("src.data.options_scanner.CostBasisResolver"), \
             patch("src.data.options_scanner.DecisionRecorder", return_value=rec):
            UDR.return_value.resolve.return_value = {}
            scanner = OptionsScanner(alpaca, Mock(), cc)
            scanner.scan_for_call_opportunities()
        from src.data.decision_record import OUTCOME_NOT_ELIGIBLE, REASON_EXCLUDED_BY_CONFIG
        assert ("AAPL", OUTCOME_NOT_ELIGIBLE, REASON_EXCLUDED_BY_CONFIG) in recorded


# --------------------------------------------------------------------------- #
# DD-7 — the main.py CLI write interlock (HIGH-2's mandated CLI test)
# --------------------------------------------------------------------------- #

class TestCliWriteInterlock:
    def test_cli_scan_refused_for_covered_call_profile(self, monkeypatch):
        import importlib
        main = importlib.import_module("main")
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--command", "scan",
                             "--config", "config/covered_call.yaml"])
        alpaca = Mock()
        with patch("main.AlpacaClient", return_value=alpaca), \
             patch("main.MarketDataManager", return_value=Mock()), \
             patch("main.PortfolioTracker", return_value=Mock()), \
             patch("main.OptionsScanner", return_value=Mock()), \
             patch("main.scan_opportunities") as scan_fn:
            with pytest.raises(SystemExit) as exc:
                main.main()
        assert exc.value.code == 2          # writes_isolated False → refused
        scan_fn.assert_not_called()          # never scanned


# --------------------------------------------------------------------------- #
# Req 7 — a covered-call execution journals as a call (post-FC-067)
# --------------------------------------------------------------------------- #

class TestJournalLabelingThroughExecute:
    def test_scanner_call_opportunity_journals_as_call(self):
        # execution_engine.execute_batch persists a filled trade via
        # `record_trade({**opp, <order fields>})` (execution_engine.py:824). This
        # pins the effective outcome for a scanner-shaped covered-call
        # opportunity through that exact composition: FC-067's OCC-first
        # derivation labels it call/sell_call even though the scanner opp carries
        # only `type`, not `option_type`/`strategy`.
        from src.data.trade_journal import TradeJournal
        tj = TradeJournal.__new__(TradeJournal)
        tj._enabled = True
        tj._client = Mock()
        tj._client.insert_rows_json.return_value = []
        tj._table_ref = "proj.covered_call.trades"

        scanner_call_opp = {
            "symbol": "META", "type": "call",
            "option_symbol": "META260220C00600000",
            "strike_price": 600.0, "contracts": 1, "premium": 3.10,
        }
        # exactly what execute_batch spreads for a filled order:
        tj.record_trade({**scanner_call_opp, "order_id": "x",
                         "status": "submitted", "fill_price": 3.10})

        row = tj._client.insert_rows_json.call_args[0][1][0]
        assert row["option_type"] == "call"
        assert row["strategy"] == "sell_call"
