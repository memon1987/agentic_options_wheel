"""Tests for put selling module.

FC-068 deleted ``find_put_opportunity`` — the dead engine path's put producer,
whose only caller was ``WheelEngine._find_new_opportunities``. The 5
``test_find_put_opportunity_*`` tests went with it. The selection behaviour they
pinned (delta / DTE / premium filtering) lives in
``market_data.find_suitable_puts``, which the scanner calls and which keeps its
own tests in ``tests/test_market_data.py``; the wheel-state gating half was
fictional (``STATE_STORAGE_BUCKET`` has never been set) and is deleted, not
migrated.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta

from src.strategy.put_seller import PutSeller
from src.utils import clock
from src.utils.config import Config


class TestPutSellerPositionSizing:
    """Test PutSeller._calculate_position_size."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.max_position_size = 0.10

        self.put_seller = PutSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)

    def test_position_size_normal(self):
        """Test standard position sizing returns 1 contract."""
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 50000.0,
            'options_buying_power': 50000.0,
        }

        put_option = {
            'symbol': 'AAPL250117P00080000',
            'strike_price': 80.0,
            'mid_price': 2.00,
        }

        result = self.put_seller._calculate_position_size(put_option)

        assert result is not None
        assert result['contracts'] == 1
        assert result['capital_required'] == 8000.0  # 80 * 100
        assert result['max_profit'] == 200.0  # 2.00 * 100
        assert result['breakeven'] == 78.0  # 80 - 2

    def test_position_size_insufficient_buying_power(self):
        """Test returns None when buying power too low."""
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 100.0,
            'options_buying_power': 100.0,
        }

        put_option = {
            'symbol': 'AAPL250117P00170000',
            'strike_price': 170.0,
            'mid_price': 2.50,
        }

        result = self.put_seller._calculate_position_size(put_option)
        assert result is None

    def test_position_size_exceeds_allocation_limit(self):
        """Test returns None when strike is too high relative to portfolio."""
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 10000.0,  # Small portfolio
            'buying_power': 50000.0,
            'options_buying_power': 50000.0,
        }
        # max_position_size=0.10 -> max position value = 1000
        # strike 200 * 100 = 20000 >> 1000

        put_option = {
            'symbol': 'AAPL250117P00200000',
            'strike_price': 200.0,
            'mid_price': 3.00,
        }

        result = self.put_seller._calculate_position_size(put_option)
        assert result is None

    def test_position_size_with_override_buying_power(self):
        """Test position sizing with override buying power parameter."""
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 50000.0,
            'options_buying_power': 50000.0,
        }

        put_option = {
            'symbol': 'AAPL250117P00080000',
            'strike_price': 80.0,
            'mid_price': 2.00,
        }

        result = self.put_seller._calculate_position_size(put_option, override_buying_power=20000.0)
        assert result is not None
        assert result['contracts'] == 1

    def test_position_size_api_error(self):
        """Test returns None when account API fails."""
        self.mock_alpaca.get_account.side_effect = Exception("API Error")

        put_option = {
            'symbol': 'AAPL250117P00170000',
            'strike_price': 170.0,
            'mid_price': 2.50,
        }

        result = self.put_seller._calculate_position_size(put_option)
        assert result is None


