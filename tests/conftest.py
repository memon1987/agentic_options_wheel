"""Shared test fixtures for options wheel strategy tests."""

import pytest
import os
import sys
import tempfile
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
from typing import Dict, Any, List
import yaml

from src.utils.config import Config
from src.utils import clock as _time_seam


# ==================== Global isolation ====================

@pytest.fixture(autouse=True)
def _reset_cloud_run_caches():
    """Reset the process-scoped strategy caches between tests (FC-075).

    ``deploy.cloud_run_server`` caches the Config and the account-interlock
    verdict at module scope. Without a reset, a verdict cached by one test
    (e.g. a mismatch → 503) would leak into the next. Only touches the module if
    a test already imported it, so non-server tests (and flask-less envs) are
    unaffected.

    Seam 4 adds the AnalyticsWriter singleton to that state: its configured
    dataset/strategy is per-process, and re-configuring it after construction
    raises by design. It is reset unconditionally — the CLI configures it
    without importing the server.
    """
    def _reset():
        from src.data import analytics_writer
        analytics_writer._reset_for_tests()
        mod = sys.modules.get("deploy.cloud_run_server")
        if mod is not None and hasattr(mod, "reset_strategy_state"):
            mod.reset_strategy_state()
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _logging_config_is_not_leaked():
    """No test may leave structlog or the root logger reconfigured.

    A test that calls ``setup_logging()`` and does not restore it changes the
    process for every test that follows: it installs a root ``FileHandler`` on
    ``logs/options_wheel.log`` (so the suite starts writing megabytes of replay
    logs to disk) and flips ``cache_logger_on_first_use=True``, which makes
    structlog's lazy proxies cache their processor chain — the exact mechanism
    that blinds ``RejectionTally`` after the first replay in a process.

    That is not hypothetical: it produced a real order-dependent failure in this
    suite (`test_scenarios.py` leaking into `test_backtest_simulator.py`'s
    dead-path assertions). Ambient global state is the same class of defect as a
    test depending on live BigQuery or a real Alpaca key, and it gets the same
    treatment — a blanket autouse guard, so a future leak fails in the test that
    caused it rather than three files later.
    """
    import logging

    import structlog

    before_structlog = structlog.get_config()
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    after_structlog = structlog.get_config()
    leaked = []
    if after_structlog != before_structlog:
        leaked.append("structlog.configure()")
    if list(root.handlers) != before_handlers:
        leaked.append("root logging handlers")
    if root.level != before_level:
        leaked.append(f"root log level ({before_level} -> {root.level})")
    if leaked:
        # Restore first, so ONE leaking test does not cascade into a hundred
        # confusing failures; then fail the test that actually did it.
        structlog.configure(**before_structlog)
        root.handlers[:] = before_handlers
        root.setLevel(before_level)
        raise AssertionError(
            "this test left global logging state modified: "
            + ", ".join(leaked)
            + ". Restore it in a finally block — see "
            "tests/test_scenarios.py::TestTheRejectionTallySurvivesQuietLogs "
            "for the pattern."
        )


@pytest.fixture(autouse=True)
def _reset_time_seam():
    """Ensure the backtest time-seam is never left frozen between tests.

    The FC-032 simulator freezes src.utils.clock.now() to simulated time; a test
    that leaked a freeze would silently shift time-dependent live-code tests.
    """
    _time_seam.set_now(None)
    yield
    _time_seam.set_now(None)


