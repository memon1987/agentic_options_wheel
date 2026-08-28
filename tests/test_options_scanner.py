"""Tests for options scanner module."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from src.data.options_scanner import OptionsScanner
from src.strategy.limit_pricing import quote_age_seconds
from src.utils.config import Config

# Sentinel for "the key is not present at all", which is a distinct failure
# from "the key is present and empty" once the floor is a single broker field.
_ABSENT = object()


class TestOptionsScannerPutScan:
    """Test OptionsScanner.scan_for_put_opportunities."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['AAPL', 'MSFT', 'GOOGL']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7

        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data, self.mock_config)

        # No existing positions by default
        self.mock_alpaca.get_positions.return_value = []

    def test_scan_finds_put_opportunities(self):
        """Test scanning returns scored put opportunities."""
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 175.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
        ]

        self.mock_market_data.find_suitable_puts.side_effect = [
            [
                {
                    'symbol': 'AAPL250117P00170000',
                    'strike_price': 170.0,
                    'expiration_date': '2025-01-17',
                    'dte': 7,
                    'delta': -0.15,
                    'mid_price': 2.50,
                    'bid': 2.45,
                    'ask': 2.55,
                    'volume': 1500,
                    'open_interest': 5000,
                    'implied_volatility': 0.25,
                }
            ],
            [
                {
                    'symbol': 'MSFT250117P00380000',
                    'strike_price': 380.0,
                    'expiration_date': '2025-01-17',
                    'dte': 7,
                    'delta': -0.12,
                    'mid_price': 3.00,
                    'bid': 2.90,
                    'ask': 3.10,
                    'volume': 2000,
                    'open_interest': 8000,
                    'implied_volatility': 0.22,
                }
            ],
        ]

        results = self.scanner.scan_for_put_opportunities(max_results=10)

        assert len(results) == 2
        # Each result should have an attractiveness score
        for opp in results:
            assert 'attractiveness_score' in opp
            assert opp['attractiveness_score'] >= 0
            assert opp['type'] == 'put'
        # Should be sorted by attractiveness_score descending
        assert results[0]['attractiveness_score'] >= results[1]['attractiveness_score']

    def test_scan_filters_existing_positions(self):
        """Test that stocks with existing positions are skipped."""
        self.mock_alpaca.get_positions.return_value = [
            {'symbol': 'AAPL', 'asset_class': 'us_equity'},
        ]

        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 175.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
        ]

        self.mock_market_data.find_suitable_puts.return_value = [
            {
                'symbol': 'MSFT250117P00380000',
                'strike_price': 380.0,
                'expiration_date': '2025-01-17',
                'dte': 7,
                'delta': -0.12,
                'mid_price': 3.00,
                'bid': 2.90,
                'ask': 3.10,
                'volume': 2000,
                'open_interest': 8000,
                'implied_volatility': 0.22,
            }
        ]

        results = self.scanner.scan_for_put_opportunities()

        # Only MSFT should appear (AAPL has existing position)
        assert len(results) == 1
        assert results[0]['symbol'] == 'MSFT'

    def test_scan_handles_empty_suitable_stocks(self):
        """Test handling when no stocks are suitable."""
        self.mock_market_data.filter_suitable_stocks.return_value = []

        results = self.scanner.scan_for_put_opportunities()
        assert results == []

    def test_scan_handles_no_puts_found(self):
        """Test handling when suitable stocks have no puts."""
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 175.0},
        ]
        self.mock_market_data.find_suitable_puts.return_value = []

        results = self.scanner.scan_for_put_opportunities()
        assert results == []

    def test_scan_handles_api_error(self):
        """Test graceful handling of API errors returns empty list."""
        self.mock_market_data.filter_suitable_stocks.side_effect = Exception("API down")

        results = self.scanner.scan_for_put_opportunities()
        assert results == []

    def test_scan_respects_max_results(self):
        """Test that max_results limits output."""
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': f'SYM{i}', 'current_price': 100.0}
            for i in range(5)
        ]

        self.mock_market_data.find_suitable_puts.return_value = [
            {
                'symbol': f'SYM250117P00090000',
                'strike_price': 90.0,
                'expiration_date': '2025-01-17',
                'dte': 7,
                'delta': -0.15,
                'mid_price': 1.50,
                'bid': 1.45,
                'ask': 1.55,
                'volume': 1000,
                'open_interest': 3000,
                'implied_volatility': 0.20,
            }
        ]

        results = self.scanner.scan_for_put_opportunities(max_results=2)
        assert len(results) <= 2


class TestOptionsScannerScoring:
    """Test opportunity scoring and ranking logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7

        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data, self.mock_config)

    def test_put_attractiveness_score_range(self):
        """Test put attractiveness score is between 0 and 100."""
        score = self.scanner._calculate_put_attractiveness_score(
            annual_return=15.0,
            delta=0.15,
            otm_percentage=8.0,
            liquidity_score=50.0,
            dte=5,
        )

        assert 0 <= score <= 100

    def test_put_score_higher_for_better_return(self):
        """Test that higher annual return gives higher score."""
        score_low = self.scanner._calculate_put_attractiveness_score(
            annual_return=5.0, delta=0.15, otm_percentage=8.0,
            liquidity_score=50.0, dte=5,
        )
        score_high = self.scanner._calculate_put_attractiveness_score(
            annual_return=20.0, delta=0.15, otm_percentage=8.0,
            liquidity_score=50.0, dte=5,
        )

        assert score_high > score_low

    def test_put_score_prefers_target_delta(self):
        """Test that delta near 0.20 scores higher than extremes."""
        score_ideal = self.scanner._calculate_put_attractiveness_score(
            annual_return=10.0, delta=0.20, otm_percentage=8.0,
            liquidity_score=50.0, dte=5,
        )
        score_extreme = self.scanner._calculate_put_attractiveness_score(
            annual_return=10.0, delta=0.50, otm_percentage=8.0,
            liquidity_score=50.0, dte=5,
        )

        assert score_ideal > score_extreme

    def test_call_attractiveness_score_above_cost_basis_bonus(self):
        """Test that calls above cost basis get a bonus."""
        score_above = self.scanner._calculate_call_attractiveness_score(
            annual_return=10.0, delta=0.15, otm_percentage=5.0,
            liquidity_score=50.0, dte=5, above_cost_basis=True,
        )
        score_below = self.scanner._calculate_call_attractiveness_score(
            annual_return=10.0, delta=0.15, otm_percentage=5.0,
            liquidity_score=50.0, dte=5, above_cost_basis=False,
        )

        assert score_above > score_below

    def test_put_score_zero_for_bad_dte(self):
        """Test that DTE exceeding target gets zero DTE component."""
        score_good = self.scanner._calculate_put_attractiveness_score(
            annual_return=10.0, delta=0.15, otm_percentage=8.0,
            liquidity_score=50.0, dte=5,  # Within target
        )
        score_bad = self.scanner._calculate_put_attractiveness_score(
            annual_return=10.0, delta=0.15, otm_percentage=8.0,
            liquidity_score=50.0, dte=30,  # Way past target of 7
        )

        assert score_good > score_bad

    def test_call_score_range(self):
        """Test call attractiveness score is between 0 and 100."""
        score = self.scanner._calculate_call_attractiveness_score(
            annual_return=10.0, delta=0.15, otm_percentage=5.0,
            liquidity_score=50.0, dte=5, above_cost_basis=True,
        )

        assert 0 <= score <= 100


class TestOptionsScannerCallScan:
    """Test OptionsScanner.scan_for_call_opportunities."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7

        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data, self.mock_config)

    def test_scan_call_opportunities_with_stock_positions(self):
        """Test scanning for calls when stock positions exist."""
        self.mock_alpaca.get_positions.return_value = [
            {
                'symbol': 'AAPL',
                'qty': '100',
                'cost_basis': '16000.0',
                'avg_entry_price': '160.0',
                'asset_class': 'us_equity',
                'side': 'long',
            }
        ]

        self.mock_market_data.get_stock_metrics.return_value = {
            'current_price': 175.0,
        }

        self.mock_market_data.find_suitable_calls.return_value = [
            {
                'symbol': 'AAPL250117C00185000',
                'strike_price': 185.0,
                'expiration_date': '2025-01-17',
                'dte': 7,
                'delta': 0.15,
                'mid_price': 1.80,
                'bid': 1.75,
                'ask': 1.85,
                'volume': 2000,
                'open_interest': 8000,
                'implied_volatility': 0.22,
            }
        ]

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert results[0]['type'] == 'call'
        assert results[0]['symbol'] == 'AAPL'
        assert results[0]['max_contracts'] == 1
        # Should filter by cost basis
        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'AAPL', min_strike_price=160.0, exclude_expiry_on_or_after=None
        )

    def test_multiple_round_lots_size_to_every_lot(self):
        """FC-068 migration of the engine path's `test_multiple_round_lots`.

        A 300-share position must offer 3 contracts, not 1. The deleted
        `_calculate_call_position` pinned this; nothing on the scanner path
        did — `test_scan_call_opportunities_with_stock_positions` only ever
        used 100 shares, so a `max_contracts = 1` regression would have been
        invisible after the deletion.
        """
        self.mock_alpaca.get_positions.return_value = [
            {
                'symbol': 'AAPL',
                'qty': '300',
                'cost_basis': '48000.0',
                'avg_entry_price': '160.0',
                'asset_class': 'us_equity',
                'side': 'long',
            }
        ]
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 175.0}
        self.mock_market_data.find_suitable_calls.return_value = [
            {
                'symbol': 'AAPL250117C00185000',
                'strike_price': 185.0,
                'expiration_date': '2025-01-17',
                'dte': 7,
                'delta': 0.15,
                'mid_price': 1.80,
                'bid': 1.75,
                'ask': 1.85,
                'volume': 2000,
                'open_interest': 8000,
                'implied_volatility': 0.22,
            }
        ]

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert results[0]['shares_owned'] == 300
        assert results[0]['max_contracts'] == 3
        # Premium income must scale with the lots, not with a bare 100.
        assert results[0]['premium_income'] == pytest.approx(1.80 * 100 * 3)

    def test_scan_call_skips_insufficient_shares(self):
        """Test that positions with < 100 shares are skipped."""
        self.mock_alpaca.get_positions.return_value = [
            {
                'symbol': 'AAPL',
                'qty': '50',
                'cost_basis': '8000.0',
                'asset_class': 'us_equity',
                'side': 'long',
            }
        ]

        results = self.scanner.scan_for_call_opportunities()
        assert results == []
        self.mock_market_data.find_suitable_calls.assert_not_called()

    def test_scan_call_handles_empty_positions(self):
        """Test handling when no stock positions exist."""
        self.mock_alpaca.get_positions.return_value = []

        results = self.scanner.scan_for_call_opportunities()
        assert results == []