class TestPutSellerCostBasisProtection:
    """Test cost basis and breakeven calculations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.max_position_size = 0.10

        self.put_seller = PutSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)

        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 50000.0,
            'options_buying_power': 50000.0,
        }

    def test_breakeven_calculation(self):
        """Test that breakeven is correctly calculated as strike - premium."""
        put_option = {
            'symbol': 'MSFT250117P00080000',
            'strike_price': 80.0,
            'mid_price': 3.50,
        }

        result = self.put_seller._calculate_position_size(put_option)

        assert result is not None
        assert result['breakeven'] == 76.5  # 80 - 3.50

    def test_portfolio_allocation_calculated(self):
        """Test portfolio allocation percentage is included."""
        put_option = {
            'symbol': 'MSFT250117P00050000',
            'strike_price': 50.0,
            'mid_price': 1.50,
        }

        result = self.put_seller._calculate_position_size(put_option)

        assert result is not None
        assert 'portfolio_allocation' in result
        # 5000 / 100000 = 0.05
        assert result['portfolio_allocation'] == 0.05


class TestPutSellerExecutePutSale:
    """Test PutSeller.execute_put_sale."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        # FC-072: the leg reads its spread bias from config now. A Mock(spec=)
        # would hand the pricer a Mock, which is a TypeError the moment the book
        # is usable — these fixtures quote a real book, so pin the shipped
        # default (the value that was hardcoded before FC-072).
        self.mock_config.put_limit_spread_fraction = 0.10

        self.put_seller = PutSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)

    def test_execute_put_sale_success(self):
        """Test successful put sale execution."""
        self.mock_alpaca.get_account.return_value = {
            'options_buying_power': 50000.0,
        }
        self.mock_alpaca.place_option_order.return_value = {
            'success': True,
            'order_id': 'order-123',
            'status': 'new',
        }

        opportunity = {
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 2.50,
            'strike_price': 170.0,
            'bid': 2.45,
            'ask': 2.55,
            'dte': 7,
        }

        result = self.put_seller.execute_put_sale(opportunity)

        assert result['success'] is True
        assert result['order_id'] == 'order-123'
        assert result['strategy'] == 'sell_put'

    def test_execute_put_sale_insufficient_buying_power(self):
        """Test rejection when buying power is insufficient."""
        self.mock_alpaca.get_account.return_value = {
            'options_buying_power': 1000.0,  # Not enough
        }

        opportunity = {
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 2.50,
            'strike_price': 170.0,
            'bid': 2.45,
            'ask': 2.55,
        }

        result = self.put_seller.execute_put_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'insufficient_buying_power'

    def test_execute_put_sale_skip_buying_power_check(self):
        """Test execution with buying power check skipped."""
        self.mock_alpaca.place_option_order.return_value = {
            'success': True,
            'order_id': 'order-456',
        }

        opportunity = {
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 2.50,
            'strike_price': 170.0,
            'bid': 2.45,
            'ask': 2.55,
        }

        result = self.put_seller.execute_put_sale(opportunity, skip_buying_power_check=True)

        assert result['success'] is True
        # get_account should not be called when skipping
        self.mock_alpaca.get_account.assert_not_called()

    def test_execute_put_sale_order_failure(self):
        """Test handling of order placement failure."""
        self.mock_alpaca.get_account.return_value = {
            'options_buying_power': 50000.0,
        }
        self.mock_alpaca.place_option_order.return_value = {
            'success': False,
            'error_type': 'order_rejected',
            'error_message': 'Insufficient margin',
        }

        opportunity = {
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 2.50,
            'strike_price': 170.0,
            'bid': 2.45,
            'ask': 2.55,
        }

        result = self.put_seller.execute_put_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'order_rejected'

    def test_execute_put_sale_exception(self):
        """Test handling of unexpected exception during execution."""
        self.mock_alpaca.get_account.side_effect = Exception("Network error")

        opportunity = {
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 2.50,
            'strike_price': 170.0,
        }

        result = self.put_seller.execute_put_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'buying_power_check_failed'