@pytest.fixture(autouse=True)
def _no_production_bigquery(monkeypatch, request):
    """No test may query production BigQuery, whatever credentials are present.

    The cost-basis layer has exactly one BigQuery chokepoint —
    `CostBasisResolver._lookup_assignment_basis`, which since FC-065 runs the
    divergence cross-check and builds its own client from ambient env/ADC. It
    is patched on the *class*, so every resolver instance is covered. The
    census, as of FC-068: the one OptionsScanner owns, the one CallRoller owns
    (FC-065 Phase 2), and any built directly inside a test. CallSeller no
    longer owns one — FC-068 deleted its resolver with the opportunity-
    generation half of that class, and `execute_call_sale` reads its floor off
    the opportunity (`opportunity_floor_per_share`) instead.
    `TestTheBigQueryChokepoint` in tests/test_cost_basis.py asserts that census
    explicitly, including CallSeller's *absence*.
    Returning None is the "no comparison available" signal, which keeps
    whatever floor the broker reported rather than inventing one.

    Three tests in test_call_seller.py were passing only because
    BigQuery auth happened to be broken on the machine: once `gcloud auth login`
    succeeded, the lookup started returning a *real* AAPL assignment strike
    ($305) in place of the fixture's $160 cost basis, and the drawdown pause
    then suppressed the covered call under test.

    A unit test whose result depends on ambient cloud credentials is not a unit
    test, so the fallback is stubbed out globally here. Tests that want to
    exercise it should inject their own value explicitly.
    """
    # A test that is specifically exercising the BigQuery fallback opts out with
    # @pytest.mark.real_bq_lookup. Without this escape hatch the blanket stub
    # made test_lookup_last_opasn_put_strike_handles_bq_failure vacuous — it
    # asserted the stub's return value, and the try/except it exists to cover
    # (the only thing keeping the bot alive when BQ is down) had zero coverage.
    if request.node.get_closest_marker("real_bq_lookup"):
        yield
        return

    from src.strategy.cost_basis import CostBasisResolver
    from src.data.decision_record import UncoveredDaysResolver

    monkeypatch.setattr(
        CostBasisResolver, "_lookup_assignment_basis",
        lambda self, symbol, shares_held: None,
    )
    # FC-065 Phase 4 adds a SECOND BigQuery chokepoint: the decision record's
    # `uncovered_days` label, batched once per scan. Patched on the class for
    # the same reason as the resolver above — every instance is covered, and
    # None is the honest "we could not tell" signal (never 0 days, which would
    # read as "covered today").
    monkeypatch.setattr(
        UncoveredDaysResolver, "_lookup_uncovered_days",
        lambda self, symbols: None,
    )
    yield


@pytest.fixture(autouse=True)
def _no_chain_lake(monkeypatch):
    """No test may reach the GCS chain lake, whatever is in the shell or .env.

    `ChainStore.from_env()` (FC-060) reads CHAIN_LAKE_BUCKET, and a developer
    with the seed variable exported would otherwise have the suite issuing real
    GCS reads *and uploads* against the production lake. Same principle as
    `_no_production_bigquery` and `_deterministic_alpaca_credentials`: a unit
    test's behaviour must not depend on ambient environment, and the failure
    here would write, not just read.

    Tests that exercise lake wiring set the variable themselves with
    monkeypatch and always inject a fake lake or a fake client factory.
    """
    monkeypatch.delenv("CHAIN_LAKE_BUCKET", raising=False)
    monkeypatch.delenv("CHAIN_LAKE_PREFIX", raising=False)
    yield


@pytest.fixture(scope="session")
def _bars_cache_root(tmp_path_factory):
    """One temp root for the whole session; each test gets a subpath under it."""
    return tmp_path_factory.mktemp("bars_cache")


@pytest.fixture(autouse=True)
def _isolated_bars_cache(monkeypatch, request, _bars_cache_root):
    """No test may read or write the developer's bar cache (FC-060 Layer 2).

    ``BarStore`` defaults to ``cache/backtest/bars`` relative to the working
    directory, so without this a test that constructs one — directly or through
    ``evaluate_symbol`` — would read whatever a real backtest happened to leave
    there and, worse, write into it. Same principle as ``_no_chain_lake``: a
    unit test's behaviour must not depend on ambient state, and here the failure
    would be a silent cross-test data dependency rather than an error.

    The path is per-test but **not created** — ``BarStore`` mkdirs on write, and
    the overwhelming majority of tests never construct one. Calling
    ``tmp_path_factory.mktemp`` per test instead (the first cut) made ~2,100
    directories nobody used and cost ~6 s of wall clock on every suite run.
    """
    monkeypatch.setenv(
        "BACKTEST_BARS_CACHE_DIR",
        str(_bars_cache_root / request.node.nodeid.replace("/", "_").replace(":", "_")),
    )
    yield