class TestCallScanFailsClosedOnUnresolvedCostBasis:
    """An unresolved cost basis must emit NO call opportunities (FC-038 review).

    This floor is the operative below-basis protection on the live path:
    `find_suitable_calls` only warns when `min_strike_price <= 0` and carries
    on. FC-065 makes the floor Alpaca's `avg_entry_price`, so the failure this
    guards is the broker reporting nothing usable for that field — FC-029
    observed exactly that (`cost_basis = 0`) on assigned paper positions, and
    the whole floor now rests on one field of the same payload.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 175.0}
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'AAPL250117C00185000', 'strike_price': 185.0,
            'expiration_date': '2025-01-17', 'dte': 7, 'delta': 0.15,
            'mid_price': 1.80, 'bid': 1.75, 'ask': 1.85, 'volume': 2000,
            'open_interest': 8000, 'implied_volatility': 0.22,
        }]

    def _position(self, avg_entry_price, cost_basis='16000.0'):
        position = {'symbol': 'AAPL', 'qty': '100', 'cost_basis': cost_basis,
                    'asset_class': 'us_equity', 'side': 'long'}
        if avg_entry_price is not _ABSENT:
            position['avg_entry_price'] = avg_entry_price
        return position

    # 'nonsense' added by FC-068: it is the third parametrization of the
    # engine-path test this one replaces
    # (`test_no_cost_basis_floor_resolved_blocks_call_write[0.0/None/nonsense]`),
    # and an unparseable string is a distinct failure from a zero or an absent
    # field — it reaches float() rather than the falsy checks.
    @pytest.mark.parametrize("avg_entry_price",
                             ['0', '0.0', 0, None, '', 'nonsense', _ABSENT])
    def test_unresolved_cost_basis_emits_nothing(self, avg_entry_price):
        self.mock_alpaca.get_positions.return_value = [
            self._position(avg_entry_price)]

        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self.scanner.scan_for_call_opportunities()

        assert results == []
        self.mock_market_data.find_suitable_calls.assert_not_called(), (
            "an unprotected chain scan was run with a zero floor")
        skips = [c.kwargs for c in mock_logger.warning.call_args_list
                 if c.kwargs.get("event_type") == "call_scan_skipped_cost_basis_unresolved"]
        assert len(skips) == 1
        assert skips[0]["event_category"] == "trade"
        assert skips[0]["symbol"] == "AAPL"
        assert skips[0]["shares"] == 100
        assert skips[0]["avg_entry_price"] == 0.0

    def test_a_positive_cost_basis_does_not_rescue_a_missing_avg_entry_price(self):
        """FC-065 fail-closed: ``cost_basis`` is not a second floor source. The
        derivation lives in the client wrapper; a position dict arriving here
        without the field means the plumbing is broken, and writing calls
        against a guessed floor is the outcome this guard exists to prevent."""
        self.mock_alpaca.get_positions.return_value = [
            self._position(_ABSENT, cost_basis='16000.0')]

        results = self.scanner.scan_for_call_opportunities()

        assert results == []
        self.mock_market_data.find_suitable_calls.assert_not_called()

    def test_a_valid_cost_basis_still_produces_opportunities(self):
        """The guard must not cost us the calls we are entitled to sell."""
        self.mock_alpaca.get_positions.return_value = [self._position('160.0')]

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert results[0]['symbol'] == 'AAPL'
        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'AAPL', min_strike_price=160.0, exclude_expiry_on_or_after=None)

    def test_one_bad_position_does_not_suppress_a_good_one(self):
        self.mock_alpaca.get_positions.return_value = [
            self._position('0'),
            {'symbol': 'MSFT', 'qty': '100', 'cost_basis': '38000.0',
             'avg_entry_price': '380.0', 'asset_class': 'us_equity',
             'side': 'long'},
        ]

        results = self.scanner.scan_for_call_opportunities()

        assert [r['symbol'] for r in results] == ['MSFT']


class TestCallScanFloorIsTheBrokerBasisCrossCheckedAgainstBigQuery:
    """FC-065 Phase 1: the scanner's floor is Alpaca's ``avg_entry_price``, and
    BigQuery runs inline per held symbol per scan as a divergence cross-check
    that can veto it.

    This inverts FC-050, where a BigQuery-resolved OPASN strike *set* the floor
    and the broker was the last resort.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 310.0}
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'AAPL260821C00320000', 'strike_price': 320.0,
            'expiration_date': '2026-08-21', 'dte': 7, 'delta': 0.20,
            'mid_price': 1.80, 'bid': 1.75, 'ask': 1.85, 'volume': 2000,
            'open_interest': 8000, 'implied_volatility': 0.22,
        }]
        # An assigned AAPL lot: assigning put struck 305.00, sold for $1.50, so
        # the broker reports the premium-netted 303.50.
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AAPL', 'qty': '100', 'cost_basis': '30350.0',
            'avg_entry_price': '303.50', 'asset_class': 'us_equity',
            'side': 'long',
        }]

    def _with_reconstruction(self, expected, shares=100):
        """Inject the cross-check's BigQuery answer (the suite stubs the real
        chokepoint out, so a test that wants one supplies it explicitly)."""
        return patch.object(
            self.scanner.cost_basis_resolver, '_lookup_assignment_basis',
            return_value={'expected_basis_per_share': expected,
                          'reconstructed_shares': shares, 'lots': []})

    def test_the_broker_basis_filters_the_chain_and_rides_the_opportunity(self):
        with self._with_reconstruction(303.50):
            results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        # The broker's number filtered the chain scan...
        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'AAPL', min_strike_price=303.50, exclude_expiry_on_or_after=None)
        # ...and is the same number carried on the opportunity, which is what
        # call_seller enforces at execution.
        assert results[0]['cost_basis_per_share'] == 303.50
        assert results[0]['assignment_above_cost_basis'] is True
        # (320 - 303.50) * 100 + 1.80 * 100 = 1830.0
        assert results[0]['total_return_if_assigned'] == pytest.approx(1830.0)

    def test_a_divergent_cross_check_skips_the_symbol(self):
        """Fails if the cross-check is reverted to warn-only: the assertion is
        that no chain scan ran and no opportunity was emitted, not that a
        warning was written."""
        with self._with_reconstruction(290.00):
            with patch('src.data.options_scanner.logger') as mock_logger:
                results = self.scanner.scan_for_call_opportunities()

        assert results == []
        self.mock_market_data.find_suitable_calls.assert_not_called()

        skips = [c.kwargs for c in mock_logger.warning.call_args_list
                 if c.kwargs.get("event_type") == "call_scan_skipped_cost_basis_divergent"]
        assert len(skips) == 1
        assert skips[0]["event_category"] == "trade"
        assert skips[0]["symbol"] == "AAPL"
        assert skips[0]["broker_basis"] == 303.50
        assert skips[0]["expected_basis"] == 290.00
        assert skips[0]["basis_delta"] == pytest.approx(13.50)
        assert skips[0]["reason"] == "basis_mismatch"

    def test_a_share_count_mismatch_skips_the_symbol(self):
        with self._with_reconstruction(303.50, shares=200):
            with patch('src.data.options_scanner.logger') as mock_logger:
                results = self.scanner.scan_for_call_opportunities()

        assert results == []
        skips = [c.kwargs for c in mock_logger.warning.call_args_list
                 if c.kwargs.get("event_type") == "call_scan_skipped_cost_basis_divergent"]
        assert skips[0]["reason"] == "share_count_mismatch"
        assert skips[0]["reconstructed_shares"] == 200

    def test_an_agreeing_cross_check_is_logged_with_its_verdict(self):
        with self._with_reconstruction(303.45):
            with patch('src.data.options_scanner.logger') as mock_logger:
                results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get("event_type") == "cost_basis_cross_check"]
        assert len(events) == 1
        assert events[0]["symbol"] == "AAPL"
        assert events[0]["source"] == "alpaca"
        assert events[0]["resolved_basis"] == 303.50
        assert events[0]["status"] == "ok"
        assert events[0]["expected_basis"] == 303.45

    def test_an_unavailable_cross_check_is_logged_but_keeps_the_floor(self):
        """No assignment history (a manual buy, or BigQuery down) means no
        comparison — which must not be mistaken for an all-clear, hence the
        event carries the status either way."""
        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self.scanner.scan_for_call_opportunities()  # suite stub → None

        assert len(results) == 1
        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'AAPL', min_strike_price=303.50, exclude_expiry_on_or_after=None)
        events = [c.kwargs for c in mock_logger.info.call_args_list
                  if c.kwargs.get("event_type") == "cost_basis_cross_check"]
        assert len(events) == 1
        assert events[0]["status"] == "unavailable"
        assert events[0]["reason"] == "no_assignment_history"
        assert events[0]["resolved_basis"] == 303.50

    def test_nothing_resolves_still_emits_no_opportunities(self):
        """FC-038's fail-closed skip survives, now on the broker field."""
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AAPL', 'qty': '100', 'cost_basis': '0',
            'avg_entry_price': '0', 'asset_class': 'us_equity', 'side': 'long',
        }]

        results = self.scanner.scan_for_call_opportunities()

        assert results == []
        self.mock_market_data.find_suitable_calls.assert_not_called()


