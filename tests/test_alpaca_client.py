"""Tests for Alpaca API client wrapper."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import requests.exceptions

from src.api.alpaca_client import (
    AlpacaClient,
    api_retry,
    is_rate_limit_error,
    is_retryable_error
)
from tenacity import RetryError


class TestRetryHelpers:
    """Test retry helper functions."""

    def test_is_rate_limit_error_with_429(self):
        """Test detection of 429 rate limit errors."""
        exc = Exception("HTTP 429: Too Many Requests")
        assert is_rate_limit_error(exc) is True

    def test_is_rate_limit_error_with_rate_limit_message(self):
        """Test detection of rate limit in message."""
        exc = Exception("Rate limit exceeded, please try again later")
        assert is_rate_limit_error(exc) is True

    def test_is_rate_limit_error_with_other_error(self):
        """Test that non-rate-limit errors return False."""
        exc = Exception("Invalid API key")
        assert is_rate_limit_error(exc) is False

    def test_is_retryable_error_timeout(self):
        """Test detection of timeout errors."""
        exc = Exception("Connection timeout after 30 seconds")
        assert is_retryable_error(exc) is True

    def test_is_retryable_error_connection(self):
        """Test detection of connection errors."""
        exc = Exception("Connection refused by server")
        assert is_retryable_error(exc) is True

    def test_is_retryable_error_503(self):
        """Test detection of 503 service unavailable."""
        exc = Exception("HTTP 503: Service Unavailable")
        assert is_retryable_error(exc) is True

    def test_is_retryable_error_502(self):
        """Test detection of 502 bad gateway."""
        exc = Exception("HTTP 502: Bad Gateway")
        assert is_retryable_error(exc) is True

    def test_is_retryable_error_invalid_request(self):
        """Test that invalid request errors are not retryable."""
        exc = Exception("Invalid symbol: XYZ123")
        assert is_retryable_error(exc) is False

    def test_is_retryable_error_auth_error(self):
        """Test that auth errors are not retryable."""
        exc = Exception("Invalid API key provided")
        assert is_retryable_error(exc) is False


class TestApiRetryDecorator:
    """Test the api_retry decorator."""

    def test_api_retry_success_first_attempt(self):
        """Test that successful calls work without retries."""
        call_count = 0

        @api_retry
        def successful_function():
            nonlocal call_count
            call_count += 1
            return "success"

        result = successful_function()
        assert result == "success"
        assert call_count == 1

    def test_api_retry_retries_on_timeout(self):
        """Test that timeout errors are retried."""
        call_count = 0

        @api_retry
        def timeout_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.Timeout("Connection timed out")
            return "success"

        result = timeout_then_success()
        assert result == "success"
        assert call_count == 2

    def test_api_retry_retries_on_connection_error(self):
        """Test that connection errors are retried."""
        call_count = 0

        @api_retry
        def connection_error_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("Failed to connect")
            return "success"

        result = connection_error_then_success()
        assert result == "success"
        assert call_count == 2

    def test_api_retry_gives_up_after_max_attempts(self):
        """Test that retries stop after max attempts."""
        call_count = 0

        @api_retry
        def always_timeout():
            nonlocal call_count
            call_count += 1
            raise requests.exceptions.Timeout("Connection timed out")

        with pytest.raises(requests.exceptions.Timeout):
            always_timeout()

        assert call_count == 3  # Initial + 2 retries

    def test_api_retry_no_retry_on_auth_error(self):
        """Test that authentication errors are not retried."""
        call_count = 0

        @api_retry
        def auth_error():
            nonlocal call_count
            call_count += 1
            raise Exception("Invalid API credentials")

        with pytest.raises(Exception) as exc_info:
            auth_error()

        assert "Invalid API credentials" in str(exc_info.value)
        assert call_count == 1  # No retries

    def test_api_retry_retries_on_rate_limit(self):
        """Test that rate limit errors (429) trigger retries."""
        call_count = 0

        @api_retry
        def rate_limited_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("HTTP 429: Too Many Requests")
            return "success"

        result = rate_limited_then_success()
        assert result == "success"
        assert call_count == 2


class TestAlpacaClientInit:
    """Test AlpacaClient initialization."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_client_initialization(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test that client initializes with correct credentials."""
        client = AlpacaClient(mock_config)

        mock_trading_client.assert_called_once_with(
            api_key='test_api_key',
            secret_key='test_secret_key',
            paper=True
        )
        mock_stock_client.assert_called_once()
        mock_option_client.assert_called_once()

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_client_stores_config(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test that client stores config reference."""
        client = AlpacaClient(mock_config)
        assert client.config == mock_config


class TestAlpacaClientAccount:
    """Test AlpacaClient account methods."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_account_returns_dict(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_account returns properly formatted dict."""
        # Setup mock account
        mock_account = MagicMock()
        mock_account.buying_power = "50000.00"
        mock_account.cash = "25000.00"
        mock_account.portfolio_value = "100000.00"
        mock_account.equity = "100000.00"
        mock_account.options_buying_power = "25000.00"
        mock_account.options_approved_level = 2

        mock_trading_client.return_value.get_account.return_value = mock_account

        client = AlpacaClient(mock_config)
        account = client.get_account()

        assert account['buying_power'] == 50000.0
        assert account['cash'] == 25000.0
        assert account['portfolio_value'] == 100000.0
        assert account['equity'] == 100000.0
        assert account['options_buying_power'] == 25000.0
        assert account['options_approved_level'] == 2

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_account_handles_missing_options_buying_power(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_account handles accounts without options_buying_power."""
        mock_account = MagicMock(spec=['buying_power', 'cash', 'portfolio_value', 'equity'])
        mock_account.buying_power = "50000.00"
        mock_account.cash = "25000.00"
        mock_account.portfolio_value = "100000.00"
        mock_account.equity = "100000.00"
        # Note: options_buying_power is NOT in spec, so hasattr returns False

        mock_trading_client.return_value.get_account.return_value = mock_account

        client = AlpacaClient(mock_config)
        account = client.get_account()

        assert account['options_buying_power'] == 0.0


class TestAlpacaClientPositions:
    """Test AlpacaClient position methods."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_positions_empty(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_positions with no positions."""
        mock_trading_client.return_value.get_all_positions.return_value = []

        client = AlpacaClient(mock_config)
        positions = client.get_positions()

        assert positions == []

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_positions_returns_list(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_positions returns properly formatted list."""
        mock_position = MagicMock()
        mock_position.symbol = "AAPL"
        mock_position.qty = "100"
        mock_position.side = "long"
        mock_position.market_value = "17500.00"
        mock_position.cost_basis = "16000.00"
        mock_position.unrealized_pl = "1500.00"
        mock_position.asset_class = "us_equity"

        mock_trading_client.return_value.get_all_positions.return_value = [mock_position]

        client = AlpacaClient(mock_config)
        positions = client.get_positions()

        assert len(positions) == 1
        assert positions[0]['symbol'] == "AAPL"
        assert positions[0]['qty'] == 100.0
        assert positions[0]['market_value'] == 17500.0

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_option_positions_filters_correctly(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_option_positions filters to only options."""
        from alpaca.trading.enums import AssetClass

        stock_position = MagicMock()
        stock_position.symbol = "AAPL"
        stock_position.qty = "100"
        stock_position.side = "long"
        stock_position.market_value = "17500.00"
        stock_position.cost_basis = "16000.00"
        stock_position.unrealized_pl = "1500.00"
        stock_position.asset_class = AssetClass.US_EQUITY

        option_position = MagicMock()
        option_position.symbol = "AAPL250117P00170000"
        option_position.qty = "-1"
        option_position.side = "short"
        option_position.market_value = "-150.00"
        option_position.cost_basis = "200.00"
        option_position.unrealized_pl = "50.00"
        option_position.asset_class = AssetClass.US_OPTION

        mock_trading_client.return_value.get_all_positions.return_value = [
            stock_position, option_position
        ]

        client = AlpacaClient(mock_config)
        option_positions = client.get_option_positions()

        assert len(option_positions) == 1
        assert "250117P" in option_positions[0]['symbol']


class _SdkPosition:
    """A position as the Alpaca SDK returns it: plain attributes, string
    numerics, and only the fields the SDK actually sets.

    Deliberately NOT a ``MagicMock``: a mock answers every attribute, so it
    cannot show whether the wrapper reads a field the SDK omits — which is the
    exact failure mode this contract test exists to catch.
    """

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class TestGetPositionsOutputContract:
    """FC-065 (plan-review BLOCKER): ``get_positions()`` must emit
    ``avg_entry_price``.

    The covered-call floor is that one field. The wrapper used to strip it, so
    a resolver reading it would have failed closed fleet-wide while unit tests
    passed against ``tests/conftest.py``'s position fixture, which happens to
    carry it. These tests therefore exercise the REAL wrapper with only the
    SDK boundary (``trading_client.get_all_positions``) mocked, and they fail
    against the pre-FC-065 implementation.
    """

    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.TradingClient')
    def _positions(self, mock_config, sdk_positions, mock_trading_client,
                   mock_stock_client, mock_option_client):
        """Run the real wrapper with only the SDK boundary mocked.

        (``mock.patch`` appends its mocks after the caller's arguments, hence
        the parameter order.)
        """
        mock_trading_client.return_value.get_all_positions.return_value = sdk_positions
        return AlpacaClient(mock_config).get_positions()

    def test_a_long_equity_position_carries_avg_entry_price(self, mock_config):
        """The four live wheel lots are shaped exactly like this."""
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL", qty="100", side="long", market_value="31000.00",
            cost_basis="30350.00", avg_entry_price="303.50",
            unrealized_pl="650.00", asset_class="us_equity",
        )])

        assert len(positions) == 1
        assert 'avg_entry_price' in positions[0], (
            "the covered-call floor field was stripped by the wrapper")
        assert positions[0]['avg_entry_price'] == 303.50
        assert isinstance(positions[0]['avg_entry_price'], float)

    def test_a_short_option_position_carries_avg_entry_price(self, mock_config):
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL260821C00320000", qty="-1", side="short",
            market_value="-150.00", cost_basis="-200.00",
            avg_entry_price="2.00", unrealized_pl="50.00",
            asset_class="us_option",
        )])

        assert positions[0]['avg_entry_price'] == 2.00

    def test_a_missing_sdk_field_is_derived_for_a_long_position(self, mock_config):
        """Plan: for long positions ``avg_entry_price == cost_basis / qty`` —
        verified on all four live lots — so that is the fallback if the SDK
        ever stops sending the field."""
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL", qty="100", side="long", market_value="31000.00",
            cost_basis="30350.00", unrealized_pl="650.00",
            asset_class="us_equity",
        )])

        assert positions[0]['avg_entry_price'] == 303.50

    @pytest.mark.parametrize("raw", [None, "", "not-a-number"])
    def test_an_unusable_sdk_value_falls_back_to_the_derivation(self, raw, mock_config):
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL", qty="100", side="long", market_value="31000.00",
            cost_basis="30350.00", avg_entry_price=raw,
            unrealized_pl="650.00", asset_class="us_equity",
        )])

        assert positions[0]['avg_entry_price'] == 303.50

    def test_an_underivable_value_is_zero_so_the_floor_fails_closed(self, mock_config):
        """A zero here is what makes the resolver skip the symbol. It must be
        0, never a guess: FC-029 saw Alpaca report ``cost_basis = 0`` on
        assigned positions, and a guessed floor is worse than no floor."""
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL", qty="100", side="long", market_value="31000.00",
            cost_basis="0", unrealized_pl="0", asset_class="us_equity",
        )])

        assert positions[0]['avg_entry_price'] == 0.0

    def test_a_short_position_is_not_derived_from_its_negative_cost_basis(self, mock_config):
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL260821C00320000", qty="-1", side="short",
            market_value="-150.00", cost_basis="-200.00",
            unrealized_pl="50.00", asset_class="us_option",
        )])

        assert positions[0]['avg_entry_price'] == 0.0

    def test_the_rest_of_the_shape_is_unchanged(self, mock_config):
        """Every consumer of this dict keeps working."""
        positions = self._positions(mock_config, [_SdkPosition(
            symbol="AAPL", qty="100", side="long", market_value="31000.00",
            cost_basis="30350.00", avg_entry_price="303.50",
            unrealized_pl="650.00", asset_class="us_equity",
        )])

        assert positions[0] == {
            'symbol': "AAPL", 'qty': 100.0, 'side': "long",
            'market_value': 31000.0, 'cost_basis': 30350.0,
            'avg_entry_price': 303.50, 'unrealized_pl': 650.0,
            'asset_class': "us_equity",
        }


