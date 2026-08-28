"""Alpaca API client wrapper for options wheel strategy."""

from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
import hashlib
import time
import pandas as pd
import structlog
from functools import wraps

from alpaca.trading.client import TradingClient
from alpaca.data import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, QueryOrderStatus
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest, OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_exception,
    before_sleep_log,
    RetryError
)
import requests
import requests.exceptions

from ..utils.config import Config
from ..utils.option_symbols import parse_option_symbol

logger = structlog.get_logger(__name__)

# Options quote feed, passed explicitly on every option quote request (FC-072).
#
# "indicative" is an ADJUSTED best-bid/offer, NOT the NBBO. It is what this
# account is entitled to: the OPRA agreement has not been signed. Every
# sell-to-open limit is therefore computed from a non-NBBO book, which is
# acceptable for paper and for the FC-072 fill-rate readout and is NOT
# acceptable for real money -- a limit priced off an adjusted book can rest
# away from the real market with nothing in the logs to say so.
#
# SIGNING THE OPRA AGREEMENT IS A PRECONDITION BEFORE THIS PRICES REAL MONEY.
# Do not flip this constant to "opra" as a way of getting better quotes: the
# entitlement, not the parameter, is what is missing, and the request would
# simply fail. Flip it when the agreement is signed, and only then.
OPTION_QUOTE_FEED = "indicative"


class CircuitBreaker:
    """Simple circuit breaker for API calls.

    States:
        closed  - Normal operation, requests flow through.
        open    - Too many failures; requests are blocked.
        half_open - After reset_timeout, one test request is allowed through.
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = 'closed'  # closed=normal, open=failing, half_open=testing

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            if self.state != 'open':
                logger.warning(
                    "Circuit breaker opened — API failures exceeded threshold",
                    event_category="system",
                    event_type="circuit_breaker_opened",
                    failure_count=self.failure_count,
                    threshold=self.failure_threshold,
                )
            self.state = 'open'

    def record_success(self) -> None:
        if self.state != 'closed':
            logger.info(
                "Circuit breaker closed — API recovered",
                event_category="system",
                event_type="circuit_breaker_closed",
                previous_state=self.state,
            )
        self.failure_count = 0
        self.state = 'closed'

    def can_execute(self) -> bool:
        if self.state == 'closed':
            return True
        if self.state == 'open' and self.last_failure_time is not None and \
                time.time() - self.last_failure_time > self.reset_timeout:
            logger.info(
                "Circuit breaker half-open — allowing test request",
                event_category="system",
                event_type="circuit_breaker_half_open",
            )
            self.state = 'half_open'
            return True
        return self.state != 'open'


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker is open and blocking requests."""
    pass


# Module-level circuit breaker shared across all Alpaca API calls
_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60)


def is_rate_limit_error(exception: Exception) -> bool:
    """Check if exception is a rate limit error (HTTP 429)."""
    error_str = str(exception).lower()
    return '429' in error_str or 'rate limit' in error_str or 'too many requests' in error_str


def is_retryable_error(exception: Exception) -> bool:
    """Check if exception is retryable (network issues, timeouts, rate limits)."""
    error_str = str(exception).lower()
    retryable_patterns = [
        'timeout', 'connection', 'network', '429', 'rate limit',
        'too many requests', 'service unavailable', '503', '502', '504'
    ]
    return any(pattern in error_str for pattern in retryable_patterns)