class TestAtFloorStrikesAreFlaggedTheWayTheGatesTreatThem:
    """FC-065 Phase 2: ``assignment_above_cost_basis`` is ``strike >= floor``.

    Every floor gate rejects only ``strike < floor`` — ``market_data``'s chain
    filter, ``call_seller``'s execute-time check
    (``test_a_strike_exactly_at_basis_is_allowed``), and
    ``risk_manager.validate_roll``
    (``test_a_strike_exactly_at_the_floor_is_allowed``). So an at-floor call is
    admitted and sold, and GOOGL's 370C was. Under strict ``>`` the flag read
    ``False`` on exactly those writes: a warning that gated nothing and
    contradicted what the bot had just done.

    At-floor is the decided policy, not an edge case to close — called away at
    cost keeps both premiums and gives up no capital.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 372.0}
        # GOOGL's real shape: floor 368.34, and the only candidate is struck
        # exactly there.
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'GOOGL', 'qty': '100', 'cost_basis': '36834.0',
            'avg_entry_price': '368.34', 'asset_class': 'us_equity',
            'side': 'long',
        }]

    def _chain(self, strike):
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'GOOGL260821C%08d' % int(strike * 1000),
            'strike_price': strike, 'expiration_date': '2026-08-21', 'dte': 7,
            'delta': 0.20, 'mid_price': 2.10, 'bid': 2.05, 'ask': 2.15,
            'volume': 1500, 'open_interest': 6000, 'implied_volatility': 0.24,
        }]

    def test_a_strike_exactly_at_the_floor_is_flagged_above_the_basis(self):
        """The mutation target. Reverted to strict ``>`` this asserts False on
        a write the gates admit and the bot executes."""
        self._chain(368.34)

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert results[0]['strike_price'] == 368.34
        assert results[0]['cost_basis_per_share'] == 368.34
        assert results[0]['assignment_above_cost_basis'] is True

    def test_the_floor_was_handed_to_the_chain_gate_that_admitted_it(self):
        """The other half of the reconciliation: the same number that produced
        the flag is the ``min_strike_price`` the chain scan filtered on, and
        that filter rejects only ``strike < min_strike_price``."""
        self._chain(368.34)

        self.scanner.scan_for_call_opportunities()

        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'GOOGL', min_strike_price=368.34, exclude_expiry_on_or_after=None)

    def test_a_strike_above_the_floor_is_still_flagged_above(self):
        self._chain(375.00)

        results = self.scanner.scan_for_call_opportunities()

        assert results[0]['assignment_above_cost_basis'] is True

    def test_a_strike_below_the_floor_is_still_flagged_below(self):
        """``>=`` loosened the boundary by one point, not the predicate. A
        below-floor strike — which the chain gate would never return, so this
        pins the flag itself — still reads False."""
        self._chain(360.00)

        results = self.scanner.scan_for_call_opportunities()

        assert results[0]['assignment_above_cost_basis'] is False


class TestAtFloorStrikesAreScoredTheWayTheyAreFlagged:
    """FC-071: the ``above_cost_basis`` scoring input is the same ``>=``
    comparison as the flag, so gate, flag, and score all agree that at-floor
    is good.

    FC-065 Phase 2 moved the flag to ``>=`` but deliberately left the scoring
    input on strict ``>`` for an operator decision. The result was an at-floor
    candidate that was *flagged* at-or-above basis while being *scored* as
    below-basis — 5 points instead of 15, a 10-point swing in a 0-100 score
    that ranks a genuinely profitable write below its peers. The operator
    decided to align (2026-08-02).

    Exact equality is near measure-zero on premium-netted floors against
    $2.50 strike grids, so this is consistency hygiene rather than a shift on
    today's book. These tests pin the boundary semantics: they fail on any
    ``>`` vs ``>=`` divergence between the flag and the scoring input.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 372.0}
        # Same GOOGL shape as the flag tests above: floor 368.34.
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'GOOGL', 'qty': '100', 'cost_basis': '36834.0',
            'avg_entry_price': '368.34', 'asset_class': 'us_equity',
            'side': 'long',
        }]

    def _chain(self, strike):
        self.mock_market_data.find_suitable_calls.return_value = [{
            # round(), not int(): the OCC strike field is in thousandths and
            # strike * 1000 can land a hair below the integer in float, which
            # int() would truncate down by a tenth of a cent.
            'symbol': 'GOOGL260821C%08d' % round(strike * 1000),
            'strike_price': strike, 'expiration_date': '2026-08-21', 'dte': 7,
            'delta': 0.20, 'mid_price': 2.10, 'bid': 2.05, 'ask': 2.15,
            'volume': 1500, 'open_interest': 6000, 'implied_volatility': 0.24,
        }]

    def _score_with(self, opportunity, above_cost_basis):
        """Re-score an emitted opportunity from its own published components,
        varying only the basis input. Reading the components back off the dict
        keeps this independent of the scan's internal arithmetic."""
        return self.scanner._calculate_call_attractiveness_score(
            opportunity['annual_premium_return_percent'],
            opportunity['delta'],
            opportunity['otm_percentage'],
            opportunity['liquidity_score'],
            opportunity['dte'],
            above_cost_basis,
        )

    def test_a_strike_exactly_at_the_floor_earns_the_above_basis_bonus(self):
        """The mutation target. Reverted to strict ``>``, the at-floor
        opportunity carries the 5-point below-basis consolation instead of the
        15-point bonus and this fails on both assertions."""
        self._chain(368.34)

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        opportunity = results[0]
        assert opportunity['strike_price'] == 368.34
        assert opportunity['cost_basis_per_share'] == 368.34

        with_bonus = self._score_with(opportunity, True)
        without_bonus = self._score_with(opportunity, False)
        # This pins the current 15-vs-5 spread at 10 points. It is a pin, not
        # a derivation: if FC-073 retunes the basis weights this assert must
        # be updated deliberately — which is the intended prompt to re-confirm
        # that at-floor still ranks with the above-basis cohort.
        assert with_bonus - without_bonus == pytest.approx(10)
        # The emitted score is the with-bonus one, not the consolation.
        assert opportunity['attractiveness_score'] == pytest.approx(with_bonus)
        assert opportunity['attractiveness_score'] != pytest.approx(without_bonus)

    def test_an_at_floor_strike_scores_the_same_as_one_cent_above(self):
        """The boundary is no longer a scoring cliff: a penny of extra capital
        gain must not be worth 10 points of rank."""
        self._chain(368.34)
        at_floor = self.scanner.scan_for_call_opportunities()[0]

        self._chain(368.35)
        above_floor = self.scanner.scan_for_call_opportunities()[0]

        assert above_floor['assignment_above_cost_basis'] is True
        # Both sides of the boundary now collect the same 15-point bonus, so
        # the residual gap is only the OTM/return effect of the extra cent.
        assert abs(above_floor['attractiveness_score']
                   - at_floor['attractiveness_score']) < 1

    @pytest.mark.parametrize('strike', [360.00, 368.34, 368.35, 375.00])
    def test_the_flag_and_the_scoring_input_are_the_same_value(self, strike):
        """FC-071's real contract: one comparison feeds both surfaces. This
        fails on any semantic divergence (``>`` vs ``>=``) at the boundary —
        the drift FC-071 closed. It does not, and cannot, catch a
        semantics-preserving refactor that merely re-derives one side from an
        equivalent comparison; the single-source shape in the scanner is what
        guards against that. Below-floor is included because the parameter
        contract survives even though the chain gate would never return such
        a strike."""
        self._chain(strike)
        recorded = {}
        real_score = self.scanner._calculate_call_attractiveness_score

        def spy(*args, **kwargs):
            # ``above_cost_basis`` is the 6th positional parameter.
            recorded['above_cost_basis'] = (
                kwargs['above_cost_basis'] if 'above_cost_basis' in kwargs
                else args[5]
            )
            return real_score(*args, **kwargs)

        self.scanner._calculate_call_attractiveness_score = spy

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert 'above_cost_basis' in recorded, 'scoring was never invoked'
        assert recorded['above_cost_basis'] is (strike >= 368.34)
        assert recorded['above_cost_basis'] is results[0]['assignment_above_cost_basis']


class TestCallScanFailsClosedOnAnUnusableQuote:
    """FC-065 (folded in from the removed Phase 3): a failed stock quote used
    to fail OPEN — the opportunity was emitted anyway, with every price-derived
    metric computed against a current price of 0."""

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'AAPL260821C00320000', 'strike_price': 320.0,
            'expiration_date': '2026-08-21', 'dte': 7, 'delta': 0.20,
            'mid_price': 1.80, 'bid': 1.75, 'ask': 1.85, 'volume': 2000,
            'open_interest': 8000, 'implied_volatility': 0.22,
        }]
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AAPL', 'qty': '100', 'cost_basis': '30350.0',
            'avg_entry_price': '303.50', 'asset_class': 'us_equity',
            'side': 'long',
        }]

    @pytest.mark.parametrize("metrics", [
        {},                                # no metrics at all
        {'current_price': 0},              # quote came back empty
        {'current_price': None},
        {'current_price': 'n/a'},          # malformed
        None,                              # get_stock_metrics returned nothing
    ])
    def test_an_unusable_quote_emits_no_opportunity(self, metrics):
        self.mock_market_data.get_stock_metrics.return_value = metrics

        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self.scanner.scan_for_call_opportunities()

        assert results == []
        skips = [c.kwargs for c in mock_logger.warning.call_args_list
                 if c.kwargs.get("event_type") == "call_scan_skipped_quote_unavailable"]
        assert len(skips) == 1
        assert skips[0]["symbol"] == "AAPL"

    def test_a_usable_quote_still_produces_the_opportunity(self):
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 310.0}

        results = self.scanner.scan_for_call_opportunities()

        assert len(results) == 1
        assert results[0]['current_stock_price'] == 310.0