class TestAlpacaClientMarketData:
    """Test AlpacaClient market data methods."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_stock_quote(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_stock_quote returns formatted data."""
        mock_quote = MagicMock()
        mock_quote.bid_price = 174.50
        mock_quote.ask_price = 175.50
        mock_quote.bid_size = 100
        mock_quote.ask_size = 100
        mock_quote.timestamp = datetime.now(timezone.utc)

        mock_stock_client.return_value.get_stock_latest_quote.return_value = {
            'AAPL': mock_quote
        }

        client = AlpacaClient(mock_config)
        quote = client.get_stock_quote('AAPL')

        assert quote['symbol'] == 'AAPL'
        assert quote['bid'] == 174.50
        assert quote['ask'] == 175.50
        assert quote['bid_size'] == 100
        assert quote['ask_size'] == 100


class TestAlpacaClientOrders:
    """Test AlpacaClient order methods."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_place_option_order_success(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test successful option order placement."""
        mock_order = MagicMock()
        mock_order.id = "test-order-id-123"
        mock_order.status = "new"
        mock_order.symbol = "MSFT250117P00380000"
        mock_order.qty = "1"
        mock_order.filled_qty = "0"
        mock_order.side = "sell"
        mock_order.type = "limit"
        mock_order.limit_price = "2.50"

        mock_trading_client.return_value.submit_order.return_value = mock_order

        client = AlpacaClient(mock_config)
        result = client.place_option_order(
            symbol="MSFT250117P00380000",
            qty=1,
            side="sell",
            order_type="limit",
            limit_price=2.50
        )

        assert result['success'] is True
        assert result['order_id'] == "test-order-id-123"
        assert result['status'] == "new"