def _generate_client_order_id(symbol: str, qty: int, side: str, limit_price: float) -> str:
    """Generate deterministic client_order_id for idempotent order submission.

    Uses a hash of order parameters plus today's date so the same logical order
    on the same day always produces the same ID, preventing duplicate orders
    when HTTP requests are retried.
    """
    raw = f"{symbol}:{date.today().isoformat()}:{side}:{qty}:{limit_price}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def api_retry(func):
    """Decorator to add retry logic with exponential backoff for API calls.

    Includes circuit breaker protection: if the API has failed repeatedly,
    new requests are blocked until a reset timeout elapses to avoid
    overwhelming a degraded service.

    Retries on:
    - Network timeouts and connection errors
    - Rate limit errors (429)
    - Service unavailable (503, 502, 504)

    Does NOT retry on:
    - Authentication errors
    - Invalid request errors
    - Business logic errors (insufficient funds, invalid symbol)
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Check circuit breaker before attempting the call
        if not _circuit_breaker.can_execute():
            logger.error("API call blocked by circuit breaker",
                        event_category="error",
                        event_type="circuit_breaker_blocked",
                        function=func.__name__)
            raise CircuitBreakerOpen(
                f"Circuit breaker is open — API call to {func.__name__} blocked"
            )

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                ConnectionError,
                TimeoutError
            )) | retry_if_exception(is_retryable_error),
            before_sleep=before_sleep_log(logger, log_level=20),  # INFO level
            reraise=True
        )
        def inner():
            return func(*args, **kwargs)

        try:
            result = inner()
            _circuit_breaker.record_success()
            return result
        except RetryError as e:
            _circuit_breaker.record_failure()
            # Log final failure after all retries exhausted
            logger.error("API call failed after all retries",
                        event_category="error",
                        event_type="api_retry_exhausted",
                        function=func.__name__,
                        error=str(e.last_attempt.exception()) if e.last_attempt else str(e))
            raise e.last_attempt.exception() if e.last_attempt else e

    return wrapper


class AlpacaClient:
    """Wrapper for Alpaca API clients with options trading support."""
    
    def __init__(self, config: Config):
        """Initialize Alpaca client.
        
        Args:
            config: Configuration instance
        """
        self.config = config
        
        # Initialize clients
        self.trading_client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper_trading
        )
        
        self.stock_data_client = StockHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key
        )
        
        self.option_data_client = OptionHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key
        )
        
        logger.info("Alpaca client initialized",
                   event_category="system",
                   event_type="client_initialized",
                   paper_trading=config.paper_trading)
    
    # Account Information
    @api_retry
    def get_account(self) -> Dict[str, Any]:
        """Get account information."""
        try:
            account = self.trading_client.get_account()
            return {
                'account_number': getattr(account, 'account_number', None),
                'buying_power': float(account.buying_power),
                'cash': float(account.cash),
                'portfolio_value': float(account.portfolio_value),
                'equity': float(account.equity),
                'long_market_value': float(getattr(account, 'long_market_value', 0.0) or 0.0),
                'options_buying_power': float(account.options_buying_power) if hasattr(account, 'options_buying_power') else 0.0,
                'options_approved_level': getattr(account, 'options_approved_level', 0)
            }
        except Exception as e:
            logger.error("Failed to get account info",
                        event_category="error",
                        event_type="account_info_error",
                        error=str(e))
            raise
    
    @staticmethod
    def _avg_entry_price(position: Any, qty: float, cost_basis: float) -> float:
        """Per-share entry price for a position, with a derived fallback.

        FC-065: this field is the covered-call cost-basis floor, so it has to
        survive an SDK that omits it. For a long position it equals
        ``cost_basis / qty`` — verified against all four live lots — so that is
        the fallback. Shorts are left at 0 rather than derived: their
        ``cost_basis`` is negative and nothing reads a floor off them.
        """
        raw = getattr(position, 'avg_entry_price', None)
        try:
            value = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        if qty > 0 and cost_basis > 0:
            return cost_basis / qty
        return 0.0

    @api_retry
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all current positions.

        FC-065 (plan-review BLOCKER): ``avg_entry_price`` is emitted here. It
        is the sole cost-basis floor source for covered calls, and this wrapper
        used to strip it — so the floor resolver would have failed closed
        fleet-wide while unit tests passed against a fixture that happened to
        carry the field.
        """
        try:
            positions = self.trading_client.get_all_positions()
            result: List[Dict[str, Any]] = []
            for pos in positions:
                qty = float(pos.qty)
                cost_basis = float(pos.cost_basis)
                result.append({
                    'symbol': pos.symbol,
                    'qty': qty,
                    'side': pos.side,
                    'market_value': float(pos.market_value),
                    'cost_basis': cost_basis,
                    'avg_entry_price': self._avg_entry_price(pos, qty, cost_basis),
                    'unrealized_pl': float(pos.unrealized_pl),
                    'asset_class': pos.asset_class
                })
            return result
        except Exception as e:
            logger.error("Failed to get positions",
                        event_category="error",
                        event_type="positions_error",
                        error=str(e))
            raise

    def get_option_positions(self) -> List[Dict[str, Any]]:
        """Get all current option positions.

        Filters positions to only return US options (excluding stock positions).
        Used for idempotency checks to prevent duplicate option trades.

        Returns:
            List of option position dictionaries
        """
        try:
            positions = self.get_positions()
            option_positions = [
                p for p in positions
                if p.get('asset_class') == AssetClass.US_OPTION
            ]
            logger.debug("Retrieved option positions",
                        event_category="system",
                        event_type="option_positions_retrieved",
                        count=len(option_positions))
            return option_positions
        except Exception as e:
            logger.error("Failed to get option positions",
                        event_category="error",
                        event_type="option_positions_error",
                        error=str(e))
            raise

    # Market Data
    @api_retry
    def get_stock_quote(self, symbol: str) -> Dict[str, Any]:
        """Get latest stock quote using IEX feed for real-time data.

        IEX provides real-time quotes for free (no 15-min delay like SIP).
        While IEX only covers ~3% of market volume, it's sufficient for
        current price quotes in options wheel strategy.

        Args:
            symbol: Stock symbol

        Returns:
            Latest quote data
        """
        try:
            request = StockLatestQuoteRequest(
                symbol_or_symbols=[symbol],
                feed='iex'  # Real-time quotes from IEX exchange (FREE)
            )
            quotes = self.stock_data_client.get_stock_latest_quote(request)
            quote = quotes[symbol]

            return {
                'symbol': symbol,
                'bid': float(quote.bid_price),
                'ask': float(quote.ask_price),
                'bid_size': int(quote.bid_size),
                'ask_size': int(quote.ask_size),
                'timestamp': quote.timestamp
            }
        except Exception as e:
            logger.error("Failed to get stock quote",
                        event_category="error",
                        event_type="stock_quote_error",
                        symbol=symbol,
                        error=str(e))
            raise

    def get_option_quote(self, option_symbol: str) -> Dict[str, Any]:
        """Get latest bid/ask quote for a specific option contract.

        The feed is passed EXPLICITLY (FC-072) rather than left to the SDK's
        default, and echoed back in the payload. This account is on the
        indicative feed because the OPRA agreement is not signed, so every
        limit priced off this quote is priced off an adjusted BBO rather than
        the NBBO -- see ``OPTION_QUOTE_FEED``. Callers log the returned ``feed``
        so that fact is auditable per order instead of remembered.

        Args:
            option_symbol: Full OCC option symbol (e.g., AAPL250117C00185000)

        Returns:
            Dict with bid, ask, mid_price, feed and timestamp, or empty dict on
            failure. Never raises.
        """
        try:
            request = OptionLatestQuoteRequest(symbol_or_symbols=[option_symbol],
                                               feed=OPTION_QUOTE_FEED)
            quotes = self.option_data_client.get_option_latest_quote(request)
            quote = quotes.get(option_symbol)
            if not quote:
                return {}

            bid = float(quote.bid_price) if quote.bid_price else 0.0
            ask = float(quote.ask_price) if quote.ask_price else 0.0
            mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else 0.0

            return {
                'symbol': option_symbol,
                'bid': bid,
                'ask': ask,
                'mid_price': mid,
                'bid_size': int(quote.bid_size) if quote.bid_size else 0,
                'ask_size': int(quote.ask_size) if quote.ask_size else 0,
                'feed': OPTION_QUOTE_FEED,
                # The BROKER's stamp, not ours: FC-072 measures a live quote's
                # age from it, because a halted or thin name's "latest" quote
                # can be hours old and must not read as fresh.
                'timestamp': getattr(quote, 'timestamp', None),
            }
        except Exception as e:
            logger.debug("Could not get option quote",
                        event_category="data",
                        event_type="option_quote_failed",
                        symbol=option_symbol,
                        error=str(e))
            return {}

    @api_retry
    def get_stock_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Get historical stock bars using SIP feed with 15-min delay buffer.

        Uses SIP (Securities Information Processor) feed for best data quality:
        - 100% market coverage (all 16 exchanges)
        - NBBO (National Best Bid Offer) prices
        - Free with 15-minute delay

        The 20-minute buffer ensures we never query recent data that would
        require a paid subscription. This is perfect for wheel strategy which
        uses daily bars for gap analysis and doesn't need real-time bars.

        Args:
            symbol: Stock symbol
            days: Number of days of history

        Returns:
            DataFrame with OHLCV data
        """
        try:
            # Account for 15-min SIP delay with 20-min buffer for safety
            end_date = datetime.now() - timedelta(minutes=20)
            start_date = end_date - timedelta(days=days)

            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Day,
                start=start_date,
                end=end_date
                # No feed parameter = defaults to SIP (best quality, 15-min delayed)
            )

            bars = self.stock_data_client.get_stock_bars(request)

            # Convert to DataFrame
            data = []
            for bar in bars[symbol]:
                data.append({
                    'timestamp': bar.timestamp,
                    'open': float(bar.open),
                    'high': float(bar.high),
                    'low': float(bar.low),
                    'close': float(bar.close),
                    'volume': int(bar.volume)
                })

            df = pd.DataFrame(data)
            df.set_index('timestamp', inplace=True)
            return df

        except Exception as e:
            logger.error("Failed to get stock bars",
                        event_category="error",
                        event_type="stock_bars_error",
                        symbol=symbol,
                        error=str(e))
            raise
    
    @api_retry
    def get_options_chain(self, underlying_symbol: str) -> List[Dict[str, Any]]:
        """Get options chain for a stock.

        Args:
            underlying_symbol: Underlying stock symbol

        Returns:
            List of option contracts
        """
        try:
            request = OptionChainRequest(underlying_symbol=underlying_symbol)
            chain = self.option_data_client.get_option_chain(request)

            options = []
            # Chain is a dictionary with option symbols as keys and OptionsSnapshot data as values
            for option_symbol, contract in chain.items():
                # Parse option symbol to extract details using shared parser
                parsed = parse_option_symbol(option_symbol, underlying_hint=underlying_symbol)
                option_type = parsed['option_type']
                strike_price = parsed['strike_price']
                exp_date = parsed['expiration_date']

                # Extract quote data
                quote = getattr(contract, 'latest_quote', None)
                trade = getattr(contract, 'latest_trade', None)
                greeks = getattr(contract, 'greeks', None)

                bid = float(quote.bid_price) if quote and hasattr(quote, 'bid_price') and quote.bid_price else 0.0
                ask = float(quote.ask_price) if quote and hasattr(quote, 'ask_price') and quote.ask_price else 0.0
                bid_size = int(quote.bid_size) if quote and hasattr(quote, 'bid_size') and quote.bid_size else 0
                ask_size = int(quote.ask_size) if quote and hasattr(quote, 'ask_size') and quote.ask_size else 0

                last_price = float(trade.price) if trade and hasattr(trade, 'price') and trade.price else 0.0
                volume = int(trade.size) if trade and hasattr(trade, 'size') and trade.size else 0

                # Extract Greeks
                delta = float(greeks.delta) if greeks and hasattr(greeks, 'delta') and greeks.delta else 0.0
                gamma = float(greeks.gamma) if greeks and hasattr(greeks, 'gamma') and greeks.gamma else 0.0
                theta = float(greeks.theta) if greeks and hasattr(greeks, 'theta') and greeks.theta else 0.0
                vega = float(greeks.vega) if greeks and hasattr(greeks, 'vega') and greeks.vega else 0.0

                implied_vol = float(contract.implied_volatility) if hasattr(contract, 'implied_volatility') and contract.implied_volatility else 0.0

                options.append({
                    'symbol': option_symbol,
                    'underlying_symbol': underlying_symbol,
                    'option_type': option_type,
                    'strike_price': strike_price,
                    'expiration_date': exp_date,
                    'bid': bid,
                    'ask': ask,
                    'bid_size': bid_size,
                    'ask_size': ask_size,
                    'last_price': last_price,
                    'volume': volume,
                    'open_interest': 0,  # Not available in snapshot data
                    'implied_volatility': implied_vol,
                    'delta': delta,
                    'gamma': gamma,
                    'theta': theta,
                    'vega': vega
                })

            return options

        except Exception as e:
            logger.error("Failed to get options chain",
                        event_category="error",
                        event_type="options_chain_error",
                        symbol=underlying_symbol,
                        error=str(e))
            raise
    
    # Trading Operations
    def place_option_order(self, symbol: str, qty: int, side: str, order_type: str = "limit", 
                          limit_price: Optional[float] = None) -> Dict[str, Any]:
        """Place an option order.
        
        Args:
            symbol: Option contract symbol
            qty: Quantity to trade
            side: 'buy' or 'sell'
            order_type: 'market' or 'limit'
            limit_price: Limit price for limit orders
            
        Returns:
            Order response
        """
        try:
            order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL

            # Generate a deterministic client_order_id so retried HTTP requests
            # don't create duplicate orders on Alpaca's side.
            effective_price = limit_price if limit_price is not None else 0.0
            client_order_id = _generate_client_order_id(symbol, qty, side.lower(), effective_price)

            if order_type.lower() == 'market':
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id
                )
            else:  # limit order
                if limit_price is None:
                    raise ValueError("Limit price required for limit orders")

                order_data = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    client_order_id=client_order_id
                )

            order = self.trading_client.submit_order(order_data)

            # Validate order response - handle potential None values
            order_id = str(order.id) if order and order.id else None
            order_status = str(order.status) if order and order.status else 'unknown'
            submitted_at = order.submitted_at.isoformat() if order and order.submitted_at else None

            if not order_id:
                logger.error("Order submitted but no order ID returned - treating as failure",
                            event_category="error",
                            event_type="order_missing_id",
                            symbol=symbol, qty=qty, side=side)
                return {
                    'success': False,
                    'error_type': 'missing_order_id',
                    'error': 'Order submitted but no order ID returned',
                    'symbol': symbol,
                    'qty': qty,
                    'side': side
                }

            logger.info("Option order placed",
                       event_category="trade",
                       event_type="order_placed",
                       symbol=symbol, qty=qty, side=side,
                       order_type=order_type, order_id=order_id,
                       client_order_id=client_order_id,
                       limit_price=limit_price)

            return {
                'success': True,
                'order_id': order_id,
                'client_order_id': client_order_id,
                'symbol': symbol,
                'qty': qty,
                'side': side,
                'limit_price': limit_price,
                'status': order_status,
                'submitted_at': submitted_at
            }
            
        except Exception as e:
            error_msg = str(e)

            # Categorize error for better handling
            error_type = "order_error"
            non_retryable = False
            if "insufficient" in error_msg.lower() or "buying power" in error_msg.lower():
                error_type = "insufficient_funds"
                non_retryable = True
            elif "not found" in error_msg.lower() or "invalid symbol" in error_msg.lower():
                error_type = "invalid_symbol"
                non_retryable = True
            elif "not eligible" in error_msg.lower() or "40310000" in error_msg:
                error_type = "account_not_eligible"
                non_retryable = True
            elif "market hours" in error_msg.lower() or "42210000" in error_msg:
                error_type = "outside_market_hours"
                non_retryable = True
            elif "rejected" in error_msg.lower():
                error_type = "order_rejected"
                non_retryable = True
            elif "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                error_type = "connection_error"

            logger.error("Failed to place option order",
                        event_category="error",
                        event_type="order_placement_error",
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        error=error_msg,
                        error_type=error_type)

            # Return structured error instead of raising for better handling upstream
            return {
                'success': False,
                'error_type': error_type,
                'error_message': error_msg,
                'non_retryable': non_retryable,
                'symbol': symbol,
                'qty': qty,
                'side': side
            }
    
    # OrderStatus VALUES (order.status.value) that live in Alpaca's "open" query
    # bucket. Everything else (filled, expired, canceled, rejected, replaced,
    # done_for_day, calculated, stopped) is in the "closed" bucket. Used to
    # route a specific-value filter to the correct GetOrdersRequest — asking
    # Alpaca for the wrong bucket is exactly the bug this method fixes: a
    # value-filter that never fetched the rows it was filtering.
    #
    # Only 'filled'/'expired' (CLOSED) and 'pending_new' (OPEN) are exercised by
    # callers today and are live-verified. The rest of the partition is
    # best-effort per Alpaca's status semantics (e.g. 'calculated' = "completed
    # for the day" → CLOSED); a misfiled non-exercised value would make
    # get_orders('<that value>') return [] silently, but none are called.
    _OPEN_STATUS_VALUES = frozenset({
        'new', 'accepted', 'accepted_for_bidding', 'partially_filled',
        'pending_new', 'pending_cancel', 'pending_replace', 'pending_review',
        'held', 'suspended',
    })

    @api_retry
    def get_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get order history, filtered by status.

        Args:
            status: One of the Alpaca query tokens ``'open'`` / ``'closed'``, a
                specific ``OrderStatus`` value (e.g. ``'filled'``,
                ``'pending_new'``), or ``None`` for all orders.

        Returns:
            List of order dicts.

        Note:
            ``status`` is NOT a value-only filter over Alpaca's default page.
            Alpaca's REST default is ``status=open``, so closed orders (filled,
            expired) are never returned unless we explicitly request the closed
            bucket. This method routes the request to the correct
            ``QueryOrderStatus`` bucket, then value-filters when the caller
            named a specific status value. ``'open'`` is a *query* token, never
            an ``OrderStatus`` value — filtering ``status.value == 'open'`` (the
            prior behaviour) matched nothing.
        """
        try:
            query_status = QueryOrderStatus.ALL
            value_filter: Optional[str] = None
            if status is not None:
                normalized = status.lower()
                if normalized == 'open':
                    query_status = QueryOrderStatus.OPEN
                elif normalized == 'closed':
                    query_status = QueryOrderStatus.CLOSED
                else:
                    # A specific OrderStatus value — fetch the bucket it lives in,
                    # then keep only exact matches.
                    query_status = (
                        QueryOrderStatus.OPEN
                        if normalized in self._OPEN_STATUS_VALUES
                        else QueryOrderStatus.CLOSED
                    )
                    value_filter = normalized

            # limit=500 raises Alpaca's default page size (50) to a safe bound
            # for the current callers. It is a HARD CAP, not pagination: the
            # OPEN bucket (duplicate guard, pending check) is always tiny, and
            # the only CLOSED-bucket caller (portfolio "last 10 filled") wants
            # the newest 10, which sit at the head of Alpaca's DESC order. A
            # future caller needing full closed history must paginate with
            # after=/until= — 500 will truncate it.
            orders = self.trading_client.get_orders(
                filter=GetOrdersRequest(status=query_status, limit=500)
            )

            order_list = []
            for order in orders:
                if value_filter is None or order.status.value.lower() == value_filter:
                    qty = int(order.qty)
                    filled_qty = int(order.filled_qty) if order.filled_qty else 0
                    is_partial_fill = filled_qty > 0 and filled_qty < qty

                    order_list.append({
                        'order_id': order.id,
                        'symbol': order.symbol,
                        'qty': qty,
                        'filled_qty': filled_qty,
                        'remaining_qty': qty - filled_qty,
                        'is_partial_fill': is_partial_fill,
                        'side': order.side.value,
                        'status': order.status.value,
                        'order_type': order.order_type.value,
                        'submitted_at': order.submitted_at,
                        'filled_at': order.filled_at,
                        'filled_avg_price': float(order.filled_avg_price) if order.filled_avg_price else None
                    })
            
            return order_list

        except Exception as e:
            logger.error("Failed to get orders",
                        event_category="error",
                        event_type="orders_error",
                        error=str(e))
            raise

    @api_retry
    def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        """Get order details by order ID.

        Args:
            order_id: The Alpaca order ID

        Returns:
            Dict with order details including status, fill info
        """
        try:
            order = self.trading_client.get_order_by_id(order_id)
            return {
                'order_id': str(order.id),
                'symbol': order.symbol,
                'status': order.status.value,
                'qty': int(order.qty),
                'filled_qty': int(order.filled_qty) if order.filled_qty else 0,
                'filled_avg_price': float(order.filled_avg_price) if order.filled_avg_price else None,
                'filled_at': order.filled_at.isoformat() if order.filled_at else None,
                'expired_at': order.expired_at.isoformat() if hasattr(order, 'expired_at') and order.expired_at else None,
                'canceled_at': order.canceled_at.isoformat() if hasattr(order, 'canceled_at') and order.canceled_at else None,
                'submitted_at': order.submitted_at.isoformat() if order.submitted_at else None
            }
        except Exception as e:
            logger.error("Failed to get order by ID",
                        event_category="error",
                        event_type="order_lookup_error",
                        order_id=order_id,
                        error=str(e))
            raise

    def get_account_activities(self, activity_types: str = 'OPASN,OPEXP',
                               after: str = None, page_size: int = 100,
                               page_token: str = None,
                               direction: str = 'desc') -> List[Dict[str, Any]]:
        """Fetch account activities from Alpaca REST API.

        The Alpaca TradingClient SDK does not expose the activities endpoint,
        so this method calls the REST API directly using requests.

        Args:
            activity_types: Comma-separated activity types (OPASN, OPEXP, FILL, etc.)
            after: ISO date string to fetch activities after
            page_size: Number of results per page (max 100)
            page_token: Pagination cursor (last activity ID from prior page)
            direction: 'asc' or 'desc' (default 'desc')

        Returns:
            List of activity dicts with: activity_type, date, symbol, qty,
            net_amount, description, status, id, price (for FILL), order_id (for FILL)
        """
        try:
            # Determine base URL based on paper vs live trading
            if self.config.paper_trading:
                base_url = 'https://paper-api.alpaca.markets'
            else:
                base_url = 'https://api.alpaca.markets'

            url = f'{base_url}/v2/account/activities'

            headers = {
                'APCA-API-KEY-ID': self.config.alpaca_api_key,
                'APCA-API-SECRET-KEY': self.config.alpaca_secret_key,
                'Accept': 'application/json',
            }

            params = {
                'activity_types': activity_types,
                'page_size': min(page_size, 100),
                'direction': direction,
            }
            if after:
                params['after'] = after
            if page_token:
                params['page_token'] = page_token

            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()

            activities = response.json()

            logger.debug("Fetched account activities",
                        event_category="data",
                        event_type="activities_fetched",
                        activity_types=activity_types,
                        count=len(activities))

            return activities

        except requests.exceptions.HTTPError as e:
            logger.error("Failed to fetch account activities (HTTP error)",
                        event_category="error",
                        event_type="activities_http_error",
                        status_code=e.response.status_code if e.response is not None else None,
                        error=str(e))
            raise
        except Exception as e:
            logger.error("Failed to fetch account activities",
                        event_category="error",
                        event_type="activities_error",
                        error=str(e))
            raise

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if successful
        """
        try:
            self.trading_client.cancel_order_by_id(order_id)
            logger.info("Order cancelled",
                       event_category="trade",
                       event_type="order_cancelled",
                       order_id=order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order",
                        event_category="error",
                        event_type="cancel_order_error",
                        order_id=order_id,
                        error=str(e))
            return False