@pytest.fixture(autouse=True)
def _deterministic_alpaca_credentials(monkeypatch):
    """Every test sees the same fake Alpaca credentials.

    `Config()` validates that ALPACA_API_KEY_ID / ALPACA_SECRET_KEY are set
    and raises otherwise. Tests that build a real Config (test_clock_seam,
    test_backtest_simulator — which want the real settings.yaml values, e.g.
    the profit_taking DTE bands) therefore passed on a developer machine
    with a .env file and failed in Cloud Build with 18 errors, because the
    result depended on ambient environment.

    Credentials are set UNCONDITIONALLY rather than only-when-missing: the
    point is that the suite behaves identically everywhere. A unit test must
    never reach a real broker, so a real key present in the shell is a
    hazard here, not a convenience. Same principle as
    `_no_production_bigquery` above.
    """
    # config/settings.yaml substitutes ${ALPACA_API_KEY}. Note the
    # validation error used to name ALPACA_API_KEY_ID, which is not read by
    # anything — corrected in src/utils/config.py alongside this fixture.
    monkeypatch.setenv("ALPACA_API_KEY", "test_api_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret_key")
    yield


@pytest.fixture(autouse=True)
def _no_finnhub(monkeypatch, request):
    """No test may reach Finnhub or the earnings L2 blob, whatever is in .env.

    Three separate defects in this suite's history were "the test depends on
    ambient environment" (live BigQuery, missing FastAPI, missing Alpaca
    credentials). The earnings gate adds a fourth surface, so it gets the same
    treatment the other two got:

    1. **The env var is pinned unconditionally.** ``Config.finnhub_api_key``
       reads ``os.getenv`` directly and ``Config.__init__`` calls
       ``load_dotenv()``, so without this a developer's real key leaks into
       tests and CI diverges from dev. Pinned to a fake value rather than
       cleared, because "enabled with a key" is the shape production runs and
       the degraded path deserves its own explicit test, not the default.
    2. **``_fetch_earnings`` is patched on the CLASS**, so every instance —
       including one a test constructs directly — answers "known clear" with
       zero network. FC-013's tri-state maps ``{"date": None}`` to
       ``('clear', None)``: an unmocked test sees an unconstrained gate, which
       is the pre-gate behaviour and therefore the safe default.
    3. **Both L2/GCS helpers are no-ops.** No unit test touches Cloud Storage;
       a read that returns None is a cache miss, which is exactly the contract.

    ``clear_earnings_cache()`` runs before *and* after each test: the L1 cache
    is module-scope by design (FC-013 §1), so without this an entry cached by
    one test would answer for another. Same principle as ``_reset_time_seam``.

    Escape hatch: ``@pytest.mark.real_finnhub_fetch`` keeps the real
    ``_fetch_earnings`` for the service's own unit tests, which mock the finnhub
    *client object* instead (still no network). Without the marker the blanket
    patch would make those tests vacuous — the ``real_bq_lookup`` lesson.
    """
    from src.api.earnings_calendar import EarningsCalendarService, clear_earnings_cache

    monkeypatch.setenv("FINNHUB_API_KEY", "test_finnhub_key")

    # The GCS no-ops and the cache reset apply to EVERY test, marker or not:
    # the escape hatch is about Finnhub's client, never about touching Cloud
    # Storage or leaking module-scope cache entries.
    monkeypatch.setattr(EarningsCalendarService, "_l2_read", lambda self: None)
    monkeypatch.setattr(EarningsCalendarService, "_store_to_l2", lambda self: None)
    # Belt and braces: even a test that restores the real `_l2_read` (to
    # exercise its exception handling) must not be able to construct a client.
    def _no_storage_client(*args, **kwargs):
        raise AssertionError("no test may construct a GCS client")
    monkeypatch.setattr(
        EarningsCalendarService, "_storage_client",
        staticmethod(_no_storage_client))
    # A stand-in client, so "does this test see a working gate" never depends on
    # whether `finnhub-python` happens to be installed. It is never called: the
    # only method that touches it is `_fetch_earnings`, chokepointed below.
    monkeypatch.setattr(
        EarningsCalendarService, "_build_client",
        staticmethod(lambda api_key: object()))

    if not request.node.get_closest_marker("real_finnhub_fetch"):
        monkeypatch.setattr(
            EarningsCalendarService, "_fetch_earnings",
            lambda self, symbol: {
                "date": None, "hour": None, "fetched_at": datetime.now(),
            },
        )

    clear_earnings_cache()
    yield
    clear_earnings_cache()


# ==================== Configuration Fixtures ====================

