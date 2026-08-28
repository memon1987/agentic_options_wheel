"""Tests for call selling module.

FC-068 deleted this class's opportunity-*generation* half, and with it two test
classes:

* ``TestCallSellerEvaluateOpportunity`` (6) — the deleted
  ``evaluate_covered_call_opportunity``. Every row it covered has a live-path
  equivalent in ``tests/test_options_scanner.py``:
  ``test_insufficient_shares`` → ``test_scan_call_skips_insufficient_shares``;
  ``test_strike_vs_cost_basis_filtering`` →
  ``TestCallScanFloorIsTheBrokerBasisCrossCheckedAgainstBigQuery``;
  ``test_no_suitable_calls_found`` →
  ``test_the_nothing_happened_case_produces_a_labelled_row``;
  ``test_api_error_returns_none`` → ``test_a_chain_exception_emits_no_opportunity``
  (added by FC-068 — the existing P4 test pinned only that the decision rows
  survive); ``test_find_suitable_covered_calls`` / ``test_multiple_round_lots``
  → ``test_scan_call_opportunities_with_stock_positions`` +
  ``test_multiple_round_lots_size_to_every_lot`` (added by FC-068 — the scanner
  test only ever used a single round lot).
* ``TestCallSellerCostBasisFloorFC065`` (14 collected) — 6 drawdown-pause tests
  **deleted, not migrated** (FC-065 OQ-3: the pause is not ported to the live
  path), and 8 floor tests deleted as duplicates of named scanner-path tests
  (``test_the_broker_basis_filters_the_chain_and_rides_the_opportunity``,
  ``test_a_divergent_cross_check_skips_the_symbol``,
  ``test_an_agreeing_cross_check_is_logged_with_its_verdict``,
  ``test_an_unavailable_cross_check_is_logged_but_keeps_the_floor``,
  ``test_unresolved_cost_basis_emits_nothing`` — parametrization parity checked
  and a missing garbage-string case added — and
  ``test_a_positive_cost_basis_does_not_rescue_a_missing_avg_entry_price``).

The below-basis protection this file still pins is the one that survives on the
live path: ``TestExecuteTimeCostBasisFloorFC050``.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from src.strategy.call_seller import CallSeller
from src.utils.config import Config


class TestCallSellerExecuteSale:
    """Test CallSeller.execute_call_sale."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)

        self.call_seller = CallSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)
        # Unit isolation: the suite's autouse hermeticity fixture stubs the
        # resolver's single BigQuery chokepoint
        # (`CostBasisResolver._lookup_assignment_basis`) to "no comparison
        # available", so these tests resolve against the position's own
        # `avg_entry_price` and never reach live production history. With
        # ambient GCP credentials (Cloud Build, dev machines with ADC) an
        # unstubbed lookup would read real AAPL assignments and the
        # cross-check would fail these fixtures closed as divergent (FC-065).

    def test_execute_call_sale_success(self):
        """Test successful call sale execution."""
        self.mock_alpaca.place_option_order.return_value = {
            'success': True,
            'order_id': 'order-789',
            'status': 'new',
        }

        opportunity = {
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 1.80,
            'strike_price': 185.0,
            'stock_cost_basis': 16000.0,
            'shares_covered': 100,
            'dte': 7,
        }

        result = self.call_seller.execute_call_sale(opportunity)

        assert result['success'] is True
        assert result['order_id'] == 'order-789'
        assert result['strategy'] == 'sell_call'

    def test_execute_call_sale_blocks_below_cost_basis(self):
        """Test that selling calls below cost basis is blocked."""
        opportunity = {
            'option_symbol': 'AAPL250117C00150000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 3.00,
            'strike_price': 150.0,  # Below cost basis per share
            'stock_cost_basis': 16000.0,  # $160/share for 100 shares
            'shares_covered': 100,
        }

        result = self.call_seller.execute_call_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'strike_below_cost_basis'
        # Order should NOT have been placed
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_execute_call_sale_allows_above_cost_basis(self):
        """Test that selling calls above cost basis proceeds."""
        self.mock_alpaca.place_option_order.return_value = {
            'success': True,
            'order_id': 'order-abc',
        }

        opportunity = {
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 1.80,
            'strike_price': 185.0,  # Above cost basis per share of $160
            'stock_cost_basis': 16000.0,
            'shares_covered': 100,
            'dte': 7,
        }

        result = self.call_seller.execute_call_sale(opportunity)
        assert result['success'] is True

    def test_execute_call_sale_order_failure(self):
        """Test handling of order placement failure."""
        self.mock_alpaca.place_option_order.return_value = {
            'success': False,
            'error_type': 'order_rejected',
            'error_message': 'Market closed',
        }

        opportunity = {
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 1.80,
            'strike_price': 185.0,
            'stock_cost_basis': 16000.0,
            'shares_covered': 100,
        }

        result = self.call_seller.execute_call_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'order_rejected'

    def test_execute_call_sale_exception(self):
        """Test handling of unexpected exception."""
        self.mock_alpaca.place_option_order.side_effect = Exception("Connection lost")

        opportunity = {
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 1.80,
            'strike_price': 185.0,
            'stock_cost_basis': 16000.0,
            'shares_covered': 100,
        }

        result = self.call_seller.execute_call_sale(opportunity)

        assert result['success'] is False
        assert result['error'] == 'execution_exception'