class TestAlpacaClientErrorHandling:
    """Test AlpacaClient error handling."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_account_raises_on_error(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_account raises exception on API error."""
        mock_trading_client.return_value.get_account.side_effect = Exception("API Error")

        client = AlpacaClient(mock_config)

        with pytest.raises(Exception) as exc_info:
            client.get_account()

        assert "API Error" in str(exc_info.value)

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_get_positions_raises_on_error(
        self,
        mock_option_client,
        mock_stock_client,
        mock_trading_client,
        mock_config
    ):
        """Test get_positions raises exception on API error."""
        mock_trading_client.return_value.get_all_positions.side_effect = Exception("API Error")

        client = AlpacaClient(mock_config)

        with pytest.raises(Exception) as exc_info:
            client.get_positions()

        assert "API Error" in str(exc_info.value)


def _mock_order(status, symbol="AMD250815P00150000", side="sell", qty="1",
                filled_qty="0"):
    """Build a mock Alpaca order with the attributes get_orders reads."""
    o = MagicMock()
    o.id = f"id-{status}-{symbol}"
    o.symbol = symbol
    o.qty = qty
    o.filled_qty = filled_qty
    o.is_partial_fill = False
    o.side = MagicMock(value=side)
    o.status = MagicMock(value=status)
    o.order_type = MagicMock(value="limit")
    o.submitted_at = None
    o.filled_at = None
    o.filled_avg_price = None
    return o