class TestOptionsScannerScanAll:
    """Test OptionsScanner.scan_all_opportunities."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['AAPL']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7

        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data, self.mock_config)
        self.mock_alpaca.get_positions.return_value = []

    def test_scan_all_returns_both_types(self):
        """Test that scan_all returns both puts and calls keys."""
        self.mock_market_data.filter_suitable_stocks.return_value = []

        result = self.scanner.scan_all_opportunities()

        assert 'puts' in result
        assert 'calls' in result
        assert 'scan_timestamp' in result
        assert 'total_opportunities' in result

    def test_scan_all_handles_exception(self):
        """Test graceful error handling in scan_all."""
        self.mock_market_data.filter_suitable_stocks.side_effect = Exception("Timeout")

        result = self.scanner.scan_all_opportunities()

        # The inner scan catches its own error and returns [],
        # so scan_all should still succeed
        assert 'puts' in result
        assert result['puts'] == []


class TestScanStageDecisionRecords:
    """FC-065 Phase 4: every held symbol gets a decision, every cycle.

    Before this, an underwater symbol produced *silence* — NVDA has ended
    every scan since 2026-07-30 at `stage_8_complete_not_found` with nothing
    downstream saying so. The acceptance test for this phase is that the
    silence becomes a labelled row.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 7

        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)
        # Deterministic label: the hermeticity fixture already forces the
        # BigQuery chokepoint to "no comparison"; tests that care inject one.
        self.scanner.uncovered_days_resolver._lookup_uncovered_days = (
            lambda symbols: {s: 9 for s in symbols})
        self.mock_market_data.last_call_rejection_stats = {}
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 199.0}

    # -- helpers ---------------------------------------------------- #

    @staticmethod
    def _nvda(qty=100, avg_entry_price='218.43', market_value='19900.0'):
        return {
            'symbol': 'NVDA', 'qty': str(qty), 'cost_basis': '21843.0',
            'avg_entry_price': avg_entry_price, 'market_value': market_value,
            'asset_class': 'us_equity', 'side': 'long',
        }

    def _rows(self, run_id="20260803T140000Z-abcdef12", **kw):
        captured = []
        with patch('src.data.decision_record.DecisionRecorder.flush',
                   autospec=True,
                   side_effect=lambda self, **_: captured.extend(self.rows)):
            self.scanner.scan_for_call_opportunities(run_id=run_id, **kw)
        return captured

    # -- the acceptance test ---------------------------------------- #

    def test_the_nothing_happened_case_produces_a_labelled_row(self):
        """NVDA's silent exclusion becomes a row — the phase's acceptance test."""
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = []
        self.mock_market_data.last_call_rejection_stats = {
            'NVDA': {'below_cost_basis': 41, 'delta_out_of_range': 0,
                     'premium_too_low': 2, 'total_rejected': 43},
        }

        rows = self._rows()

        assert len(rows) == 1
        row = rows[0]
        assert row['symbol'] == 'NVDA'
        assert row['outcome'] == 'no_candidates'
        assert row['reason'] == 'no_qualifying_strikes'
        assert row['candidates'] == 0
        assert row['cost_basis_per_share'] == 218.43
        # Both legibility labels present and meaningful.
        assert row['underwater_pct'] == pytest.approx(-0.088953, abs=1e-6)
        assert row['uncovered_days'] == 9
        # The chain breakdown rides along as a REPEATED record, and the
        # meaningless roll-up is not one of its entries.
        assert row['reason_counts'] == [
            {'reason': 'below_cost_basis', 'count': 41},
            {'reason': 'premium_too_low', 'count': 2},
        ]

    def test_under_100_shares_is_recorded_not_just_skipped(self):
        self.mock_alpaca.get_positions.return_value = [
            self._nvda(qty=50, market_value='9950.0')]

        rows = self._rows()

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('not_eligible', 'insufficient_shares')]
        assert rows[0]['shares'] == 50

    def test_an_unresolved_floor_is_recorded_as_blocked(self):
        self.mock_alpaca.get_positions.return_value = [
            self._nvda(avg_entry_price='0')]

        rows = self._rows()

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('blocked', 'floor_unresolved')]

    def test_a_divergent_cross_check_is_recorded_as_blocked(self):
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.scanner.cost_basis_resolver._lookup_assignment_basis = (
            lambda symbol, shares_held: {
                'expected_basis_per_share': 300.0, 'reconstructed_shares': 100})

        rows = self._rows()

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('blocked', 'floor_divergent')]

    @staticmethod
    def _chain():
        return [{
            'symbol': 'NVDA260807C00230000', 'strike_price': 230.0,
            'expiration_date': '2026-08-07', 'dte': 7, 'delta': 0.18,
            'mid_price': 1.20, 'bid': 1.15, 'ask': 1.25,
            'volume': 900, 'open_interest': 3000, 'implied_volatility': 0.4,
        }]

    def test_a_symbol_with_candidates_is_deferred_to_the_run_stage(self):
        """Its terminal verdict is `sold`/`dropped`, which only /run knows."""
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = self._chain()

        rows = self._rows()

        assert rows == []

    def test_candidates_carry_the_scan_labels_into_the_run_stage(self):
        """BLOCKER: without this every /run row had a NULL uncovered_days.

        /run is a separate stateless request with no positions snapshot and no
        BigQuery lookup, so the labels have to ride the blob exactly as
        `run_id` does.
        """
        from src.data.decision_record import decision_labels

        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = self._chain()

        opportunities = self.scanner.scan_for_call_opportunities(run_id="R1")

        assert len(opportunities) == 1
        labels = decision_labels(opportunities[0])
        assert labels['uncovered_days'] == 9
        assert labels['shares'] == 100
        assert labels['current_price'] == pytest.approx(199.0)
        assert labels['underwater'] == pytest.approx(-0.088953, abs=1e-6)

    def test_a_covered_symbol_with_candidates_stamps_zero_days(self):
        """The path the old covered-symbol test did NOT exercise.

        A covered symbol has candidates far more often than not, so it is
        terminated by /run — and a NULL there is what mailed the operator a
        CHECK_FAILED every trading day.
        """
        from src.data.decision_record import decision_labels

        self.mock_alpaca.get_positions.return_value = [
            self._nvda(),
            {'symbol': 'NVDA260807C00230000', 'qty': '-1',
             'asset_class': 'us_option', 'side': 'short', 'market_value': '-120.0'},
        ]
        self.mock_market_data.find_suitable_calls.return_value = self._chain()
        self.scanner.uncovered_days_resolver._lookup_uncovered_days = (
            lambda symbols: (_ for _ in ()).throw(
                AssertionError("must not query for a covered symbol")))

        opportunities = self.scanner.scan_for_call_opportunities(run_id="R1")

        assert decision_labels(opportunities[0])['uncovered_days'] == 0

    def test_a_builder_failure_is_not_filed_as_a_bad_quote(self):
        """A closed vocabulary that misfiles is worse than one more value."""
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = self._chain()
        # Quote is fine; the builder swallowed its own exception and returned
        # None, which is exactly what its `except` clause does in production.
        self.scanner._create_call_opportunity = lambda *a, **k: None

        rows = self._rows()

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('no_candidates', 'opportunity_build_failed')]

    def test_a_dead_quote_records_no_candidates_rather_than_vanishing(self):
        # The chain HAD qualifying strikes; opportunity construction failed
        # closed on an unusable quote (Phase 1). That must not be silence.
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'NVDA260807C00230000', 'strike_price': 230.0,
            'expiration_date': '2026-08-07', 'dte': 7, 'delta': 0.18,
            'mid_price': 1.20, 'bid': 1.15, 'ask': 1.25,
            'volume': 900, 'open_interest': 3000, 'implied_volatility': 0.4,
        }]
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 0}

        rows = self._rows()

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('no_candidates', 'quote_unavailable')]

    def test_every_held_symbol_gets_exactly_one_row(self):
        self.mock_alpaca.get_positions.return_value = [
            self._nvda(),
            {'symbol': 'AAPL', 'qty': '100', 'cost_basis': '30350.0',
             'avg_entry_price': '303.50', 'market_value': '31000.0',
             'asset_class': 'us_equity', 'side': 'long'},
            {'symbol': 'F', 'qty': '40', 'cost_basis': '400.0',
             'avg_entry_price': '10.0', 'market_value': '440.0',
             'asset_class': 'us_equity', 'side': 'long'},
        ]
        self.mock_market_data.find_suitable_calls.return_value = []

        rows = self._rows()

        assert sorted(r['symbol'] for r in rows) == ['AAPL', 'F', 'NVDA']
        assert len({r['dedup_key'] for r in rows}) == 3

    def test_all_rows_carry_the_run_id_they_were_given(self):
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = []

        rows = self._rows(run_id="20260803T150000Z-99887766")

        assert rows[0]['run_id'] == "20260803T150000Z-99887766"
        assert rows[0]['dedup_key'] == "20260803T150000Z-99887766|NVDA|scan"
        assert rows[0]['stage'] == 'scan'
        assert rows[0]['endpoint'] == '/scan'

    def test_a_covered_symbol_reports_zero_uncovered_days(self):
        # An open short call on the underlying means covered TODAY, answered
        # from the positions snapshot without a BigQuery round trip.
        self.mock_alpaca.get_positions.return_value = [
            self._nvda(),
            {'symbol': 'NVDA260807C00230000', 'qty': '-1',
             'asset_class': 'us_option', 'side': 'short', 'market_value': '-120.0'},
        ]
        self.mock_market_data.find_suitable_calls.return_value = []
        self.scanner.uncovered_days_resolver._lookup_uncovered_days = (
            lambda symbols: (_ for _ in ()).throw(
                AssertionError("must not query for a covered symbol")))

        rows = self._rows()

        assert rows[0]['uncovered_days'] == 0

    def test_an_underivable_uncovered_days_is_null_not_zero(self):
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = []
        self.scanner.uncovered_days_resolver._lookup_uncovered_days = (
            lambda symbols: None)

        rows = self._rows()

        assert rows[0]['uncovered_days'] is None

    def test_records_survive_a_scan_that_dies_partway(self):
        # A cycle that crashed is exactly the cycle worth investigating; the
        # rows it did produce must not die with it.
        self.mock_alpaca.get_positions.return_value = [
            self._nvda(qty=50, market_value='9950.0'), self._nvda()]
        self.mock_market_data.find_suitable_calls.side_effect = Exception("chain down")

        rows = self._rows()

        assert [r['symbol'] for r in rows] == ['NVDA']
        assert rows[0]['outcome'] == 'not_eligible'

    def test_a_chain_exception_emits_no_opportunity(self):
        """FC-068 migration of `test_api_error_returns_none`.

        The test above pins that the *records* survive a mid-scan exception.
        The deleted engine-path test pinned the other half — that the scan
        emits nothing rather than a half-built opportunity — and nothing on
        the scanner path asserted it.
        """
        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.side_effect = Exception("chain down")

        assert self.scanner.scan_for_call_opportunities() == []

    def test_a_scan_without_a_run_id_still_records_under_a_minted_one(self):
        from src.data.decision_record import is_run_id

        self.mock_alpaca.get_positions.return_value = [self._nvda()]
        self.mock_market_data.find_suitable_calls.return_value = []

        captured = []
        with patch('src.data.decision_record.DecisionRecorder.flush',
                   autospec=True,
                   side_effect=lambda self, **_: captured.extend(self.rows)):
            self.scanner.scan_for_call_opportunities()

        assert len(captured) == 1
        assert is_run_id(captured[0]['run_id'])


# =========================================================================== #
# FC-013 — the earnings gate
#
# Plan: docs/plans/fc-013.md. Per-leg semantics (DD-3, operator 2026-08-03):
#   puts  = N=2 days-until, symbol-level
#   calls = TRUE SPAN per candidate (expiry >= next earnings date)
#   unknown = symbol-level on BOTH legs, fail closed, its own event/reason
# =========================================================================== #

from datetime import date as _date, time as dtime, timedelta
from src.utils import clock as _clock
from src.api.earnings_calendar import (
    EARNINGS_CLEAR, EARNINGS_KNOWN, EARNINGS_UNKNOWN,
)

_TODAY = _date(2026, 8, 3)