class TestCallSellerEarlyClose:
    """Test CallSeller.should_close_call_early."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.use_call_stop_loss = True
        self.mock_config.call_stop_loss_percent = 0.50
        self.mock_config.stop_loss_multiplier = 1.5
        self.mock_config.use_dynamic_profit_target = False
        self.mock_config.profit_taking_static_target = 0.50

        self.call_seller = CallSeller(self.mock_alpaca, self.mock_market_data, self.mock_config)
        # Unit isolation: the suite's autouse hermeticity fixture stubs the
        # resolver's single BigQuery chokepoint
        # (`CostBasisResolver._lookup_assignment_basis`) to "no comparison
        # available", so these tests resolve against the position's own
        # `avg_entry_price` and never reach live production history. With
        # ambient GCP credentials (Cloud Build, dev machines with ADC) an
        # unstubbed lookup would read real AAPL assignments and the
        # cross-check would fail these fixtures closed as divergent (FC-065).

    def test_should_close_at_profit_target(self):
        """Test closing when profit target is reached."""
        position = {
            'symbol': 'AAPL250117C00185000',
            'unrealized_pl': 120.0,
            'market_value': -200.0,  # 120/200 = 0.60 > 0.50
        }

        result = self.call_seller.should_close_call_early(position)
        assert result is True

    def test_should_not_close_below_profit_target(self):
        """Test not closing below profit target."""
        position = {
            'symbol': 'AAPL250117C00185000',
            'unrealized_pl': 50.0,
            'market_value': -200.0,  # 50/200 = 0.25 < 0.50
        }

        result = self.call_seller.should_close_call_early(position)
        assert result is False

    def test_stop_loss_triggered(self):
        """Test stop loss triggers for large losses."""
        # stop_loss_threshold = 0.50 * 1.5 = 0.75
        position = {
            'symbol': 'AAPL250117C00185000',
            'unrealized_pl': -200.0,
            'market_value': -200.0,  # loss_pct = 200/200 = 1.0 > 0.75
        }

        result = self.call_seller.should_close_call_early(position)
        assert result is True

    def test_delta_stop_loss_triggered(self):
        """Test delta-based stop loss when option goes ITM."""
        position = {
            'symbol': 'AAPL250117C00185000',
            'unrealized_pl': -50.0,
            'market_value': -300.0,  # loss_pct = 50/300 = 0.167 < 0.75
        }
        current_option_data = {'delta': 0.7}  # > 0.5, likely ITM

        result = self.call_seller.should_close_call_early(position, current_option_data)
        assert result is True

    def test_exception_returns_false(self):
        """Test that exceptions are handled gracefully."""
        position = {
            'symbol': 'AAPL250117C00185000',
            # Missing required keys to trigger exception
        }

        result = self.call_seller.should_close_call_early(position)
        assert result is False


class TestWrongSellerGuard:
    """FC-048: call_seller must refuse a PUT routed to it.

    put_seller has had the mirror of this guard since FC-021; the call side did
    not. That asymmetry is the same one that let the covered-call misroute hide
    — the put side rejected loudly, the call side had nothing to reject with.
    """

    def _seller(self):
        from src.strategy.call_seller import CallSeller
        s = CallSeller.__new__(CallSeller)      # builder-only; no __init__ wiring
        s.config = Mock(spec=Config)
        s.alpaca = Mock()
        s.market_data = Mock()
        return s

    def test_a_put_routed_to_the_call_seller_is_rejected(self):
        result = self._seller().execute_call_sale({
            'option_symbol': 'AAPL250117P00170000',   # a PUT
            'symbol': 'AAPL', 'strike_price': 170.0, 'contracts': 1,
        })

        assert result['success'] is False
        assert result['error_type'] == 'wrong_seller'
        assert result['non_retryable'] is True
        # And it never reached the broker.
        self.__dict__.setdefault('_', None)

    def test_a_call_is_not_rejected_by_the_guard(self):
        """The guard must not block legitimate calls."""
        seller = self._seller()
        result = seller.execute_call_sale({
            'option_symbol': 'AAPL250117C00190000',   # a CALL
            'symbol': 'AAPL', 'strike_price': 190.0, 'contracts': 1,
        })
        # It may fail later for other reasons, but never as wrong_seller.
        if result is not None:
            assert result.get('error_type') != 'wrong_seller'


class TestExecuteTimeCostBasisFloorFC050:
    """FC-050: the execute-time below-basis floor on the path production runs.

    ``execute_call_sale`` used to read only ``stock_cost_basis`` /
    ``shares_covered`` — keys that only the wheel-engine path sets. Every
    opportunity production actually executes comes from the scanner and carries
    ``cost_basis_per_share`` / ``max_contracts`` instead, so the guard silently
    never ran. The below-basis tests here fail against pre-FC-050 code.
    """

    def _seller(self):
        self.mock_alpaca = Mock()
        return CallSeller(self.mock_alpaca, Mock(), Mock(spec=Config))

    def _scanner_opportunity(self, strike, cost_basis_per_share=303.50):
        """The shape OptionsScanner._create_call_opportunity emits."""
        return {
            'type': 'call',
            'symbol': 'AAPL',
            'option_symbol': f'AAPL260821C{int(strike * 1000):08d}',
            'strike_price': strike,
            'premium': 1.80,
            'dte': 7,
            'shares_owned': 100,
            'max_contracts': 1,
            'contracts': 1,
            'cost_basis_per_share': cost_basis_per_share,
        }

    def test_a_scanner_opportunity_below_basis_is_blocked(self):
        """THE regression: $290 strike on shares that cost $303.50."""
        seller = self._seller()

        result = seller.execute_call_sale(self._scanner_opportunity(290.0))

        assert result['success'] is False
        assert result['error'] == 'strike_below_cost_basis'
        assert '303.50' in result['message']
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_a_scanner_opportunity_above_basis_still_trades(self):
        """The restored guard must not cost us the calls we may write."""
        seller = self._seller()
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-fc050',
        }

        result = seller.execute_call_sale(self._scanner_opportunity(310.0))

        assert result['success'] is True
        self.mock_alpaca.place_option_order.assert_called_once()

    def test_a_strike_exactly_at_basis_is_allowed(self):
        """Called away at cost is flat on the shares plus the premium — the
        guard blocks losses, not break-even."""
        seller = self._seller()
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-flat',
        }

        result = seller.execute_call_sale(self._scanner_opportunity(303.50))

        assert result['success'] is True

    def test_a_multi_contract_wheel_engine_opportunity_divides_by_contracts(self):
        """FC-038 made multi-contract covered calls reachable. A total basis of
        $32,000 over 2 contracts is $160/share — dividing by a bare 100 would
        read $320 and block a perfectly good $170 strike."""
        seller = self._seller()
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-multi',
        }

        result = seller.execute_call_sale({
            'option_symbol': 'AAPL250117C00170000',
            'symbol': 'AAPL',
            'strike_price': 170.0,
            'premium': 1.80,
            'contracts': 2,
            'stock_cost_basis': 32000.0,   # 200 shares at $160
            'dte': 7,
        })

        assert result['success'] is True

    def test_a_multi_contract_opportunity_below_the_true_basis_is_blocked(self):
        seller = self._seller()

        result = seller.execute_call_sale({
            'option_symbol': 'AAPL250117C00150000',
            'symbol': 'AAPL',
            'strike_price': 150.0,
            'premium': 3.00,
            'contracts': 2,
            'stock_cost_basis': 32000.0,   # $160/share, not $320
            'dte': 7,
        })

        assert result['success'] is False
        assert result['error'] == 'strike_below_cost_basis'
        assert '160.00' in result['message']
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_an_opportunity_with_no_resolvable_floor_is_blocked(self):
        """Fail closed: post-FC-050 both producers carry a floor, so none means
        a malformed opportunity — never 'skip the check and trade'."""
        seller = self._seller()

        result = seller.execute_call_sale({
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'strike_price': 185.0,
            'premium': 1.80,
            'contracts': 1,
        })

        assert result['success'] is False
        assert result['error_type'] == 'cost_basis_floor_unresolved_at_execution'
        assert result['non_retryable'] is False
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_a_zero_cost_basis_per_share_is_blocked_not_ignored(self):
        """Alpaca reports 0 for freshly assigned positions (FC-029)."""
        seller = self._seller()

        result = seller.execute_call_sale(
            self._scanner_opportunity(310.0, cost_basis_per_share=0.0))

        assert result['success'] is False
        assert result['error_type'] == 'cost_basis_floor_unresolved_at_execution'
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_a_misrouted_put_is_still_rejected_before_the_floor_check(self):
        """The FC-045/FC-048 guard stays ahead of the floor: a put must be
        rejected as wrong_seller, not as an unresolved floor."""
        seller = self._seller()

        result = seller.execute_call_sale({
            'option_symbol': 'AAPL250117P00170000',
            'symbol': 'AAPL',
            'strike_price': 170.0,
            'contracts': 1,
        })

        assert result['error_type'] == 'wrong_seller'
        assert result['non_retryable'] is True
        self.mock_alpaca.place_option_order.assert_not_called()


class TestCallSaleExecutedTelemetryFC065P4:
    """FC-065 Phase 4: `call_sale_executed` must carry real economics.

    The event read ``shares_covered`` / ``stock_cost_basis`` /
    ``total_return_if_called`` straight off the opportunity. Those are
    wheel-engine-only keys and production runs the *scanner* path, so every
    live covered-call fill logged **0 / 0 / 0** — the fourth instance of the
    untyped-shape root cause (FC-050 fixed the floor's copy of it) and the
    reason FC-029's production-validation item could not be satisfied.

    These tests fail against pre-Phase-4 code: the assertions are on the very
    fields that were zero.
    """

    def _seller(self, order_id='order-123'):
        self.mock_alpaca = Mock()
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': order_id, 'status': 'new',
        }
        return CallSeller(self.mock_alpaca, Mock(), Mock(spec=Config))

    @staticmethod
    def _scanner_opportunity(**kw):
        """The shape OptionsScanner._create_call_opportunity emits.

        AAPL at the live post-Phase-1 floor (303.50 = Alpaca avg_entry_price).
        """
        opp = {
            'type': 'call',
            'symbol': 'AAPL',
            'option_symbol': 'AAPL260821C00310000',
            'strike_price': 310.0,
            'premium': 1.80,
            'dte': 7,
            'shares_owned': 100,
            'max_contracts': 1,
            'contracts': 1,
            'cost_basis_per_share': 303.50,
        }
        opp.update(kw)
        return opp

    def _execute_and_capture(self, opportunity, seller=None):
        seller = seller or self._seller()
        events = []
        with patch('src.strategy.call_seller.log_trade_event',
                   side_effect=lambda logger, **kw: events.append(kw)):
            result = seller.execute_call_sale(opportunity)
        assert result['success'] is True
        executed = [e for e in events if e.get('event_type') == 'call_sale_executed']
        assert len(executed) == 1
        return executed[0]

    def test_shares_covered_is_not_zero_on_a_scanner_opportunity(self):
        event = self._execute_and_capture(self._scanner_opportunity())
        assert event['shares_covered'] == 100, (
            "pre-Phase-4 this read the wheel-engine-only `shares_covered` key "
            "and logged 0 on every production fill")

    def test_cost_basis_is_not_zero_on_a_scanner_opportunity(self):
        event = self._execute_and_capture(self._scanner_opportunity())
        assert event['stock_cost_basis'] == 30350.0    # 303.50 x 100 shares
        assert event['cost_basis_per_share'] == 303.50

    def test_total_return_if_called_is_not_zero_on_a_scanner_opportunity(self):
        event = self._execute_and_capture(self._scanner_opportunity())
        # premium 1.80 x 100 + (310.00 - 303.50) x 100 = 180 + 650
        assert event['total_return_if_called'] == pytest.approx(830.0)

    def test_multi_contract_scales_by_contracts_not_by_a_bare_100(self):
        event = self._execute_and_capture(
            self._scanner_opportunity(contracts=3, shares_owned=300,
                                      max_contracts=3))
        assert event['shares_covered'] == 300
        assert event['stock_cost_basis'] == 91050.0
        assert event['total_return_if_called'] == pytest.approx(2490.0)

    def test_a_partially_sized_write_reports_what_it_actually_sold(self):
        # 300 shares owned, only 1 contract selected. The scanner's own
        # `total_return_if_assigned` is max_contracts-scaled and would
        # overstate this by 3x, which is why the value is derived instead.
        event = self._execute_and_capture(
            self._scanner_opportunity(contracts=1, shares_owned=300,
                                      max_contracts=3,
                                      total_return_if_assigned=2490.0))
        assert event['shares_covered'] == 100
        assert event['total_return_if_called'] == pytest.approx(830.0)

    def test_the_wheel_engine_shape_still_works(self):
        # Symmetry: fixing the scanner shape must not break the other producer.
        event = self._execute_and_capture({
            'option_symbol': 'AAPL250117C00185000',
            'symbol': 'AAPL',
            'contracts': 1,
            'premium': 1.80,
            'strike_price': 185.0,
            'stock_cost_basis': 16000.0,
            'shares_covered': 100,
            'dte': 7,
        })
        assert event['shares_covered'] == 100
        assert event['stock_cost_basis'] == 16000.0
        assert event['total_return_if_called'] == pytest.approx(2680.0)

    def test_an_at_floor_write_reports_premium_only(self):
        # GOOGL's 370C case: at-floor is allowed and gives up no capital.
        event = self._execute_and_capture(
            self._scanner_opportunity(symbol='GOOGL',
                                      option_symbol='GOOGL260821C00370000',
                                      strike_price=368.34,
                                      cost_basis_per_share=368.34,
                                      premium=2.50))
        assert event['total_return_if_called'] == pytest.approx(250.0)


class TestSellerStateOrphanDeletedFC069:
    """FC-069 item 8, stage 1 — the orphaned seller state plumbing is gone.

    A census in the shape of ``test_cost_basis.py``'s resolver-instance
    census: these assertions fail the moment someone reintroduces the
    ``wheel_state`` half on a seller. The layer they pinned never ran in
    production — ``/run`` and ``/monitor`` (and the replay) construct both
    sellers with three positional arguments — so the regression to guard
    against is re-adding an optional state channel that would once again fire
    nowhere while implying it does.
    """

    def _call_seller(self):
        return CallSeller(Mock(), Mock(), Mock(spec=Config))

    def test_call_seller_has_no_wheel_state_attribute(self):
        assert not hasattr(self._call_seller(), 'wheel_state')

    def test_call_seller_takes_no_wheel_state_manager_keyword(self):
        with pytest.raises(TypeError):
            CallSeller(Mock(), Mock(), Mock(spec=Config), wheel_state_manager=Mock())

    def test_call_seller_no_longer_handles_assignments(self):
        # `handle_call_assignment` had zero callers; the live assignment
        # bookkeeping is `WheelStateManager.handle_call_assignment`, driven
        # from `wheel_engine.reconcile_positions` (stage 2's territory).
        assert not hasattr(self._call_seller(), 'handle_call_assignment')

    def test_neither_seller_carries_a_private_option_symbol_parser(self):
        # The canonical parser is `src.utils.option_symbols.parse_option_symbol`;
        # the private per-seller twins were dead (zero callers).
        from src.strategy.put_seller import PutSeller
        assert not hasattr(self._call_seller(), '_parse_option_symbol')
        assert not hasattr(PutSeller(Mock(), Mock(), Mock(spec=Config)),
                           '_parse_option_symbol')

    def test_the_dte_parser_both_sellers_actually_use_survives(self):
        # Mutation guard for the delete above: the monitor path's
        # `_parse_dte_from_option_symbol` is live on both sellers and must not
        # be swept up with its dead namesake.
        from src.strategy.put_seller import PutSeller
        assert self._call_seller()._parse_dte_from_option_symbol(
            'AAPL250117C00185000') >= 0
        assert PutSeller(Mock(), Mock(), Mock(spec=Config))._parse_dte_from_option_symbol(
            'AAPL250117P00185000') >= 0


class TestCallLimitPricingFC072:
    """FC-072: the covered-call sell-to-open limit is priced at mid, not 5% under.

    Pre-FC-072 ``execute_call_sale`` priced every write at
    ``round(premium * 0.95, 2)`` — unconditionally, quote or no quote. Measured
    over the trade journal the call leg's limits sat **5.0% below mid** while
    the put leg's sat **1.0% above** it; across 138 call fills the discount was
    worth up to ~$2,350 of forgone premium. These tests fail against pre-FC-072
    code: every one of them asserts a limit the old formula could not produce.

    The knob (``strategy.call_limit_spread_fraction``) defaults to 0.0 — price
    at mid, no more aggressive than the market's own midpoint. Biasing toward
    the ask like the put leg is a separate, separately-measured step (the call
    leg fills at 87% today, the put leg at 73%).
    """

    def _seller(self, spread_fraction=0.0):
        self.mock_alpaca = Mock()
        self.mock_alpaca.place_option_order.return_value = {
            'success': True, 'order_id': 'order-fc072', 'status': 'new',
        }
        config = Mock(spec=Config)
        config.call_limit_spread_fraction = spread_fraction
        return CallSeller(self.mock_alpaca, Mock(), config)

    @staticmethod
    def _opportunity(**kw):
        """The shape OptionsScanner._create_call_opportunity emits.

        ``premium`` is the chain mid (``mid_price``), and the scanner puts
        ``bid`` / ``ask`` on the call payload exactly as it does on the put one
        (options_scanner.py — ``'bid': call_option.get('bid', 0)``).

        Default mid is **$3.24**, the measured average call premium, and it is
        deliberately OFF the $0.05 tick: FC-072 verified against a year of
        closed Alpaca orders that off-tick option limits at or above $3.00 are
        accepted and filled (46 such sell-call limits, 38 filled, 0 rejected),
        so there is no tick snapping to hide behind here.
        """
        opp = {
            'type': 'call',
            'symbol': 'AAPL',
            'option_symbol': 'AAPL260821C00310000',
            'strike_price': 310.0,
            'premium': 3.24,
            'bid': 3.14,
            'ask': 3.34,
            'dte': 7,
            'shares_owned': 100,
            'max_contracts': 1,
            'contracts': 1,
            'cost_basis_per_share': 303.50,
        }
        opp.update(kw)
        return opp

    def _limit_of(self, opportunity, spread_fraction=0.0):
        seller = self._seller(spread_fraction)
        result = seller.execute_call_sale(opportunity)
        assert result['success'] is True
        kwargs = self.mock_alpaca.place_option_order.call_args.kwargs
        # The limit that reached the broker and the one reported back to the
        # engine must be the same number.
        assert result['limit_price'] == kwargs['limit_price']
        return kwargs['limit_price']

    # --- Test 1: the core regression -------------------------------------

    def test_a_usable_quote_prices_exactly_at_mid(self):
        """THE regression. Pre-fix this was 3.08 (3.24 * 0.95); it is now 3.24.

        Also pins that a $3.24 limit is submitted as $3.24 — not snapped up to
        $3.25 — because Alpaca accepts penny increments at every price level.
        """
        assert self._limit_of(self._opportunity()) == 3.24

    def test_the_five_percent_discount_is_gone_across_the_premium_range(self):
        """Mutation guard: any surviving `premium * 0.95` shows up here."""
        for mid, bid, ask in [(0.35, 0.30, 0.40),
                              (1.80, 1.75, 1.85),
                              (3.24, 3.14, 3.34),
                              (14.41, 14.20, 14.62)]:
            limit = self._limit_of(
                self._opportunity(premium=mid, bid=bid, ask=ask))
            assert limit == round(mid, 2), f"mid={mid} priced at {limit}"
            assert limit != round(mid * 0.95, 2)

    # --- Test 2: the knob is actually consumed ---------------------------

    def test_a_nonzero_spread_fraction_biases_the_limit_toward_the_ask(self):
        """f=0.10 -> mid + 10% of the spread, the put leg's bias.

        3.24 + 0.10 * (3.34 - 3.14) = 3.26.
        """
        assert self._limit_of(self._opportunity(), spread_fraction=0.10) == 3.26

    def test_the_full_fraction_prices_at_the_ask(self):
        """f=0.5 is the validated ceiling and lands exactly on the ask —
        which is why 0.5 is the ceiling. mid + 0.5*spread = 3.24 + 0.10."""
        assert self._limit_of(self._opportunity(), spread_fraction=0.50) == 3.34

    # --- Test 3: the fallback is untouched -------------------------------

    def test_a_missing_quote_falls_back_to_five_percent_under_mid(self):
        opp = self._opportunity()
        del opp['bid']
        del opp['ask']
        assert self._limit_of(opp) == 3.08  # round(3.24 * 0.95, 2)

    def test_a_zero_bid_falls_back(self):
        """The scanner defaults absent quotes to 0, so 0 is the real shape of
        'no bid' on a production opportunity — not None."""
        assert self._limit_of(self._opportunity(bid=0, ask=0)) == 3.08

    def test_a_none_quote_falls_back(self):
        assert self._limit_of(self._opportunity(bid=None, ask=None)) == 3.08

    def test_the_fallback_is_unchanged_even_with_a_nonzero_knob(self):
        """A degraded quote must not be priced off the knob: there is no third
        path, and f multiplies a spread that does not exist."""
        opp = self._opportunity()
        del opp['bid']
        del opp['ask']
        assert self._limit_of(opp, spread_fraction=0.10) == 3.08

    # --- Test 4: the spread sanity cap ------------------------------------

    def test_a_crossed_quote_falls_back(self):
        """ask <= bid is not a market. Pricing off its 'spread' would produce a
        limit BELOW mid on a positive fraction."""
        assert self._limit_of(self._opportunity(bid=3.34, ask=3.14)) == 3.08

    def test_a_locked_quote_falls_back(self):
        """ask == bid: zero spread, and a book that is not quoting a market."""
        assert self._limit_of(self._opportunity(bid=3.24, ask=3.24)) == 3.08

    def test_a_stale_quote_wider_than_half_of_mid_falls_back(self):
        """spread/mid = 2.00/3.24 = 0.617 > 0.5 -> the midpoint is noise."""
        assert self._limit_of(self._opportunity(bid=2.24, ask=4.24)) == 3.08

    def test_a_spread_exactly_at_the_cap_is_still_usable(self):
        """The cap is `>` not `>=`: spread/mid == 0.5 exactly is admitted, so
        the boundary cannot silently swallow a legitimate wide-but-real book.
        mid 3.00, bid 2.25, ask 3.75 -> spread 1.50, ratio 0.50."""
        assert self._limit_of(
            self._opportunity(premium=3.00, bid=2.25, ask=3.75)) == 3.00

    def test_the_cap_matters_because_the_fallback_is_cheaper(self):
        """Guard against a 'cap' that is a no-op: with f=0 a capped quote must
        price 5% under mid, not at mid."""
        capped = self._limit_of(self._opportunity(bid=2.24, ask=4.24))
        uncapped = self._limit_of(self._opportunity())
        assert capped < uncapped

    # --- Test 5: telemetry -------------------------------------------------

    def _executing_event(self, opportunity, spread_fraction=0.0):
        seller = self._seller(spread_fraction)
        with patch('src.strategy.call_seller.logger') as mock_logger:
            result = seller.execute_call_sale(opportunity)
        assert result['success'] is True
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get('event_type') == 'call_sale_executing']
        assert len(events) == 1
        return events[0]

    def test_the_executing_event_carries_the_pricing_inputs(self):
        """Without these fields the two-week fill-rate study in fc-072.md
        cannot be run from logs, which would mean a second change."""
        event = self._executing_event(self._opportunity())

        assert event['bid'] == 3.14
        assert event['ask'] == 3.34
        assert event['spread'] == 0.20
        assert event['premium'] == 3.24
        assert event['limit_price'] == 3.24
        assert event['spread_fraction'] == 0.0

    def test_the_executing_event_reports_the_applied_fraction(self):
        event = self._executing_event(self._opportunity(), spread_fraction=0.10)
        assert event['spread_fraction'] == 0.10
        assert event['limit_price'] == 3.26

    def test_a_fallback_write_is_distinguishable_in_the_logs(self):
        """`spread_fraction: None` is what separates a priced-at-mid write from
        a degraded-quote write when f is 0 and the two limits differ only by
        5%. The stale quote's spread is still reported, so the reason is
        visible rather than merely absent."""
        event = self._executing_event(self._opportunity(bid=2.24, ask=4.24))
        assert event['spread_fraction'] is None
        assert event['spread'] == 2.00
        assert event['limit_price'] == 3.08

    def test_a_missing_quote_logs_no_spread(self):
        opp = self._opportunity()
        del opp['bid']
        del opp['ask']
        event = self._executing_event(opp)
        assert event['bid'] is None
        assert event['ask'] is None
        assert event['spread'] is None
        assert event['spread_fraction'] is None

    # --- Guardrails on what must NOT change --------------------------------

    def test_pricing_does_not_run_before_the_cost_basis_floor(self):
        """The floor is the strongest control in the system; a pricing change
        must not have moved order submission ahead of it."""
        seller = self._seller()
        result = seller.execute_call_sale(
            self._opportunity(strike_price=290.0,
                              option_symbol='AAPL260821C00290000'))
        assert result['success'] is False
        assert result['error'] == 'strike_below_cost_basis'
        self.mock_alpaca.place_option_order.assert_not_called()

    def test_order_type_side_and_quantity_are_unchanged(self):
        seller = self._seller()
        seller.execute_call_sale(self._opportunity(contracts=2, max_contracts=2))
        kwargs = self.mock_alpaca.place_option_order.call_args.kwargs
        assert kwargs['side'] == 'sell'
        assert kwargs['order_type'] == 'limit'
        assert kwargs['qty'] == 2
        assert kwargs['symbol'] == 'AAPL260821C00310000'