class TestAlpacaClientGetOrders:
    """FC-043: the status filter must route to the right query bucket.

    The prior implementation called ``trading_client.get_orders()`` with no
    request (Alpaca defaults to status=open, so closed orders were never
    fetched) and value-filtered ``status.value == status`` against query tokens
    like ``'open'`` that are never OrderStatus values — so every status filter
    returned nothing useful. These tests pin the routing that fixes it.
    """

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def _client(self, mo, ms, mt, mock_config, returned_orders):
        mt.return_value.get_orders.return_value = returned_orders
        client = AlpacaClient(mock_config)
        return client, mt.return_value

    def _query_status_arg(self, trading_client):
        """The QueryOrderStatus the wrapper asked Alpaca for."""
        _, kwargs = trading_client.get_orders.call_args
        req = kwargs.get('filter') or trading_client.get_orders.call_args.args[0]
        return req.status

    def test_open_requests_the_open_bucket(self, mock_config):
        # A resting order in the OPEN bucket has status.value 'accepted',
        # never 'open'. The old code filtered value == 'open' and dropped it.
        from alpaca.trading.enums import QueryOrderStatus
        order = _mock_order("accepted")
        client, tc = self._client(mock_config=mock_config, returned_orders=[order])

        result = client.get_orders(status='open')

        assert self._query_status_arg(tc) == QueryOrderStatus.OPEN
        assert len(result) == 1, "an open order must be returned for status='open'"
        assert result[0]['status'] == 'accepted'

    def test_filled_requests_the_closed_bucket_and_value_filters(self, mock_config):
        from alpaca.trading.enums import QueryOrderStatus
        orders = [_mock_order("filled"), _mock_order("expired")]
        client, tc = self._client(mock_config=mock_config, returned_orders=orders)

        result = client.get_orders(status='filled')

        assert self._query_status_arg(tc) == QueryOrderStatus.CLOSED, \
            "'filled' lives in the CLOSED bucket — must fetch it, not the open default"
        assert [o['status'] for o in result] == ['filled'], \
            "value filter must drop the expired order"

    def test_expired_requests_the_closed_bucket(self, mock_config):
        from alpaca.trading.enums import QueryOrderStatus
        orders = [_mock_order("filled"), _mock_order("expired")]
        client, tc = self._client(mock_config=mock_config, returned_orders=orders)

        result = client.get_orders(status='expired')

        assert self._query_status_arg(tc) == QueryOrderStatus.CLOSED
        assert [o['status'] for o in result] == ['expired']

    def test_pending_new_requests_the_open_bucket(self, mock_config):
        from alpaca.trading.enums import QueryOrderStatus
        client, tc = self._client(
            mock_config=mock_config, returned_orders=[_mock_order("pending_new")])

        result = client.get_orders(status='pending_new')

        assert self._query_status_arg(tc) == QueryOrderStatus.OPEN
        assert [o['status'] for o in result] == ['pending_new']

    def test_none_requests_all(self, mock_config):
        from alpaca.trading.enums import QueryOrderStatus
        orders = [_mock_order("filled"), _mock_order("accepted")]
        client, tc = self._client(mock_config=mock_config, returned_orders=orders)

        result = client.get_orders()

        assert self._query_status_arg(tc) == QueryOrderStatus.ALL
        assert len(result) == 2, "no value filter for status=None"

    def test_closed_token_no_value_filter(self, mock_config):
        from alpaca.trading.enums import QueryOrderStatus
        orders = [_mock_order("filled"), _mock_order("expired")]
        client, tc = self._client(mock_config=mock_config, returned_orders=orders)

        result = client.get_orders(status='closed')

        assert self._query_status_arg(tc) == QueryOrderStatus.CLOSED
        assert len(result) == 2, "'closed' is a bucket token, not a value filter"

    def test_request_carries_an_explicit_limit(self, mock_config):
        # ALL/CLOSED must not silently truncate at Alpaca's default page of 50.
        client, tc = self._client(mock_config=mock_config, returned_orders=[])
        client.get_orders()
        _, kwargs = tc.get_orders.call_args
        req = kwargs.get('filter') or tc.get_orders.call_args.args[0]
        assert req.limit is not None and req.limit >= 100