class FakeCalendar:
    """An injectable calendar with the tri-state surface, no network.

    Constructed from per-symbol answers so a test can say exactly what the
    calendar knows: a date, ``'clear'``, or ``'unknown'``.
    """

    def __init__(self, answers, today=_TODAY):
        # answers: {symbol: date | 'clear' | 'unknown'}
        self.answers = answers
        self.today = today
        self.queried = []

    def next_earnings_info(self, symbol):
        self.queried.append(symbol)
        answer = self.answers.get(symbol, 'clear')
        if answer == 'unknown':
            return (EARNINGS_UNKNOWN, None)
        if answer == 'clear':
            return (EARNINGS_CLEAR, None)
        return (EARNINGS_KNOWN, answer)

    def earnings_within(self, symbol, n_days):
        status, earnings_date = self.next_earnings_info(symbol)
        if status == EARNINGS_UNKNOWN:
            return None
        if status == EARNINGS_CLEAR:
            return False
        return 0 <= (earnings_date - self.today).days <= n_days

    def get_earnings_proximity(self, symbol):
        status, earnings_date = self.next_earnings_info(symbol)
        if status != EARNINGS_KNOWN:
            return {"next_earnings_date": None, "days_until": None,
                    "earnings_hour": None}
        return {"next_earnings_date": earnings_date.isoformat(),
                "days_until": (earnings_date - self.today).days,
                "earnings_hour": "amc"}


def _events(mock_logger, event_type):
    calls = (list(mock_logger.info.call_args_list)
             + list(mock_logger.warning.call_args_list))
    return [c.kwargs for c in calls if c.kwargs.get("event_type") == event_type]


class TestPutLegEarningsGate:
    """Tests 1 and 2 — N=2 days-until, symbol-level."""

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['AAPL', 'MSFT']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7
        self.mock_config.earnings_enabled = True
        self.mock_config.earnings_blackout_days = 2
        self.mock_alpaca.get_positions.return_value = []
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 300.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
        ]
        self.mock_market_data.find_suitable_puts.side_effect = lambda symbol: [{
            'symbol': '%s260810P00280000' % symbol, 'strike_price': 280.0,
            'expiration_date': '2026-08-10', 'dte': 7, 'delta': -0.15,
            'mid_price': 2.50, 'bid': 2.45, 'ask': 2.55, 'volume': 1500,
            'open_interest': 5000, 'implied_volatility': 0.25,
        }]

    def _scan(self, calendar):
        scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                 self.mock_config, earnings_calendar=calendar)
        return scanner.scan_for_put_opportunities()

    def test_put_scan_skips_symbol_in_earnings_blackout(self):
        """Test 1 — the GOOGL incident geometry (days_until = 1 <= 2).

        MUTATION CHECK: with the gate reverted (the
        `_put_leg_blocked_by_earnings` call removed from the loop), AAPL
        produces a put opportunity and this test FAILS.
        """
        calendar = FakeCalendar({'AAPL': _TODAY + timedelta(days=1)})

        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self._scan(calendar)

        assert [r['symbol'] for r in results] == ['MSFT']
        blocked = _events(mock_logger, 'put_scan_skipped_earnings_blackout')
        assert len(blocked) == 1
        assert blocked[0]['symbol'] == 'AAPL'
        assert blocked[0]['days_until'] == 1
        assert blocked[0]['blackout_days'] == 2

    @pytest.mark.parametrize("days_until,blocked", [(0, True), (2, True), (3, False)])
    def test_put_window_boundary(self, days_until, blocked):
        """Test 2 — inclusive at both ends; day-of is blocked."""
        calendar = FakeCalendar({'AAPL': _TODAY + timedelta(days=days_until)})

        results = self._scan(calendar)

        emitted = [r['symbol'] for r in results]
        assert ('AAPL' in emitted) is (not blocked)
        assert 'MSFT' in emitted

    def test_the_gate_runs_before_the_existing_position_check(self):
        """Placement matters: the event count must read as the earnings exposure
        of the post-stage-1 candidate set, held or not."""
        calendar = FakeCalendar({'AAPL': _TODAY + timedelta(days=1)})
        scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                 self.mock_config, earnings_calendar=calendar)
        scanner._has_existing_position = Mock(return_value=False)

        scanner.scan_for_put_opportunities()

        # AAPL was gated before `_has_existing_position` was ever consulted for it.
        assert [c.args[0] for c in scanner._has_existing_position.call_args_list] == [
            'MSFT']


class TestCallLegEarningsSpanGate:
    """Tests 3, 4, 5, 6, 7, 8 at the scanner — a REAL MarketDataManager, so the
    span predicate under test is the shipped one and not a mock's opinion."""

    def setup_method(self):
        from src.api.market_data import MarketDataManager

        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 45
        self.mock_config.call_delta_range = [0.10, 0.25]
        self.mock_config.min_call_premium = 0.30
        self.mock_config.earnings_enabled = True
        self.mock_config.earnings_blackout_days = 2

        self.market_data = MarketDataManager(self.mock_alpaca, self.mock_config)
        self.market_data.get_stock_metrics = Mock(
            return_value={'current_price': 236.73})

        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AMZN', 'qty': '100', 'cost_basis': '26120.0',
            'avg_entry_price': '261.20', 'market_value': '23673.0',
            'asset_class': 'us_equity', 'side': 'long',
        }]

    def _chain(self, expiries, strike=262.5):
        calls = [{
            'symbol': 'AMZN%sC00262500' % e.strftime("%y%m%d"),
            'underlying_symbol': 'AMZN', 'option_type': 'call',
            'strike_price': strike, 'expiration_date': e.isoformat(),
            'dte': (e - _TODAY).days, 'delta': 0.17, 'mid_price': 2.22,
            'bid': 2.17, 'ask': 2.27, 'last_price': 2.22, 'volume': 900,
            'open_interest': 4000, 'implied_volatility': 0.68,
        } for e in expiries]
        self.market_data.get_option_chain_with_analysis = Mock(
            return_value={'puts': [], 'calls': calls})

    def _scanner(self, calendar):
        return OptionsScanner(self.mock_alpaca, self.market_data,
                              self.mock_config, earnings_calendar=calendar)

    def _rows(self, scanner, run_id="20260803T140000Z-abcdef12"):
        captured = []
        with patch('src.data.decision_record.DecisionRecorder.flush',
                   autospec=True,
                   side_effect=lambda self, **_: captured.extend(self.rows)):
            results = scanner.scan_for_call_opportunities(run_id=run_id)
        return results, captured

    def test_a_spanning_candidate_is_dropped_and_a_sibling_survives(self):
        """Test 3, through the scanner: the date threads into the chain."""
        earnings = _TODAY + timedelta(days=5)
        self._chain([_TODAY + timedelta(days=7), _TODAY + timedelta(days=3)])

        scanner = self._scanner(FakeCalendar({'AMZN': earnings}))
        results, _ = self._rows(scanner)

        assert len(results) == 1
        assert results[0]['expiration_date'] == (
            _TODAY + timedelta(days=3)).isoformat()
        assert self.market_data.last_call_rejection_stats['AMZN'][
            'expires_into_earnings'] == 1

    def test_call_candidate_expiring_before_earnings_is_allowed(self):
        """Test 4 — the DTE-extension pin, end to end.

        MUTATION CHECK (both must be caught): a hardcoded N=7 symbol-level
        block, or one deriving N from DTE, blocks AMZN entirely at
        days_until = 5 and this test FAILS.
        """
        self._chain([_TODAY + timedelta(days=3)])

        scanner = self._scanner(FakeCalendar({'AMZN': _TODAY + timedelta(days=5)}))
        results, rows = self._rows(scanner)

        assert len(results) == 1
        assert rows == []  # terminated by /run, not by the gate

    def test_span_boundary_expiry_on_earnings_day_is_blocked(self):
        """Test 5 at the scanner."""
        earnings = _TODAY + timedelta(days=5)
        self._chain([earnings, earnings - timedelta(days=1)])

        scanner = self._scanner(FakeCalendar({'AMZN': earnings}))
        results, _ = self._rows(scanner)

        assert [r['expiration_date'] for r in results] == [
            (earnings - timedelta(days=1)).isoformat()]

    def test_call_set_emptied_by_span_writes_blocked_row(self):
        """Test 6 — the span-emptied symbol must NOT go silent.

        A `no_candidates{no_qualifying_strikes}` row here would be a mislabel:
        strikes qualified, the event took them. The NVDA-silent-exclusion
        failure mode in a new costume.
        """
        earnings = _TODAY + timedelta(days=2)
        self._chain([_TODAY + timedelta(days=4), _TODAY + timedelta(days=11)])

        scanner = self._scanner(FakeCalendar({'AMZN': earnings}))
        with patch('src.data.options_scanner.logger') as mock_logger:
            results, rows = self._rows(scanner)

        assert results == []
        assert len(rows) == 1
        row = rows[0]
        assert (row['outcome'], row['reason']) == ('blocked', 'earnings_blackout')
        # Carries the floor labels: the chain had to be fetched to know it emptied.
        assert row['cost_basis_per_share'] == 261.20
        assert row['underwater_pct'] is not None
        assert {'reason': 'expires_into_earnings', 'count': 2} in row['reason_counts']

        blocked = _events(mock_logger, 'call_scan_skipped_earnings_blackout')
        assert len(blocked) == 1
        assert blocked[0]['spanning_candidates_rejected'] == 2

    def test_an_empty_chain_with_no_span_rejections_is_still_no_candidates(self):
        """The ordinary path must not be swallowed by the new one."""
        self._chain([])

        scanner = self._scanner(FakeCalendar({'AMZN': _TODAY + timedelta(days=5)}))
        _, rows = self._rows(scanner)

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('no_candidates', 'no_qualifying_strikes')]

    def test_a_chain_emptied_by_delta_is_not_filed_as_earnings(self):
        """Attribution: earnings must not absorb a symbol it did not terminate."""
        self._chain([_TODAY + timedelta(days=3)])
        self.market_data.get_option_chain_with_analysis.return_value[
            'calls'][0]['delta'] = 0.85

        scanner = self._scanner(FakeCalendar({'AMZN': _TODAY + timedelta(days=5)}))
        _, rows = self._rows(scanner)

        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('no_candidates', 'no_qualifying_strikes')]

    def test_the_incident_geometry_is_blocked(self):
        """Test 7 — the corrected record (rev 2.2).

        AMZN C262.5 and AAPL C347.5 were both sold on 2026-07-30, the DAY OF
        their AMC reports (`days_until = 0`), both with expiries after the
        event. AMZN gapped +12.3% through the strike: $2,308 of upside
        surrendered against $222 collected. AAPL gapped -8.5% and the identical
        trade shape profited $165 — same geometry, coin the other way.

        (The rev 2.1 "Monday fill at days_until = 3" premise is falsified and
        deliberately not encoded here.)

        MUTATION CHECK: revert span and the spanning candidate is emitted, so
        this test FAILS.
        """
        earnings_today = _TODAY  # AMC report tonight
        self._chain([_TODAY + timedelta(days=4)])  # the 7-DTE spanner

        scanner = self._scanner(FakeCalendar({'AMZN': earnings_today}))
        results, rows = self._rows(scanner)

        assert results == []
        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('blocked', 'earnings_blackout')]

    def test_clear_symbol_candidates_unconstrained(self):
        """Test 8 at the scanner: a 'clear' answer constrains nothing."""
        self._chain([_TODAY + timedelta(days=4), _TODAY + timedelta(days=11)])

        scanner = self._scanner(FakeCalendar({}))  # everything clear
        results, rows = self._rows(scanner)

        assert len(results) == 2
        assert rows == []
        assert self.market_data.last_call_rejection_stats['AMZN'][
            'expires_into_earnings'] == 0