@pytest.fixture
def test_config_data() -> Dict[str, Any]:
    """Return a complete, valid test configuration dictionary."""
    return {
        'strategy_id': 'wheel',  # FC-075: required top-level key
        'alpaca': {
            'paper_trading': True,
            'api_key_id': 'test_api_key',
            'secret_key': 'test_secret_key',
            'expected_account_number': 'PA_TEST_ACCT'  # FC-075 Seam 2 interlock
        },
        'strategy': {
            'put_target_dte': 7,
            'call_target_dte': 7,
            'put_delta_range': [0.10, 0.20],
            'call_delta_range': [0.10, 0.20],
            'min_put_premium': 0.50,
            'min_call_premium': 0.30,
            'min_stock_price': 20.0,
            'max_stock_price': 500.0,
            'min_avg_volume': 1000000,
            'opportunity_max_age_minutes': 30
        },
        'risk': {
            'max_position_size': 0.10,
            'use_put_stop_loss': False,
            'use_call_stop_loss': True,
            'put_stop_loss_percent': 0.50,
            'call_stop_loss_percent': 0.50,
            'stop_loss_multiplier': 1.5,
            'profit_taking': {
                'use_dynamic_profit_target': True,
                'static_profit_target': 0.50,
                'min_profit_target': 0.30,
                'max_profit_target': 0.80,
                'default_long_dte_target': 0.50,
                'dte_bands': []
            }
            # FC-069 S1 deleted the gap_risk_controls block with GapDetector.
        },
        'stocks': {
            'symbols': ['AAPL', 'MSFT', 'GOOGL']
        }
        # FC-069 S1 deleted the monitoring block and dropped it from
        # Config's required_sections.
    }


@pytest.fixture
def config_file(test_config_data, tmp_path):
    """Create a temporary config file with valid test data."""
    config_path = tmp_path / "test_settings.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(test_config_data, f)
    return str(config_path)


@pytest.fixture
def mock_config():
    """Create a mock Config object with common settings."""
    config = Mock(spec=Config)

    # Alpaca settings
    config.paper_trading = True
    config.alpaca_api_key = 'test_api_key'
    config.alpaca_secret_key = 'test_secret_key'

    # Strategy settings
    config.put_target_dte = 7
    config.call_target_dte = 7
    config.put_delta_range = [0.10, 0.20]
    config.call_delta_range = [0.10, 0.20]
    config.min_put_premium = 0.50
    config.min_call_premium = 0.30
    config.min_stock_price = 20.0
    config.max_stock_price = 500.0
    config.min_avg_volume = 1000000
    config.stock_symbols = ['AAPL', 'MSFT', 'GOOGL']
    config.opportunity_max_age_minutes = 30

    # Risk settings
    config.max_position_size = 0.10
    config.use_put_stop_loss = False
    config.use_call_stop_loss = True
    config.put_stop_loss_percent = 0.50
    config.call_stop_loss_percent = 0.50
    config.stop_loss_multiplier = 1.5

    # Profit taking settings
    config.use_dynamic_profit_target = True
    config.profit_taking_static_target = 0.50
    config.profit_taking_min_target = 0.30
    config.profit_taking_max_target = 0.80
    config.profit_taking_default_long_dte = 0.50
    config.profit_taking_dte_bands = []

    # FC-069 S1 deleted the gap-risk and monitoring knobs along with
    # GapDetector; nothing under test reads them any more.

    return config


# ==================== Account Fixtures ====================

@pytest.fixture
def sample_account_info() -> Dict[str, Any]:
    """Return a sample Alpaca account info dictionary."""
    return {
        'id': 'test_account_id',
        'account_number': '123456789',
        'status': 'ACTIVE',
        'currency': 'USD',
        'portfolio_value': 100000.0,
        'cash': 25000.0,
        'buying_power': 50000.0,
        'equity': 100000.0,
        'last_equity': 99500.0,
        'long_market_value': 75000.0,
        'short_market_value': 0.0,
        'initial_margin': 37500.0,
        'maintenance_margin': 25000.0,
        'daytrading_buying_power': 100000.0,
        'non_marginable_buying_power': 25000.0,
        'accrued_fees': 0.0,
        'pending_transfer_in': 0.0,
        'pending_transfer_out': 0.0,
        'pattern_day_trader': False,
        'trading_blocked': False,
        'transfers_blocked': False,
        'account_blocked': False,
        'created_at': '2024-01-01T00:00:00Z',
        'trade_suspended_by_user': False,
        'multiplier': '4',
        'shorting_enabled': True,
        'options_trading_level': 2,
        'options_approved_level': 2
    }