class TestPutSellerEarlyClose:
    """Test PutSeller.should_close_put_early."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.use_put_stop_loss = False
        self.mock_config.use_dynamic_profit_target = False
        self.mock_config.profit_taking_static_target = 0.50

        self.put_seller = PutSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)

    def test_should_close_at_profit_target(self):
        """Test closing when profit target reached."""
        position = {
            'symbol': 'AAPL250117P00170000',
            'unrealized_pl': 120.0,
            'market_value': -200.0,  # abs = 200, 120/200 = 0.60 > 0.50
        }

        result = self.put_seller.should_close_put_early(position)
        assert result is True

    def test_should_not_close_below_profit_target(self):
        """Test not closing when below profit target."""
        position = {
            'symbol': 'AAPL250117P00170000',
            'unrealized_pl': 50.0,
            'market_value': -200.0,  # abs = 200, 50/200 = 0.25 < 0.50
        }

        result = self.put_seller.should_close_put_early(position)
        assert result is False

    def test_should_not_close_losing_position_no_stop_loss(self):
        """Test not closing losing position when stop loss disabled."""
        position = {
            'symbol': 'AAPL250117P00170000',
            'unrealized_pl': -100.0,
            'market_value': -200.0,
        }

        result = self.put_seller.should_close_put_early(position)
        assert result is False


class TestPutLimitPricingFC072:
    """FC-072 rev 2: the put leg gets the same re-quote, and keeps its price.

    The pricing change in FC-072 is the CALL leg's. The put leg is here because
    of the Symmetry Principle: the execute-time re-quote is the fix, and a fix
    on one leg is applied to both unless the difference is justified. The
    15-minute :00-scan / :15-run gap is identical on this leg.

    What must NOT change is the price on a normal book. `mid + 0.10 x spread`
    was hardcoded and is now `strategy.put_limit_spread_fraction`, defaulted to
    the same 0.10, and the first family below pins that against 20 real books
    from the journal. The only intended behaviour change on this leg is tick
    snapping at/above $3.00 — this leg averages $1.44, so it is rare.
    """

    LIVE = {'bid': 1.40, 'ask': 1.48}

    def _seller(self, spread_fraction=0.10, live_quote='default'):
        self.mock_alpaca = Mock()
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 500000.0,
            'options_buying_power': 500000.0,
        }
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-fc072-put', 'status': 'new',
        }
        if live_quote == 'default':
            live_quote = dict(self.LIVE)
        if isinstance(live_quote, Exception):
            self.mock_alpaca.get_option_quote.side_effect = live_quote
        else:
            self.mock_alpaca.get_option_quote.return_value = live_quote
        config = Mock(spec=Config)
        config.put_limit_spread_fraction = spread_fraction
        return PutSeller(self.mock_alpaca, Mock(), config)

    @staticmethod
    def _opportunity(**kw):
        """The shape OptionsScanner._create_put_opportunity emits."""
        opp = {
            'type': 'put',
            'symbol': 'AAPL',
            'option_symbol': 'AAPL260821P00290000',
            'strike_price': 290.0,
            'premium': 1.30,
            'bid': 1.20,
            'ask': 1.40,
            'dte': 7,
            'contracts': 1,
            'scan_timestamp': (clock.now() - timedelta(seconds=900)).isoformat(),
        }
        opp.update(kw)
        return opp

    def _limit_of(self, opportunity, **kw):
        seller = self._seller(**kw)
        result = seller.execute_put_sale(opportunity)
        assert result['success'] is True, result
        kwargs = self.mock_alpaca.place_option_order.call_args.kwargs
        assert result['limit_price'] == kwargs['limit_price']
        return kwargs['limit_price']

    def _executing_event(self, opportunity, **kw):
        seller = self._seller(**kw)
        with patch('src.strategy.put_seller.logger') as mock_logger:
            result = seller.execute_put_sale(opportunity)
        assert result['success'] is True
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get('event_type') == 'put_sale_executing']
        assert len(events) == 1
        return events[0]

    # --- Test 5: the historical formula, byte for byte --------------------

    # Real put books, all below $3.00, where the pre-FC-072 price is
    # `round(mid + 0.10 * (ask - bid), 2)` computed on the SAME book. Expected
    # values are the literals that formula produced, written out rather than
    # recomputed, so a change in the implementation cannot move the target.
    HISTORICAL_BOOKS = [
        (0.50, 0.55, 0.53), (0.60, 0.65, 0.63), (0.70, 0.75, 0.73),
        (0.80, 0.90, 0.86), (0.95, 1.05, 1.01), (1.00, 1.10, 1.06),
        (1.10, 1.20, 1.16), (1.15, 1.25, 1.21), (1.20, 1.40, 1.32),
        (1.25, 1.35, 1.31), (1.30, 1.40, 1.36), (1.35, 1.45, 1.41),
        (1.40, 1.48, 1.45), (1.45, 1.55, 1.51), (1.50, 1.60, 1.56),
        (1.60, 1.70, 1.66), (1.75, 1.85, 1.81), (2.00, 2.10, 2.06),
        (2.45, 2.55, 2.51), (2.90, 2.98, 2.95),
    ]

    @pytest.mark.parametrize("bid,ask,expected", HISTORICAL_BOOKS)
    def test_a_normal_book_below_three_dollars_prices_as_it_always_has(
            self, bid, ask, expected):
        """The no-regression assertion for the whole put leg."""
        limit = self._limit_of(self._opportunity(),
                               live_quote={'bid': bid, 'ask': ask})
        assert limit == expected

    def test_the_put_bias_still_sits_above_mid(self):
        """This leg is the aggressive one; that asymmetry is deliberate and
        survives FC-072."""
        for bid, ask, expected in self.HISTORICAL_BOOKS:
            assert expected > (bid + ask) / 2 or ask == bid

    # --- The re-quote, same as the call leg -------------------------------

    def test_the_live_quote_prices_the_order_not_the_scan_time_one(self):
        """Scan book 1.20/1.40 (mid 1.30), live book 1.40/1.48 (mid 1.44).
        Pre-rev-2: 1.32. Now 1.45, off the current book."""
        assert self._limit_of(self._opportunity()) == 1.45

    def test_an_empty_refresh_falls_back_to_the_scan_time_book(self):
        """Scan book 1.20/1.40: 1.30 + 0.10 x 0.20 = 1.32, exactly as before."""
        assert self._limit_of(self._opportunity(), live_quote={}) == 1.32

    def test_a_raising_refresh_still_places_the_order(self):
        limit = self._limit_of(self._opportunity(),
                               live_quote=RuntimeError("alpaca down"))
        assert limit == 1.32
        self.mock_alpaca.place_option_order.assert_called_once()

    def test_the_event_reports_source_and_age(self):
        live = self._executing_event(self._opportunity())
        assert live['quote_source'] == 'live'
        assert live['quote_age_s'] == 0.0

        blob = self._executing_event(self._opportunity(), live_quote={})
        assert blob['quote_source'] == 'blob'
        assert 890 <= blob['quote_age_s'] <= 910

    # --- Test 3: the book predicate, both new cases ------------------------

    def test_a_locked_book_prices_at_the_bid(self):
        """NEW. Pre-FC-072 `if bid and ask` accepted this and priced at bid + 0
        anyway — but rev 1 of this FC made it fall back and price 5% THROUGH a
        quoting market. It prices at the bid."""
        assert self._limit_of(self._opportunity(),
                              live_quote={'bid': 1.30, 'ask': 1.30}) == 1.30

    def test_a_wide_book_takes_the_normal_formula(self):
        """The real AMZN 2026-05-27 book: 0.68 / 1.14 -> 0.91 + 0.046 = 0.956
        -> 0.96. Rev 1's spread cap priced this 0.86 and was rejected for
        exactly this reason: it conceded most where the mid was least
        informative."""
        assert self._limit_of(self._opportunity(),
                              live_quote={'bid': 0.68, 'ask': 1.14}) == 0.96

    def test_a_crossed_book_takes_the_premium_fallback(self):
        """NEW. Pre-FC-072 `ask - bid` was NEGATIVE here and the +10% bias
        inverted, pricing the put BELOW mid — on the leg that was supposed to
        be the disciplined one."""
        limit = self._limit_of(self._opportunity(bid=1.40, ask=1.30),
                               live_quote={'bid': 1.50, 'ask': 1.40})
        assert limit == 1.24  # round(1.30 * 0.95, 2)

    def test_a_one_sided_book_takes_the_premium_fallback(self):
        assert self._limit_of(self._opportunity(bid=0, ask=0),
                              live_quote={}) == 1.24

    # --- Test 4: tick snapping on this leg too -----------------------------

    def test_a_limit_at_or_above_three_dollars_is_snapped_up(self):
        """Rare on this leg ($1.44 average) but the broker rule is the same."""
        limit = self._limit_of(self._opportunity(),
                               live_quote={'bid': 3.14, 'ask': 3.34})
        assert limit == 3.30  # mid 3.24 + 0.10 x 0.20 = 3.26 -> 3.30

    def test_a_penny_program_underlying_is_exempt(self):
        limit = self._limit_of(
            self._opportunity(symbol='SPY', option_symbol='SPY260821P00600000',
                              strike_price=600.0),
            live_quote={'bid': 3.14, 'ask': 3.34})
        assert limit == 3.26

    # --- Test 7: the log contract ------------------------------------------

    def test_the_executing_event_carries_every_field_the_readout_needs(self):
        event = self._executing_event(self._opportunity())
        assert event['bid'] == 1.40
        assert event['ask'] == 1.48
        assert event['mid'] == 1.44
        assert event['spread'] == pytest.approx(0.08)
        assert event['spread_fraction'] == 0.10
        assert event['limit_price'] == 1.45
        assert event['quote_source'] == 'live'
        assert event['quote_age_s'] == 0.0
        assert event['tick_snapped'] is False

    def test_the_fallback_reports_no_mid_and_no_fraction(self):
        event = self._executing_event(self._opportunity(bid=0, ask=0),
                                      live_quote={})
        assert event['mid'] is None
        assert event['spread_fraction'] is None

    # --- Ordering guardrails ------------------------------------------------

    def test_the_buying_power_gate_still_runs_before_any_quote_refresh(self):
        """FC-072 must not have moved a broker call ahead of the collateral
        check."""
        seller = self._seller()
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 100.0,
            'options_buying_power': 100.0,
        }
        result = seller.execute_put_sale(self._opportunity())

        assert result['success'] is False
        assert result['error'] == 'insufficient_buying_power'
        self.mock_alpaca.get_option_quote.assert_not_called()
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_a_misrouted_call_is_rejected_before_any_quote_refresh(self):
        seller = self._seller()
        result = seller.execute_put_sale(
            self._opportunity(option_symbol='AAPL260821C00310000'))
        assert result['success'] is False
        assert result['error_type'] == 'wrong_seller'
        self.mock_alpaca.get_option_quote.assert_not_called()