class TestEarningsGateFailureSemantics:
    """Tests 9, 10, 11, 11b."""

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['AAPL']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7
        self.mock_config.earnings_enabled = True
        self.mock_config.earnings_blackout_days = 2
        self.mock_config.finnhub_api_key = "test_finnhub_key"
        self.mock_config.earnings_cache_ttl_hours = 24
        self.mock_config.earnings_lookahead_days = 90
        self.mock_market_data.last_call_rejection_stats = {}
        self.mock_market_data.get_stock_metrics.return_value = {'current_price': 310.0}
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 310.0}]
        self.mock_market_data.find_suitable_puts.return_value = [{
            'symbol': 'AAPL260810P00300000', 'strike_price': 300.0,
            'expiration_date': '2026-08-10', 'dte': 7, 'delta': -0.15,
            'mid_price': 2.50, 'bid': 2.45, 'ask': 2.55, 'volume': 1500,
            'open_interest': 5000, 'implied_volatility': 0.25,
        }]
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'AAPL260821C00320000', 'strike_price': 320.0,
            'expiration_date': '2026-08-21', 'dte': 7, 'delta': 0.20,
            'mid_price': 1.80, 'bid': 1.75, 'ask': 1.85, 'volume': 2000,
            'open_interest': 8000, 'implied_volatility': 0.22,
        }]
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AAPL', 'qty': '100', 'cost_basis': '30350.0',
            'avg_entry_price': '303.50', 'market_value': '31000.0',
            'asset_class': 'us_equity', 'side': 'long',
        }]

    def _scanner(self, **kw):
        return OptionsScanner(self.mock_alpaca, self.mock_market_data,
                              self.mock_config, **kw)

    def _rows(self, scanner):
        captured = []
        with patch('src.data.decision_record.DecisionRecorder.flush',
                   autospec=True,
                   side_effect=lambda self, **_: captured.extend(self.rows)):
            results = scanner.scan_for_call_opportunities(
                run_id="20260803T140000Z-abcdef12")
        return results, captured

    def test_earnings_gate_fails_closed_on_unknown(self):
        """Test 9 — both legs skip, with the UNKNOWN event, not the blackout one.

        And on the call side the cost-basis resolver is never called: the
        unknown skip sits before it, so an unanswerable symbol spends neither a
        resolver call nor a BigQuery cross-check.
        """
        scanner = self._scanner(earnings_calendar=FakeCalendar({'AAPL': 'unknown'}))
        scanner.cost_basis_resolver.resolve_detailed = Mock(
            side_effect=AssertionError("resolver must not run on an unknown symbol"))

        with patch('src.data.options_scanner.logger') as mock_logger:
            calls, rows = self._rows(scanner)
            # Flat book for the put leg, so only the gate can suppress AAPL.
            self.mock_alpaca.get_positions.return_value = []
            puts = scanner.scan_for_put_opportunities()

        assert puts == []
        assert calls == []
        assert len(_events(mock_logger, 'put_scan_skipped_earnings_unknown')) == 1
        assert len(_events(mock_logger, 'call_scan_skipped_earnings_unknown')) == 1
        assert _events(mock_logger, 'put_scan_skipped_earnings_blackout') == []
        assert _events(mock_logger, 'call_scan_skipped_earnings_blackout') == []
        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('blocked', 'earnings_unknown')]

    def test_enabled_with_no_key_degrades_loudly_not_crash(self):
        """Test 10 — DEFAULT-constructed service (no injection), empty key.

        Regression: the half-built-constructor AttributeError being swallowed
        as `scan_failure`, producing zero opportunities and zero gate telemetry
        — a state indistinguishable from a broken scanner.
        """
        self.mock_config.finnhub_api_key = ""

        scanner = self._scanner()  # no injection: builds the real service
        assert scanner.earnings_calendar is not None
        assert scanner.earnings_calendar._client is None

        with patch('src.data.options_scanner.logger') as mock_logger:
            calls, rows = self._rows(scanner)
            self.mock_alpaca.get_positions.return_value = []
            puts = scanner.scan_for_put_opportunities()

        assert puts == []
        assert calls == []
        assert len(_events(mock_logger, 'put_scan_skipped_earnings_unknown')) == 1
        assert len(_events(mock_logger, 'call_scan_skipped_earnings_unknown')) == 1
        assert [(r['outcome'], r['reason']) for r in rows] == [
            ('blocked', 'earnings_unknown')]
        # And crucially: no exception was swallowed as a scan failure.
        assert [c.kwargs for c in mock_logger.error.call_args_list
                if c.kwargs.get('error_type') == 'scan_failure'] == []

    def test_gate_absent_when_earnings_disabled(self):
        """Test 11 — config off restores pre-FC-013 behaviour byte-identically."""
        self.mock_config.earnings_enabled = False

        scanner = self._scanner(
            earnings_calendar=FakeCalendar({'AAPL': _TODAY}))  # injected AND ignored
        assert scanner.earnings_calendar is None

        calls, rows = self._rows(scanner)
        # The put leg is exercised against a flat book, so the only thing that
        # could suppress AAPL is the gate — which is off.
        self.mock_alpaca.get_positions.return_value = []
        puts = scanner.scan_for_put_opportunities()

        assert [p['symbol'] for p in puts] == ['AAPL']
        assert len(calls) == 1
        self.mock_market_data.find_suitable_calls.assert_called_once_with(
            'AAPL', min_strike_price=303.50, exclude_expiry_on_or_after=None)

    def test_env_override_disables_the_gate(self, monkeypatch, tmp_path):
        """Test 11b (DD-7) — EARNINGS_ENABLED beats the baked yaml value.

        The emergency lever must not lose to baked config: rolling the yaml key
        back rides Cloud Build, the pipeline that once sat silently red for 11
        days (FC-031).
        """
        import yaml as _yaml
        from src.utils.config import Config as _RealConfig

        with open('config/settings.yaml') as fh:
            settings = _yaml.safe_load(fh)
        path = tmp_path / "settings.yaml"
        path.write_text(_yaml.safe_dump(settings))
        assert settings['earnings']['enabled'] is True

        monkeypatch.delenv("EARNINGS_ENABLED", raising=False)
        assert _RealConfig(str(path)).earnings_enabled is True

        monkeypatch.setenv("EARNINGS_ENABLED", "false")
        assert _RealConfig(str(path)).earnings_enabled is False

        monkeypatch.setenv("EARNINGS_ENABLED", "TRUE")
        assert _RealConfig(str(path)).earnings_enabled is True

        # A typo must not silently disable a risk control.
        monkeypatch.setenv("EARNINGS_ENABLED", "maybe")
        assert _RealConfig(str(path)).earnings_enabled is True

    def test_the_env_override_reaches_the_scanner(self, monkeypatch, tmp_path):
        import yaml as _yaml
        from src.utils.config import Config as _RealConfig

        with open('config/settings.yaml') as fh:
            settings = _yaml.safe_load(fh)
        path = tmp_path / "settings.yaml"
        path.write_text(_yaml.safe_dump(settings))

        monkeypatch.setenv("EARNINGS_ENABLED", "false")
        scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                 _RealConfig(str(path)),
                                 earnings_calendar=FakeCalendar({'AAPL': _TODAY}))

        assert scanner.earnings_calendar is None