# ==================== Position Fixtures ====================

@pytest.fixture
def sample_stock_position() -> Dict[str, Any]:
    """Return a sample stock position."""
    return {
        'asset_id': 'test_asset_id',
        'symbol': 'AAPL',
        'exchange': 'NASDAQ',
        'asset_class': 'us_equity',
        'asset_marginable': True,
        'qty': 100,
        'qty_available': 100,
        'side': 'long',
        'market_value': 17500.0,
        'cost_basis': 16000.0,
        'unrealized_pl': 1500.0,
        'unrealized_plpc': 0.09375,
        'unrealized_intraday_pl': 250.0,
        'unrealized_intraday_plpc': 0.0145,
        'current_price': 175.0,
        'lastday_price': 172.5,
        'change_today': 0.0145,
        'avg_entry_price': 160.0
    }


@pytest.fixture
def sample_option_position() -> Dict[str, Any]:
    """Return a sample option position (short put)."""
    return {
        'asset_id': 'test_option_id',
        'symbol': 'AAPL250117P00170000',
        'exchange': 'OPRA',
        'asset_class': 'us_option',
        'asset_marginable': True,
        'qty': -1,
        'qty_available': -1,
        'side': 'short',
        'market_value': -150.0,
        'cost_basis': 200.0,
        'unrealized_pl': 50.0,
        'unrealized_plpc': 0.25,
        'unrealized_intraday_pl': 10.0,
        'unrealized_intraday_plpc': 0.071,
        'current_price': 1.50,
        'lastday_price': 1.60,
        'change_today': -0.0625,
        'avg_entry_price': 2.00
    }


@pytest.fixture
def sample_positions(sample_stock_position, sample_option_position) -> List[Dict[str, Any]]:
    """Return a list of sample positions."""
    return [sample_stock_position, sample_option_position]


# ==================== Opportunity Fixtures ====================

@pytest.fixture
def sample_put_opportunity() -> Dict[str, Any]:
    """Return a sample put selling opportunity."""
    return {
        'strategy': 'sell_put',
        'symbol': 'MSFT',
        'option_symbol': 'MSFT250117P00380000',
        'strike_price': 380.0,
        'strike': 380.0,
        'current_stock_price': 400.0,
        'expiration': '2025-01-17',
        'dte': 7,
        'delta': -0.15,
        'premium': 2.50,
        'bid': 2.45,
        'ask': 2.55,
        'volume': 1500,
        'open_interest': 5000,
        'implied_volatility': 0.25,
        'capital_required': 38000.0,
        'contracts': 1,
        'annual_return': 0.24,
        'created_at': datetime.now(timezone.utc).isoformat()
    }


@pytest.fixture
def sample_call_opportunity() -> Dict[str, Any]:
    """Return a sample call selling opportunity."""
    return {
        'strategy': 'sell_call',
        'symbol': 'AAPL',
        'option_symbol': 'AAPL250117C00185000',
        'strike_price': 185.0,
        'strike': 185.0,
        'current_stock_price': 175.0,
        'expiration': '2025-01-17',
        'dte': 7,
        'delta': 0.15,
        'premium': 1.80,
        'bid': 1.75,
        'ask': 1.85,
        'volume': 2000,
        'open_interest': 8000,
        'implied_volatility': 0.22,
        'capital_required': 0.0,
        'contracts': 1,
        'annual_return': 0.54,
        'created_at': datetime.now(timezone.utc).isoformat()
    }


# ==================== Order Fixtures ====================

@pytest.fixture
def sample_order_response() -> Dict[str, Any]:
    """Return a sample order response."""
    return {
        'id': 'test_order_id_123',
        'client_order_id': 'client_order_123',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'submitted_at': datetime.now(timezone.utc).isoformat(),
        'filled_at': None,
        'expired_at': None,
        'canceled_at': None,
        'failed_at': None,
        'replaced_at': None,
        'replaced_by': None,
        'replaces': None,
        'asset_id': 'test_asset_id',
        'symbol': 'MSFT250117P00380000',
        'asset_class': 'us_option',
        'notional': None,
        'qty': 1,
        'filled_qty': 0,
        'filled_avg_price': None,
        'order_class': 'simple',
        'order_type': 'limit',
        'type': 'limit',
        'side': 'sell',
        'time_in_force': 'day',
        'limit_price': 2.50,
        'stop_price': None,
        'status': 'new',
        'extended_hours': False,
        'legs': None,
        'trail_percent': None,
        'trail_price': None,
        'hwm': None
    }