class TestOptionsChainCarriesTheQuoteStamp:
    """FC-120 PR-1 (T-LOW): each chain row carries its quote's broker stamp as
    ``quote_timestamp`` — the roller's ``chain_quote_age_s`` reads it. The
    chain call passes no feed, so no feed is implied on the row."""

    @patch('src.api.alpaca_client.TradingClient')
    @patch('src.api.alpaca_client.StockHistoricalDataClient')
    @patch('src.api.alpaca_client.OptionHistoricalDataClient')
    def test_rows_carry_quote_timestamp(self, mock_option_client,
                                        mock_stock_client, mock_trading_client,
                                        mock_config):
        stamp = datetime(2026, 9, 30, 19, 30, 1, tzinfo=timezone.utc)
        quote = Mock(bid_price=8.0, ask_price=8.4, bid_size=3, ask_size=4,
                     timestamp=stamp)
        stale = Mock(spec=['bid_price', 'ask_price', 'bid_size', 'ask_size'],
                     bid_price=1.0, ask_price=1.2, bid_size=1, ask_size=1)
        mock_option_client.return_value.get_option_chain.return_value = {
            'GOOGL261016C00350000': Mock(latest_quote=quote, latest_trade=None,
                                         greeks=None, implied_volatility=None),
            'GOOGL261016C00355000': Mock(latest_quote=stale, latest_trade=None,
                                         greeks=None, implied_volatility=None),
        }
        rows = AlpacaClient(mock_config).get_options_chain('GOOGL')
        by_symbol = {r['symbol']: r for r in rows}
        assert by_symbol['GOOGL261016C00350000']['quote_timestamp'] == stamp
        assert by_symbol['GOOGL261016C00355000']['quote_timestamp'] is None
        assert 'feed' not in by_symbol['GOOGL261016C00350000']