class TestTheDayAfterEarningsIsTradeable:
    """Reliability review, HIGH — the false blackout on the IV-crush day.

    End-to-end form of the past-date guard. Seeds the module cache exactly the
    way a real day-D fetch would leave it (AMC report day, fetched late,
    comfortably inside the 24h TTL), advances the clock one day, and runs a
    real scan.

    Without the guard the cached 07-30 date threads into the chain as
    `exclude_expiry_on_or_after=2026-07-30`, every candidate expires on or
    after that, the chain empties, and the symbol is falsely reported
    `blocked{earnings_blackout}` — on the post-report day DD-3 explicitly wants
    the wheel selling into. It would recur every earnings cycle.
    """

    REPORT_DAY = _date(2026, 7, 30)
    NEXT_DAY = _date(2026, 7, 31)
    NEXT_QUARTER = _date(2026, 10, 29)

    def setup_method(self):
        from src.api.market_data import MarketDataManager

        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.call_target_dte = 45
        self.mock_config.call_delta_range = [0.10, 0.25]
        self.mock_config.min_call_premium = 0.30
        self.mock_config.earnings_enabled = True
        self.mock_config.earnings_blackout_days = 2
        self.mock_config.finnhub_api_key = "test_finnhub_key"
        self.mock_config.earnings_cache_ttl_hours = 24
        self.mock_config.earnings_lookahead_days = 90

        self.market_data = MarketDataManager(self.mock_alpaca, self.mock_config)
        self.market_data.get_stock_metrics = Mock(
            return_value={'current_price': 264.80})
        # A 7-DTE call, sold the morning after the report. Expiry 08-07 is
        # after the (now past) 07-30 date and before the next quarter's.
        expiry = _date(2026, 8, 7)
        self.market_data.get_option_chain_with_analysis = Mock(return_value={
            'puts': [],
            'calls': [{
                'symbol': 'AMZN260807C00280000',
                'underlying_symbol': 'AMZN', 'option_type': 'call',
                'strike_price': 280.0, 'expiration_date': expiry.isoformat(),
                'dte': 7, 'delta': 0.17, 'mid_price': 1.90,
                'bid': 1.85, 'ask': 1.95, 'last_price': 1.90, 'volume': 900,
                'open_interest': 4000, 'implied_volatility': 0.31,
            }],
        })
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AMZN', 'qty': '100', 'cost_basis': '26120.0',
            'avg_entry_price': '261.20', 'market_value': '26480.0',
            'asset_class': 'us_equity', 'side': 'long',
        }]

    def _seed_day_d_fetch(self):
        """Exactly what a real report-day scan leaves behind: the report date,
        fetched at 16:00 that day — ~18h old on the next morning, well inside
        the 24h TTL, so the TTL alone does NOT evict it."""
        from src.api import earnings_calendar as ec_module

        ec_module._EARNINGS_CACHE['AMZN'] = {
            'date': self.REPORT_DAY, 'hour': 'amc',
            'fetched_at': datetime(2026, 7, 30, 16, 0),
        }

    def _scan_on_the_next_day(self, monkeypatch):
        from src.api.earnings_calendar import EarningsCalendarService

        # A refetch answers with the NEXT quarter, as Finnhub would.
        monkeypatch.setattr(
            EarningsCalendarService, "_fetch_earnings",
            lambda svc, symbol: {'date': self.NEXT_QUARTER, 'hour': 'amc',
                                 'fetched_at': _clock.now()})

        scanner = OptionsScanner(self.mock_alpaca, self.market_data,
                                 self.mock_config)
        captured = []
        with _clock.frozen(datetime.combine(self.NEXT_DAY, dtime(10, 0))):
            with patch('src.data.decision_record.DecisionRecorder.flush',
                       autospec=True,
                       side_effect=lambda self, **_: captured.extend(self.rows)):
                results = scanner.scan_for_call_opportunities(
                    run_id="20260731T140000Z-abcdef12")
        return results, captured

    def test_candidates_are_emitted_on_the_day_after_earnings(self, monkeypatch):
        self._seed_day_d_fetch()

        results, rows = self._scan_on_the_next_day(monkeypatch)

        assert len(results) == 1, (
            "the day after the report produced no candidate — the stale "
            "past-dated cache entry span-rejected the whole chain")
        assert rows == [], "the symbol should terminate at /run, not at the gate"
        assert self.market_data.last_call_rejection_stats['AMZN'][
            'expires_into_earnings'] == 0

    def test_no_false_blackout_row_is_written(self, monkeypatch):
        """The specific mislabel: a `blocked{earnings_blackout}` row naming an
        event that already happened."""
        self._seed_day_d_fetch()

        with patch('src.data.options_scanner.logger') as mock_logger:
            _, rows = self._scan_on_the_next_day(monkeypatch)

        assert [(r['outcome'], r['reason']) for r in rows] == []
        assert _events(mock_logger, 'call_scan_skipped_earnings_blackout') == []

    def test_the_still_future_date_is_honoured_the_same_day(self, monkeypatch):
        """The other half of the boundary: on report day itself the date is
        still ahead, so the spanning candidate IS blocked. The guard must not
        have weakened day-of blocking, which is the incident geometry."""
        from src.api import earnings_calendar as ec_module
        from src.api.earnings_calendar import EarningsCalendarService

        ec_module._EARNINGS_CACHE['AMZN'] = {
            'date': self.REPORT_DAY, 'hour': 'amc',
            'fetched_at': datetime(2026, 7, 30, 9, 0),
        }
        monkeypatch.setattr(
            EarningsCalendarService, "_fetch_earnings",
            lambda svc, symbol: pytest.fail("must be served from cache"))

        scanner = OptionsScanner(self.mock_alpaca, self.market_data,
                                 self.mock_config)
        captured = []
        with _clock.frozen(datetime.combine(self.REPORT_DAY, dtime(10, 0))):
            with patch('src.data.decision_record.DecisionRecorder.flush',
                       autospec=True,
                       side_effect=lambda self, **_: captured.extend(self.rows)):
                results = scanner.scan_for_call_opportunities(
                    run_id="20260730T140000Z-abcdef12")

        assert results == []
        assert [(r['outcome'], r['reason']) for r in captured] == [
            ('blocked', 'earnings_blackout')]