# ==================== Option Chain Fixtures ====================

@pytest.fixture
def sample_option_contract() -> Dict[str, Any]:
    """Return a sample option contract from chain."""
    return {
        'symbol': 'MSFT250117P00380000',
        'underlying_symbol': 'MSFT',
        'expiration': '2025-01-17',
        'strike': 380.0,
        'option_type': 'PUT',
        'bid': 2.45,
        'ask': 2.55,
        'last': 2.50,
        'volume': 1500,
        'open_interest': 5000,
        'delta': -0.15,
        'gamma': 0.02,
        'theta': -0.05,
        'vega': 0.10,
        'implied_volatility': 0.25
    }


@pytest.fixture
def sample_option_chain(sample_option_contract) -> Dict[str, List[Dict[str, Any]]]:
    """Return a sample option chain with puts and calls."""
    puts = []
    calls = []

    # Generate puts at different strikes
    for strike in [370, 375, 380, 385, 390]:
        put = sample_option_contract.copy()
        put['strike'] = float(strike)
        put['symbol'] = f"MSFT250117P00{strike}000"
        put['delta'] = -0.10 - (390 - strike) * 0.02
        puts.append(put)

    # Generate calls at different strikes
    for strike in [400, 405, 410, 415, 420]:
        call = sample_option_contract.copy()
        call['strike'] = float(strike)
        call['option_type'] = 'CALL'
        call['symbol'] = f"MSFT250117C00{strike}000"
        call['delta'] = 0.10 + (strike - 400) * 0.02
        calls.append(call)

    return {'puts': puts, 'calls': calls}


# ==================== Mock Client Fixtures ====================

@pytest.fixture
def mock_trading_client():
    """Create a mock Alpaca trading client."""
    client = MagicMock()

    # Mock account info
    mock_account = MagicMock()
    mock_account.id = 'test_account_id'
    mock_account.status = 'ACTIVE'
    mock_account.portfolio_value = '100000.0'
    mock_account.cash = '25000.0'
    mock_account.buying_power = '50000.0'
    mock_account.equity = '100000.0'
    mock_account.options_trading_level = 2
    mock_account.options_approved_level = 2
    client.get_account.return_value = mock_account

    # Mock empty positions by default
    client.get_all_positions.return_value = []

    return client


@pytest.fixture
def mock_data_client():
    """Create a mock Alpaca data client."""
    client = MagicMock()

    # Mock stock quote
    mock_quote = MagicMock()
    mock_quote.bid_price = 174.50
    mock_quote.ask_price = 175.50
    mock_quote.bid_size = 100
    mock_quote.ask_size = 100
    client.get_stock_latest_quote.return_value = {'AAPL': mock_quote}

    return client


@pytest.fixture
def mock_option_client():
    """Create a mock Alpaca option client."""
    client = MagicMock()
    return client


# ==================== Environment Fixtures ====================

@pytest.fixture
def mock_env_vars():
    """Patch environment variables for testing."""
    env_vars = {
        'ALPACA_API_KEY': 'test_api_key',
        'ALPACA_SECRET_KEY': 'test_secret_key',
        'GCS_BUCKET_NAME': 'test-bucket',
        'GOOGLE_CLOUD_PROJECT': 'test-project'
    }
    with patch.dict(os.environ, env_vars):
        yield env_vars


# ==================== Time Fixtures ====================

@pytest.fixture
def market_hours_time():
    """Return a datetime during market hours (10:00 AM ET)."""
    return datetime(2025, 1, 15, 15, 0, 0, tzinfo=timezone.utc)  # 10 AM ET = 3 PM UTC


@pytest.fixture
def after_hours_time():
    """Return a datetime after market hours (5:00 PM ET)."""
    return datetime(2025, 1, 15, 22, 0, 0, tzinfo=timezone.utc)  # 5 PM ET = 10 PM UTC


@pytest.fixture
def weekend_time():
    """Return a datetime on a weekend (Saturday)."""
    return datetime(2025, 1, 18, 15, 0, 0, tzinfo=timezone.utc)  # Saturday
