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
from datetime import datetime

from src.strategy.put_seller import PutSeller
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


class TestPutLimitPricingSanityCapFC072:
    """FC-072: the put leg keeps its formula and gains the spread sanity cap.

    The pricing change in FC-072 is the CALL leg's. The put leg is touched only
    because the Symmetry Principle says a fix on one leg is applied to both
    unless the difference is justified: the call leg gained a cap that rejects
    a stale or crossed book, and the put leg had none — it would price
    ``mid + 10% of spread`` off any two numbers the chain handed it, including
    a crossed one, where the "spread" is negative and the bias inverts.

    ``mid + 10% of spread`` on a normal quote is deliberately unchanged, and
    the first test pins that with a fixture value so a future refactor of the
    shared helper cannot quietly re-price the put leg.
    """

    def _seller(self):
        self.mock_alpaca = Mock()
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 50000.0,
            'options_buying_power': 50000.0,
        }
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-fc072-put', 'status': 'new',
        }
        return PutSeller(self.mock_alpaca, Mock(), Mock(spec=Config))

    @staticmethod
    def _opportunity(**kw):
        """The shape OptionsScanner._create_put_opportunity emits.

        Mid is $1.44 — the measured average put premium, which is exactly why
        the missing tick question never surfaced on this leg (it rarely crosses
        $3.00).
        """
        opp = {
            'type': 'put',
            'symbol': 'AAPL',
            'option_symbol': 'AAPL260821P00290000',
            'strike_price': 290.0,
            'premium': 1.44,
            'bid': 1.40,
            'ask': 1.50,
            'dte': 7,
            'contracts': 1,
        }
        opp.update(kw)
        return opp

    def _limit_of(self, opportunity):
        seller = self._seller()
        result = seller.execute_put_sale(opportunity)
        assert result['success'] is True
        kwargs = self.mock_alpaca.place_option_order.call_args.kwargs
        assert result['limit_price'] == kwargs['limit_price']
        return kwargs['limit_price']

    def test_a_normal_quote_prices_exactly_as_it_did_before_fc072(self):
        """Pinned fixture value: 1.44 + 0.10 * (1.50 - 1.40) = 1.45.

        This is the no-regression assertion for the whole put leg. If the
        shared helper ever changes the put formula, this fails.
        """
        assert self._limit_of(self._opportunity()) == 1.45

    def test_the_put_bias_is_still_above_mid(self):
        """The put leg is the aggressive one; that asymmetry is deliberate and
        survives FC-072."""
        limit = self._limit_of(self._opportunity())
        assert limit > 1.44

    def test_a_wider_normal_spread_still_prices_off_the_formula(self):
        """1.44 + 0.10 * (1.80 - 1.10) = 1.51. spread/mid = 0.486 <= 0.5, so
        this is admitted, not capped — the cap must not eat ordinary width."""
        assert self._limit_of(self._opportunity(bid=1.10, ask=1.80)) == 1.51

    def test_a_stale_quote_wider_than_half_of_mid_falls_back(self):
        """NEW in FC-072. spread/mid = 1.00/1.44 = 0.69 > 0.5.

        Pre-fix this priced at 1.44 + 0.10 = 1.54 off a book that is not a
        market. Now: round(1.44 * 0.95, 2) = 1.37.
        """
        assert self._limit_of(self._opportunity(bid=1.00, ask=2.00)) == 1.37

    def test_a_crossed_quote_falls_back(self):
        """NEW in FC-072. ask < bid meant a NEGATIVE spread, and
        `premium + spread * 0.10` priced the put BELOW mid — the exact
        donation this FC exists to stop, on the leg that was supposed to be
        the disciplined one."""
        assert self._limit_of(self._opportunity(bid=1.50, ask=1.40)) == 1.37

    def test_a_missing_quote_still_falls_back(self):
        """Unchanged behavior, pinned so the rewrite cannot lose it."""
        opp = self._opportunity()
        del opp['bid']
        del opp['ask']
        assert self._limit_of(opp) == 1.37

    def test_the_executing_event_carries_the_applied_fraction(self):
        """Symmetry: the call leg's new `spread_fraction` field exists on this
        leg too, so one BigQuery query can compare the two legs' pricing."""
        seller = self._seller()
        with patch('src.strategy.put_seller.logger') as mock_logger:
            result = seller.execute_put_sale(self._opportunity())
        assert result['success'] is True
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get('event_type') == 'put_sale_executing']
        assert len(events) == 1
        assert events[0]['spread_fraction'] == 0.10
        assert events[0]['spread'] == 0.10
        assert events[0]['limit_price'] == 1.45

    def test_a_capped_quote_reports_no_applied_fraction(self):
        seller = self._seller()
        with patch('src.strategy.put_seller.logger') as mock_logger:
            seller.execute_put_sale(self._opportunity(bid=1.00, ask=2.00))
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get('event_type') == 'put_sale_executing']
        assert events[0]['spread_fraction'] is None
        assert events[0]['spread'] == 1.00

    def test_the_buying_power_gate_still_runs_before_pricing(self):
        """Guardrail: FC-072 must not have moved order submission ahead of the
        collateral check."""
        seller = self._seller()
        self.mock_alpaca.get_account.return_value = {
            'portfolio_value': 100000.0,
            'buying_power': 100.0,
            'options_buying_power': 100.0,
        }
        result = seller.execute_put_sale(self._opportunity())
        assert result['success'] is False
        assert result['error'] == 'insufficient_buying_power'
        self.mock_alpaca.place_option_order.assert_not_called()