class TestPutSideExistingPositionSkip:
    """FC-069 item 12 — the put-side existing-position check.

    Two changes under test. (1) The option limb parses the underlying
    (``parse_option_symbol(...)['underlying'] == symbol``) instead of asking
    whether the scanned ticker appears *anywhere* inside the OCC contract
    symbol — the 7th instance of the substring family behind
    FC-041/043/045/048/052, which made a held ``PFE…`` contract block every F
    put. (2) The skip is no longer silent: it emits
    ``put_scan_skipped_existing_position``, closing the tally gap FC-068 §5
    accepted "until FC-069 item 12 rewires that check".
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'  # FC-075 Phase 2: wheel-profile defaults
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['F', 'MSFT']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7
        # The earnings gate is a separate stage with its own tests; off here so
        # the only thing that can skip a symbol is the check under test.
        self.mock_config.earnings_enabled = False
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'F', 'current_price': 11.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
        ]
        self.mock_market_data.find_suitable_puts.side_effect = lambda symbol: [{
            'symbol': '%s260821P00010000' % symbol, 'strike_price': 10.0,
            'expiration_date': '2026-08-21', 'dte': 7, 'delta': -0.15,
            'mid_price': 0.60, 'bid': 0.55, 'ask': 0.65, 'volume': 1500,
            'open_interest': 5000, 'implied_volatility': 0.25,
        }]
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)

    def _scan(self, positions):
        self.mock_alpaca.get_positions.return_value = positions
        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self.scanner.scan_for_put_opportunities()
        return [r['symbol'] for r in results], mock_logger

    def test_a_held_stock_position_still_skips_that_symbol(self):
        """The exact-match equity limb is untouched by the rewire."""
        emitted, mock_logger = self._scan([
            {'symbol': 'F', 'asset_class': 'us_equity', 'qty': 100},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('F', 'stock_position')]

    def test_a_held_option_on_the_symbol_still_blocks_it(self):
        """The parse-exact match must keep the real per-underlying block."""
        emitted, mock_logger = self._scan([
            {'symbol': 'F260821P00010000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('F', 'option_position')]

    def test_a_held_pfe_option_no_longer_blocks_f(self):
        """MUTATION CHECK — the whole point of item 12.

        Against the pre-fix ``symbol in position['symbol']`` this FAILS:
        ``'F' in 'PFE260821P00025000'`` is True, so F is skipped and
        ``emitted`` comes back as ``['MSFT']``. Verified by reverting the fix
        before the commit landed. Live behavior change, deliberate: this is a
        loosening, and it is the correct one.
        """
        emitted, mock_logger = self._scan([
            {'symbol': 'PFE260821P00025000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['F', 'MSFT']
        assert _events(mock_logger, 'put_scan_skipped_existing_position') == []

    def test_the_api_error_path_is_still_fail_closed(self):
        """A positions call that raises blocks every symbol, and emits no skip
        event — it fires ``position_check_failed`` instead, because an outage
        is not a holding and must not share the tally bucket with one."""
        self.mock_alpaca.get_positions.side_effect = RuntimeError("alpaca 500")

        with patch('src.data.options_scanner.logger') as mock_logger:
            assert self.scanner._has_existing_position('F') is True
            results = self.scanner.scan_for_put_opportunities()

        assert results == []
        assert _events(mock_logger, 'put_scan_skipped_existing_position') == []
        errors = [c.kwargs for c in mock_logger.error.call_args_list
                  if c.kwargs.get('event_type') == 'position_check_failed']
        assert [e['symbol'] for e in errors] == ['F', 'F', 'MSFT']

    def test_the_skip_event_carries_symbol_reason_and_the_held_contract(self):
        """The event's full shape — the ride-along exists to make the skip
        countable *and* debuggable, so the held contract rides along too."""
        _emitted, mock_logger = self._scan([
            {'symbol': 'MSFT260821P00380000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert len(skips) == 1
        assert skips[0]['symbol'] == 'MSFT'
        assert skips[0]['reason'] == 'option_position'
        assert skips[0]['position_symbol'] == 'MSFT260821P00380000'
        assert skips[0]['event_category'] == 'trade'

    @pytest.mark.parametrize("held", ['', 'NOT_AN_OCC', '123456'])
    def test_an_unparseable_option_symbol_fails_closed(self, held):
        """Documented decision: a held option we cannot identify is treated as
        potentially-matching, the same posture as the API-error path. The cost
        is a skipped put; the alternative is writing a second position on an
        underlying we may already hold.

        FC-079 changed the *label*, never the posture: this limb used to file
        under ``option_position``, indistinguishable in the tally from a
        genuine holding. It is now ``unparseable_position``. The skip itself is
        identical — `emitted == []`.
        """
        emitted, mock_logger = self._scan([
            {'symbol': held, 'asset_class': 'us_option', 'qty': -1},
        ])

        assert emitted == []
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('F', 'unparseable_position'), ('MSFT', 'unparseable_position')]

    def test_the_three_skip_reasons_are_told_apart(self):
        """FC-079 — one scan, all three limbs, three distinct reason strings.

        Before this, "we hold an option on this symbol" and "we hold something
        we cannot identify" shared one bucket, so the fail-closed limb was
        invisible: a run where every put was suppressed by garbage position
        data looked exactly like a run where the wheel was simply already
        fully invested.
        """
        self.mock_config.stock_symbols = ['F', 'MSFT', 'GOOGL']
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'F', 'current_price': 11.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
            {'symbol': 'GOOGL', 'current_price': 180.0},
        ]

        _emitted, mock_logger = self._scan([
            {'symbol': 'F', 'asset_class': 'us_equity', 'qty': 100},
            {'symbol': 'MSFT260821P00380000', 'asset_class': 'us_option',
             'qty': -1},
            {'symbol': 'NOT_AN_OCC', 'asset_class': 'us_option', 'qty': -1},
        ])

        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('F', 'stock_position'),
            ('MSFT', 'option_position'),
            ('GOOGL', 'unparseable_position'),
        ]

    def test_option_position_matches_returns_the_three_verdicts(self):
        """The helper's contract, directly. It is a str now, not a bool — and
        a truthiness test on it would treat ``'no_match'`` as a match."""
        assert self.scanner._option_position_matches(
            'F260821P00010000', 'F') == 'match'
        assert self.scanner._option_position_matches(
            'MSFT260821P00380000', 'F') == 'no_match'
        assert self.scanner._option_position_matches('NOT_AN_OCC', 'F') == 'unparseable'
        assert self.scanner._option_position_matches('', 'F') == 'unparseable'
        # Adjusted roots still resolve — the parser's leading-letters fallback
        # gives the answer we want, so this is a real match, not a guess.
        assert self.scanner._option_position_matches(
            'AAPL1260821C00230000', 'AAPL') == 'match'

    def test_a_held_short_CALL_blocks_puts_on_the_same_underlying(self):
        """Review fix 3 — pin the option-type dimension.

        Every other held-option fixture here is a P contract, so the mutation
        `underlying == symbol and option_type == 'put'` survived the whole
        suite: a held covered CALL would silently stop blocking new puts on the
        same underlying, and the wheel would stack a short put under a short
        call it is already carrying. The check is deliberately type-BLIND —
        the invariant is one option position per underlying, whichever leg it
        is. MUTATION CHECK: adding a `'put'` type condition fails this test.
        """
        emitted, mock_logger = self._scan([
            {'symbol': 'F260821C00012000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['position_symbol']) for s in skips] == [
            ('F', 'F260821C00012000')]

    def test_the_match_is_equality_not_a_prefix_relation(self):
        """Review fix 4 — kill the `startswith` survivor.

        A held F contract must not block FDX, and a held FDX contract must not
        block F. Both directions, because `underlying.startswith(symbol)` and
        `symbol.startswith(underlying)` are different bugs and each passes the
        other's test. MUTATION CHECK: either startswith form fails one of these.
        """
        self.mock_config.stock_symbols = ['F', 'FDX']
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'F', 'current_price': 11.0},
            {'symbol': 'FDX', 'current_price': 130.0},
        ]

        held_f, _ = self._scan([
            {'symbol': 'F260821P00010000', 'asset_class': 'us_option',
             'qty': -1},
        ])
        held_fdx, _ = self._scan([
            {'symbol': 'FDX260821P00125000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert held_f == ['FDX']
        assert held_fdx == ['F']

    def test_an_adjusted_root_still_resolves_to_its_underlying(self):
        """The parser's leading-letters fallback is the wanted answer here, not
        a guess to fail closed on: an adjusted MSFT contract still blocks MSFT
        and still leaves F alone."""
        emitted, _logger = self._scan([
            {'symbol': 'MSFT1260821P00380000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['F']


class TestPutSideSkipHandlesClassShareTickers:
    """FC-041 test 6 — the put-side skip on a dotted ticker.

    Same defect class as the share ledger, on the other leg: the scanned
    symbol is the equity spelling (``BRK.B``) and a held contract's underlying
    is the OCC root (``BRKB``), so the raw-string equality missed and a held
    ``BRKB`` option did NOT block a new ``BRK.B`` put — breaking the
    one-position-per-underlying invariant on exactly the tickers nobody is
    watching.

    MUTATION CHECK: revert ``occ_root`` to the identity and
    ``test_a_held_option_on_the_occ_root_blocks_the_dotted_symbol`` fails.
    """

    def setup_method(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.strategy_id = 'wheel'
        self.mock_config.excluded_symbols = set()
        self.mock_config.min_open_interest = None
        self.mock_config.max_spread_pct = None
        self.mock_config.bigquery_dataset = 'options_wheel'
        self.mock_config.stock_symbols = ['BRK.B', 'MSFT']
        self.mock_config.put_target_dte = 7
        self.mock_config.call_target_dte = 7
        self.mock_config.earnings_enabled = False
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'BRK.B', 'current_price': 450.0},
            {'symbol': 'MSFT', 'current_price': 400.0},
        ]
        # The OCC root, not the equity symbol — this is how Alpaca names the
        # contracts on a class share (verified 2026-08-28: BRK.B ->
        # BRKB260828C00270000, root_symbol 'BRKB').
        _roots = {'BRK.B': 'BRKB', 'MSFT': 'MSFT'}
        self.mock_market_data.find_suitable_puts.side_effect = lambda symbol: [{
            'symbol': '%s260821P00400000' % _roots[symbol], 'strike_price': 400.0,
            'expiration_date': '2026-08-21', 'dte': 7, 'delta': -0.15,
            'mid_price': 0.60, 'bid': 0.55, 'ask': 0.65, 'volume': 1500,
            'open_interest': 5000, 'implied_volatility': 0.25,
        }]
        self.scanner = OptionsScanner(self.mock_alpaca, self.mock_market_data,
                                      self.mock_config)

    def _scan(self, positions):
        self.mock_alpaca.get_positions.return_value = positions
        with patch('src.data.options_scanner.logger') as mock_logger:
            results = self.scanner.scan_for_put_opportunities()
        return [r['symbol'] for r in results], mock_logger

    def test_a_held_option_on_the_occ_root_blocks_the_dotted_symbol(self):
        """THE FC-041 REGRESSION on this leg. Pre-fix ``emitted`` comes back
        as ``['BRK.B', 'MSFT']`` — the held BRKB contract blocks nothing."""
        emitted, mock_logger = self._scan([
            {'symbol': 'BRKB260821P00400000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('BRK.B', 'option_position')]

    def test_a_held_short_call_on_the_occ_root_blocks_it_too(self):
        """The check is type-blind: one option position per underlying,
        whichever leg it is."""
        emitted, mock_logger = self._scan([
            {'symbol': 'BRKB260821C00470000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('BRK.B', 'option_position')]

    def test_the_equity_limb_still_blocks_on_the_dotted_spelling(self):
        """The stock limb is an exact match and Alpaca renders the equity as
        ``BRK.B`` on both sides, so it was never broken — pinned so the
        normalization cannot silently be moved onto this limb and change it."""
        emitted, mock_logger = self._scan([
            {'symbol': 'BRK.B', 'asset_class': 'us_equity', 'qty': 100},
        ])

        assert emitted == ['MSFT']
        skips = _events(mock_logger, 'put_scan_skipped_existing_position')
        assert [(s['symbol'], s['reason']) for s in skips] == [
            ('BRK.B', 'stock_position')]

    def test_another_class_of_the_same_issuer_does_not_block(self):
        """The fix must not over-block: BRKA is a different underlying."""
        emitted, mock_logger = self._scan([
            {'symbol': 'BRKA260821P00400000', 'asset_class': 'us_option',
             'qty': -1},
        ])

        assert emitted == ['BRK.B', 'MSFT']
        assert _events(mock_logger, 'put_scan_skipped_existing_position') == []


class TestOpportunityCarriesItsScanStampFC072:
    """FC-072: `quote_age_s` is computed from the opportunity's `scan_timestamp`.

    The stamp is not new — the scanner has always written it on both
    opportunity dicts — but FC-072 gave it a second consumer on the execute
    path, so it is now load-bearing rather than decoration. `/scan` runs at :00
    and `/run` at :15; without the stamp there is no way to tell, from the logs,
    a limit priced off a fresh book from one priced off a 15-minute-old blob,
    and the two-week readout in `docs/plans/fc-072.md` needs exactly that split.

    A deliberate note for the reader: rev 2 of the plan asked for a NEW
    `scanned_at` key. `scan_timestamp` already carries that value on both dicts,
    is already in the committed blob fixtures, and a synonym would be a second
    name for one fact — this repo's documented config/knob failure mode. The
    existing key is used instead.
    """

    def _scanner(self):
        self.mock_alpaca = Mock()
        self.mock_market_data = Mock()
        config = Mock(spec=Config)
        config.strategy_id = 'wheel'
        config.excluded_symbols = set()
        config.min_open_interest = None
        config.max_spread_pct = None
        config.bigquery_dataset = 'options_wheel'
        config.stock_symbols = ['AAPL']
        config.put_target_dte = 7
        config.call_target_dte = 7
        self.mock_alpaca.get_positions.return_value = []
        return OptionsScanner(self.mock_alpaca, self.mock_market_data, config)

    def _put_opportunity(self):
        scanner = self._scanner()
        self.mock_market_data.filter_suitable_stocks.return_value = [
            {'symbol': 'AAPL', 'current_price': 175.0},
        ]
        self.mock_market_data.find_suitable_puts.return_value = [{
            'symbol': 'AAPL250117P00170000',
            'strike_price': 170.0,
            'expiration_date': '2025-01-17',
            'dte': 7,
            'delta': -0.15,
            'mid_price': 2.50,
            'bid': 2.45,
            'ask': 2.55,
            'volume': 1500,
            'open_interest': 5000,
            'implied_volatility': 0.25,
        }]
        results = scanner.scan_for_put_opportunities()
        assert len(results) == 1
        return results[0]

    def _call_opportunity(self):
        scanner = self._scanner()
        self.mock_alpaca.get_positions.return_value = [{
            'symbol': 'AAPL',
            'qty': '100',
            'cost_basis': '16000.0',
            'avg_entry_price': '160.0',
            'asset_class': 'us_equity',
            'side': 'long',
        }]
        self.mock_market_data.get_stock_metrics.return_value = {
            'current_price': 175.0,
        }
        self.mock_market_data.find_suitable_calls.return_value = [{
            'symbol': 'AAPL250117C00185000',
            'strike_price': 185.0,
            'expiration_date': '2025-01-17',
            'dte': 7,
            'delta': 0.15,
            'mid_price': 1.80,
            'bid': 1.75,
            'ask': 1.85,
            'volume': 2000,
            'open_interest': 8000,
            'implied_volatility': 0.22,
        }]
        results = scanner.scan_for_call_opportunities()
        assert len(results) == 1
        return results[0]

    def test_the_put_opportunity_carries_a_readable_scan_stamp(self):
        opportunity = self._put_opportunity()
        assert 'scan_timestamp' in opportunity
        assert quote_age_seconds(opportunity['scan_timestamp']) is not None

    def test_the_call_opportunity_carries_a_readable_scan_stamp(self):
        """Symmetry: the call leg re-quotes too, so it needs the same stamp."""
        opportunity = self._call_opportunity()
        assert 'scan_timestamp' in opportunity
        assert quote_age_seconds(opportunity['scan_timestamp']) is not None

    def test_a_fresh_stamp_reads_as_a_near_zero_age(self):
        """The stamp is written at scan time, so age at scan time is ~0. A
        wrong-zone or wrong-unit stamp shows up here as hours, not seconds."""
        for opportunity in (self._put_opportunity(), self._call_opportunity()):
            age = quote_age_seconds(opportunity['scan_timestamp'])
            assert 0 <= age < 60, f"implausible scan age {age}s"

    def test_both_legs_also_carry_the_book_the_stamp_describes(self):
        """The blob fallback needs bid/ask AND the stamp; a stamp without a
        book, or a book without a stamp, is half a fallback."""
        for opportunity in (self._put_opportunity(), self._call_opportunity()):
            assert opportunity['bid'] > 0
            assert opportunity['ask'] > opportunity['bid']
            assert opportunity['premium'] > 0
